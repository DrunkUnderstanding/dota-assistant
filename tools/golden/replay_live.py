"""Live 回放快照：用冻结目录里的截图 / 存下的 GSI 推送，按顺序推完整一局，每步记 Live.snapshot() 的变化。

怎么造 Live：参照 tests/test_webui.py（use_gsi=False、use_hotkeys=False，GSI 换成一个 GameState 直接改推送内容），
其余全用真的：推荐（data/ 统计）、头像识别（data/portraits + calibration.json）、选人网格、知识库、本局要点 / 局内提醒
（联网走 cassette）、队友战绩（mates.Service + 本地 HTTP 接口，cassette）、认位置 / 买活 OCR（OCR cassette）。只有这些换成假的：
  - 屏幕：FakeScreen，按剧本换帧（选人截图 → 局内截图 → 合成的「顶栏某格变灰」「消息栏买活了」帧）
  - 语音：只记下要念什么
  - AI 打法：直接抛 llm.Unavailable（不调 Claude；走「AI 打法用不了」那条路）
确定性：
  - 不起 Live 的工作线程：本进程主线程就是「工作线程」，submit 的任务按顺序在这里跑（Live._run，和工作线程里一样）；
  - Live 开的后台线程（本局要点 / 局内提醒 / 网格 / 认位置 / 买活 OCR）就地同步跑完，结果排进队列的顺序固定；
  - 队友战绩（d1bfef6 起是独立页 web/mates.html，不走 Live 的工作线程）：主线程直接调 mates.Service，
    再起一次 WebServer 用 http.client 调 /api/mates、/api/match（一次只发一个请求、读完回应再记，顺序固定）；
  - 时钟是假的（g_clock virtual）：time.time / monotonic / datetime.now 只在剧本里拨。
除了 snapshot()，还记：叠加层位置 / 网页 /overlay 接口回的配置 / 叠加层状态文件（extras）、开头一次 live.heroes()、
结尾冻结目录里的文件变动（g_files；config.toml、fixes.jsonl 贴全文）。认位置成功那条路用合成的 OCR 结果走一次。
导入路径集中在 _modules()：新路径（重构后的 dota_assistant.app.live 等）优先，旧路径兜底，搬家后不用改这里。
"""
from __future__ import annotations

import ast
import copy
import hashlib
import http.client
import importlib
import inspect
import json
import logging
import queue
import threading
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

# Live.action 能处理的网页动作（type），回放里每个都要走到。PLAN 第 1 节列了 24 个；d1bfef6 把队友战绩挪到独立页
# （webui 的 /api/mates、/api/match），去掉了 mate_query / mate_remove，剩 22 个。运行时还从 Live.action 源码里抽一遍对账
WEB_ACTIONS = ("set_slot set_bans set refresh clear rescan learn shot dismiss_coach show_brief explain_pick brief_page "
               "buyback ult name_slot ult_set toggle_overlay toggle_lock close_overlay overlay_moved save_settings "
               "open_logs").split()

# 叠加层相关的动作：做完以后不管变没变，都把叠加层位置 / 网页 /overlay 接口回的东西 / 状态文件记一次
OVERLAY_ACTIONS = {"toggle_overlay", "toggle_lock", "close_overlay", "overlay_moved"}
EXTRA_KEYS = ("额外.overlay_pos", "额外.overlay_config", "额外.overlay_file")
# 认位置成功那条路：真 OCR 认不出（手头截图都没有位置字样），这一步给 roles.ocr 换成合成的「顶栏位置字样」
ROLE_TEXT = {1: "优势路", 2: "中路", 3: "劣势路", 4: "辅助", 5: "纯辅助"}
SYNTH_ROLES = [2, 3, 4, 1, 5]               # 夜魇 5 格各自排的位置（我在第 3 格 → 4 号位，和 config 的 1 号位不同）
# 回放结束时列出冻结目录里的文件变动：文本附归一化后的全文（超过 64KB 只记 sha1），这两个不管多长都贴全文
FULL_TEXT = ("config.toml", "data/fixes.jsonl")
# 只记大小 + sha1、不贴全文的：队友战绩缓存的 OpenDota 英雄分位（原样转存 cassette 里的回应，一个英雄一个文件）
NO_TEXT = ("data/benchmarks/",)

MATCH = "9064593746"                         # 冻结目录里有这场的 11 次死亡推送（司夜刺客，夜魇第 3 格）
SEL_FRAMES = ["20261023-013637", "20261023-013638", "20261023-013639", "20261023-014333", "20261023-014335",
              "20261023-014347"]
GAME_FRAME = "deaths/20261028-010824-0s.png"
DEATHS = ["20261028-010328", "20261028-010824", "20261028-011337"]   # 0:04 / 4:59 / 10:13 死的三次
FEED_SB = "20261028-014106-radiant2-buyback-78.png"    # 消息栏「裂魂人 买活了」（这场天辉第 2 格）
FEED_HW = "20261028-000215-dire2-buyback-66.png"       # 消息栏「森海飞霞 买活了」（别的场次，借来走「顶栏没看出死过」那条路）
RADIANT = ["潮汐猎人", "裂魂人", "莱恩", "森海飞霞", "水晶室女"]    # 对面（我在夜魇）
DIRE = ["敌法师", "痛苦女王", "司夜刺客", "撼地者", "巫医"]
SELECTION = "DOTA_GAMERULES_STATE_HERO_SELECTION"
STRATEGY = "DOTA_GAMERULES_STATE_STRATEGY_TIME"
PRE_GAME = "DOTA_GAMERULES_STATE_PRE_GAME"
IN_PROGRESS = "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"
POST_GAME = "DOTA_GAMERULES_STATE_POST_GAME"


def _import(*names: str):
    """依次试几个模块路径，返回第一个 import 得到的。只吞「这个路径本身不存在」的 ModuleNotFoundError"""
    last = None
    for n in names:
        try:
            return importlib.import_module(n)
        except ModuleNotFoundError as e:
            if not (e.name and (n == e.name or n.startswith(e.name + "."))):
                raise
            last = e
    raise last


def _modules() -> SimpleNamespace:
    """回放用到的产品模块，全部在这里 import（新路径优先、旧路径兜底）"""
    return SimpleNamespace(
        live=_import("dota_assistant.app.live", "dota_assistant.live"),
        config=_import("dota_assistant.core.config", "dota_assistant.config"),
        gsi=_import("dota_assistant.game.gsi", "dota_assistant.gsi"),
        roles=_import("dota_assistant.vision.roles", "dota_assistant.roles"),
        vision=_import("dota_assistant.vision.recognize", "dota_assistant.vision"),
        buybackscan=_import("dota_assistant.vision.buybackscan", "dota_assistant.buybackscan"),
        llm=_import("dota_assistant.ai.llm", "dota_assistant.llm"),
        webui=_import("dota_assistant.app.webui", "dota_assistant.webui"),
        mates=_import("dota_assistant.domain.mates", "dota_assistant.mates"),
    )


def handled_actions(Live) -> set[str]:
    """Live.action 的 if / elif 分发表里比过的动作名：t = a.get("type") / a["type"] 以后 t == "x"、t in ("x", "y")。
    用 inspect.getsource 取源码（拆成 mixin 以后照样能取）"""
    fn = ast.parse(textwrap.dedent(inspect.getsource(Live.action))).body[0]

    def is_type(v) -> bool:
        if isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr == "get" and v.args:
            return isinstance(v.args[0], ast.Constant) and v.args[0].value == "type"
        return isinstance(v, ast.Subscript) and isinstance(v.slice, ast.Constant) and v.slice.value == "type"

    names = {t.id for n in ast.walk(fn) if isinstance(n, ast.Assign) and is_type(n.value)
             for t in n.targets if isinstance(t, ast.Name)}
    out = set()
    for n in ast.walk(fn):
        if not (isinstance(n, ast.Compare) and isinstance(n.left, ast.Name) and n.left.id in names):
            continue
        for op, c in zip(n.ops, n.comparators):
            if isinstance(op, ast.Eq) and isinstance(c, ast.Constant) and isinstance(c.value, str):
                out.add(c.value)
            elif isinstance(op, ast.In) and isinstance(c, (ast.Tuple, ast.List, ast.Set)):
                out.update(e.value for e in c.elts if isinstance(e, ast.Constant) and isinstance(e.value, str))
    return out


def pretty(v, ind: int = 0) -> str:
    """JSON：字典逐键换行（键排序），只有标量的列表写一行（帮认候选 127 个 id 不占 127 行）"""
    pad = " " * (ind + 1)
    if isinstance(v, dict):
        if not v:
            return "{}"
        body = ",\n".join(f"{pad}{json.dumps(k, ensure_ascii=False)}: {pretty(v[k], ind + 1)}" for k in sorted(v))
        return "{\n" + body + "\n" + " " * ind + "}"
    if isinstance(v, list) and any(isinstance(x, (dict, list)) for x in v):
        return "[\n" + ",\n".join(pad + pretty(x, ind + 1) for x in v) + "\n" + " " * ind + "]"
    return json.dumps(v, ensure_ascii=False)


class FakeScreen:
    source = "window"

    def __init__(self):
        self.img = None

    def grab(self):
        return self.img


class FakeSpeaker:
    def __init__(self):
        self.enabled = True
        self.said: list[str] = []

    def say(self, text):
        self.said.append(f"{'' if self.enabled else '（语音关着）'}{text}")

    def close(self):
        pass


class LogCapture(logging.Handler):
    """收 dota_assistant.* 的日志：只记级别 + 消息（模块名会因搬家变，不记；异常只记类型和消息）"""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception as e:                     # noqa: BLE001
            msg = f"<日志格式化出错 {type(e).__name__}: {e}>"
        line = f"{record.levelname} {msg}"
        if record.exc_info and record.exc_info[1] is not None:
            e = record.exc_info[1]
            line += f"\n    ! {type(e).__name__}: {e}"
        self.lines.append(line)

    def take(self) -> list[str]:
        out, self.lines = self.lines, []
        return out


def inline_threads():
    """Live 开的后台线程（target 是 dota_assistant 里的函数）就地同步跑：结果进队列的顺序固定"""
    orig = threading.Thread.start
    log = logging.getLogger("dota_assistant")

    def start(self):
        tgt = getattr(self, "_target", None)
        if tgt is not None and str(getattr(tgt, "__module__", "")).startswith("dota_assistant"):
            try:
                self.run()
            except Exception:                    # noqa: BLE001 —— 真线程里没接住的异常会进 threading.excepthook
                log.exception("后台线程 %s 出错（golden 同步跑）", self.name)
            return None
        return orig(self)
    threading.Thread.start = start


class Replay:
    def __init__(self, home: Path, clock, ocr_cassette, norm):
        self.home, self.clock, self.norm = home, clock, norm
        self.m = _modules()
        self.out: list[str] = []
        self.n = 0
        self.prev: dict = {}
        self.covered: set[str] = set()
        self.covered_cue: set[str] = set()
        import g_files
        self.files = g_files
        self.files_before = g_files.scan(home)          # 回放前的冻结目录（Live 建之前）
        self.cap = LogCapture()
        logger = logging.getLogger("dota_assistant")
        logger.addHandler(self.cap)
        logger.setLevel(logging.INFO)
        m = self.m
        # OCR 录放：Live 默认的 ocr / read_roles 都会走 roles.ocr（模块属性，运行时查名字）
        m.roles.ocr = ocr_cassette.wrap(m.roles.ocr, m.roles.Word)
        inline_threads()
        self.read_image = m.vision.read_image
        self.screen = FakeScreen()
        self.speaker = FakeSpeaker()
        self.cfg = m.config.load()

        def no_ai(*a, **kw):
            raise m.llm.Unavailable("golden 回放不调 AI")
        self.live = m.live.Live(self.cfg, screen_factory=lambda: self.screen, use_gsi=False, use_hotkeys=False,
                                speaker=self.speaker, plan_build=no_ai)
        self.state = m.gsi.GameState()
        self.live.gsi = SimpleNamespace(state=self.state, stop=lambda: None)   # 不开端口，直接改推送内容（同 test_webui）
        self.live.use_gsi = True
        self.record("Live 建好（还没推送）", snap_all=True)
        # 网页 /api/heroes 用的（帮认菜单靠它列英雄）
        heroes = json.loads(json.dumps(self.live.heroes(), ensure_ascii=False))
        blob = json.dumps(heroes, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.out.append(self.norm.text(f"### live.heroes()：{len(heroes)} 个，sha1 {hashlib.sha1(blob).hexdigest()[:16]}\n"
                                       + "\n".join(json.dumps(h, ensure_ascii=False, sort_keys=True) for h in heroes)))

    # ---------- 记录 ----------
    def extras(self) -> dict:
        """snapshot() 里没有、但网页 / Electron 会用到的状态：叠加层拖过的位置、/overlay 接口回的配置、叠加层状态文件"""
        live = self.live
        try:
            raw = live.overlay_file.read_text(encoding="utf-8")
            try:
                ofile = json.loads(raw)
            except ValueError:
                ofile = {"<不是 JSON>": raw}
        except OSError:
            ofile = None
        try:
            ocfg = self.m.webui.overlay_config(live.cfg, live.overlay_pos)
        except Exception as e:                     # noqa: BLE001
            ocfg = f"出错 {type(e).__name__}: {e}"
        return json.loads(json.dumps({"额外.overlay_pos": live.overlay_pos, "额外.overlay_config": ocfg,
                                      "额外.overlay_file": ofile}, ensure_ascii=False))

    def record(self, label: str, result: str | None = None, snap_all: bool = False, force: tuple = ()) -> None:
        self.n += 1
        lines = [f"### {self.n:03d} {label}"]
        if result is not None:
            lines.append(f"结果：{result}")
        lines += [f"日志：{x}" for x in self.cap.take()]
        if self.speaker.said:
            lines += [f"播报：{x}" for x in self.speaker.said]
            self.speaker.said = []
        snap = json.loads(json.dumps(self.live.snapshot(), ensure_ascii=False))
        snap.update(self.extras())
        changed = [k for k in sorted(snap) if snap_all or k in force or snap[k] != self.prev.get(k)]
        for k in changed:
            lines.append(f"快照.{k} = " + pretty(snap[k]))
        if not changed:
            lines.append("快照：没变")
        self.prev = snap
        self.out.append(self.norm.text("\n".join(lines)))

    def pump(self) -> None:
        """跑完队列里的任务（含后台「线程」排进来的结果）"""
        live = self.live
        while True:
            try:
                fn, args, fut = live._tasks.get_nowait()
            except queue.Empty:
                return
            live._run(fn, args, fut)

    def call(self, label: str, fn, *args, force: tuple = ()):
        fut = self.live.submit(fn, *args)
        self.pump()
        exc = fut.exception(timeout=0)
        res = f"出错 {type(exc).__name__}: {exc}" if exc else (None if fut.result() is None else repr(fut.result()))
        self.record(label, res, force=force)
        return None if exc else fut.result()

    def act(self, a: dict) -> None:
        t = a.get("type")
        self.covered.add(t)
        overlay = t in OVERLAY_ACTIONS or (t == "set" and str(a.get("key", "")).startswith("overlay_"))
        self.call("动作 " + json.dumps(a, ensure_ascii=False, sort_keys=True), self.live.action, a,
                  force=EXTRA_KEYS if overlay else ())

    def push(self, raw: dict, dt: float = 1.0) -> None:
        self.clock.advance(dt)
        self.state.update(raw, time.time())

    def tick(self, label: str) -> None:
        self.call(label, self.live.tick)

    # ---------- 队友战绩（独立页，不走 Live.action）----------
    def svc(self, label: str, fn, *args):
        """直接调 mates.Service 的方法（主线程，阻塞到查完）；记返回值或异常"""
        try:
            res = fn(*args)
        except Exception as e:                     # noqa: BLE001 —— 抛出来的也是行为（网页那边会变成 400 / 404）
            self.record(label, f"出错 {type(e).__name__}: {e}")
            return None
        self.record(label, pretty(json.loads(json.dumps(res, ensure_ascii=False))))
        return res

    def http(self, port: int, method: str, path: str, body: dict | None = None):
        """本地服务发一个请求，读完回应再记（服务端日志都在回应之前打完，顺序固定）；返回解析后的 JSON"""
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=300)
        try:
            data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
            conn.request(method, path, body=data, headers={"Content-Type": "application/json"} if data else {})
            r = conn.getresponse()
            raw = r.read()
            status, ctype = r.status, r.getheader("Content-Type")
        finally:
            conn.close()
        try:
            res = json.loads(raw.decode("utf-8"))
            text = pretty(res)
        except ValueError:
            res, text = None, f"<不是 JSON> {raw[:200]!r}"
        label = f"HTTP {method} {path}" + ("" if body is None else " " + json.dumps(body, ensure_ascii=False, sort_keys=True))
        self.record(label, f"{status} {ctype}\n{text}")
        return res

    def mates_flow(self) -> None:
        """队友战绩页的全部后端入口：查（第一次 / 20 分钟内不重查 / 刷新 / 查不到 / 乱填）、列表、点一局看整局（缓存、
        STRATZ 评分）、从整局里点名字查别人（「和你一起」走 peers）、删人；再起一次 WebServer 走一遍 HTTP 层"""
        m, cfg = self.m, self.cfg
        code = str(cfg.account_id or 149388561)
        svc = m.mates.Service(cfg, db=self.live.db)
        self.svc("队友战绩 Service.list()（冻结目录里原有的）", svc.list)
        self.svc(f"队友战绩 Service.query({code})", svc.query, code)
        self.svc(f"队友战绩 Service.query({code})：20 分钟内查过，不重查", svc.query, code)
        self.clock.advance(60)
        self.svc(f"队友战绩 Service.query({code}, force=True)：刷新", svc.query, code, True)
        self.svc("队友战绩 Service.query('1')：好友代码 1（OpenDota 上有资料）", svc.query, "1")
        self.svc("队友战绩 Service.query('4294967294')：没这个号", svc.query, "4294967294")
        self.svc("队友战绩 Service.query('不是数字')", svc.query, "不是数字")
        views = self.svc("队友战绩 Service.list()", svc.list) or []
        me = next((v for v in views if str(v.get("account_id")) == code), {})
        mid = next((x["match_id"] for x in me.get("matches") or [] if x.get("match_id")), None)
        other = None
        if mid:
            view = self.svc(f"队友战绩 Service.match(最近一局, focus={code})", svc.match, mid, int(code))
            self.svc("队友战绩 Service.match(同一局, focus=None)：整局读缓存", svc.match, mid, None)
            players = [p for t in ("radiant", "dire") for p in ((view or {}).get("teams") or {}).get(t) or []]
            other = next((p["account_id"] for p in players if p.get("account_id") and str(p["account_id"]) != code), None)
        else:
            self.out.append("### 队友战绩：自己的最近比赛是空的，没走「看整局」")
        self.svc("队友战绩 Service.match(99999999999)：没有这局", svc.match, 99999999999, None)
        if other:
            self.svc("队友战绩 Service.query(整局里另一个人)：点名字查", svc.query, str(other))
        self.svc("队友战绩 Service.remove(1)", svc.remove, 1)
        self.svc("队友战绩 Service.list()：删掉以后", svc.list)
        # HTTP 层：WebServer 自己建一个 Service（读上面存下的 data/mates.json）
        ws = m.webui.WebServer(self.live).start()
        port = ws.server.server_address[1]
        try:
            self.http(port, "GET", "/api/mates")
            self.http(port, "POST", "/api/mates", {"op": "query", "code": code})        # 刚查过：不重查
            self.http(port, "POST", "/api/mates", {"op": "query", "code": "abc"})
            self.http(port, "POST", "/api/mates", {"op": "nope"})
            self.http(port, "POST", "/api/mates", {"op": "remove"})
            if mid:
                self.http(port, "GET", f"/api/match/{mid}?p={code}")
            self.http(port, "GET", "/api/match/99999999999")
            if other:
                self.http(port, "POST", "/api/mates", {"op": "remove", "account_id": other})
            self.http(port, "GET", "/api/mates")
        finally:
            ws.stop()

    def files_changed(self) -> str:
        """冻结目录里回放新增 / 改动 / 删除的文件（logs/ 不算）：路径 + 大小 + sha1；文本附归一化后的内容"""
        f = self.files
        lines = f.report(self.home, self.files_before, f.scan(self.home), self.norm, f.Scrubber(None),
                         content=lambda rel: not rel.startswith(NO_TEXT), limit=lambda rel: 0 if rel in FULL_TEXT else 64_000)
        return "### 冻结目录里的文件变动（logs/ 不算）\n" + ("\n".join(lines) if lines else "（没有）")

    def synth_role_words(self, img, side: str, positions: list[int]) -> list:
        """合成「顶栏位置字样」的 OCR 结果：坐标按 read_roles 裁出来、放大过的那条图算，每个位置词写在对应格子正下方"""
        roles, cal = self.m.roles, self.live.cal
        slots = cal.radiant if side == "radiant" else cal.dire
        W = img.shape[1]
        up = roles.UPSCALE
        x0 = int(min(r[0] for r in slots) * W)
        words, cw = [], 14.0 * up
        for r, pos in zip(slots, positions):
            text = ROLE_TEXT[pos]
            cx = ((r[0] + r[2] / 2) * W - x0) * up
            start = cx - cw * len(text) / 2
            for k, ch in enumerate(text):                       # 中文 OCR 一个字一个词
                words.append(roles.Word(1, ch, start + k * cw, 40.0, cw, 28.0))
        return words

    # ---------- 推送 ----------
    def load_deaths(self, stem: str) -> list[tuple[float, dict]]:
        raw = json.loads((self.home / "shots" / "deaths" / f"{stem}.json").read_text(encoding="utf-8"))
        return [(p.pop("t"), p) for p in raw["pushes"]]

    def synth(self, base: dict, clock: int | None = None, state: str | None = None, deaths: int | None = None,
              alive: bool | None = None, **hero) -> dict:
        """拿一份真推送改几个字段（时钟 / 阶段 / 死亡数 / 活没活 / 英雄字段）"""
        p = copy.deepcopy(base)
        p.pop("previously", None)
        p.pop("added", None)
        mp = p.setdefault("map", {})
        if clock is not None:
            mp["game_time"] = mp.get("game_time", 0) - mp.get("clock_time", 0) + clock
            mp["clock_time"] = clock
        if state:
            mp["game_state"] = state
        if deaths is not None:
            p.setdefault("player", {})["deaths"] = deaths
        if alive is not None:
            p.setdefault("hero", {})["alive"] = alive
            if alive:
                p["hero"]["respawn_seconds"] = 0
        p.setdefault("hero", {}).update(hero)
        return p

    def play_pushes(self, stem: str) -> dict:
        no = DEATHS.index(stem) + 1
        pushes = self.load_deaths(stem)
        prev_t = pushes[0][0]
        for t, p in pushes:
            self.push(p, max(0.0, t - prev_t) or 0.5)
            prev_t = t
            mp = p.get("map") or {}
            self.tick(f"死亡推送#{no} clock={mp.get('clock_time')} 死亡={p.get('player', {}).get('deaths')}"
                      f" 活着={p.get('hero', {}).get('alive')}")
        return pushes[-1][1]

    def game_push(self, base: dict, clock: int, label: str, dt: float | None = None, **kw) -> None:
        prev = self.state.raw.get("map", {}).get("clock_time")
        step = dt if dt is not None else (clock - prev if isinstance(prev, (int, float)) and clock > prev else 1.0)
        self.push(self.synth(base, clock=clock, **kw), step)
        self.tick(f"{label} clock={clock}")

    # ---------- 剧本 ----------
    def run(self) -> str:
        live, m = self.live, self.m
        sel = [self.read_image(self.home / "shots" / f"{s}.png") for s in SEL_FRAMES]
        game = self.read_image(self.home / "shots" / GAME_FRAME)
        db = live.db
        hid = lambda name: db.find(name).id                       # noqa: E731

        self.call("start：先刷新一次（同 Live.start）", live.refresh)

        # ---- 1. 选人阶段：截图帧 → 识别 / 网格禁用 / 认位置 ----
        template = self.load_deaths(DEATHS[0])[0][1]                # 这场的第一份真推送（PRE_GAME），改出各阶段
        sel_push = self.synth(template, clock=0, state=SELECTION)
        sel_push["hero"] = {}
        for i, img in enumerate(sel):
            self.screen.img = img
            for k in range(2):                                     # 连续两帧一样才采用（识别 / 建网格都要）
                self.push(sel_push)
                self.tick(f"选人帧 {i + 1}/{len(sel)} 第 {k + 1} 次")
        self.clock.advance(3)                                      # 认位置隔 3 秒再试一次
        self.push(sel_push)
        self.tick("选人：过 3 秒再认一次位置")
        # 认位置成功那条路：这一次 OCR 回合成的位置字样（裁图 / 找字 / 对格子 / 改位置 / 播报都是真的）
        real_ocr = m.roles.ocr
        words = self.synth_role_words(sel[-1], "dire", SYNTH_ROLES)
        m.roles.ocr = lambda img, lang="zh-Hans-CN": list(words)
        try:
            self.clock.advance(3)
            self.push(sel_push)
            self.tick("选人：认位置（合成 OCR：夜魇顶栏 " + " ".join(ROLE_TEXT[r] for r in SYNTH_ROLES) + "）")
        finally:
            m.roles.ocr = real_ocr
        self.clock.advance(3)
        self.push(sel_push)
        self.tick("选人：认出来以后这局不再认")
        for side, i in (("radiant", 0), ("dire", 4)):
            c = live.pick_candidates(side, i)
            self.out.append(self.norm.text(f"### 帮认候选 {side} {i}：前 20 个 {c['order'][:20]}，网格里剩下的 {c['taken']}"))
        self.act({"type": "rescan"})
        self.act({"type": "learn"})
        self.act({"type": "shot"})
        self.act({"type": "set", "key": "position", "value": 4})
        self.act({"type": "set", "key": "side_mode", "value": "radiant"})
        self.act({"type": "set", "key": "side_mode", "value": "auto"})
        self.act({"type": "set", "key": "mode", "value": "manual"})
        self.push(sel_push)
        self.tick("手动模式下 tick（不识别）")
        self.act({"type": "set", "key": "mode", "value": "auto"})
        self.act({"type": "set", "key": "voice", "value": True})
        self.act({"type": "set", "key": "voice", "value": False})
        self.act({"type": "set", "key": "scan_without_gsi", "value": False})
        self.act({"type": "set", "key": "scan_without_gsi", "value": True})
        self.act({"type": "set", "key": "bogus", "value": 1})
        self.act({"type": "set_bans", "text": "小小，斧王,不存在"})
        self.act({"type": "refresh"})
        # 「如果选它」：点推荐里第一个英雄 → 翻页 → 关掉
        groups = (live.view or {}).get("groups") or []
        first = next((it["id"] for g in groups for it in g.get("items") or []), hid("斧王"))
        self.act({"type": "explain_pick", "hero_id": first})
        self.act({"type": "brief_page", "page": "mid"})
        self.act({"type": "show_brief"})
        self.act({"type": "brief_page", "page": "bogus"})
        self.act({"type": "dismiss_coach"})
        self.act({"type": "explain_pick", "hero_id": first})
        self.act({"type": "toggle_overlay"})                      # 先关「如果选它」
        self.act({"type": "toggle_overlay"})                      # 再隐藏整个叠加层
        self.act({"type": "toggle_overlay"})                      # 再显示
        # 叠加层：解锁 / 拖动 / 拉宽 / 样式 / 关闭
        self.act({"type": "toggle_lock"})
        self.act({"type": "overlay_moved", "x": 0.5, "y": 0.3, "width": 500})
        self.act({"type": "overlay_moved", "x": 0.61234, "y": 0.25})
        self.act({"type": "overlay_moved", "x": 5, "y": 0.3})
        self.act({"type": "set", "key": "overlay_locked", "value": True})
        self.act({"type": "set", "key": "overlay_opacity", "value": 0.5, "save": False})
        self.act({"type": "set", "key": "overlay_opacity", "value": 0.1})
        self.act({"type": "set", "key": "overlay_color", "value": "#11AA33"})
        self.act({"type": "set", "key": "overlay_color", "value": "red"})
        self.act({"type": "close_overlay"})
        self.act({"type": "toggle_overlay"})
        # 阵容：清空后按这场比赛填（帮认 / 手填 / 填错 / 重复）
        self.act({"type": "clear"})
        self.act({"type": "name_slot", "side": "radiant", "index": 1, "hero_id": hid("裂魂人")})
        self.act({"type": "name_slot", "side": "dire", "index": 0, "hero_id": hid("裂魂人")})     # 已经在别的格子
        self.act({"type": "name_slot", "side": "radiant", "index": 9, "hero_id": hid("莱恩")})    # 格子不存在
        for side, names in (("radiant", RADIANT), ("dire", DIRE)):
            for i, name in enumerate(names):
                if (side, i) != ("radiant", 1):
                    self.act({"type": "set_slot", "side": side, "index": i, "text": name})
        self.act({"type": "set_slot", "side": "dire", "index": 4, "text": "不存在的英雄"})
        self.act({"type": "set_slot", "side": "dire", "index": 4, "text": DIRE[4]})
        self.push(sel_push)
        self.tick("选人：阵容填好后 tick")

        # ---- 2. 策略时间：本局要点 ----
        strat = self.synth(template, clock=0, state=STRATEGY)
        self.push(strat)
        self.tick("策略时间（做本局要点）")
        self.act({"type": "show_brief"})                          # 翻到下一页
        self.act({"type": "brief_page", "page": "late"})
        self.act({"type": "dismiss_coach"})
        self.act({"type": "show_brief"})                          # 关了再打开

        # ---- 3. 出门 → 号角 → 第一次死（真推送，PRE_GAME -54 秒到 0:04）----
        self.screen.img = game
        last = self.play_pushes(DEATHS[0])
        self.clock.advance(4)                                      # 死亡卡片至少显示 3 秒
        self.game_push(last, 20, "复活", alive=True, deaths=1)
        for c in range(23, 145, 3):                                # 野区提醒（屯野 x:53 / 拉野 x:15 …）
            self.game_push(last, c, "对线期", alive=True, deaths=1)
            if live.jungle_cue and "dismiss_coach" not in self.covered_cue:
                self.covered_cue.add("dismiss_coach")
                self.act({"type": "dismiss_coach"})               # 点 × 关掉这一种野区提醒
        for c in (180, 185, 200):                                  # 3:00 对线记录
            self.game_push(last, c, "对线期", alive=True, deaths=1)

        # ---- 4. 第二次死（4:59）→ 复活 → 5:00 / 6:00 ----
        last = self.play_pushes(DEATHS[1])
        self.clock.advance(4)
        self.game_push(last, 330, "复活", alive=True, deaths=2)
        for c in (360, 365, 400):
            self.game_push(last, c, "对线期", alive=True, deaths=2)

        # ---- 5. 第三次死（10:13，中间过 10:00：中期那页 + 对线记录）→ 复活 ----
        last = self.play_pushes(DEATHS[2])
        self.clock.advance(4)
        self.game_push(last, 650, "复活", alive=True, deaths=3)
        self.act({"type": "toggle_overlay"})
        self.game_push(last, 655, "局内", alive=True, deaths=3)

        # ---- 6. 买活：顶栏自动记（变灰 → 复活 → 消息栏「买活了」）、自己 GSI、手动点 ----
        bb = self.m.buybackscan
        gray = game.copy()
        box = bb._box(gray, "radiant", 1, *bb.TIMER)
        import cv2
        box[:] = cv2.cvtColor(cv2.cvtColor(box, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)

        def with_feed(name):
            img = game.copy()
            feed = self.read_image(self.home / "shots" / "buyback" / "feed" / name)
            x0, y0 = bb.FEED[0], bb.FEED[1]
            img[y0:y0 + feed.shape[0], x0:x0 + feed.shape[1]] = feed
            return img
        # 复活后确认期间：先两帧「森海飞霞买活了」（他顶栏没灰过 → 按消息栏记），再两帧「裂魂人买活了」（对上刚复活的他）
        script = ([("裂魂人变灰", gray)] * 3 + [("复活", game)] * 2 + [("消息栏：森海飞霞买活了", with_feed(FEED_HW))] * 2
                  + [("消息栏：裂魂人买活了", with_feed(FEED_SB))] * 2 + [("消息栏没了", game)] * 3)
        clock = 700
        for what, img in script:
            self.screen.img = img
            clock += 1
            self.game_push(last, clock, f"买活自动记：{what}", alive=True, deaths=3)
        gt = self.synth(last, clock=clock + 1)["map"]["game_time"]
        self.game_push(last, clock + 1, "自己买活（GSI buyback_cooldown）", alive=True, deaths=3,
                       buyback_cooldown=gt + 480, buyback_cost=1200)
        self.act({"type": "buyback", "side": "radiant", "index": 0})
        self.act({"type": "buyback", "side": "radiant", "index": 0})          # 再点 = 取消
        self.act({"type": "buyback", "side": "radiant", "index": 2})
        self.act({"type": "buyback", "side": "dire", "index": 2})             # 自己：GSI 自动算
        self.act({"type": "buyback", "side": "dire", "index": 7})             # 格子不存在

        # ---- 7. 对面大招 ----
        self.act({"type": "ult", "side": "radiant", "index": 0})
        self.act({"type": "ult_set", "side": "radiant", "index": 0, "level": 2})
        mods = ((live.buyback_view().get("slots") or {}).get("radiant") or [{}])[0].get("ult") or {}
        mod = next((x["key"] for x in mods.get("mods") or []), None)
        if mod:
            self.act({"type": "ult_set", "side": "radiant", "index": 0, "mod": mod, "on": True})
            self.act({"type": "ult_set", "side": "radiant", "index": 0, "mod": mod, "on": False})
        self.act({"type": "ult_set", "side": "radiant", "index": 0, "mod": "bogus", "on": True})
        self.act({"type": "ult_set", "side": "radiant", "index": 0, "level": 5})
        self.act({"type": "ult", "side": "radiant", "index": 0})              # 再点 = 取消
        self.act({"type": "ult", "side": "radiant", "index": 2})
        self.act({"type": "ult", "side": "dire", "index": 0})                 # 自己这边：没有大招小条
        self.act({"type": "rescan"})                                          # 局内不能重识别

        # ---- 8. 中后期：5 分钟一次的检查点、30:00 后期那页、比赛结束 ----
        for c in (900, 1200, 1500, 1800, 1805):
            self.game_push(last, c, "局内", alive=True, deaths=3)
            if live.coach_card and c in (1200, 1805):
                self.act({"type": "close_overlay"})                          # 弹着卡片时 × 只关卡片
        self.act({"type": "show_brief"})
        self.act({"type": "toggle_overlay"})
        self.act({"type": "show_brief"})
        self.act({"type": "close_overlay"})
        self.game_push(last, 2400, "比赛结束", state=POST_GAME, alive=True, deaths=3)

        # ---- 9. 设置 / 队友战绩 / 日志 ----
        self.act({"type": "save_settings", "ai_plan": True, "ai_provider": "claude_code", "ai_model": "claude-opus-5-5"})
        self.act({"type": "save_settings", "ai_base_url": "ftp://x"})
        self.act({"type": "save_settings", "ai_provider": "bogus"})
        self.act({"type": "save_settings", "hotkeys": {"hotkey_toggle": "F7"}})
        self.act({"type": "save_settings", "hotkeys": {"hotkey_toggle": "F11"}})
        self.act({"type": "save_settings", "account_id": "abc"})
        self.act({"type": "save_settings", "ai_api_key": "has space"})
        self.act({"type": "save_settings", "dota_dir": ""})
        self.act({"type": "save_settings", "account_id": str(self.cfg.account_id or 0)})
        self.act({"type": "save_settings"})
        # 进下一局选人：上一局的阵容 / 裁图 / 帮认标记清掉；AI 打法开着时「如果选它」走 AI 那条（这里直接报用不了）
        self.screen.img = sel[-1]
        nxt = self.synth(template, clock=0, state=SELECTION)
        nxt["hero"] = {}
        nxt["map"]["matchid"] = str(int(MATCH) + 1)
        self.push(nxt)
        self.tick("下一局选人")
        self.act({"type": "explain_pick", "hero_id": first})
        self.mates_flow()
        self.act({"type": "open_logs"})
        self.act({"type": "bogus_action"})

        self.record("回放结束：叠加层状态（全量）", force=EXTRA_KEYS)
        self.out.append(self.files_changed())

        handled = handled_actions(type(live))
        if handled != set(WEB_ACTIONS):
            raise AssertionError(f"Live.action 的分发表和 WEB_ACTIONS 对不上：多了 {sorted(handled - set(WEB_ACTIONS))}，"
                                 f"少了 {sorted(set(WEB_ACTIONS) - handled)}")
        missing = [a for a in WEB_ACTIONS if a not in self.covered]
        self.out.append(f"### 网页动作覆盖：{len(WEB_ACTIONS) - len(missing)}/{len(WEB_ACTIONS)}"
                        + (f"，没走到：{missing}" if missing else "，全部走到"))
        if missing:
            raise AssertionError(f"回放没走到这些网页动作：{missing}")
        return "\n\n".join(self.out)



def run(home: Path, clock, ocr_cassette, norm) -> str:
    rp = Replay(home, clock, ocr_cassette, norm)
    try:
        return rp.run()
    finally:
        rp.live.close()
