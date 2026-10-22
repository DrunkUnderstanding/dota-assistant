"""实时助手的主控（不依赖任何界面库）：定时识别、局面、推荐、语音、热键，状态整理成 JSON 推给网页界面。
选完人（GSI 策略时间）弹「本局要点」卡片（brief.py），后台再让大模型写「打法」第二页（gameplan.py），号角响后收起；
对局开始后接着做局内缺口提醒（coach.py）：死亡时 / 每 5 分钟检查一次，有缺口就在叠加层弹一张卡片。

「控制面板的格子」就是这里的 self.slots，识别和网页输入都只往里写。
所有改状态的操作都放进同一个工作线程按顺序执行（网页请求、热键、定时识别都只是往队列里丢任务）：
  - mss 截屏句柄只能在创建它的线程里用；
  - 不用到处加锁。
状态每变一次就重新生成快照（snapshot），和上次不同才推给订阅者（网页的 SSE 连接）。
"""
from __future__ import annotations

import json
import queue
import threading
import time
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import advisor as adv
from . import brief, coach, gameplan
from .config import DATA_DIR, ROOT, Config
from .engine import POSITION_NAMES
from .gsi import STRATEGY_STATE, GSIServer
from .heroes import Hero
from .hotkeys import Hotkeys
from .speech import Speaker
from .vision import EMPTY, Calibration, DraftView, TemplateBank, recognize, write_image

SHOTS_DIR = ROOT / "shots"
OVERLAY_FILE = DATA_DIR / "overlay.json"       # 叠加层拖动后的位置 + 是否固定（Electron 那边拖完报过来）
SIDES = ("radiant", "dire")
SIDE_MODES = ("auto", "radiant", "dire")       # 阵营：自动（GSI）/ 我在天辉 / 我在夜魇
SCAN_MODES = ("auto", "manual")                # 自动识别 / 手动输入


class Live:
    def __init__(self, cfg: Config, screen_factory=None, use_gsi: bool = True, use_hotkeys: bool = True,
                 speaker: Speaker | None = None, advisor: adv.Advisor | None = None,
                 bank: TemplateBank | None = None, calibration: Calibration | None = None,
                 overlay_file: Path | None = None, coach_prepare: Callable | None = None,
                 brief_build: Callable | None = None, plan_build: Callable | None = None):
        """后面几个参数给测试换成假的；正常运行都不用传"""
        self.cfg = cfg
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
            except OSError:
                self.gsi = None                  # 端口被占用：照样能用，只是没有 GSI

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
        self.overlay_pos: dict | None = None     # 拖过的位置（占屏幕宽 / 高的比例），没拖过就用 config 里的
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
        self.brief_page = "data"                 # 卡片显示哪一页：data 数据 / plan AI 打法
        self._plan_build = plan_build or gameplan.generate
        self.coach_voice = cfg.coach_voice       # 先不念（玩家 2026-10-22 决定），config 里能打开
        self._coach_deaths = 0
        self._coach_checkpoint = 0

        self.last_view: DraftView | None = None
        self.pending_key = None
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
            self.hotkeys = Hotkeys({cfg.hotkey_toggle: lambda: self.submit(self.toggle_overlay),
                                    cfg.hotkey_lock: lambda: self.submit(self.toggle_lock),
                                    cfg.hotkey_rescan: lambda: self.submit(self.scan, True),
                                    cfg.hotkey_shot: lambda: self.submit(self.save_shot),
                                    cfg.hotkey_brief: lambda: self.submit(self.show_brief)}).start()

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
            self.message = f"出错：{e}"
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
            self.coach_card = None
        elif t == "show_brief":
            self.show_brief()
        elif t == "toggle_overlay":
            self.toggle_overlay()
        elif t == "toggle_lock":
            self.toggle_lock()
        elif t == "close_overlay":
            self.close_overlay()
        elif t == "overlay_moved":
            self.overlay_moved(float(a["x"]), float(a["y"]))
        else:
            raise ValueError(f"不认识的操作 {t!r}")
        return self.message

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
        my = None
        if self.gsi and self.gsi.state.alive and self.gsi.state.my_hero_key:
            my = next((h for h in self.db if h.key == self.gsi.state.my_hero_key), None)
        allies = [h for h in team[side] if not my or h.id != my.id]
        return adv.Draft(allies, team[other], bans, my)

    def refresh(self, force_speech: bool = False) -> None:
        draft = self.current_draft()
        advice = self.advisor.advise(draft, self.position)
        self.view = adv.render_view(advice, self.side())
        self.notice = None
        if force_speech or self.prev_draft is None or draft.key() != self.prev_draft.key():
            text = adv.speech(self.prev_draft, advice, force=force_speech)
            if text:
                self.speaker.say(text)
        self.prev_draft = draft

    def clear(self) -> None:
        self.slots = {s: [""] * 5 for s in SIDES}
        self.hints = {s: [{} for _ in range(5)] for s in SIDES}
        self.bans_text = ""
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

    def tick(self) -> None:
        if self.should_scan():
            self.scan()
        self.coach_tick()

    def grab(self):
        if self.screen is None:
            self.screen = self._screen_factory()
        return self.screen.grab()

    def scan(self, force: bool = False) -> None:
        if not (self.cal and self.cal.ready):
            self.message = "还没校准：先运行 python -m dota_assistant calibrate <截图>"
            return
        view = recognize(self.grab(), self.cal, self.bank, self.cfg.match_threshold, keep_images=True)
        self.last_scan = time.time()
        key = view.key()
        stable = force or key == self.pending_key            # 连续两次一致才采用，躲开动画过程
        self.pending_key = key
        if not stable:
            return
        changed = self.last_view is None or self.last_view.key() != key
        self.last_view = view
        if changed or force:
            self.fill_slots(view)
            self.refresh(force_speech=force)

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

    # ---------- 局内缺口提醒 ----------

    def coach_tick(self) -> None:
        st = self.gsi.state if self.gsi else None
        strategy = bool(st and st.alive and st.game_state == STRATEGY_STATE)
        if not (st and st.alive and (st.in_match or strategy)):
            self.coach_card = None               # 比赛结束 / 回到选人：收起卡片
            return
        key = st.match_id or "?"
        if key != self.brief_match:              # 策略时间就做；错过了（中途开助手）进游戏也补一张
            self._start_brief(key)
        if strategy:
            return
        if self.coach_card and self.coach_card.get("kind") == "brief" and st.clock_minutes >= 0:
            self.coach_card = None               # 号角响了：本局要点收起，按热键还能再看
        if key != self.coach_match:
            self._start_coach(key)
            return
        if self.coach is None:                   # 还在准备（后台线程）或者准备失败
            return
        minute = st.clock_minutes
        died = st.deaths > self._coach_deaths
        self._coach_deaths = st.deaths
        passed = [c for c in coach.CHECKPOINTS if self._coach_checkpoint < c <= minute]
        if passed:
            self._coach_checkpoint = passed[-1]
        if not (died or passed):
            return
        needs = self.coach.tick(coach.State(minute, st.items, st.last_hits, st.net_worth), checkpoint=bool(passed))
        if needs:
            self._show_coach(f"局内提醒 · {coach._mmss(minute)} {'死亡' if died else '时间点'}",
                             [("warn" if n.priority == 0 else "row", n.text) for n in needs])

    def _start_coach(self, key: str) -> None:
        """敌方阵容用选人时格子里的（识别 / 手动填的），自己的英雄用 GSI 的"""
        draft = self.current_draft()
        if not draft.my_hero:
            return                               # GSI 还没给英雄，下一轮再试
        self.coach_match, self.coach = key, None
        my_id, enemy_ids, pos = draft.my_hero.id, [h.id for h in draft.enemies], self.position

        def work():
            try:
                res, err = self._coach_prepare(self.cfg, my_id, enemy_ids, pos), None
            except Exception as e:
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
        self.brief_match, self.brief = key, None
        ids = (draft.my_hero.id, [h.id for h in draft.allies], [h.id for h in draft.enemies])

        def work():
            try:
                res, err = self._brief_build(self.cfg, *ids, self.position), None
            except Exception as e:
                res, err = None, e
            self.submit(self._brief_ready, key, res, err, ids)
        threading.Thread(target=work, daemon=True, name="brief").start()

    def _brief_ready(self, key: str, res, err, ids: tuple = ()) -> None:
        if key != self.brief_match:
            return
        if err:
            self.message = f"本局要点没做出来：{err}"
            return
        self.brief, self.brief_page = res, "data"
        if self.cfg.ai_plan:
            self._start_plan(key, res, ids)
        else:
            res.plan_note = "AI 打法没开（config.toml 里 ai_plan = true 打开）"
        st = self.gsi.state if self.gsi else None
        if st and st.alive and st.in_match and st.clock_minutes >= 0:
            self.message = f"本局要点已就绪（已经开打，按 {self.cfg.hotkey_brief} 查看）"
            return
        self.show_brief()

    def _start_plan(self, key: str, b: brief.Brief, ids: tuple) -> None:
        b.plan_note = "AI 正在写本局打法…（一般 30~60 秒，做好会自动切到这页）"

        def work():
            try:
                res, err = self._plan_build(self.cfg, *ids, b.lane_ids, b.text()), None
            except Exception as e:
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
        if self._showing_brief():                # 正在看要点：自动翻到打法页
            self.brief_page = "plan"
            self.coach_card = self._brief_view()
        else:
            self.message = f"本局打法已就绪，按 {self.cfg.hotkey_brief} 查看"

    def _showing_brief(self) -> bool:
        return bool(self.coach_card and self.coach_card.get("kind") == "brief")

    def _brief_view(self) -> dict:
        return self.brief.view(self.brief_page, f"{self.cfg.hotkey_brief} 切换数据 / 打法")

    def show_brief(self) -> None:
        """热键：没在看就打开（打法做好了先看打法），正在看就翻页"""
        if not self.brief:
            self.message = "这局还没有本局要点（要等选完人、GSI 告诉我你的英雄）"
            return
        if self._showing_brief():
            self.brief_page = "plan" if self.brief_page == "data" else "data"
        else:
            self.brief_page = "plan" if self.brief.plan else "data"
        self.coach_card = self._brief_view()
        self.user_hidden = False

    def _show_coach(self, title: str, lines: list[tuple[str, str]]) -> None:
        self.coach_card = {"kind": "notice", "title": title, "closable": True,
                           "lines": [{"kind": k, "text": t} for k, t in lines]}
        self.user_hidden = False                 # 新提醒总要让人看见；不想看就关掉这张卡片
        if self.coach_voice:
            self.speaker.say("；".join(t for _, t in lines))

    def save_shot(self) -> None:
        path = SHOTS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}.png"
        write_image(path, self.grab())
        self.message = f"截图已保存：{path}"

    # ---------- 显示 ----------

    def toggle_overlay(self) -> None:
        """有局内提醒弹着时先关提醒（局内叠加层鼠标点不到，靠热键关）"""
        if self.coach_card:
            self.coach_card = None
        else:
            self.user_hidden = not self.user_hidden

    def close_overlay(self) -> None:
        """叠加层右上角的 ×：弹着局内提醒就只关提醒，否则把整个叠加层收起（解锁着也一并固定）"""
        if self.coach_card:
            self.coach_card = None
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

    def overlay_moved(self, x: float, y: float) -> None:
        if not (-1 <= x <= 2 and -1 <= y <= 2):
            raise ValueError("叠加层位置不对")
        self.overlay_pos = {"x": round(x, 4), "y": round(y, 4)}
        self._save_overlay_state()

    def _load_overlay_state(self) -> None:
        try:
            raw = json.loads(self.overlay_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.overlay_locked = bool(raw.get("locked", True))
        if isinstance(raw.get("x"), (int, float)) and isinstance(raw.get("y"), (int, float)):
            self.overlay_pos = {"x": raw["x"], "y": raw["y"]}

    def _save_overlay_state(self) -> None:
        self.overlay_file.parent.mkdir(parents=True, exist_ok=True)
        self.overlay_file.write_text(json.dumps({"locked": self.overlay_locked, **(self.overlay_pos or {})}),
                                     encoding="utf-8")

    def overlay_visible(self) -> bool:
        if not self.overlay_locked:              # 解锁是为了摆位置，总得看得见
            return True
        if self.user_hidden:
            return False
        if self.coach_card:
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
            "hotkeys": [{"key": self.cfg.hotkey_toggle, "text": "显隐 / 关提醒"},
                        {"key": self.cfg.hotkey_lock, "text": "解锁 / 固定位置"},
                        {"key": self.cfg.hotkey_rescan, "text": "重识别"},
                        {"key": self.cfg.hotkey_shot, "text": "截图"},
                        {"key": self.cfg.hotkey_brief, "text": "本局要点 / 翻页"}],
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
            "settings": {"position": self.position, "side_mode": self.side_mode, "mode": self.mode,
                         "voice": self.voice, "scan_without_gsi": self.scan_without_gsi},
            "slots": slots,
            "bans_text": self.bans_text,
            "overlay": {"visible": self.overlay_visible(), "locked": self.overlay_locked,
                        "lock_key": self.cfg.hotkey_lock, "toggle_key": self.cfg.hotkey_toggle,
                        "view": self.coach_card or self.notice or self.view},
            "status": self.status(),
        }

    def heroes(self) -> list[dict]:
        return sorted(({"id": h.id, "name": h.name_cn, "en": h.name_en} for h in self.db),
                      key=lambda h: h["name"])

    def close(self) -> None:
        self._stop.set()
        if self.hotkeys:
            self.hotkeys.stop()
        self.speaker.close()
        if self.gsi:
            self.gsi.stop()
