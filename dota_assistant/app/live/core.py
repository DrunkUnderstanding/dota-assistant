"""实时助手的主控（不依赖任何界面库）：定时识别、局面、推荐、语音、热键，状态整理成 JSON 推给网页界面。
选完人（GSI 策略时间）弹「本局要点」卡片的对线期一页（brief.py），后台再让大模型补上各阶段打法（gameplan.py），号角响后收起；
到 10 / 30 分钟再弹一次中期 / 后期那页（正弹着死亡复盘之类的就等它关了再弹），关掉以后热键 / 面板按钮随时再看、翻页；
对局开始后接着做局内缺口提醒（coach.py）：死亡时 / 每 5 分钟检查一次，有缺口就在叠加层弹一张卡片；
死亡时同一张卡片先说「刚才是怎么死的」（death.py，看 GSI 里自己死前 20 秒的血量 / 状态 / 保命装备），复活后自动收起。
对线期按位置 / 英雄到点前几秒提醒封野、拉野、屯野、抢莲花（jungle.py），叠在卡片顶上一行，没卡片时单独一张小卡片，过点自己收起。
双方 10 个英雄的买活冷却（buyback.py）显示在游戏顶栏头像正下方（另一个透明窗口 web/topbar.html）：自己的按 GSI 自动算，别人的玩家点一下开始计时；
对面 5 个英雄头像下面再加一条大招冷却（ultcd.py）：看到对面放大招点一下开始计时，右键弹菜单改大招等级、勾天赋 / 神杖 / 玲珑心减冷却。
选人时点某个推荐英雄，就按「如果选它」先做一张同样的要点卡片（数据页 + AI 打法页），看完点 × 回到推荐。

「控制面板的格子」就是这里的 self.slots，识别和网页输入都只往里写。
所有改状态的操作都放进同一个工作线程按顺序执行（网页请求、热键、定时识别都只是往队列里丢任务）：
  - mss 截屏句柄只能在创建它的线程里用；
  - 不用到处加锁。
状态每变一次就重新生成快照（snapshot），和上次不同才推给订阅者（网页的 SSE 连接）。
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

from ...ai import gameplan
from ...domain import advisor as adv
from ...domain import brief, buyback, coach, jungle, mates, ultcd
from ...vision import buybackscan, grid, roles
from ...core import log as logs
from ...core.config import Config
from ...core.dota_install import find_dota
from ...core.gsi_fields import SELECTION_STATE, STRATEGY_STATE
from ...core.heroes import Hero
from ...domain.engine import POSITION_NAMES
from ...game.gsi import GSIServer
from ...game.speech import Speaker
from ...vision.recognize import EMPTY, Calibration, DraftView, TemplateBank, confident, feature, recognize, write_image
from .ingame import InGameMixin
from .timers import TimersMixin
from .cards import CardsMixin
from .overlay import OverlayMixin
from .settings import SettingsMixin
from .mates import MatesMixin
from . import consts

log = logging.getLogger(__name__)


class Live(InGameMixin, TimersMixin, CardsMixin, OverlayMixin, SettingsMixin, MatesMixin):
    def __init__(self, cfg: Config, screen_factory=None, use_gsi: bool = True, use_hotkeys: bool = True,
                 speaker: Speaker | None = None, advisor: adv.Advisor | None = None,
                 bank: TemplateBank | None = None, calibration: Calibration | None = None,
                 overlay_file: Path | None = None, coach_prepare: Callable | None = None,
                 brief_build: Callable | None = None, plan_build: Callable | None = None,
                 ban_watcher: grid.BanWatcher | None = None, read_roles: Callable | None = None,
                 ocr: Callable | None = None, mate_fetch: Callable | None = None):
        """后面几个参数给测试换成假的；正常运行都不用传"""
        self.cfg = cfg
        self.config_file: Path | None = None    # 账号设置写到哪；None = 项目根目录的 config.toml，测试里换成临时文件
        self.advisor = advisor or adv.Advisor(cfg)
        self.db = self.advisor.db
        self.speaker = speaker or Speaker(cfg.voice_rate, cfg.voice)
        self.cal = calibration if calibration is not None else Calibration.load()
        self.bank = bank or TemplateBank()
        self.screen = None
        if screen_factory is None:
            from ...vision.capture import Screen
            screen_factory = lambda: Screen(cfg.monitor)
        self._screen_factory = screen_factory
        self.gsi = None
        self.use_gsi = use_gsi
        if use_gsi:
            try:
                self.gsi = GSIServer(cfg.gsi_port).start()
            except OSError as e:
                self.gsi = None                  # 端口被占用：照样能用，只是没有 GSI
                log.warning("GSI 端口 %d 用不了（%s），这次没有 GSI", cfg.gsi_port, e)
        # GSI 配置写在哪个 Dota 目录；None = 没找到（GSI 一直连不上，状态栏要说清楚为什么）
        self.dota_dir: Path | None = find_dota(cfg.dota_dir) if use_gsi else None

        # ---- 当前局面（控制面板的格子是唯一的「当前局面」，识别和手动输入都只是往里写）----
        self.slots: dict[str, list[str]] = {s: [""] * 5 for s in consts.SIDES}
        self.hints: dict[str, list[dict]] = {s: [{} for _ in range(5)] for s in consts.SIDES}   # 识别给的猜测 / 相似度
        self.bans_text = ""
        self.position = cfg.position
        self.side_mode = "auto"
        self.mode = "auto"
        self.voice = cfg.voice
        self.speaker.enabled = cfg.voice
        self.scan_without_gsi = True
        self.user_hidden = False
        self.overlay_file = overlay_file or consts.OVERLAY_FILE
        self.overlay_locked = True               # 固定：鼠标穿透；解锁：可以拖动叠加层
        self.overlay_pos: dict | None = None     # 拖过的位置（占屏幕宽 / 高的比例）和拉过的宽度，没拖过就用 config 里的
        self._load_overlay_state()
        self.message = ""                        # 状态栏最后一条操作提示
        self.notice: dict | None = None          # 演示用：临时盖住选人推荐的内容

        # ---- 局内缺口提醒 ----
        self._coach_prepare = coach_prepare or coach.prepare
        self.coach: coach.Coach | None = None
        self.coach_match: str | None = None      # 为哪一局（GSI matchid）建的；换局就重建
        self.coach_card: dict | None = None      # 正弹着的卡片（本局要点 / 局内提醒）；关掉 / 比赛结束就是 None
        self._brief_build = brief_build or brief.build
        self.brief_match: str | None = None      # 本局要点为哪一局做的
        self.brief: brief.Brief | None = None    # 做好的本局要点（关掉以后按热键还能再看）
        self.brief_page = "lane"                 # 卡片显示哪一页：lane 对线期 / mid 中期 / late 后期（brief.STAGES）
        self._brief_stage = "lane"               # 这局自动弹到了哪一阶段（每阶段只自动弹一次）
        self._horn_match: str | None = None      # 哪一局已经在号角响时收起过本局要点
        self._drafts_seen = 0                    # 进过几次选人阶段：本地 / 机器人局 matchid 一直是 0，靠它区分换局
        self._in_selection = False
        self._plan_build = plan_build or gameplan.generate
        self.coach_voice = cfg.coach_voice       # 先不念（玩家 2026-10-22 决定），config 里能打开
        self._coach_deaths = 0
        self._coach_checkpoint = 0
        self._death_names: tuple | None = None   # 死亡复盘用的中文名 + 保命技能（按英雄缓存）
        self._death_shown = 0.0                  # 死亡卡片弹出的时间；复活后收起
        self._death_shots: list[tuple[float, Path]] = []   # 待截的死亡画面：(到点时间, 存到哪)
        self.jungle: jungle.Plan | None = None   # 这局提醒哪些野区动作（局内提醒准备好时按位置 + 同分段数据定）
        self.jungle_cue: jungle.Cue | None = None   # 正显示的那条
        self._cue_muted: set[str] = set()        # 这局点 × 关掉的种类
        self._lane_logged: set[int] = set()      # 这局 3 / 6 / 10 分钟哪几个已经记进 data/lane_log.jsonl（lanestate.py）
        self.buyback = buyback.Tracker()          # 双方买活冷却（顶栏头像下面的小条）
        self._bb_match: str | None = None         # 为哪一局记的；换局清空
        self._bb_show = False                     # 进游戏（号角前出门）就显示，比赛结束收起
        self._bb_anchor: float | None = None      # 游戏时钟 0 秒对应的本机时间（time.time()），页面据此自己走秒
        self._bb_paused: float | None = None      # 暂停时停在的游戏时钟
        self.ults = ultcd.Tracker()               # 对面大招冷却（买活小条下面那条）
        self._bb_watch = buybackscan.Watcher()    # 买活自动记：每秒看顶栏头像灰没灰
        self._bb_scan_err = False                 # 截图 / 认图出过错（这局只记一次日志）
        self._ocr = ocr or roles.ocr              # 认复活倒计时 / 消息栏「买活了」（Windows OCR），测试换成假的
        self._bb_checks: list[buybackscan.FeedCheck] = []   # 刚复活、正在看消息栏确认是不是买活的格子
        self._bb_orphans: dict[str, list[float]] = {}   # 消息栏「买活了」认出是这局某个英雄、但他没在确认的：英雄 key → 看到的几帧的游戏时钟
        self._hero_icons: dict[str, object] = {}  # 英雄小图标（key → 图，下不到是 None），和消息栏行首的小图比
        self._kb_base = None                      # 知识库（查对面大招冷却用，第一次用到才读；读不到是 False）
        self._kb_ops: dict = {}                   # 大招神杖 / 魔晶冷却的写法（cd_ops.load_ops）

        # ---- 选人时「如果选它」：点推荐英雄做的要点卡片 ----
        self.pick_key: tuple | None = None       # (英雄 id, 局面 key, 位置)；None = 没在看
        self.pick_brief: brief.Brief | None = None
        self.pick_page = "lane"
        self.pick_note = ""                      # 还没做好 / 出错时显示什么

        # ---- 队友战绩：控制面板填好友代码查（mates.py）----
        self.mates = mates.Mates()
        self._mate_fetch = mate_fetch or mates.fetch

        self.last_view: DraftView | None = None
        self.pending_key = None
        # 这局顶栏每格裁下来的头像（选人界面那套格子）：blank = 这局第一次认时的样子（还没人选）、sel = 选人阶段最后一次、
        # strat = 策略时间最后一次。玩家在顶栏 / 面板上告诉我们这格是谁，就拿它们记成模板（learn_slot），下次认得
        # game = 局内顶栏头像（买活那套几何），只给帮认的候选排序、不记成模板（大小和选人界面不一样）
        self._crops: dict[str, dict[tuple[str, int], np.ndarray]] = {"blank": {}, "sel": {}, "strat": {}, "game": {}}
        self._crops_ver = 0
        self._cand_cache: tuple = (None, {})
        self._picked_cache: tuple = (None, {})
        # 玩家在叠加层 / 顶栏 / 控制面板告诉我们的格子（这局）：选人时识别整排重写格子也不冲掉它们
        self._named: set[tuple[str, int]] = set()
        self.fix_file = consts.FIX_FILE
        # 选人网格上的禁用（红斜线），见 grid.py；新布局在后台建图
        self.grid = ban_watcher if ban_watcher is not None else grid.BanWatcher()
        self._grid_building = False
        self._grid_retry_at = 0.0                # 建图失败后隔一会儿再试
        self._grid_last_boxes: tuple = ()
        # 自动认位置（roles.py）：天梯选人时顶栏名字下面的「优势路 / 纯辅助…」
        self._read_roles = read_roles or roles.read_roles
        self.position_auto = False               # 当前位置是不是自动认出来的
        self.team_roles: list[int | None] = []   # 这局我方 5 格各自排的位置（OCR 认出来的，猜队友分路用）
        self._base_position = self.position      # 认不出时回到的位置（config 或玩家手动选的）
        self._role_draft = -1                    # 哪一局已经认出来了（或者玩家手动改过），不再认
        self._role_busy = False
        self._role_next = 0.0
        self.prev_draft: adv.Draft | None = None
        self.last_scan = 0.0
        self.view: dict = {}

        self._tasks: queue.Queue = queue.Queue()
        self._subs: list[queue.Queue] = []
        self._subs_lock = threading.Lock()
        self._snapshot = ""
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="live")

        self.hotkeys = None
        if use_hotkeys:
            self._start_hotkeys()
        log.info("校准：%s；模板：官方 %s 张 + 学习 %s 张", "已校准" if self.cal and self.cal.ready else "没校准",
                 self.bank.portraits, self.bank.learned)

    # ---------- 线程 / 订阅 ----------

    def start(self) -> Live:
        self.submit(self.refresh)
        self._thread.start()
        return self

    def submit(self, fn: Callable, *args) -> Future:
        """把操作丢给工作线程；返回的 Future 里是操作的返回值（一般是给状态栏的提示）"""
        fut: Future = Future()
        self._tasks.put((fn, args, fut))
        return fut

    def _loop(self) -> None:
        next_tick = time.monotonic()
        while not self._stop.is_set():
            wait = next_tick - time.monotonic()
            if wait <= 0:                         # 操作再密也要按时识别
                next_tick = time.monotonic() + self.cfg.scan_interval
                self._run(self.tick, (), None)
                continue
            try:
                fn, args, fut = self._tasks.get(timeout=wait)
            except queue.Empty:
                continue
            self._run(fn, args, fut)

    def _run(self, fn, args, fut: Future | None) -> None:
        try:
            res = fn(*args)
            if fut:
                fut.set_result(res)
        except Exception as e:                   # 单个操作出错不能拖垮整个助手
            log.exception("操作出错：%s%s", getattr(fn, "__name__", fn), args or "")
            self.message = f"出错：{e}（详情见日志）"
            if fut:
                fut.set_exception(e)
        self.publish()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._subs_lock:
            self._subs.append(q)
        if self._snapshot:
            q.put(self._snapshot)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._subs_lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self) -> None:
        snap = json.dumps(self.snapshot(), ensure_ascii=False)
        if snap == self._snapshot:
            return
        self._snapshot = snap
        with self._subs_lock:
            for q in self._subs:
                q.put(snap)

    @property
    def current_snapshot(self) -> str:
        return self._snapshot

    # ---------- 网页发来的操作 ----------

    def action(self, a: dict) -> str | None:
        """在工作线程里执行一条网页操作，返回状态栏提示。未知操作抛 ValueError。"""
        t = a.get("type")
        log.info("网页操作 %s", {k: ("***" if k in consts.SECRET_FIELDS and v else v) for k, v in a.items()})
        if t == "set_slot":
            side, i = a["side"], int(a["index"])
            if side not in consts.SIDES or not 0 <= i < 5:
                raise ValueError("格子不存在")
            before = self.slots[side][i]
            self.slots[side][i] = str(a.get("text") or "").strip()
            if self.slots[side][i]:
                self._named.add((side, i))
            else:
                self._named.discard((side, i))
            if (h := self._hero(self.slots[side][i])) and (n := self.learn_slot(side, i, h)):
                self.message = f"{h.name_cn}这格的头像记住了，下次认得"
            if h and self._hero(before) != h:
                self._record_fix(side, i, before, h, n, "控制面板")
            self.refresh()
        elif t == "set_bans":
            self.bans_text = str(a.get("text") or "")
            self.refresh()
        elif t == "set":
            key, value = a.get("key"), a.get("value")
            if key == "position" and int(value) in POSITION_NAMES:
                self.position = int(value)
                self.position_auto = False
                self._base_position = self.position
                self._role_draft = self._drafts_seen         # 玩家手动选了，这局不再自动改
                self.refresh(force_speech=True)
            elif key == "side_mode" and value in consts.SIDE_MODES:
                self.side_mode = value
                self.refresh(force_speech=True)
            elif key == "mode" and value in consts.SCAN_MODES:
                self.mode = value
                self.refresh()
            elif key == "voice":
                self.voice = self.speaker.enabled = bool(value)
            elif key == "scan_without_gsi":
                self.scan_without_gsi = bool(value)
            elif key == "overlay_locked":
                self.set_locked(bool(value))
            elif key in ("overlay_opacity", "overlay_color"):
                self.set_overlay_style(key, value, save=bool(a.get("save", True)))
            else:
                raise ValueError(f"不认识的设置 {key}={value!r}")
        elif t == "refresh":
            self.refresh()
        elif t == "clear":
            self.clear()
        elif t == "rescan":
            self.scan(force=True)
        elif t == "learn":
            self.learn()
        elif t == "shot":
            self.save_shot()
        elif t == "dismiss_coach":
            if self.coach_card:
                self._close_card()
            elif self.jungle_cue:
                self.mute_cue()
            else:
                self.pick_key = None
        elif t == "show_brief":
            self.show_brief()
        elif t == "explain_pick":
            self.explain_pick(int(a["hero_id"]))
        elif t == "brief_page":
            self.set_brief_page(str(a.get("page")))
        elif t == "buyback":
            self.mark_buyback(str(a.get("side")), int(a["index"]))
        elif t == "ult":
            self.mark_ult(str(a.get("side")), int(a["index"]))
        elif t == "name_slot":
            self.name_slot(str(a.get("side")), int(a["index"]), int(a["hero_id"]))
        elif t == "ult_set":
            self.set_ult(str(a.get("side")), int(a["index"]), a.get("level"), a.get("mod"), a.get("on"))
        elif t == "toggle_overlay":
            self.toggle_overlay()
        elif t == "toggle_lock":
            self.toggle_lock()
        elif t == "close_overlay":
            self.close_overlay()
        elif t == "overlay_moved":
            self.overlay_moved(float(a["x"]), float(a["y"]), a.get("width"))
        elif t == "save_settings":
            self.save_settings(a)
        elif t == "mate_query":
            self.query_mate(a.get("code"), bool(a.get("force")))
        elif t == "mate_remove":
            self.mates.remove(int(a["account_id"]))
        elif t == "open_logs":
            self.message = f"已打开日志文件夹 {logs.open_dir()}（出问题时把最新的 .log 发给开发者）"
        else:
            raise ValueError(f"不认识的操作 {t!r}")
        return self.message

    # ---------- 局面 ----------

    def side(self) -> str | None:
        if self.side_mode in consts.SIDES:
            return self.side_mode
        return self.gsi.state.team if self.gsi and self.gsi.state.alive else None

    def _hero(self, text: str) -> Hero | None:
        text = text.strip()
        if not text:
            return None
        try:
            return self.db.find(text)
        except KeyError:
            return None

    def current_draft(self) -> adv.Draft:
        team = {s: [h for t in self.slots[s] if (h := self._hero(t))] for s in consts.SIDES}
        side = self.side() or consts.SIDE_FALLBACK
        other = "dire" if side == "radiant" else "radiant"
        bans = [h for t in self.bans_text.replace("，", ",").split(",") if (h := self._hero(t))]
        if self.last_view and self.mode == "auto":
            bans += [self.db.by_id[i] for i in self.last_view.ids("bans") if i in self.db.by_id]
        if self.mode == "auto":
            bans += [self.db.by_id[i] for i in sorted(self.grid.bans) if i in self.db.by_id]
        bans = list({h.id: h for h in bans}.values())
        my = None
        if self.gsi and self.gsi.state.alive and self.gsi.state.my_hero_key:
            my = next((h for h in self.db if h.key == self.gsi.state.my_hero_key), None)
        allies = [h for h in team[side] if not my or h.id != my.id]
        # 队友排的位置（OCR）：格子顺序和 team_roles 一一对应
        ally_pos = {h.id: r for t, r in zip(self.slots[side], self.team_roles)
                    if r and (h := self._hero(t)) and h in allies}
        return adv.Draft(allies, team[other], bans, my, ally_pos)

    def refresh(self, force_speech: bool = False) -> None:
        draft = self.current_draft()
        advice = self.advisor.advise(draft, self.position)
        self.view = adv.render_view(advice, self.side())
        self.view["position_auto"] = self.position_auto
        if self.side() is None:                  # 友 / 敌是按默认的天辉分的，可能整个反了
            self.view["side_assumed"] = consts.SIDE_FALLBACK
        self.notice = None
        changed = self.prev_draft is None or draft.key() != self.prev_draft.key()
        if changed:
            log.info("局面：我 %s · 队友 %s · 对面 %s · 禁用 %s · %d 号位 → 推荐 %s",
                     draft.my_hero.name_cn if draft.my_hero else "未选", consts._names(draft.allies), consts._names(draft.enemies),
                     consts._names(draft.bans), self.position,
                     "、".join(f"{s.hero.name_cn}({s.winrate:.1f}%)" for s in advice.picks + advice.targeted) or "无")
        if force_speech or changed:
            text = adv.speech(self.prev_draft, advice, force=force_speech)
            if text:
                log.info("播报：%s", text)
                self.speaker.say(text)
        self.prev_draft = draft

    def draft_view(self) -> dict:
        """选人卡片；进了游戏（GSI：出门到比赛结束）就不再显示剩余位置"""
        st = self.gsi.state if self.gsi else None
        view = {**self.view, "slots": self.slots_view()} if self.view.get("kind") == "draft" else self.view
        if view.get("team") and st and st.alive and st.in_match:
            return {**view, "team": None}
        return view

    def slots_view(self) -> dict:
        """叠加层「顶栏对照」：双方 5 格按 Dota 顶栏的顺序，玩家一眼看出哪格没认出、点了帮认。
        state：ok 认出了（点了能改）/ ask 这格有人但没认出（金色「?」）/ empty 看着还没选（也能点）/ me 自己。
        ask 怎么定：策略时间起（GSI）人人都有英雄了，空着的都是没认出；选人时看这格比这局开头多出了彩色头像（_looks_picked）。
        没连 GSI 时不知道在不在选人，只给 empty"""
        st = self.gsi.state if self.gsi and self.gsi.state.alive else None
        game_state = st.game_state if st else None
        over = game_state is not None and game_state != SELECTION_STATE
        me = self._my_slot(st) if st else None
        my_hero = next((h for h in self.db if h.key == st.my_hero_key), None) if st and st.my_hero_key else None
        out: dict = {"ally": self.side() or consts.SIDE_FALLBACK}
        for side in consts.SIDES:
            cells = []
            for i in range(5):
                h = self._hero(self.slots[side][i])
                if me == (side, i):
                    h = h or my_hero
                    cells.append({"state": "me", "hero": adv.hero_ref(h) if h else None})
                    continue
                if h:
                    state = "ok"
                elif over or (game_state == SELECTION_STATE and self._looks_picked(side, i)):
                    state = "ask"
                else:
                    state = "empty"
                hint = self.hints[side][i]
                cells.append({"state": state, "hero": adv.hero_ref(h) if h else None,
                              "guess": hint.get("guess", "") if state == "ask" else "",
                              "manual": (side, i) in self._named, "pick": self._pick_view(side, i)})
            out[side] = cells
        out["asks"] = sum(c["state"] == "ask" for s in consts.SIDES for c in out[s])
        return out

    def _looks_picked(self, side: str, index: int) -> bool:
        """选人阶段这格看着是不是有人选定了：和这局开头那张不一样、而且多出了彩色（队友预选是半透明灰图，不算）"""
        if self._picked_cache[0] != (id(self._crops), self._crops_ver):
            self._picked_cache = ((id(self._crops), self._crops_ver), {})
        cache = self._picked_cache[1]
        if (side, index) not in cache:
            cur, blank = self._crops["sel"].get((side, index)), self._crops["blank"].get((side, index))
            cache[(side, index)] = (cur is not None and blank is not None and cur is not blank
                                    and float(feature(cur) @ feature(blank)) <= consts.SAME_CROP
                                    and consts._colorful(cur) >= consts._colorful(blank) + consts.PICKED_COLOR_GAIN)
        return cache[(side, index)]

    def clear(self) -> None:
        self.slots = {s: [""] * 5 for s in consts.SIDES}
        self._named.clear()
        self.hints = {s: [{} for _ in range(5)] for s in consts.SIDES}
        self.bans_text = ""
        self.grid.reset()
        self.prev_draft = None
        self.refresh()

    def show_notice(self, title: str, lines: list[tuple[str, str]]) -> None:
        """临时盖住选人推荐显示一段提醒（演示 / 局内提醒用）；下一次 refresh 会换回推荐"""
        self.notice = {"kind": "notice", "title": title, "lines": [{"kind": k, "text": t} for k, t in lines]}

    def should_scan(self) -> bool:
        if self.mode != "auto" or not (self.cal and self.cal.ready):
            return False
        if self.gsi and self.gsi.state.alive:
            return self.gsi.state.in_draft
        return self.scan_without_gsi

    def should_scan_bans(self) -> bool:
        """禁用只在选人阶段出现；不需要顶栏校准"""
        if self.mode != "auto":
            return False
        if self.gsi and self.gsi.state.alive:
            return self.gsi.state.game_state == SELECTION_STATE
        return self.scan_without_gsi

    def tick(self) -> None:
        self.coach_tick()                        # 先更新阶段（进新一局选人要先清掉上一局的禁用）
        if "side" in self.view and self.view["side"] != self.side():
            self.refresh()                       # GSI 连上 / 断开、阵营变了：友 / 敌要重新分
        scan, bans = self.should_scan(), self.should_scan_bans()
        if scan or bans:
            img = self.grab()                    # 顶栏识别和禁用识别共用一帧
            if bans and img is not None:
                self.scan_bans(img)              # 先看网格：顶栏认不准的格子拿「网格里已被选走」核对
                self.scan_role(img)
            if scan:
                self.scan(img=img)

    def scan_bans(self, img) -> None:
        boxes = grid.find_cells(img)
        # 只在 GSI 确认是选人阶段时建新布局，免得把桌面上别的图片墙当成选人网格
        # 格子位置连续两帧一样才建（刚进选人时有过场动画，2026-10-23 实测第一帧建出了 134 格的错图）
        selecting = bool(self.gsi and self.gsi.state.alive and self.gsi.state.game_state == SELECTION_STATE)
        sig = tuple(boxes)
        steady, self._grid_last_boxes = sig == self._grid_last_boxes, sig
        if (selecting and steady and not self._grid_building and time.monotonic() >= self._grid_retry_at
                and self.grid.needs_map(img, boxes)):
            self._grid_building = True
            log.info("选人网格：新布局（%d 格），后台建图", len(boxes))

            def work():
                t0 = time.monotonic()
                try:
                    gmap = self.grid.build(img, boxes)
                    log.info("选人网格建好了（%.1f 秒）：%d 格认出 %d 个，%s", time.monotonic() - t0, len(gmap.cells),
                             sum(h is not None for h in gmap.heroes), "干净画面" if gmap.clean else "画面里已有禁用，先凑合用")
                except Exception:
                    log.exception("选人网格建图失败")
                    gmap = None
                self.submit(self._grid_ready, gmap)
            threading.Thread(target=work, daemon=True, name="grid").start()
        if self.grid.update(img, boxes):
            log.info("网格识别：禁用 %s；已选 %s", consts._names(self._ids(self.grid.bans)), consts._names(self._ids(self.grid.taken)))
            self.refresh()

    def scan_role(self, img) -> None:
        """选人阶段每 3 秒试一次认自己的位置，认出来这局就不再认"""
        st = self.gsi.state if self.gsi else None
        if (self._role_busy or self._role_draft == self._drafts_seen or time.monotonic() < self._role_next
                or not (self.cal and self.cal.ready and st and st.alive and st.team)):
            return
        self._role_busy, self._role_next = True, time.monotonic() + consts.ROLE_RETRY
        draft, side, name, slot = self._drafts_seen, st.team, st.player_name, st.team_slot

        def work():
            try:
                res = self._read_roles(img, self.cal, side, name, slot)
            except Exception as e:               # 没有 OCR 之类：这局不再试
                log.warning("认位置失败：%s", e)
                res = None
            self.submit(self._role_ready, draft, res)
        threading.Thread(target=work, daemon=True, name="roles").start()

    def _role_ready(self, draft: int, res) -> None:
        self._role_busy = False
        if res is None:
            self._role_draft = draft
            return
        team, me = res
        if draft != self._drafts_seen or self._role_draft == draft:
            return                               # 已经换局 / 玩家这期间手动改了
        if me is None or team[me] is None:
            return                               # 没有位置这行字（加速、普通匹配）或者被挡住了：过几秒再试
        self._role_draft = draft
        self.team_roles = list(team)
        log.info("认出位置：我在第 %d 格，排的是 %d 号位（我方：%s）", me + 1, team[me],
                 " ".join(str(r) if r else "?" for r in team))
        if team[me] != self.position or not self.position_auto:
            self.position, self.position_auto = team[me], True
            self.message = f"自动认出你这局打{POSITION_NAMES[team[me]]}"
            self.refresh(force_speech=True)

    def _grid_ready(self, gmap: grid.GridMap | None) -> None:
        self._grid_building = False
        if gmap is None:
            self._grid_retry_at = time.monotonic() + 60
            return
        self.grid.add_map(gmap)

    def _ids(self, ids) -> list[Hero]:
        return [self.db.by_id[i] for i in sorted(ids) if i in self.db.by_id]

    def grab(self):
        if self.screen is None:
            self.screen = self._screen_factory()
        return self.screen.grab()

    def scan(self, force: bool = False, img=None) -> None:
        if not (self.cal and self.cal.ready):
            self.message = "还没校准：先运行 python -m dota_assistant calibrate <截图>"
            return
        # 校准的是选人界面的顶栏；进了游戏顶栏头像小一圈、位置也不一样，按选人的格子裁出来全是乱的。
        # 2026-10-28 实战：局内连按 F9，每次认出一套不同的错英雄，把选人时认对的对面三个全冲掉了，大招 / 买活小条跟着错
        st = self.gsi.state if self.gsi else None
        game_state = st.game_state if st and st.alive else None
        if game_state not in (None, SELECTION_STATE, STRATEGY_STATE):
            if force:
                self.message = "进了游戏不能重识别（顶栏和选人界面不一样，会认乱）；没认出 / 认错的英雄在控制面板对应格子里手动改"
                log.info("局内按了重识别：不认，%s", self.message)
            return
        # 策略时间画面换了，顶栏有的格子会认乱（同一局：选人时认对的潮汐猎人、主宰被换成修补匠、百戏大王）：只补空着的格子
        only_empty = game_state == STRATEGY_STATE
        img = self.grab() if img is None else img
        view = recognize(img, self.cal, self.bank, self.cfg.match_threshold, keep_images=True)
        self._confirm_with_grid(view)
        self.last_scan = time.time()
        key = view.key()
        if log.isEnabledFor(logging.DEBUG):                  # 每秒一次，只在 DEBUG 时记（调 match_threshold 用）
            log.debug("识别：天辉 %s | 夜魇 %s", *consts._view_text(self.db, view, consts.SIDES))
        stable = force or key == self.pending_key            # 连续两次一致才采用，躲开动画过程
        self.pending_key = key
        if not stable:
            return
        self._keep_crops(view, "strat" if only_empty else "sel")
        # 玩家告诉过的格子：头像后来变了样（刚选时盖着的数字徽章没了、策略时间换了样子、帮认时那格还没出头像）也接着记，
        # 已经认得出的 learn_slot 自己跳过，不会每秒重复存
        for side, i in sorted(self._named):
            if h := self._hero(self.slots[side][i]):
                self.learn_slot(side, i, h)
        changed = self.last_view is None or self.last_view.key() != key
        self.last_view = view
        if changed or force:
            log.info("识别结果%s：天辉 %s | 夜魇 %s | 禁用 %s", "变了" if changed else "（手动重识别）",
                     *consts._view_text(self.db, view))
            self.fill_slots(view, only_empty=only_empty)
            self.refresh(force_speech=force)

    def _keep_crops(self, view: DraftView, phase: str) -> None:
        for side in consts.SIDES:
            for i, s in enumerate(getattr(view, side)):
                if s.image is not None and s.image.size:
                    if phase == "sel":
                        self._crops["blank"].setdefault((side, i), s.image)
                    self._crops[phase][(side, i)] = s.image
        self._crops_ver += 1

    def learn_slot(self, side: str, index: int, h: Hero) -> int:
        """玩家告诉我们顶栏这格是 h：把这局在这格裁到的头像记成 h 的模板，返回记了几张。
        不记的：和这局一开始（还没人选）一样的（那格根本没出头像，例如 2026-10-28 断线的那个人，记了会把空位认成他）、
        两张一样的、本来就认得出的"""
        blank = self._crops["blank"].get((side, index))
        fb = feature(blank) if blank is not None else None
        seen, n = [], 0
        for phase in ("sel", "strat"):
            c = self._crops[phase].get((side, index))
            if c is None:
                continue
            f = feature(c)
            if (fb is not None and float(f @ fb) > consts.SAME_CROP) or any(float(f @ g) > consts.SAME_CROP for g in seen):
                continue
            seen.append(f)
            hid, score, gap = self.bank.best(c)
            if hid == h.id and confident(score, gap, self.cfg.match_threshold):
                continue
            self.bank.learn(h.id, c)
            n += 1
        if n:
            log.info("记住%s第 %d 格是%s：存了 %d 张模板（共学习 %d 张）",
                     consts._side_cn(side), index + 1, h.name_cn, n, self.bank.learned)
        return n

    def name_slot(self, side: str, index: int, hero_id: int) -> None:
        """顶栏「这格是谁」点了一个英雄：填进格子，顺手记成模板"""
        if side not in consts.SIDES or not 0 <= index < 5:
            raise ValueError("格子不存在")
        h = self.db.by_id.get(int(hero_id))
        if h is None:
            raise ValueError(f"没有这个英雄 {hero_id}")
        other = next(((sd, j) for sd in consts.SIDES for j, t in enumerate(self.slots[sd])
                      if (sd, j) != (side, index) and self._hero(t) == h), None)
        if other:
            self.message = f"{h.name_cn}已经在{consts._side_cn(other[0])}第 {other[1] + 1} 格了，先把那格改掉"
            return
        before = self.slots[side][index] or (f"?{self.hints[side][index].get('guess')}" if self.hints[side][index].get("guess") else "")
        self.slots[side][index] = h.name_cn
        self.hints[side][index] = {}
        self._named.add((side, index))
        n = self.learn_slot(side, index, h)
        self._record_fix(side, index, before, h, n, "帮认")
        self.message = f"{consts._side_cn(side)}第 {index + 1} 格填上{h.name_cn}" + (f"，这格头像记住了，下次认得" if n else "")
        log.info("帮认：%s", self.message)
        self.refresh()

    def _record_fix(self, side: str, index: int, before: str, h: Hero, learned: int, how: str) -> None:
        """记一次玩家纠正（data/fixes.jsonl）：模板本身已经存进 templates/ 了，这里是给人看的流水"""
        st = self.gsi.state if self.gsi and self.gsi.state.alive else None
        row = {"time": datetime.now().isoformat(timespec="seconds"), "match": st.match_id if st else "",
               "phase": (st.game_state or "").removeprefix("DOTA_GAMERULES_STATE_") if st else "",
               "side": side, "slot": index + 1, "before": before, "hero": h.name_cn, "hero_id": h.id,
               "learned": learned, "how": how}
        try:
            self.fix_file.parent.mkdir(parents=True, exist_ok=True)
            with self.fix_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("纠正记录写不了 %s：%s", self.fix_file, e)

    def pick_candidates(self, side: str, index: int) -> dict:
        """顶栏「这格是谁」的候选：除了已经在别的格子里的，所有英雄排个序——先是选人网格里被选走、还没分到格子、没被禁的（taken），
        再是别的，各自按和这格头像像不像排。页面先选属性、再把这个属性的全列出来（玩家 2026-10-28 要的），按这个顺序。
        只给 id（英雄名 / 图标 / 属性页面从 /api/heroes 拿一次），快照里 10 格都带也不大"""
        used = {h.id for sd in consts.SIDES for t in self.slots[sd] if (h := self._hero(t))}
        pool = set(self.grid.taken) - used - set(self.grid.bans)
        sims: dict[int, float] = {}
        # 选人界面裁的优先；这局没裁到过（中途开的助手）就用局内顶栏头像——只拿来排序（对的那个分数 0.3~0.6，常排前几，见第 37 节）
        for phases in (("sel", "strat"), ("game",)):
            for phase in phases:
                if (c := self._crops[phase].get((side, index))) is not None:
                    for hid, v in self.bank.scores(c).items():
                        sims[hid] = max(v, sims.get(hid, -2.0))
            if sims:
                break
        if not sims:                                         # 什么都没裁到：按名字
            sims = {i: 0.0 for i in self.db.by_id}
        ids = sorted((i for i in sims if i not in used), key=lambda i: (i not in pool, -sims[i], self.db.by_id[i].name_cn
                                                                       if i in self.db.by_id else ""))
        ids = [i for i in ids if i in self.db.by_id]
        return {"order": ids, "taken": [i for i in ids if i in pool]}

    def _confirm_with_grid(self, view: DraftView) -> None:
        """顶栏分数没过阈值、但最像的英雄在选人网格里确实已被选走（变暗没斜线）的，也算认出来。
        2026-10-23 实测：拉比克在顶栏只有 0.63（阈值 0.70），网格里已经变暗。"""
        for s in view.radiant + view.dire:
            if s.hero_id is None and s.guess in self.grid.taken and s.score >= consts.GRID_CONFIRM_MIN:
                s.hero_id = s.guess

    def fill_slots(self, view: DraftView, only_empty: bool = False) -> None:
        """正在手动改的格子由网页自己保护（输入框有焦点时不覆盖）；玩家这局告诉过我们的格子（_named）不动。
        only_empty：只往空格子里填认出来的英雄（已经有的不换、不清），填进去的英雄不能和别的格子重复"""
        for side in consts.SIDES:
            for i, s in enumerate(getattr(view, side)):
                if (side, i) in self._named and self.slots[side][i]:
                    continue
                if only_empty:
                    h = self.db.by_id.get(s.hero_id)
                    if (self.slots[side][i] or not h
                            or any(self._hero(t) == h for sd in consts.SIDES for t in self.slots[sd])):
                        continue
                self.slots[side][i] = self.db.by_id[s.hero_id].name_cn if s.hero_id in self.db.by_id else ""
                guess = self.db.by_id.get(s.guess)
                self.hints[side][i] = {"score": round(s.score, 2),
                                       "guess": guess.name_cn if guess and not s.hero_id else ""}

    def learn(self) -> None:
        if not self.last_view:
            self.message = "还没有识别结果可以记"
            return
        n = 0
        for side in consts.SIDES:
            for text, s in zip(self.slots[side], getattr(self.last_view, side)):
                if s.image is None:
                    continue
                h = self._hero(text)
                if text.strip() and not h:
                    continue                                   # 名字写错的格子跳过
                self.bank.learn(h.id if h else EMPTY, s.image)
                n += 1
        self.message = f"已记住 {n} 个格子，模板共 {self.bank.portraits} 张官方 + {self.bank.learned} 张学习"
        log.info(self.message)

    def _pick_view(self, side: str, index: int) -> dict:
        """顶栏帮认的候选；快照每秒都做，格子 / 裁图 / 网格 / 模板没变就用上次算的"""
        key = (tuple(self.slots["radiant"] + self.slots["dire"]), id(self._crops), self._crops_ver,
               len(self.grid.taken), len(self.grid.bans), self.bank.learned)
        if self._cand_cache[0] != key:
            self._cand_cache = (key, {})
        cache = self._cand_cache[1]
        if (side, index) not in cache:
            cache[(side, index)] = self.pick_candidates(side, index)
        return cache[(side, index)]

    def save_shot(self) -> None:
        path = consts.SHOTS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}.png"
        write_image(path, self.grab())
        self.message = f"截图已保存：{path}"
        log.info(self.message)

    # ---------- 显示 ----------

    def status(self) -> dict:
        if not self.use_gsi:
            gsi = {"state": "disabled", "text": "GSI 未启用"}
        elif self.gsi is None:
            gsi = {"state": "busy", "text": f"GSI 端口 {self.cfg.gsi_port} 被占用"}
        elif self.gsi.state.alive:
            team = {"radiant": "天辉", "dire": "夜魇"}.get(self.gsi.state.team or "", "?")
            gsi = {"state": "ok", "text": f"GSI 已连接 · {team}", "game_state": self.gsi.state.game_state}
        elif self.dota_dir is None:
            gsi = {"state": "nodota", "text": "GSI 未连接 · 没找到 Dota 目录",
                   "tip": "点这里到「设置」填 Dota 安装目录；填好之前阵营在下面手动选"}
        else:
            gsi = {"state": "off", "text": "GSI 未连接",
                   "tip": f"GSI 配置在 {self.dota_dir}。连不上的话：Steam 启动选项加 -gamestateintegration，"
                          "先开助手再开（或重开）Dota；还不行就到「设置」看 Dota 目录对不对"}
        return {
            "gsi": gsi,
            "calibrated": bool(self.cal and self.cal.ready),
            "last_scan": time.strftime("%H:%M:%S", time.localtime(self.last_scan)) if self.last_scan else "",
            "hotkeys": [{"key": getattr(self.cfg, f), "text": text, "short": short}
                        for f, (text, short) in consts.HOTKEY_ACTIONS.items()],
            "hotkeys_failed": list(self.hotkeys.failed) if self.hotkeys else [],
            "message": self.message,
        }

    def snapshot(self) -> dict:
        slots = {}
        for side in consts.SIDES:
            slots[side] = []
            for text, hint in zip(self.slots[side], self.hints[side]):
                h = self._hero(text)
                slots[side].append({"text": text, "hero_id": h.id if h else None,
                                    "valid": not text or h is not None, **hint})
        return {
            "settings": {"position": self.position, "position_auto": self.position_auto, "side_mode": self.side_mode, "mode": self.mode,
                         "voice": self.voice, "scan_without_gsi": self.scan_without_gsi},
            "config": self.settings_view(),
            "slots": slots,
            "bans_text": self.bans_text,
            "buyback": self.buyback_view(),
            "mates": self.mates.view(self.db),
            "overlay": {"visible": self.overlay_visible(), "locked": self.overlay_locked,
                        "lock_key": self.cfg.hotkey_lock, "toggle_key": self.cfg.hotkey_toggle, "style": self.overlay_style(),
                        "view": self.overlay_view()},
            "status": self.status(),
        }

    def heroes(self) -> list[dict]:
        return sorted(({"id": h.id, "name": h.name_cn, "en": h.name_en, "key": h.key, "attr": h.attr} for h in self.db),
                      key=lambda h: h["name"])

    def close(self) -> None:
        log.info("主控关闭")
        self._stop.set()
        if self.hotkeys:
            self.hotkeys.stop()
        self.speaker.close()
        if self.gsi:
            self.gsi.stop()
