"""实时助手的主控（不依赖任何界面库）：定时识别、局面、推荐、语音、热键，状态整理成 JSON 推给网页界面。
选完人（GSI 策略时间）弹「本局要点」卡片的对线期一页（brief.py），后台再让大模型补上各阶段打法（gameplan.py），号角响后收起；
到 10 / 30 分钟再弹一次中期 / 后期那页（正弹着死亡复盘之类的就等它关了再弹），关掉以后热键 / 面板按钮随时再看、翻页；
对局开始后接着做局内缺口提醒（coach.py）：死亡时 / 每 5 分钟检查一次，有缺口就在叠加层弹一张卡片；
死亡时同一张卡片先说「刚才是怎么死的」（death.py，看 GSI 里自己死前 20 秒的血量 / 状态 / 保命装备），复活后自动收起。
对线期按位置 / 英雄到点前几秒提醒封野、拉野、屯野、抢莲花（jungle.py），叠在卡片顶上一行，没卡片时单独一张小卡片，过点自己收起。
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
import re
import threading
import time
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import advisor as adv
from . import brief, coach, config, death, gameplan, grid, jungle, lanestate, llm, roles
from . import log as logs
from .config import DATA_DIR, ROOT, Config
from .engine import POSITION_NAMES
from .gsi import SELECTION_STATE, STRATEGY_STATE, GSIServer
from .heroes import Hero
from .hotkeys import Hotkeys, vk_code
from .speech import Speaker
from .vision import EMPTY, Calibration, DraftView, TemplateBank, recognize, write_image

SHOTS_DIR = ROOT / "shots"
OVERLAY_FILE = DATA_DIR / "overlay.json"       # 叠加层拖动后的位置 + 是否固定（Electron 那边拖完报过来）
OPACITY_MIN = 0.3                              # 叠加层底色再透就看不清字了
OVERLAY_WIDTH = (300, 4000)                    # 拉宽叠加层时能存的宽度（逻辑像素），Electron 那边还会收到屏幕以内
SIDES = ("radiant", "dire")
DEATH_MIN_SHOW = 3                             # 死亡卡片至少显示几秒（复活很快 / 演示加速时也看得到）
SIDE_MODES = ("auto", "radiant", "dire")       # 阵营：自动（GSI）/ 我在天辉 / 我在夜魇
ROLE_RETRY = 3.0                               # 认位置：没认出来隔几秒再试
GRID_CONFIRM_MIN = 0.5                         # 顶栏分数至少这么多，再加上网格核对才算认出
SCAN_MODES = ("auto", "manual")                # 自动识别 / 手动输入
log = logging.getLogger(__name__)


def _names(hs) -> str:
    return "、".join(h.name_cn for h in hs) or "无"


def _slot_text(db, s) -> str:
    """识别结果的一个格子：认出来的写名字，认不出写「?最像的」，后面带相似度"""
    if s.hero_id in db.by_id:
        return f"{db.by_id[s.hero_id].name_cn}({s.score:.2f})"
    guess = db.by_id.get(s.guess)
    return f"?{guess.name_cn if guess else ''}({s.score:.2f})"


def _check_hotkeys(cfg: Config, keys: dict) -> dict:
    """设置里改的热键：键名能注册、几个功能不撞键；返回要写进 config 的字段"""
    updates = {}
    for field, name in keys.items():
        if field not in HOTKEY_ACTIONS:
            raise ValueError(f"不认识的热键项 {field!r}")
        name = str(name or "").strip().upper()
        vk_code(name)                                  # 不支持的键名直接报错
        updates[field] = name
    merged = {f: updates.get(f, getattr(cfg, f)).upper() for f in HOTKEY_ACTIONS}
    seen: dict[str, str] = {}
    for f, name in merged.items():
        if name in seen:
            raise ValueError(f"{name} 同时绑了「{HOTKEY_ACTIONS[seen[name]][0]}」和「{HOTKEY_ACTIONS[f][0]}」，换一个")
        seen[name] = f
    return updates


def _view_text(db, view, kinds=("radiant", "dire", "bans")) -> list[str]:
    return [" ".join(_slot_text(db, s) for s in getattr(view, k)) or "-" for k in kinds]


SECRET_FIELDS = {"stratz_token": "STRATZ token", "ai_api_key": "API Key"}   # 不发给页面、不进日志
# 热键：config 字段 → (完整说明, 面板窗口窄时的短说明)；按哪个键在「设置」里改
HOTKEY_ACTIONS = {
    "hotkey_toggle": ("显隐 / 关提醒", "显隐"),
    "hotkey_lock": ("解锁 / 固定位置", "解锁"),
    "hotkey_rescan": ("重识别", "重识别"),
    "hotkey_shot": ("截图", "截图"),
    "hotkey_brief": ("本局要点 / 翻页", "要点"),
}


def _next_stage(page: str) -> str:
    order = [p for p, _ in brief.STAGES]
    return order[(order.index(page) + 1) % len(order)] if page in order else order[0]


def _mask(secret: str) -> dict:
    return {"set": bool(secret), "tail": secret[-4:] if len(secret) >= 8 else ""}


class Live:
    def __init__(self, cfg: Config, screen_factory=None, use_gsi: bool = True, use_hotkeys: bool = True,
                 speaker: Speaker | None = None, advisor: adv.Advisor | None = None,
                 bank: TemplateBank | None = None, calibration: Calibration | None = None,
                 overlay_file: Path | None = None, coach_prepare: Callable | None = None,
                 brief_build: Callable | None = None, plan_build: Callable | None = None,
                 ban_watcher: grid.BanWatcher | None = None, read_roles: Callable | None = None):
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
            from .capture import Screen
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

        # ---- 当前局面（控制面板的格子是唯一的「当前局面」，识别和手动输入都只是往里写）----
        self.slots: dict[str, list[str]] = {s: [""] * 5 for s in SIDES}
        self.hints: dict[str, list[dict]] = {s: [{} for _ in range(5)] for s in SIDES}   # 识别给的猜测 / 相似度
        self.bans_text = ""
        self.position = cfg.position
        self.side_mode = "auto"
        self.mode = "auto"
        self.voice = cfg.voice
        self.speaker.enabled = cfg.voice
        self.scan_without_gsi = True
        self.user_hidden = False
        self.overlay_file = overlay_file or OVERLAY_FILE
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
        self.jungle: jungle.Plan | None = None   # 这局提醒哪些野区动作（局内提醒准备好时按位置 + 同分段数据定）
        self.jungle_cue: jungle.Cue | None = None   # 正显示的那条
        self._cue_muted: set[str] = set()        # 这局点 × 关掉的种类
        self._lane_logged: set[int] = set()      # 这局 3 / 6 / 10 分钟哪几个已经记进 data/lane_log.jsonl（lanestate.py）

        # ---- 选人时「如果选它」：点推荐英雄做的要点卡片 ----
        self.pick_key: tuple | None = None       # (英雄 id, 局面 key, 位置)；None = 没在看
        self.pick_brief: brief.Brief | None = None
        self.pick_page = "lane"
        self.pick_note = ""                      # 还没做好 / 出错时显示什么

        self.last_view: DraftView | None = None
        self.pending_key = None
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

    def _start_hotkeys(self) -> None:
        """按 cfg 里的键注册热键；设置里改了键就整组重新注册"""
        actions = {"hotkey_toggle": (self.toggle_overlay,), "hotkey_lock": (self.toggle_lock,),
                   "hotkey_rescan": (self.scan, True), "hotkey_shot": (self.save_shot,),
                   "hotkey_brief": (self.show_brief,)}

        def key(name: str, fn: Callable, *args):
            def press():
                log.info("热键 %s", name)
                self.submit(fn, *args)
            return press
        if self.hotkeys:
            self.hotkeys.stop()
        self.hotkeys = Hotkeys({getattr(self.cfg, f): key(getattr(self.cfg, f), *actions[f])
                                for f in HOTKEY_ACTIONS}).start()

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
        log.info("网页操作 %s", {k: ("***" if k in SECRET_FIELDS and v else v) for k, v in a.items()})
        if t == "set_slot":
            side, i = a["side"], int(a["index"])
            if side not in SIDES or not 0 <= i < 5:
                raise ValueError("格子不存在")
            self.slots[side][i] = str(a.get("text") or "").strip()
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
            elif key == "side_mode" and value in SIDE_MODES:
                self.side_mode = value
                self.refresh(force_speech=True)
            elif key == "mode" and value in SCAN_MODES:
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
        elif t == "open_logs":
            self.message = f"已打开日志文件夹 {logs.open_dir()}（出问题时把最新的 .log 发给开发者）"
        else:
            raise ValueError(f"不认识的操作 {t!r}")
        return self.message

    def save_settings(self, a: dict) -> None:
        """控制面板「设置」：账号（好友代码、STRATZ token）和 AI 打法来源写进 config.toml。
        密钥字段没传 = 不改（面板不会把已存的密钥发回页面）；传空字符串 = 清掉。
        AI 设置下一局就生效；账号要重启助手才会重新拉个人数据和分位置数据。"""
        updates = {}
        if "account_id" in a:
            acc = str(a["account_id"] or "0").strip()
            if not acc.isdigit():
                raise ValueError("好友代码应该是一串数字")
            updates["account_id"] = int(acc)
        for key, name in SECRET_FIELDS.items():
            if key in a:
                val = str(a[key] or "").strip()
                if any(c.isspace() or c == '"' for c in val):
                    raise ValueError(f"{name}里不该有空格或引号，检查一下是不是复制多了")
                updates[key] = val
        if "ai_plan" in a:
            updates["ai_plan"] = bool(a["ai_plan"])
        if "ai_provider" in a:
            if a["ai_provider"] not in llm.PROVIDERS:
                raise ValueError(f"不认识的 AI 来源 {a['ai_provider']!r}")
            updates["ai_provider"] = a["ai_provider"]
        if "ai_model" in a:
            model = str(a["ai_model"] or "").strip()
            if not model:
                raise ValueError("模型名不能空")
            updates["ai_model"] = model
        if "ai_base_url" in a:
            url = str(a["ai_base_url"] or "").strip()
            if url and not url.startswith(("http://", "https://")):
                raise ValueError("接口地址要以 http:// 或 https:// 开头")
            updates["ai_base_url"] = url
        if "hotkeys" in a:
            updates.update(_check_hotkeys(self.cfg, a["hotkeys"]))
        if not updates:
            return
        hotkeys_changed = any(updates.get(f, getattr(self.cfg, f)) != getattr(self.cfg, f) for f in HOTKEY_ACTIONS)
        account_changed = any(updates.get(k, getattr(self.cfg, k)) != getattr(self.cfg, k)
                              for k in ("account_id", "stratz_token"))
        path = config.save(updates, self.config_file)
        for k, v in updates.items():
            setattr(self.cfg, k, v)
        log.info("设置已保存到 %s：%s", path, {k: ("已设置" if v else "未设置") if k in SECRET_FIELDS else v
                                               for k, v in updates.items()})
        if hotkeys_changed and self.hotkeys:
            self._start_hotkeys()
        self.message = ("设置已保存。账号改了要重启助手才会重新拉个人数据" if account_changed
                        else "设置已保存，热键已换好" if hotkeys_changed
                        else "设置已保存，AI 打法下一局按新设置")

    def set_overlay_style(self, key: str, value, save: bool = True) -> None:
        """叠加层底色 / 不透明度。面板上拖滑块时 save=False 只改内存（叠加层马上跟着变），松手再写进 config.toml"""
        if key == "overlay_opacity":
            value = round(min(1.0, max(OPACITY_MIN, float(value))), 2)
        elif not re.fullmatch(r"#[0-9a-fA-F]{6}", str(value)):
            raise ValueError(f"颜色要写成 #rrggbb，收到 {value!r}")
        else:
            value = str(value).lower()
        setattr(self.cfg, key, value)
        if save:
            path = config.save({key: value}, self.config_file)
            log.info("叠加层外观已保存到 %s：%s = %s", path, key, value)
            self.message = "叠加层外观已保存"

    def overlay_style(self) -> dict:
        try:
            opacity = min(1.0, max(OPACITY_MIN, float(self.cfg.overlay_opacity)))
        except (TypeError, ValueError):
            opacity = Config.overlay_opacity
        color = self.cfg.overlay_color if re.fullmatch(r"#[0-9a-fA-F]{6}", str(self.cfg.overlay_color)) else Config.overlay_color
        return {"opacity": opacity, "color": color}

    def settings_view(self) -> dict:
        """给面板「设置」窗口看的；密钥本身不发给页面，只给末 4 位让玩家认得出是哪个"""
        c = self.cfg
        return {"account_id": c.account_id, "stratz_token": _mask(c.stratz_token), "ai_plan": c.ai_plan,
                "ai_provider": c.ai_provider, "ai_model": c.ai_model, "ai_api_key": _mask(c.ai_api_key),
                "ai_base_url": c.ai_base_url, "providers": llm.PROVIDERS,
                "hotkeys": [{"field": f, "key": getattr(c, f), "text": text} for f, (text, _) in HOTKEY_ACTIONS.items()]}

    # ---------- 局面 ----------

    def side(self) -> str | None:
        if self.side_mode in SIDES:
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
        team = {s: [h for t in self.slots[s] if (h := self._hero(t))] for s in SIDES}
        side = self.side() or "radiant"
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
        self.notice = None
        changed = self.prev_draft is None or draft.key() != self.prev_draft.key()
        if changed:
            log.info("局面：我 %s · 队友 %s · 对面 %s · 禁用 %s · %d 号位 → 推荐 %s",
                     draft.my_hero.name_cn if draft.my_hero else "未选", _names(draft.allies), _names(draft.enemies),
                     _names(draft.bans), self.position,
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
        if self.view.get("team") and st and st.alive and st.in_match:
            return {**self.view, "team": None}
        return self.view

    def clear(self) -> None:
        self.slots = {s: [""] * 5 for s in SIDES}
        self.hints = {s: [{} for _ in range(5)] for s in SIDES}
        self.bans_text = ""
        self.grid.reset()
        self.prev_draft = None
        self.refresh()

    def show_notice(self, title: str, lines: list[tuple[str, str]]) -> None:
        """临时盖住选人推荐显示一段提醒（演示 / 局内提醒用）；下一次 refresh 会换回推荐"""
        self.notice = {"kind": "notice", "title": title, "lines": [{"kind": k, "text": t} for k, t in lines]}

    # ---------- 识别 ----------

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
            log.info("网格识别：禁用 %s；已选 %s", _names(self._ids(self.grid.bans)), _names(self._ids(self.grid.taken)))
            self.refresh()

    def scan_role(self, img) -> None:
        """选人阶段每 3 秒试一次认自己的位置，认出来这局就不再认"""
        st = self.gsi.state if self.gsi else None
        if (self._role_busy or self._role_draft == self._drafts_seen or time.monotonic() < self._role_next
                or not (self.cal and self.cal.ready and st and st.alive and st.team)):
            return
        self._role_busy, self._role_next = True, time.monotonic() + ROLE_RETRY
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
        img = self.grab() if img is None else img
        view = recognize(img, self.cal, self.bank, self.cfg.match_threshold, keep_images=True)
        self._confirm_with_grid(view)
        self.last_scan = time.time()
        key = view.key()
        if log.isEnabledFor(logging.DEBUG):                  # 每秒一次，只在 DEBUG 时记（调 match_threshold 用）
            log.debug("识别：天辉 %s | 夜魇 %s", *_view_text(self.db, view, SIDES))
        stable = force or key == self.pending_key            # 连续两次一致才采用，躲开动画过程
        self.pending_key = key
        if not stable:
            return
        changed = self.last_view is None or self.last_view.key() != key
        self.last_view = view
        if changed or force:
            log.info("识别结果%s：天辉 %s | 夜魇 %s | 禁用 %s", "变了" if changed else "（手动重识别）",
                     *_view_text(self.db, view))
            self.fill_slots(view)
            self.refresh(force_speech=force)

    def _confirm_with_grid(self, view: DraftView) -> None:
        """顶栏分数没过阈值、但最像的英雄在选人网格里确实已被选走（变暗没斜线）的，也算认出来。
        2026-10-23 实测：拉比克在顶栏只有 0.63（阈值 0.70），网格里已经变暗。"""
        for s in view.radiant + view.dire:
            if s.hero_id is None and s.guess in self.grid.taken and s.score >= GRID_CONFIRM_MIN:
                s.hero_id = s.guess

    def fill_slots(self, view: DraftView) -> None:
        """正在手动改的格子由网页自己保护（输入框有焦点时不覆盖），这里照常写"""
        for side in SIDES:
            for i, s in enumerate(getattr(view, side)):
                self.slots[side][i] = self.db.by_id[s.hero_id].name_cn if s.hero_id in self.db.by_id else ""
                guess = self.db.by_id.get(s.guess)
                self.hints[side][i] = {"score": round(s.score, 2),
                                       "guess": guess.name_cn if guess and not s.hero_id else ""}

    def learn(self) -> None:
        if not self.last_view:
            self.message = "还没有识别结果可以记"
            return
        n = 0
        for side in SIDES:
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

    # ---------- 局内缺口提醒 ----------

    def coach_tick(self) -> None:
        st = self.gsi.state if self.gsi else None
        selecting = bool(st and st.alive and st.game_state == SELECTION_STATE)
        if selecting and not self._in_selection:
            self._drafts_seen += 1
            self.grid.reset()                    # 新一局：上一局的禁用不算
            # 上一局点 × 收起的叠加层、点开的「如果选它」卡片，新一局选人时自动恢复，不用再按热键
            self.user_hidden = False
            self.pick_key = None
            if self.position_auto:               # 上一局自动认的位置不带到这一局（这局可能是加速，没有位置字样）
                self.position, self.position_auto = self._base_position, False
            self.team_roles = []
            log.info("进入选人阶段（第 %d 次）：叠加层自动显示", self._drafts_seen)
        self._in_selection = selecting
        strategy = bool(st and st.alive and st.game_state == STRATEGY_STATE)
        if not (st and st.alive and (st.in_match or strategy)):
            self.coach_card = None               # 比赛结束 / 回到选人：收起卡片
            self.jungle_cue = None
            return
        # 天梯的 matchid 选人时就有；本地 / 机器人局一直是 0（2026-10-23 实测），连开两局会被当成同一局
        key = st.match_id if st.match_id not in ("", "0") else f"本地{self._drafts_seen}"
        if key != self.brief_match:              # 策略时间就做；错过了（中途开助手）进游戏也补一张
            self._start_brief(key)
        if strategy:
            return
        if st.clock_minutes >= 0 and key != self._horn_match:
            self._horn_match = key               # 号角响了：本局要点收起一次，之后按热键再看就不收了
            if self._showing_brief():
                self.coach_card = None
                self.message = f"本局要点已收起，到中期 / 后期会再弹；想看按 {self.cfg.hotkey_brief}"
        if key != self.coach_match:
            self._start_coach(key)
            return
        self._jungle_tick(st)
        self._lane_log(st, key)
        minute = st.clock_minutes
        died = st.deaths > self._coach_deaths
        self._coach_deaths = st.deaths
        respawned = (st.raw.get("hero") or {}).get("alive") is True
        if self._death_shown and respawned and time.monotonic() - self._death_shown >= DEATH_MIN_SHOW:
            self._death_shown = 0.0              # 复活了：死亡卡片收起（别的卡片不动）
            if self.coach_card and self.coach_card.get("death"):
                self.coach_card = None
        recap = self._death_recap(st) if died else None
        passed = [c for c in coach.CHECKPOINTS if self._coach_checkpoint < c <= minute] if self.coach else []
        if passed:
            self._coach_checkpoint = passed[-1]
        needs, nxt, by_death = [], None, []
        if self.coach and (died or passed):     # coach 是 None：还在准备（后台线程）或者准备失败
            cst = coach.State(minute, st.items, st.last_hits, st.net_worth, st.gold, st.inventory, st.buyback)
            needs = self.coach.tick(cst, checkpoint=bool(passed))
            by_death = self.coach.on_death(recap, cst) if recap else []   # 先按死法调顺序，再说下一件
            nxt = self.coach.next_line(cst)
        lines = [("warn" if n.priority == 0 else "row", n.text) for n in needs]
        # 死了正好回家买东西：每次死亡都说下一件买什么；时间点的提醒只在本来就要弹的时候顺带一句
        shop = ([("head", "回家买")] if died else []) + by_death + ([("row" if died else "dim", nxt)] if nxt else [])             if (nxt or by_death) and (died or lines) else []
        if recap and recap.lines:
            self._show_coach(recap.title, recap.lines + ([("head", "还缺")] + lines if lines else []) + shop,
                             on_death=True)
        elif lines or shop:
            self._show_coach(f"局内提醒 · {coach._mmss(minute)} {'死亡' if died else '时间点'}", lines + shop,
                             on_death=died)
        self._stage_tick(minute)

    def _stage_tick(self, minute: float) -> None:
        """到了中期 / 后期弹一次本局要点的那一页；正弹着别的卡片（死亡复盘、缺口提醒）就等它关了再弹"""
        stage, order = brief.stage_at(minute), [p for p, _ in brief.STAGES]
        if not self.brief or order.index(stage) <= order.index(self._brief_stage):
            return
        if self.coach_card and not self._showing_brief():
            return
        self._brief_stage = stage
        if not self.brief.has(stage):            # 例如没开 AI 时后期那页是空的
            return
        self.brief_page = stage
        self.coach_card = self._brief_view()
        self.user_hidden = False
        log.info("本局要点：%s，弹%s那页", coach._mmss(minute), dict(brief.STAGES)[stage])

    def _jungle_tick(self, st) -> None:
        """到点前几秒亮一条野区提醒；死了不提醒（做不了，也别挡死亡复盘）"""
        alive = (st.raw.get("hero") or {}).get("alive") is not False
        clock = round(st.clock_minutes * 60)     # 分钟换回秒有浮点误差（4:07 会算成 4:06.999）
        cue = self.jungle.cue(clock, self._cue_muted) if self.jungle and alive else None
        if cue and cue != self.jungle_cue:
            log.info("野区提醒「%s」：%s", cue.title, " / ".join(t for _, t in cue.lines))
            self.user_hidden = False             # 和局内提醒一样，新的一条总要让人看见
        self.jungle_cue = cue

    def _lane_log(self, st, key: str) -> None:
        """3:00 / 6:00 / 10:00 把自己的 GSI 数据原样记一笔，赛后 lane-review 对照 STRATZ 的对线结果。
        过了 20 秒才开助手的那个时刻不补记（数据已经不是那一刻的）"""
        clock = round(st.clock_minutes * 60)
        for m in lanestate.LOG_MINUTES:
            if m in self._lane_logged or not m * 60 <= clock <= m * 60 + 20:
                continue
            self._lane_logged.add(m)
            hero = st.raw.get("hero") or {}
            ratio = lambda a, b: hero[a] / hero[b] if hero.get(a) is not None and hero.get(b) else None
            draft = self.current_draft()
            rec = {"at": datetime.now().isoformat(timespec="seconds"), "match": key, "minute": m, "clock": clock,
                   "hero_key": st.my_hero_key, "hero_id": hero.get("id") or (draft.my_hero.id if draft.my_hero else None),
                   "hero_name": draft.my_hero.name_cn if draft.my_hero else None, "pos": self.position, "team": st.team,
                   "player": {k: v for k, v in (st.raw.get("player") or {}).items() if not isinstance(v, (dict, list))},
                   "hero": {k: v for k, v in hero.items() if not isinstance(v, (dict, list))},
                   "hp": ratio("health", "max_health"), "mana": ratio("mana", "max_mana"),
                   "items": sorted(st.items), "cue": self.jungle_cue.title if self.jungle_cue else None}
            try:
                lanestate.log_moment(rec)
                log.info("对线记录：%d:00 已记下（赛后 python -m dota_assistant lane-review 对照）", m)
            except OSError:
                log.warning("对线记录没写进去", exc_info=True)

    def mute_cue(self) -> None:
        """点 × / 按 F8 关掉正显示的野区提醒：这局不再提醒这一种"""
        kind = self.jungle_cue.kind
        self._cue_muted.add(kind)
        self.jungle_cue = None
        self.message = f"这局不再提醒{jungle.LABELS[kind]}（config.toml 里 jungle_cues = false 能全部关掉）"
        log.info(self.message)

    def _death_recap(self, st) -> death.Recap | None:
        """刚死：把死前那段推送整理成「怎么死的」，顺手原样存一份（核对字段 / 调阈值用）"""
        if self._death_names is None or self._death_names[0] != st.my_hero_key:
            from . import kb
            base = kb.KnowledgeBase.load()
            self._death_names = (st.my_hero_key, *(death.names_from_kb(base, st.my_hero_key) if base else ({}, set())))
        history = st.history()
        rec = death.analyze(history, st.team, *self._death_names[1:])
        try:
            death.dump(history, rec)
        except OSError:
            log.warning("死亡复盘：推送没存下来", exc_info=True)
        if rec is None:
            log.info("死亡复盘：历史里没有死前的推送（刚开助手？）")
        return rec

    def _start_coach(self, key: str) -> None:
        """敌方阵容用选人时格子里的（识别 / 手动填的），自己的英雄用 GSI 的"""
        draft = self.current_draft()
        if not draft.my_hero:
            return                               # GSI 还没给英雄，下一轮再试
        self.coach_match, self.coach = key, None
        self.jungle, self.jungle_cue, self._cue_muted, self._lane_logged = None, None, set(), set()
        self._coach_deaths = self.gsi.state.deaths  # 中途开助手：之前的死亡不算
        my_id, enemy_ids, pos = draft.my_hero.id, [h.id for h in draft.enemies], self.position
        log.info("比赛 %s：准备局内提醒（%s · %d 号位 · 对面 %s）", key, draft.my_hero.name_cn, pos, _names(draft.enemies))

        def work():
            t0 = time.monotonic()
            try:
                res, err = self._coach_prepare(self.cfg, my_id, enemy_ids, pos), None
                log.info("局内提醒准备好了（%.1f 秒）", time.monotonic() - t0)
            except Exception as e:
                log.exception("局内提醒准备失败")
                res, err = None, e
            self.submit(self._coach_ready, key, res, err)
        threading.Thread(target=work, daemon=True, name="coach").start()

    def _coach_ready(self, key: str, res, err) -> None:
        if key != self.coach_match:
            return
        if err:
            self.message = f"局内提醒没准备好：{err}"
            return
        self.coach, notes = res
        if self.cfg.jungle_cues:
            self.jungle = jungle.make(self.position, getattr(self.coach, "bench", None),
                                      self.cfg.stack_second, self.cfg.pull_second)
            log.info("野区提醒：%d 号位 · %s", self.position,
                     "、".join(jungle.LABELS[k] for k in sorted(self.jungle.kinds)) or "不提醒")
        st = self.gsi.state
        self._coach_deaths = st.deaths
        self._coach_checkpoint = max((c for c in coach.CHECKPOINTS if c < st.clock_minutes), default=0)
        self.message = "局内提醒已就绪"
        if notes and not self.brief:             # 本局要点里已经有这些了
            self._show_coach("开局提示", [("row", n) for n in notes])

    def _start_brief(self, key: str) -> None:
        draft = self.current_draft()
        if not draft.my_hero:
            return                               # GSI 还没给英雄，下一轮再试
        self.brief_match, self.brief, self._brief_stage = key, None, "lane"
        self.pick_key = None                     # 真的选完了，「如果选它」的卡片就不用了
        ids = (draft.my_hero.id, [h.id for h in draft.allies], [h.id for h in draft.enemies])
        lane_info = {"side": self.side(), "ally_pos": draft.ally_pos}
        log.info("比赛 %s：做本局要点（%s · 队友 %s · 对面 %s）", key, draft.my_hero.name_cn, _names(draft.allies),
                 _names(draft.enemies))

        def work():
            t0 = time.monotonic()
            try:
                res, err = self._brief_build(self.cfg, *ids, self.position, **lane_info), None
                log.info("本局要点做好了（%.1f 秒）", time.monotonic() - t0)
            except Exception as e:
                log.exception("本局要点没做出来")
                res, err = None, e
            self.submit(self._brief_ready, key, res, err, ids)
        threading.Thread(target=work, daemon=True, name="brief").start()

    def _brief_ready(self, key: str, res, err, ids: tuple = ()) -> None:
        if key != self.brief_match:
            return
        if err:
            self.message = f"本局要点没做出来：{err}"
            return
        self.brief, self.brief_page = res, "lane"
        if self.cfg.ai_plan:
            self._start_plan(key, res, ids)
        else:
            res.plan_note = "AI 打法没开（控制面板右上角「设置」里打开）"
        st = self.gsi.state if self.gsi else None
        if st and st.alive and st.in_match and st.clock_minutes >= 0:
            self._brief_stage = brief.stage_at(st.clock_minutes)   # 中途开的助手：当前阶段不弹，后面的阶段照弹
            self.message = f"本局要点已就绪（已经开打，按 {self.cfg.hotkey_brief} 查看）"
            return
        self.show_brief()

    def _start_plan(self, key: str, b: brief.Brief, ids: tuple) -> None:
        b.plan_note = "AI 正在写本局打法…（一般 30~60 秒，做好会补进各阶段的页里）"

        def work():
            t0 = time.monotonic()
            try:
                res, err = self._plan_build(self.cfg, *ids, b.lane_ids, b.text()), None
                log.info("AI 打法做好了（%.1f 秒）", time.monotonic() - t0)
            except Exception as e:
                log.exception("AI 打法没做出来")
                res, err = None, e
            self.submit(self._plan_ready, key, res, err)
        threading.Thread(target=work, daemon=True, name="plan").start()

    def _plan_ready(self, key: str, plan, err) -> None:
        if key != self.brief_match or not self.brief:
            return
        if err:
            self.brief.plan_note = f"AI 打法没做出来：{err}"
        else:
            self.brief.plan = plan
            self.brief.plan_note = "" if plan.sections else "AI 给的建议出处都对不上资料，全部丢掉了"
        if self._showing_brief():                # 正在看要点：这一页补上 AI 写的
            self.coach_card = self._brief_view()
        else:
            self.message = f"本局打法已就绪，按 {self.cfg.hotkey_brief} 查看"

    def _showing_brief(self) -> bool:
        return bool(self.coach_card and self.coach_card.get("kind") == "brief")

    def _brief_view(self) -> dict:
        return self.brief.view(self.brief_page, f"{self.cfg.hotkey_brief} 翻页")

    def show_brief(self) -> None:
        """热键 / 面板按钮：没在看就打开当前阶段那页，正在看就翻到下一阶段（后期再按回到对线期）"""
        if not self._showing_brief() and self.pick_brief and self.pick_key:
            self.pick_page = _next_stage(self.pick_page)
            self.user_hidden = False
            return
        if not self.brief:
            self.message = "这局还没有本局要点（要等选完人、GSI 告诉我你的英雄）"
            return
        if self._showing_brief():
            self.brief_page = _next_stage(self.brief_page)
        else:
            st = self.gsi.state if self.gsi else None
            self.brief_page = brief.stage_at(st.clock_minutes) if st and st.alive and st.in_match else "lane"
        self.coach_card = self._brief_view()
        self.user_hidden = False

    # ---------- 选人时「如果选它」 ----------

    def explain_pick(self, hero_id: int) -> None:
        """点了某个推荐英雄：按当前局面做「如果选它」的要点（联网，后台），做好再让 AI 写打法"""
        h = self.db.by_id.get(hero_id)
        if h is None:
            raise ValueError("没有这个英雄")
        draft = self.current_draft()
        key = (hero_id, draft.key(), self.position)
        self.user_hidden = False
        if key == self.pick_key:
            return                               # 同一个局面已经在做 / 做好了，直接看
        self.pick_key, self.pick_brief, self.pick_page = key, None, "lane"
        self.pick_note = f"正在整理选{h.name_cn}的对线 / 装备数据…"
        ids = (hero_id, [a.id for a in draft.allies], [e.id for e in draft.enemies])
        lane_info = {"side": self.side(), "ally_pos": draft.ally_pos}
        log.info("如果选%s：做要点卡片（%d 号位）", h.name_cn, self.position)

        def work():
            t0 = time.monotonic()
            try:
                res, err = self._brief_build(self.cfg, *ids, self.position, **lane_info), None
                log.info("如果选%s：要点做好了（%.1f 秒）", h.name_cn, time.monotonic() - t0)
            except Exception as e:
                log.exception("如果选%s：要点没做出来", h.name_cn)
                res, err = None, e
            self.submit(self._pick_ready, key, res, err, ids)
        threading.Thread(target=work, daemon=True, name="pick").start()

    def _pick_ready(self, key: tuple, res, err, ids: tuple) -> None:
        if key != self.pick_key:
            return
        if err:
            self.pick_note = f"没做出来：{err}"
            return
        res.title = f"如果选{self.db.by_id[key[0]].name_cn} · {POSITION_NAMES[key[2]]}"
        self.pick_brief = res
        if not self.cfg.ai_plan:
            res.plan_note = "AI 打法没开（控制面板右上角「设置」里打开）"
            return
        res.plan_note = "AI 正在写选它怎么打…（一般 30~60 秒，做好会补进各阶段的页里）"

        def work():
            t0 = time.monotonic()
            try:
                plan, perr = self._plan_build(self.cfg, *ids, res.lane_ids, res.text()), None
                log.info("如果选它：AI 打法做好了（%.1f 秒）", time.monotonic() - t0)
            except Exception as e:
                log.exception("如果选它：AI 打法没做出来")
                plan, perr = None, e
            self.submit(self._pick_plan_ready, key, plan, perr)
        threading.Thread(target=work, daemon=True, name="pick-plan").start()

    def _pick_plan_ready(self, key: tuple, plan, err) -> None:
        if key != self.pick_key or not self.pick_brief:
            return
        if err:
            self.pick_brief.plan_note = f"AI 打法没做出来：{err}"
            return
        self.pick_brief.plan = plan
        self.pick_brief.plan_note = "" if plan.sections else "AI 给的建议出处都对不上资料，全部丢掉了"

    def set_brief_page(self, page: str) -> None:
        if page not in dict(brief.STAGES):
            raise ValueError(f"没有这一页 {page!r}")
        if self._showing_brief():
            self.brief_page = page
            self.coach_card = self._brief_view()
        elif self.pick_key:
            self.pick_page = page

    def pick_view(self) -> dict | None:
        if not self.pick_key:
            return None
        name = self.db.by_id[self.pick_key[0]].name_cn
        stale = self.prev_draft is not None and (self.prev_draft.key(), self.position) != self.pick_key[1:]
        if not self.pick_brief:
            return {"kind": "notice", "title": f"如果选{name}", "closable": True,
                    "lines": [{"kind": "dim", "text": self.pick_note}]}
        v = self.pick_brief.view(self.pick_page, f"点标签或按 {self.cfg.hotkey_brief} 翻页 · × 回到推荐")
        if stale:
            v["title"] += "（阵容变了，再点一次英雄更新）"
        return v

    def _show_coach(self, title: str, lines: list[tuple[str, str]], on_death: bool = False) -> None:
        self.coach_card = {"kind": "notice", "title": title, "closable": True, "death": on_death,
                           "lines": [{"kind": k, "text": t} for k, t in lines]}
        self._death_shown = time.monotonic() if on_death else 0.0
        log.info("弹卡片「%s」：%s", title, " / ".join(t for _, t in lines))
        self.user_hidden = False                 # 新提醒总要让人看见；不想看就关掉这张卡片
        if self.coach_voice:
            self.speaker.say("；".join(t for _, t in lines))

    def save_shot(self) -> None:
        path = SHOTS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}.png"
        write_image(path, self.grab())
        self.message = f"截图已保存：{path}"
        log.info(self.message)

    # ---------- 显示 ----------

    def toggle_overlay(self) -> None:
        """有局内提醒弹着时先关提醒（局内叠加层鼠标点不到，靠热键关）"""
        if self.coach_card:
            self._close_card()
        elif self.jungle_cue:
            self.mute_cue()
        elif self.pick_key:
            self.pick_key = None
        else:
            self.user_hidden = not self.user_hidden

    def _close_card(self) -> None:
        if self._showing_brief():
            self.message = f"本局要点已收起，按 {self.cfg.hotkey_brief} 或控制面板「本局要点」再看"
        self.coach_card = None

    def close_overlay(self) -> None:
        """叠加层右上角的 ×：弹着局内提醒就只关提醒，否则把整个叠加层收起（解锁着也一并固定）"""
        if self.coach_card:
            self._close_card()
            return
        if self.jungle_cue:
            self.mute_cue()
            return
        if self.pick_key:
            self.pick_key = None
            return
        self.user_hidden = True
        if not self.overlay_locked:
            self.set_locked(True)
        self.message = f"叠加层已关闭，按 {self.cfg.hotkey_toggle} 或点「显示叠加层」重新打开"

    def toggle_lock(self) -> None:
        self.set_locked(not self.overlay_locked)

    def set_locked(self, locked: bool) -> None:
        self.overlay_locked = locked
        self.message = "叠加层位置已固定" if locked else f"叠加层已解锁：按住卡片拖动，放好后点右上角的锁（或按 {self.cfg.hotkey_lock}）固定"
        self._save_overlay_state()

    def overlay_moved(self, x: float, y: float, width: int | None = None) -> None:
        if not (-1 <= x <= 2 and -1 <= y <= 2):
            raise ValueError("叠加层位置不对")
        width = width if width is not None else (self.overlay_pos or {}).get("width")
        self.overlay_pos = {"x": round(x, 4), "y": round(y, 4)}
        if width is not None:
            if not OVERLAY_WIDTH[0] <= int(width) <= OVERLAY_WIDTH[1]:
                raise ValueError("叠加层宽度不对")
            self.overlay_pos["width"] = int(width)
        self._save_overlay_state()

    def _load_overlay_state(self) -> None:
        try:
            raw = json.loads(self.overlay_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.overlay_locked = bool(raw.get("locked", True))
        if isinstance(raw.get("x"), (int, float)) and isinstance(raw.get("y"), (int, float)):
            self.overlay_pos = {"x": raw["x"], "y": raw["y"]}
            if isinstance(raw.get("width"), int) and OVERLAY_WIDTH[0] <= raw["width"] <= OVERLAY_WIDTH[1]:
                self.overlay_pos["width"] = raw["width"]

    def _save_overlay_state(self) -> None:
        self.overlay_file.parent.mkdir(parents=True, exist_ok=True)
        self.overlay_file.write_text(json.dumps({"locked": self.overlay_locked, **(self.overlay_pos or {})}),
                                     encoding="utf-8")

    def overlay_visible(self) -> bool:
        if not self.overlay_locked:              # 解锁是为了摆位置，总得看得见
            return True
        if self.user_hidden:
            return False
        if self.coach_card or self.pick_key or self.jungle_cue:
            return True
        in_game_not_draft = bool(self.gsi and self.gsi.state.alive and not self.gsi.state.in_draft
                                 and self.mode == "auto")
        return not in_game_not_draft

    def status(self) -> dict:
        if not self.use_gsi:
            gsi = {"state": "disabled", "text": "GSI 未启用"}
        elif self.gsi is None:
            gsi = {"state": "busy", "text": f"GSI 端口 {self.cfg.gsi_port} 被占用"}
        elif self.gsi.state.alive:
            team = {"radiant": "天辉", "dire": "夜魇"}.get(self.gsi.state.team or "", "?")
            gsi = {"state": "ok", "text": f"GSI 已连接 · {team}", "game_state": self.gsi.state.game_state}
        else:
            gsi = {"state": "off", "text": "GSI 未连接"}
        return {
            "gsi": gsi,
            "calibrated": bool(self.cal and self.cal.ready),
            "last_scan": time.strftime("%H:%M:%S", time.localtime(self.last_scan)) if self.last_scan else "",
            "hotkeys": [{"key": getattr(self.cfg, f), "text": text, "short": short}
                        for f, (text, short) in HOTKEY_ACTIONS.items()],
            "hotkeys_failed": list(self.hotkeys.failed) if self.hotkeys else [],
            "message": self.message,
        }

    def snapshot(self) -> dict:
        slots = {}
        for side in SIDES:
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
            "overlay": {"visible": self.overlay_visible(), "locked": self.overlay_locked,
                        "lock_key": self.cfg.hotkey_lock, "toggle_key": self.cfg.hotkey_toggle, "style": self.overlay_style(),
                        "view": self.overlay_view()},
            "status": self.status(),
        }

    def overlay_view(self) -> dict:
        """野区提醒：有卡片时叠在卡片顶上一行，没卡片时自己一张小卡片（能关）"""
        cue = self.jungle_cue
        if cue and self.coach_card:
            return {**self.coach_card, "cue": {"title": cue.title, "text": cue.lines[0][1]}}
        if cue:
            return {"kind": "notice", "title": cue.title, "closable": True, "jungle": True,
                    "lines": [{"kind": k, "text": t} for k, t in cue.lines]}
        return self.coach_card or self.pick_view() or self.notice or self.draft_view()

    def heroes(self) -> list[dict]:
        return sorted(({"id": h.id, "name": h.name_cn, "en": h.name_en} for h in self.db),
                      key=lambda h: h["name"])

    def close(self) -> None:
        log.info("主控关闭")
        self._stop.set()
        if self.hotkeys:
            self.hotkeys.stop()
        self.speaker.close()
        if self.gsi:
            self.gsi.stop()
