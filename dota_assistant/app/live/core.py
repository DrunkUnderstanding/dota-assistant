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
from ...domain import brief, buyback, coach, jungle, ultcd
from ...vision import buybackscan, grid, roles
from ...core import log as logs
from ...core.config import Config
from ...core.dota_install import find_dota
from ...core.heroes import Hero
from ...domain.engine import POSITION_NAMES
from ...game.gsi import GSIServer
from ...game.speech import Speaker
from ...vision.recognize import Calibration, DraftView, TemplateBank, write_image
from .draft import DraftMixin
from .ingame import InGameMixin
from .timers import TimersMixin
from .cards import CardsMixin
from .overlay import OverlayMixin
from .settings import SettingsMixin
from . import consts
from .. import upgrade

log = logging.getLogger(__name__)


class Live(DraftMixin, InGameMixin, TimersMixin, CardsMixin, OverlayMixin, SettingsMixin):
    def __init__(self, cfg: Config, screen_factory=None, use_gsi: bool = True, use_hotkeys: bool = True,
                 speaker: Speaker | None = None, advisor: adv.Advisor | None = None,
                 bank: TemplateBank | None = None, calibration: Calibration | None = None,
                 overlay_file: Path | None = None, coach_prepare: Callable | None = None,
                 brief_build: Callable | None = None, plan_build: Callable | None = None,
                 ban_watcher: grid.BanWatcher | None = None, read_roles: Callable | None = None,
                 ocr: Callable | None = None):
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
        self.updater = upgrade.Updater()         # 助手自己的更新（检查 / 下载在它自己的后台线程里）
        self.quit_requested = threading.Event()  # 更新准备好了：webui 看到就关界面、退出，交给收尾进程
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
            elif key in consts.TIMER_SWITCHES:
                self.set_timer(key, bool(value))
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
            self.overlay_moved(float(a["x"]), float(a["y"]), a.get("width"), a.get("height"), a.get("screen"))
        elif t == "save_settings":
            self.save_settings(a)
        elif t == "upgrade_check":
            if not self.updater.check_async():
                raise ValueError("正在检查 / 下载，等一下")
        elif t == "upgrade_install":
            if not self.updater.install_async(self.quit_requested.set):
                raise ValueError("现在没有能装的新版本（或者正在下载）")
            self.message = "开始更新，好了以后助手会自己重启"
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
                         "voice": self.voice, "scan_without_gsi": self.scan_without_gsi,
                         **{k: getattr(self.cfg, k) for k in consts.TIMER_SWITCHES}},
            "config": self.settings_view(),
            "slots": slots,
            "bans_text": self.bans_text,
            "buyback": self.buyback_view(),
            "overlay": {"visible": self.overlay_visible(), "locked": self.overlay_locked,
                        "lock_key": self.cfg.hotkey_lock, "toggle_key": self.cfg.hotkey_toggle, "style": self.overlay_style(),
                        "view": self.overlay_view()},
            "status": self.status(),
            "upgrade": self.updater.view(),
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
