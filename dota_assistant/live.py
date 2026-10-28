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
import re
import threading
import time
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from .domain import advisor as adv
from .domain import brief, jungle
from .domain import buyback, lanelog, ultcd
from .vision import buybackscan
from .vision import roles
from .vision import grid
from .domain import mates
from .domain import coach, itembuild
from .domain import death
from .ai import gameplan, llm
from .core import config
from .core import log as logs
from .core.config import DATA_DIR, ROOT, Config
from .core.dota_install import find_dota, is_dota_dir
from .core.gsi_fields import SELECTION_STATE, STRATEGY_STATE
from .core.heroes import Hero
from .domain.engine import POSITION_NAMES
from .gsi import GSIServer, ensure_cfg
from .hotkeys import Hotkeys, vk_code
from .kb import cd_ops
from .sources import assets, http
from .speech import Speaker
from .vision.recognize import EMPTY, Calibration, DraftView, TemplateBank, confident, feature, recognize, write_image

SHOTS_DIR = ROOT / "shots"
OVERLAY_FILE = DATA_DIR / "overlay.json"       # 叠加层拖动后的位置 + 是否固定（Electron 那边拖完报过来）
FIX_FILE = DATA_DIR / "fixes.jsonl"            # 玩家帮认 / 改过的格子（一行一次），回头看学到的模板管不管用、哪次填错了
OPACITY_MIN = 0.3                              # 叠加层底色再透就看不清字了
OVERLAY_WIDTH = (300, 4000)                    # 拉宽叠加层时能存的宽度（逻辑像素），Electron 那边还会收到屏幕以内
SIDES = ("radiant", "dire")
DEATH_MIN_SHOW = 3                             # 死亡卡片至少显示几秒（复活很快 / 演示加速时也看得到）
# 死后第几秒截一张 Dota 窗口，和死前推送存在一起（shots/deaths/<时间>-<秒>s.png）。
# 2026-10-26：先看死亡画面上有没有「谁杀的 / 受到的伤害」，有的话再做识别（出装要看这局谁威胁最大）
DEATH_SHOT_DELAYS = (0, 2)
SIDE_MODES = ("auto", "radiant", "dire")       # 阵营：自动（GSI）/ 我在天辉 / 我在夜魇
SIDE_FALLBACK = "radiant"                      # 不知道阵营（自动但 GSI 没连上）时先当左边是我方，卡片和面板上要标出来
ROLE_RETRY = 3.0                               # 认位置：没认出来隔几秒再试
GRID_CONFIRM_MIN = 0.5                         # 顶栏分数至少这么多，再加上网格核对才算认出
SAME_CROP = 0.92                               # 两张顶栏裁图特征相似度高于这个算同一个样子（没换头像 / 重复，不用再记）
# 选人时顶栏这格比这局开头多出这么多彩色像素，才算真有人选了（没认出就显示「?」）。2026-10-23 截图量的：
# 空位是灰底 + 段位小旗（红旗那几格 0.08~0.10），队友预选是半透明灰图（和空位一样 0.00~0.02），选定的头像 0.17~0.91
PICKED_COLOR_GAIN = 0.04
SCAN_MODES = ("auto", "manual")                # 自动识别 / 手动输入
ORPHAN_WINDOW = 30                             # 消息栏认到「买活了」但顶栏没看出死过的：几帧要在这么多游戏秒内
CLOCK_SLACK = 1.5                              # 买活倒计时用的「游戏时钟 0 秒对应的本机时间」差多少秒才改（GSI 时钟是整秒、推送有延迟）
log = logging.getLogger(__name__)


def _side_cn(side: str) -> str:
    return "天辉" if side == "radiant" else "夜魇"


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


def _check_dota_dir(text) -> str:
    """设置里填的 Dota 目录：空 = 自动找；选到 game\\dota 这种下一层也认，往上找到「dota 2 beta」那层"""
    text = str(text or "").strip().strip('"').strip()
    if not text:
        return ""
    p = Path(text)
    for d in (p, *list(p.parents)[:2]):
        if is_dota_dir(d):
            return str(d)
    raise ValueError(f"{text} 里没有 game\\dota\\pak01_dir.vpk，不像 Dota 安装目录。"
                     "Steam 里右键 Dota 2 → 管理 → 浏览本地文件，复制打开的那个文件夹地址")


def _colorful(img) -> float:
    """顶栏裁图里彩色像素占多少（下面 15% 是玩家颜色条，不算）"""
    hsv = cv2.cvtColor(img[: max(1, int(img.shape[0] * 0.85))], cv2.COLOR_BGR2HSV)
    return float(((hsv[..., 1] > 89) & (hsv[..., 2] > 51)).mean())


def _view_text(db, view, kinds=("radiant", "dire", "bans")) -> list[str]:
    return [" ".join(_slot_text(db, s) for s in getattr(view, k)) or "-" for k in kinds]


def plan_error(err: Exception) -> str:
    """AI 打法页上的那行说明。调不了 AI（llm.Unavailable）不算出错：说缺什么、怎么办，数据页照常"""
    if isinstance(err, llm.Unavailable):
        return f"AI 打法用不了：{err}。数据页不受影响；控制面板右上角「设置」里换 AI 来源，或者关掉 AI 打法"
    return f"AI 打法没做出来：{err}"


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
            from .vision.capture import Screen
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
        self.fix_file = FIX_FILE
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
        if "dota_dir" in a:
            updates["dota_dir"] = _check_dota_dir(a["dota_dir"])
        if not updates:
            return
        hotkeys_changed = any(updates.get(f, getattr(self.cfg, f)) != getattr(self.cfg, f) for f in HOTKEY_ACTIONS)
        account_changed = any(updates.get(k, getattr(self.cfg, k)) != getattr(self.cfg, k)
                              for k in ("account_id", "stratz_token"))
        dota_changed = updates.get("dota_dir", self.cfg.dota_dir) != self.cfg.dota_dir
        path = config.save(updates, self.config_file)
        for k, v in updates.items():
            setattr(self.cfg, k, v)
        log.info("设置已保存到 %s：%s", path, {k: ("已设置" if v else "未设置") if k in SECRET_FIELDS else v
                                               for k, v in updates.items()})
        if hotkeys_changed and self.hotkeys:
            self._start_hotkeys()
        dota_msg = self._apply_dota_dir() if dota_changed else ""
        self.message = (dota_msg if dota_msg
                        else "设置已保存。账号改了要重启助手才会重新拉个人数据" if account_changed
                        else "设置已保存，热键已换好" if hotkeys_changed
                        else "设置已保存，AI 打法下一局按新设置")

    def _apply_dota_dir(self) -> str:
        """设置里改了 Dota 目录：重新找一次，GSI 配置马上写进去（不用重启助手，但 Dota 要重开才读）"""
        if not self.use_gsi:
            return ""
        self.dota_dir = find_dota(self.cfg.dota_dir)
        if self.dota_dir is None:
            return "设置已保存，但还是没找到 Dota 目录，阵营先在控制面板手动选"
        written = ensure_cfg(self.dota_dir, self.cfg.gsi_port)
        if written:
            log.info("GSI 配置写进了 %s", written)
            return f"设置已保存，GSI 配置写进了 {self.dota_dir}：重开 Dota 才生效"
        return f"设置已保存，Dota 目录 {self.dota_dir}（GSI 配置已经在了）"

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
                "dota_dir": c.dota_dir, "dota_found": str(self.dota_dir or ""),
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
        side = self.side() or SIDE_FALLBACK
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
            self.view["side_assumed"] = SIDE_FALLBACK
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
        out: dict = {"ally": self.side() or SIDE_FALLBACK}
        for side in SIDES:
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
        out["asks"] = sum(c["state"] == "ask" for s in SIDES for c in out[s])
        return out

    def _looks_picked(self, side: str, index: int) -> bool:
        """选人阶段这格看着是不是有人选定了：和这局开头那张不一样、而且多出了彩色（队友预选是半透明灰图，不算）"""
        if self._picked_cache[0] != (id(self._crops), self._crops_ver):
            self._picked_cache = ((id(self._crops), self._crops_ver), {})
        cache = self._picked_cache[1]
        if (side, index) not in cache:
            cur, blank = self._crops["sel"].get((side, index)), self._crops["blank"].get((side, index))
            cache[(side, index)] = (cur is not None and blank is not None and cur is not blank
                                    and float(feature(cur) @ feature(blank)) <= SAME_CROP
                                    and _colorful(cur) >= _colorful(blank) + PICKED_COLOR_GAIN)
        return cache[(side, index)]

    def clear(self) -> None:
        self.slots = {s: [""] * 5 for s in SIDES}
        self._named.clear()
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
            log.debug("识别：天辉 %s | 夜魇 %s", *_view_text(self.db, view, SIDES))
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
                     *_view_text(self.db, view))
            self.fill_slots(view, only_empty=only_empty)
            self.refresh(force_speech=force)

    def _keep_crops(self, view: DraftView, phase: str) -> None:
        for side in SIDES:
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
            if (fb is not None and float(f @ fb) > SAME_CROP) or any(float(f @ g) > SAME_CROP for g in seen):
                continue
            seen.append(f)
            hid, score, gap = self.bank.best(c)
            if hid == h.id and confident(score, gap, self.cfg.match_threshold):
                continue
            self.bank.learn(h.id, c)
            n += 1
        if n:
            log.info("记住%s第 %d 格是%s：存了 %d 张模板（共学习 %d 张）",
                     _side_cn(side), index + 1, h.name_cn, n, self.bank.learned)
        return n

    def name_slot(self, side: str, index: int, hero_id: int) -> None:
        """顶栏「这格是谁」点了一个英雄：填进格子，顺手记成模板"""
        if side not in SIDES or not 0 <= index < 5:
            raise ValueError("格子不存在")
        h = self.db.by_id.get(int(hero_id))
        if h is None:
            raise ValueError(f"没有这个英雄 {hero_id}")
        other = next(((sd, j) for sd in SIDES for j, t in enumerate(self.slots[sd])
                      if (sd, j) != (side, index) and self._hero(t) == h), None)
        if other:
            self.message = f"{h.name_cn}已经在{_side_cn(other[0])}第 {other[1] + 1} 格了，先把那格改掉"
            return
        before = self.slots[side][index] or (f"?{self.hints[side][index].get('guess')}" if self.hints[side][index].get("guess") else "")
        self.slots[side][index] = h.name_cn
        self.hints[side][index] = {}
        self._named.add((side, index))
        n = self.learn_slot(side, index, h)
        self._record_fix(side, index, before, h, n, "帮认")
        self.message = f"{_side_cn(side)}第 {index + 1} 格填上{h.name_cn}" + (f"，这格头像记住了，下次认得" if n else "")
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
        used = {h.id for sd in SIDES for t in self.slots[sd] if (h := self._hero(t))}
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
            if s.hero_id is None and s.guess in self.grid.taken and s.score >= GRID_CONFIRM_MIN:
                s.hero_id = s.guess

    def fill_slots(self, view: DraftView, only_empty: bool = False) -> None:
        """正在手动改的格子由网页自己保护（输入框有焦点时不覆盖）；玩家这局告诉过我们的格子（_named）不动。
        only_empty：只往空格子里填认出来的英雄（已经有的不换、不清），填进去的英雄不能和别的格子重复"""
        for side in SIDES:
            for i, s in enumerate(getattr(view, side)):
                if (side, i) in self._named and self.slots[side][i]:
                    continue
                if only_empty:
                    h = self.db.by_id.get(s.hero_id)
                    if (self.slots[side][i] or not h
                            or any(self._hero(t) == h for sd in SIDES for t in self.slots[sd])):
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
        self._take_death_shots()
        st = self.gsi.state if self.gsi else None
        selecting = bool(st and st.alive and st.game_state == SELECTION_STATE)
        if selecting and not self._in_selection:
            self._drafts_seen += 1
            self.grid.reset()                    # 新一局：上一局的禁用不算
            self._crops = {"blank": {}, "sel": {}, "strat": {}, "game": {}}
            self._named.clear()
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
            self._bb_show = False
            return
        # 天梯的 matchid 选人时就有；本地 / 机器人局一直是 0（2026-10-23 实测），连开两局会被当成同一局
        key = st.match_id if st.match_id not in ("", "0") else f"本地{self._drafts_seen}"
        if key != self.brief_match:              # 策略时间就做；错过了（中途开助手）进游戏也补一张
            self._start_brief(key)
        self._buyback_tick(st, key)
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
            nxt = self.coach.next_parts(cst)
        lines = [("row", f"提示：{n.text}（{n.tip}）") if n.tip else ("warn" if n.priority == 0 else "row", n.text)
                 for n in needs]
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
        for m in lanelog.LOG_MINUTES:
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
                lanelog.log_moment(rec)
                log.info("对线记录：%d:00 已记下（赛后 python -m dota_assistant lane-review 对照）", m)
            except OSError:
                log.warning("对线记录没写进去", exc_info=True)

    # ---------- 买活计时 ----------

    def _buyback_tick(self, st, key: str) -> None:
        """换局清空；对上游戏时钟（页面自己走秒，这里只在差得多 / 暂停时改）；自己的冷却按 GSI 自动算"""
        if key != self._bb_match:
            self._bb_match = key
            self.buyback.reset()
            self.ults.reset()
            self._bb_watch.reset()
            self._bb_checks.clear()
            self._bb_orphans.clear()
            self._bb_scan_err = False
        m = st.raw.get("map") or {}
        clock = m.get("clock_time")
        # 进游戏（号角前出门）就显示，玩家 2026-10-26 要的「开局就可以显示」；号角前时钟是负数，倒计时照样算
        self._bb_show = bool((self.cfg.buyback_timer or self.cfg.ult_timer) and st.in_match
                             and isinstance(clock, (int, float)))
        if self._bb_show and self.cfg.ult_timer:
            self._knowledge()                    # 第一次读知识库要一会儿，放在这里读，别拖慢推快照
        if not isinstance(clock, (int, float)):
            return
        if m.get("paused"):
            self._bb_paused = float(clock)
        else:
            self._bb_paused = None
            anchor = st.updated - clock
            if self._bb_anchor is None or abs(anchor - self._bb_anchor) > CLOCK_SLACK:
                self._bb_anchor = anchor
        me = self._my_slot(st)
        if me and st.buyback:
            self.buyback.sync_self(*me, float(clock), st.buyback[1])
        if self._bb_show and self.cfg.buyback_timer and self.cfg.buyback_auto and self._bb_paused is None and clock >= 0:
            self._scan_buyback(float(clock), me)

    def _scan_buyback(self, clock: float, me) -> None:
        """截一帧 Dota 窗口看顶栏：刚死的在后台认复活倒计时，复活得比倒计时早 = 买活（buybackscan.py）"""
        try:
            img = self.grab()
            if getattr(self.screen, "source", "window") != "window":
                return                           # 截到的是整块屏幕（Dota 切出去了），顶栏不在那儿
            deaths, revs = self._bb_watch.update(img, clock, skip={me} if me else ())
            self._keep_game_crops(img)
        except Exception:
            if not self._bb_scan_err:
                self._bb_scan_err = True
                log.warning("买活自动记：截图 / 认顶栏出错，这局不再记日志", exc_info=True)
            return
        match = self._bb_match
        for d in deaths:
            def work(d=d):
                try:
                    sec = buybackscan.read_timer(d, lambda im: [w.text for w in self._ocr(im)])
                except Exception as e:           # 没有 OCR 之类
                    log.warning("买活自动记：复活倒计时没认（%s）", e)
                    sec = None
                self.submit(self._timer_read, match, d, sec)
            threading.Thread(target=work, daemon=True, name="buyback").start()
        for rev in revs:
            self._revived(rev)
        self._check_feed(img, match, clock, me)

    def _keep_game_crops(self, img) -> None:
        """没填英雄的格子留一张局内顶栏头像（活着、彩色的时候），给顶栏帮认的候选排序；每格留一次就够"""
        got = self._crops["game"]
        new = False
        for side in SIDES:
            for i in range(5):
                if ((side, i) not in got and not self.slots[side][i]
                        and buybackscan.saturation(img, side, i) >= buybackscan.ALIVE_SAT):
                    got[(side, i)] = buybackscan.portrait_crop(img, side, i)
                    new = True
        if new:
            self._crops_ver += 1

    def _timer_read(self, match, d, sec) -> None:
        if match == self._bb_match:
            self._bb_watch.set_timer(d, sec)
            log.info("买活自动记：%s %s 死了，复活倒计时认到 %s", coach._mmss(d.clock / 60), self._slot_name(d.side, d.index), sec)

    def _slot_name(self, side: str, index: int) -> str:
        return self.slots[side][index] or f"{'天辉' if side == 'radiant' else '夜魇'}第 {index + 1} 个"

    def _revived(self, rev) -> None:
        """复活了：接下来几帧看消息栏有没有「买活了」、行首小图是不是他（buybackscan.FeedCheck），够帧数了在 _feed_done 定"""
        h = self._hero(self.slots[rev.side][rev.index])
        known = frozenset(x.key for side in SIDES for t in self.slots[side] if (x := self._hero(t)))
        self._bb_checks.append(buybackscan.FeedCheck(rev, h.key if h else None, h.name_cn if h else None, known))
        log.info("买活自动记：%s %s 复活（灰了 %.0f 秒，倒计时 %s → 该在 %s 复活），看消息栏确认", coach._mmss(rev.clock / 60),
                 self._slot_name(rev.side, rev.index), rev.dead_for, rev.seconds,
                 coach._mmss(rev.expected / 60) if rev.expected is not None else "?")

    def _hero_icon(self, key: str, download: bool = True):
        """英雄小图标（Valve CDN，第一次下、之后读 data/icons）；下不到是 None（这次运行不再试）。
        第一次可能要联网下，只在后台线程里调。download=False：本地没有就算了（不在局内挨个下 127 张）"""
        if key not in self._hero_icons:
            rel = buybackscan.HERO_ICON.format(key)
            if not download and not (assets.ICON_DIR / rel).is_file():
                return None
            self._hero_icons[key] = buybackscan.load_icon(assets.fetch_icon(rel))
        return self._hero_icons[key]

    def _check_feed(self, img, match, clock: float, me=None) -> None:
        """每个还在确认的格子拿这一帧看一次消息栏（后台线程，OCR 一次约 0.5 秒；上一帧还没认完就跳过这帧）"""
        todo = [c for c in self._bb_checks if not c.busy]
        if not todo:
            return
        keys = {h.key for side in SIDES for t in self.slots[side] if (h := self._hero(t))}
        for c in todo:
            c.busy = True

        def work():
            # 行首小图和全部英雄比（格子没填的也认得出；本地有的才用，这局的英雄没有就现下）
            icons = {h.key: ic for h in self.db if (ic := self._hero_icon(h.key, download=h.key in keys)) is not None}
            for c in todo:
                c.has_icon = c.hero in icons
            try:
                lines = buybackscan.read_feed(img, self._ocr, icons)
            except Exception as e:               # 没有 OCR 之类
                log.warning("买活自动记：消息栏没认（%s）", e)
                lines = []
            feed = buybackscan.feed_crop(img).copy()
            self.submit(self._feed_read, match, todo, lines, feed, clock, me)
        threading.Thread(target=work, daemon=True, name="buyback").start()

    def _feed_read(self, match, checks, lines, feed, clock: float = 0.0, me=None) -> None:
        if match != self._bb_match:
            return
        if lines:
            log.info("买活自动记：消息栏 %s", "；".join(f"「{ln.text}」小图 {ln.hero or '?'} {ln.score:.2f}" for ln in lines))
        for c in checks:
            if c not in self._bb_checks:
                continue
            c.add(lines, feed)
            bb, why = c.verdict
            if bb is not None:
                self._bb_checks.remove(c)
                self._feed_done(c, bb, why)
        self._feed_orphans(lines, checks, clock, me)

    def _feed_orphans(self, lines, checks, clock: float, me) -> None:
        """「买活了」那行认出是这局某个英雄，但他不在确认的里头：顶栏没看出他死过（2026-10-27 裂魂人死了马上买活，
        头像灰不到 2 帧），有 FEED_AGREE 帧都看到就按第一次看到的时刻记（买活是在这之前一点）"""
        claimed = {k for c in (*checks, *self._bb_checks) for k in (c.hero, c.found) if k}
        seen = {ln.hero for ln in lines if ln.hero and ln.hero not in claimed}
        self._bb_orphans = {k: v for k, v in self._bb_orphans.items() if clock - v[0] <= ORPHAN_WINDOW}
        for side in SIDES:
            for i, t in enumerate(self.slots[side]):
                if (h := self._hero(t)) is None or h.key not in seen or (side, i) == me:
                    continue
                at = self._bb_orphans.setdefault(h.key, [])
                at.append(clock)
                if len(at) >= buybackscan.FEED_AGREE and self.buyback.start(side, i, at[0]):
                    when = coach._mmss(at[0] / 60)
                    log.info("买活自动记：%s %s → 买活（顶栏没看出他死过，消息栏 %d 帧认到他买活）", when, h.name_cn, len(at))
                    self.message = f"{when} {h.name_cn}买活了（消息栏认到的），冷却 8:00 开始倒计时；认错了点他头像下面的小条取消"

    def _feed_done(self, c, bb: bool, why: str) -> None:
        rev = c.rev
        name, when = self._slot_name(rev.side, rev.index), coach._mmss(rev.clock / 60)
        if bb and c.hero is None and (h := next((h for h in self.db if h.key == c.found), None)):
            name += f"（格子没填，消息栏小图是{h.name_cn}）"
        log.info("买活自动记：%s %s → %s（%s；倒计时那边：%s）", when, name, "买活" if bb else "不是买活", why,
                 {True: "早复活了", False: "按时复活", None: "没认出倒计时"}[rev.buyback])
        try:
            buybackscan.save_sample(rev, ("buyback" if bb else "respawn") + f"-{rev.seconds}", feed=c.feed)
        except OSError:
            log.warning("买活样本没存下来", exc_info=True)
        if bb and self.buyback.start(rev.side, rev.index, rev.clock):
            self.message = f"{when} {name}买活了（消息栏认到的），冷却 8:00 开始倒计时；认错了点他头像下面的小条取消"

    def _game_clock(self) -> float | None:
        if self._bb_paused is not None:
            return self._bb_paused
        return time.time() - self._bb_anchor if self._bb_anchor is not None else None

    def _my_slot(self, st) -> tuple[str, int] | None:
        """我在顶栏哪一格：GSI 的 team_slot，没有就在格子里找我的英雄"""
        side = st.team
        if side not in SIDES:
            return None
        if st.team_slot is not None and 0 <= st.team_slot < 5:
            return side, st.team_slot
        i = next((i for i, t in enumerate(self.slots[side]) if (h := self._hero(t)) and h.key == st.my_hero_key), None)
        return (side, i) if i is not None else None

    def mark_buyback(self, side: str, index: int) -> None:
        """顶栏头像下面的小条被点了：这个英雄刚买活，开始 8 分钟倒计时；正在倒计时就取消（点错了）"""
        if side not in SIDES or not 0 <= index < 5:
            raise ValueError("格子不存在")
        clock = self._game_clock()
        if not (self._bb_show and self.cfg.buyback_timer) or clock is None:
            self.message = "进游戏后才能记买活"
            return
        if (side, index) in self.buyback.auto:
            self.message = "自己的买活冷却按 GSI 自动算，不用点"
            return
        name = self.slots[side][index] or f"{'天辉' if side == 'radiant' else '夜魇'}第 {index + 1} 个"
        if self.buyback.mark(side, index, clock):
            self.message = f"{name}买活了，冷却 {buyback.COOLDOWN // 60}:00 开始倒计时（点错了再点一下取消）"
        else:
            self.message = f"已取消{name}的买活计时"
        log.info("买活计时 %s（游戏时间 %s）", self.message, coach._mmss(clock / 60))

    def buyback_view(self) -> dict:
        """给 topbar.html：冷却结束的游戏时钟 + 时钟锚点，页面自己每秒算剩多少（快照不用每秒变）"""
        clock = self._game_clock()
        if not self._bb_show or clock is None or self._bb_anchor is None:
            return {"show": False}
        st = self.gsi.state
        me = self._my_slot(st)
        return {"show": True, "anchor": round(self._bb_anchor * 1000), "paused": self._bb_paused,
                "ready_show": buyback.READY_SHOW, "ult_ready_show": ultcd.READY_SHOW, "my_side": st.team,
                "buyback_on": self.cfg.buyback_timer,
                "slots": {s: [{"name": self.slots[s][i], "end": self.buyback.end(s, i, clock), "me": me == (s, i),
                               "auto": (s, i) in self.buyback.auto, "ult": self._ult_view(s, i, clock),
                               "pick": self._pick_view(s, i)}
                              for i in range(5)] for s in SIDES}}

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

    # ---------- 对面大招冷却 ----------

    def _knowledge(self):
        if self._kb_base is None:
            from . import kb
            self._kb_base = kb.KnowledgeBase.load() or False
            self._kb_ops = cd_ops.load_ops(self._kb_base or None)
        return self._kb_base or None

    def _ult_of(self, side: str, index: int):
        """顶栏这一格是对面的、填了英雄、知识库里有要计时的大招：(英雄, 大招, 能勾的减冷却)，否则 None"""
        mine = self.side()
        if not self.cfg.ult_timer or mine not in SIDES or side == mine:
            return None
        h = self._hero(self.slots[side][index])
        base = self._knowledge() if h else None
        hk = base.heroes.get(h.id) if base else None
        ab = cd_ops.pick(hk) if hk else None
        return (hk, ab, ultcd.mods(hk, ab, base, self._kb_ops)) if ab else None

    def _ult_cd(self, side: str, index: int, ab, mods) -> tuple[int, bool, list, float]:
        """(按几级大招算, 等级是不是菜单里选的, 勾了的减冷却, 算出来的冷却)"""
        key = (side, index)
        lv = self.ults.levels.get(key)
        fixed = lv is not None
        if not fixed:
            lv = ultcd.guess_level(self.gsi.state.level if self.gsi else None)
        on = [m for m in mods if m.key in self.ults.mods.get(key, ())]
        return lv, fixed, on, ultcd.cooldown(ab, lv, on)

    def _ult_check(self, side: str, index: int):
        if side not in SIDES or not 0 <= index < 5:
            raise ValueError("格子不存在")
        clock = self._game_clock()
        if not self._bb_show or clock is None:
            self.message = "进游戏后才能记大招"
            return None
        u = self._ult_of(side, index)
        if not u:
            self.message = "这一格没有要计时的大招（不是对面 / 没填英雄 / 知识库里没有）"
            return None
        return clock, *u

    def mark_ult(self, side: str, index: int) -> None:
        """对面头像下面的大招小条被点了：他刚放了大招，开始倒计时；正在倒计时就取消（点错了）"""
        if not (u := self._ult_check(side, index)):
            return
        clock, hk, ab, mods = u
        lv, _, on, cd = self._ult_cd(side, index, ab, mods)
        extra = "、".join(m.label.split("：")[0] for m in on)
        if self.ults.mark(side, index, clock, cd):
            self.message = (f"{hk.name}放了{ab.name}，按 {lv} 级{'+' + extra if extra else ''}算冷却 {cd:g} 秒开始倒计时"
                            "（点错了再点一下取消，右键改等级 / 减冷却）")
        else:
            self.message = f"已取消{hk.name}{ab.name}的计时"
        log.info("大招计时 %s（游戏时间 %s）", self.message, coach._mmss(clock / 60))

    def set_ult(self, side: str, index: int, level=None, mod=None, on=None) -> None:
        """右键菜单：选大招等级 / 勾减冷却，这局一直按选的算；正在冷却的跟着重算（从放的时候算起）"""
        if not (u := self._ult_check(side, index)):
            return
        _, hk, ab, mods = u
        key = (side, index)
        if level is not None:
            if int(level) not in range(1, len(ultcd.ULT_LEVELS) + 1):
                raise ValueError("大招等级只有 1~3")
            self.ults.levels[key] = int(level)
        if mod is not None:
            if mod not in {m.key for m in mods}:
                raise ValueError(f"{ab.name}没有这项减冷却：{mod}")
            got = self.ults.mods.setdefault(key, set())
            got.add(mod) if on else got.discard(mod)
        lv, _, picked, cd = self._ult_cd(side, index, ab, mods)
        self.ults.recool(side, index, cd)
        extra = "、".join(m.label.split("：")[0] for m in picked)
        self.message = f"{hk.name}的{ab.name}按 {lv} 级{'+' + extra if extra else ''}算：冷却 {cd:g} 秒"
        log.info("大招计时 %s", self.message)

    def _ult_view(self, side: str, index: int, clock: float) -> dict | None:
        u = self._ult_of(side, index)
        if not u:
            return None
        hk, ab, mods = u
        lv, fixed, on, cd = self._ult_cd(side, index, ab, mods)
        return {"hero": hk.name, "name": ab.name, "key": ab.key, "end": self.ults.end(side, index, clock), "level": lv, "fixed": fixed,
                "cd": cd, "cds": "/".join(f"{c:g}" for c in cd_ops.cooldowns(ab)),
                "mods": [{"key": m.key, "label": m.label, "on": m in on} for m in mods]}

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
            path = death.dump(history, rec)
        except OSError:
            log.warning("死亡复盘：推送没存下来", exc_info=True)
        else:
            now = time.monotonic()
            self._death_shots = [(now + d, path.with_name(f"{path.stem}-{d}s.png")) for d in DEATH_SHOT_DELAYS]
            self._take_death_shots()
        if rec is None:
            log.info("死亡复盘：历史里没有死前的推送（刚开助手？）")
        return rec

    def _take_death_shots(self) -> None:
        """到点的死亡截图截下来（只截 Dota 窗口，同 F10）"""
        now = time.monotonic()
        due = [p for t, p in self._death_shots if t <= now]
        if not due:
            return
        self._death_shots = [(t, p) for t, p in self._death_shots if t > now]
        try:
            img = self.grab()
            for p in due:
                write_image(p, img)
        except Exception:                        # 截不到不影响别的
            log.warning("死亡截图没截下来", exc_info=True)

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
            except llm.Unavailable as e:
                log.info("AI 打法跳过：%s", e)
                res, err = None, e
            except Exception as e:
                log.exception("AI 打法没做出来")
                res, err = None, e
            self.submit(self._plan_ready, key, res, err)
        threading.Thread(target=work, daemon=True, name="plan").start()

    def _plan_ready(self, key: str, plan, err) -> None:
        if key != self.brief_match or not self.brief:
            return
        if err:
            self.brief.plan_note = plan_error(err)
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

    def query_mate(self, code, force: bool = False) -> None:
        """查队友战绩：后台线程联网，查完排进工作线程写回。20 分钟内查过的不重查（force = 面板上点了「刷新」）"""
        aid = mates.parse_code(code)
        e = self.mates.touch(aid)
        if e["busy"]:
            return
        if not force and mates.Mates.fresh(e):
            self.mates.save()
            self.message = f"{e['data']['name']} 刚查过，要重查点「刷新」"
            return
        e["busy"], e["error"] = True, ""
        self.message = f"正在查好友代码 {aid} 的战绩…"

        def work():
            t0 = time.monotonic()
            try:
                res, err = self._mate_fetch(self.cfg, aid), ""
                log.info("队友战绩 %s 查好了（%.1f 秒）", aid, time.monotonic() - t0)
            except LookupError as ex:
                res, err = None, str(ex)
            except http.NETWORK_ERRORS as ex:     # 断网 / 限速：一行就够，不记堆栈
                log.warning("队友战绩 %s 没查到：%s", aid, ex)
                res, err = None, f"联网失败：{ex}"
            except Exception as ex:
                log.exception("队友战绩 %s 出错", aid)
                res, err = None, f"出错：{ex}（详情见日志）"
            self.submit(self._mate_ready, aid, res, err)
        threading.Thread(target=work, daemon=True, name="mate").start()

    def _mate_ready(self, aid: int, res: dict | None, err: str) -> None:
        self.mates.done(aid, res, err)
        self.message = err or (f"{res['name']} 的战绩查好了" if res else "")

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
            except llm.Unavailable as e:
                log.info("如果选它：AI 打法跳过：%s", e)
                plan, perr = None, e
            except Exception as e:
                log.exception("如果选它：AI 打法没做出来")
                plan, perr = None, e
            self.submit(self._pick_plan_ready, key, plan, perr)
        threading.Thread(target=work, daemon=True, name="pick-plan").start()

    def _pick_plan_ready(self, key: tuple, plan, err) -> None:
        if key != self.pick_key or not self.pick_brief:
            return
        if err:
            self.pick_brief.plan_note = plan_error(err)
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

    def _show_coach(self, title: str, lines: list[tuple], on_death: bool = False) -> None:
        """lines：(kind, 文字或片段列表)，片段里的装备在界面上显示图标（itembuild.line_view）"""
        self.coach_card = {"kind": "notice", "title": title, "closable": True, "death": on_death,
                           "lines": [{"kind": k, **itembuild.line_view(t)} for k, t in lines]}
        self._death_shown = time.monotonic() if on_death else 0.0
        log.info("弹卡片「%s」：%s", title, " / ".join(itembuild.text(t) for _, t in lines))
        self.user_hidden = False                 # 新提醒总要让人看见；不想看就关掉这张卡片
        if self.coach_voice:
            self.speaker.say("；".join(itembuild.text(t) for _, t in lines))

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
            "buyback": self.buyback_view(),
            "mates": self.mates.view(self.db),
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
