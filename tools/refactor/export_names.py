"""P0.7 搬家清单：导出现有模块的名字、生成 docs/refactor/moves.json、做分层冲突分析。

全程只用 ast 读源码，不 import 产品代码（不触发联网 / 读配置）。

用法：
  python tools/refactor/export_names.py                 打印每个旧模块要进清单的名字（JSON）
  python tools/refactor/export_names.py --write-moves   在现有 docs/refactor/moves.json 上合并（只追加新名字，已有条目不删）
  python tools/refactor/export_names.py --layering      打印分层冲突分析（Markdown，docs/refactor/layering.md 的表格部分）
  python tools/refactor/export_names.py --live-groups   打印 live.py 方法分组的调用关系统计（Markdown）

进清单的名字：
  1. 旧模块顶层定义的公开名字（不以 _ 开头的 def / class / 赋值 / 注解赋值；import 进来的不算）
  2. 测试里 patch / 赋值 / 直接引用的名字（含私有名，含 import 进来又被测试 patch 的，例如 data.DATA_DIR）
  3. 别的产品模块跨模块引用的私有名（data._get、from .x import _y）
  4. 要拆的五个模块（data gsi gamefiles ultcd lanestate）和 live.py：顶层所有名字都逐个定了去处，一并进清单
  5. live.py 的 Live 每个方法，写成 "Live.方法名"

--write-moves 的合并规则（重构中途也能安全地跑）：
  - 已有条目一条不删、顺序不动（test_moves 核对基线 791 条的摘要）；名字已经搬走、旧模块里没了，条目照样留着，
    test_moves 才能一直核对新模块里有它。
  - 新出现的名字追加到末尾。
  - 规则（SPLIT / LIVE_GROUPS）改了某个还没搬的名字的去处：更新那一条的 new / new_file 并打印出来；
    已经搬到新文件的名字规则又说去别处：报错，手工处理。
  - SPLIT / LIVE_GROUPS 里写了、源码里没有的名字：表里已经有（已搬走）不算错；表里也没有才算写错。
  - 旧模块里某个已进表、原本自己定义的名字现在变成 import 进来（留了转发）：报错。IMPORTED_PATCHED 是基线白名单，
    只有 data.DATA_DIR、webui.HTTPServer，不许往里加。

test_moves.py 用到这里的 bindings() / class_methods() / export() / SPLIT / LAYERS / LOGGER_NAMES /
IMPORTED_PATCHED，改名字或签名要一起改。
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PKG_DIR = REPO / "dota_assistant"
TESTS_DIR = REPO / "tests"
MOVES_FILE = REPO / "docs" / "refactor" / "moves.json"
PKG = "dota_assistant"

# ---------- 搬家规则（PLAN 2.1，按实际依赖调整过；调整原因见 NOTES） ----------

# 整体搬的模块：旧模块名 → 新模块（相对 dota_assistant）
WHOLE = {
    "config": "core.config", "log": "core.log", "heroes": "core.heroes",
    "kb": "kb",
    "llm": "ai.llm", "gameplan": "ai.gameplan",
    "engine": "domain.engine", "advisor": "domain.advisor", "lanes": "domain.lanes", "explain": "domain.explain",
    "teamfight": "domain.teamfight", "itembuild": "domain.itembuild", "coach": "domain.coach",
    "brief": "domain.brief", "death": "domain.death", "jungle": "domain.jungle", "buyback": "domain.buyback",
    "mates": "domain.mates",
    "capture": "vision.capture", "vision": "vision.recognize", "grid": "vision.grid", "roles": "vision.roles",
    "buybackscan": "vision.buybackscan", "calibrate": "vision.calibrate",
    "hotkeys": "game.hotkeys", "speech": "game.speech",
    "backtest": "research.backtest", "aibacktest": "research.aibacktest",
    "webui": "app.webui", "start": "app.start", "demo": "app.demo",
    "cli": "cli",
}

# 整体搬的模块里个别名字换地方：(旧模块, 名字) → 新模块
WHOLE_EXCEPT = {
    ("coach", "ITEMS_CONST"): "sources.assets",   # PLAN 2.1：data.fetch_items_const 要写它，sources 不能往上 import coach
    ("coach", "items_const"): "sources.assets",
}

# 要拆的模块：旧模块 → {新模块: [名字…]}。顶层每个名字（含私有）都要出现且只出现一次，漏了脚本报错
SPLIT = {
    "data": {
        "sources.http": [
            "OPENDOTA", "STRATZ", "APIError", "NETWORK_ERRORS", "_session", "_get", "_stratz", "_sql",
            "_STRATZ_BRACKETS", "_stratz_brackets", "_say", "logger",
        ],
        "sources.assets": [
            "VALVE_HEROLIST", "CDN", "fetch_heroes", "PORTRAIT_DIR", "fetch_portraits",
            "ICON_DIR", "ICON_PATH", "_icon_locks", "_icon_failed", "fetch_icon", "_download_icon", "ICONS_DONE",
            "ITEM_ICON_DIR", "fetch_items_const", "fetch_item_icons", "load_heroes",
        ],
        "sources.stats": [
            "IDS_PER_DAY", "CHUNK_IDS", "MIN_DURATION", "_bracket_sql", "_chunk_queries", "fetch_stats", "stats_path",
            "load_stats", "_POSITIONS", "POSITION_MIN_MINUTES", "fetch_positions", "positions_path",
            "OPENDOTA_POSITIONS", "positions_source", "load_positions",
        ],
        "sources.player": [
            "MEDALS", "MAIN_POSITION_SHARE", "rank_name", "_player_matches_stratz", "_player_matches_opendota",
            "fetch_player", "update_player", "load_player", "player_history", "_main_position",
        ],
        "app.update": [
            "ROLE_SPLIT", "positions_from_lane_roles", "fetch_positions_opendota", "fetch_icons", "update",
        ],
    },
    "gsi": {
        "core.dota_install": ["is_dota_dir", "steam_libraries", "find_dota"],
        "core.gsi_fields": ["STRATEGY_STATE", "SELECTION_STATE", "DRAFT_STATES", "MATCH_STATES", "buyback_left"],
        "game.gsi": [
            "HISTORY_SECONDS", "log", "CFG_NAME", "CFG_TEMPLATE", "cfg_path", "install_cfg", "ensure_cfg",
            "GameState", "GSIServer", "dump",
        ],
    },
    "gamefiles": {
        "core.vpk": ["VPK"],
        "vision.gamefiles": [
            "GRID_PORTRAIT_DIR", "SELECTION", "STAMP", "log", "_texture_info", "_rgb565", "dxt5", "ycocg_to_bgr",
            "decode_vtex", "extract_selection",
        ],
    },
    "ultcd": {
        # MIN_CD / cooldowns / pick 从「domain/ultcd」改放到 kb/cd_ops：update_ops 要用 pick 挑大招，留在 domain 就成了 kb → domain
        "kb.cd_ops": [
            "MIN_CD", "cooldowns", "pick",
            "OPS_PATH", "HERO_FILE", "MIRROR", "parse_kv", "_find", "cd_ops", "_reader", "update_ops",
            "_read_ops_file", "load_ops",
        ],
        "domain.ultcd": [
            "READY_SHOW", "ULT_LEVELS", "OCTARINE", "log", "guess_level", "_at", "cooldown", "Mod", "parse_op",
            "_describe", "mods", "Tracker",
        ],
    },
    "lanestate": {
        "domain.lanelog": ["LOG_FILE", "LOG_MINUTES", "log_moment"],
        "research.lanestate": [
            "CACHE", "REPORT", "BATCH", "UNTIL", "INV_OFFSET", "FAMANGOS", "MINUTES", "CLASSES", "CLASS_NAMES",
            "SLOTS", "_FIELDS", "compact", "fetch", "lane_of", "outcome", "_n", "Row", "rows", "OWN_SET", "LANE_SET",
            "FEATURE_SETS", "_matrix", "Model", "cross_proba", "_pct", "_dist", "_bucket_table", "BUCKETS",
            "evaluate", "choices", "report", "run", "own_features", "_models", "_guess", "review",
        ],
    },
}

# 测试 patch 的、但在旧模块里是 import 进来的名字：搬到哪个新模块（那个模块也得 import 它，patch 才打得到调用方）。
# 基线（b1113e8）白名单，写死：原本在旧模块里自己定义的名字后来变成 import（留转发），绝不能加到这里洗白。
IMPORTED_BASELINE = frozenset({("data", "DATA_DIR"), ("webui", "HTTPServer")})
IMPORTED_PATCHED = {
    ("data", "DATA_DIR"): "sources.stats",   # test_nokey：patch data.DATA_DIR 后调 positions_source / load_positions（都在 stats）
    ("webui", "HTTPServer"): "app.webui",    # test_webui：patch webui.HTTPServer（从 log import 的），webui 整体搬，照旧 import
}

# live.py：模块级名字一律 consts（Live 类本身、log 在 core）；Live 的方法按 P4 第 2 步的 mixin 分组
LIVE_MODULE_LEVEL = {"Live": "core", "log": "core"}
LIVE_GROUPS = {
    "core": [
        "__init__", "start", "submit", "_loop", "_run", "subscribe", "unsubscribe", "publish", "current_snapshot",
        "action", "snapshot", "status", "close", "heroes", "side", "_hero", "save_shot",
    ],
    "draft": [
        "current_draft", "refresh", "draft_view", "slots_view", "_looks_picked", "clear", "show_notice",
        "should_scan", "should_scan_bans", "tick", "scan_bans", "scan_role", "_role_ready", "_grid_ready", "_ids",
        "grab", "scan", "_keep_crops", "learn_slot", "name_slot", "_record_fix", "pick_candidates", "_pick_view",
        "_confirm_with_grid", "fill_slots", "learn",
    ],
    "ingame": [
        "coach_tick", "_stage_tick", "_jungle_tick", "_lane_log", "_death_recap", "_take_death_shots",
        "_start_coach", "_coach_ready", "mute_cue", "_show_coach",
    ],
    "timers": [
        "_buyback_tick", "_scan_buyback", "_keep_game_crops", "_timer_read", "_slot_name", "_revived", "_hero_icon",
        "_check_feed", "_feed_read", "_feed_orphans", "_feed_done", "_game_clock", "_my_slot", "mark_buyback",
        "buyback_view", "_knowledge", "_ult_of", "_ult_cd", "_ult_check", "mark_ult", "set_ult", "_ult_view",
    ],
    "cards": [
        "_start_brief", "_brief_ready", "_start_plan", "_plan_ready", "_showing_brief", "_brief_view", "show_brief",
        "explain_pick", "_pick_ready", "_pick_plan_ready", "set_brief_page", "pick_view",
    ],
    "overlay": [
        "toggle_overlay", "_close_card", "close_overlay", "toggle_lock", "set_locked", "overlay_moved",
        "_load_overlay_state", "_save_overlay_state", "overlay_visible", "overlay_view", "set_overlay_style",
        "overlay_style",
    ],
    "settings": ["save_settings", "_apply_dota_dir", "settings_view", "_start_hotkeys"],
    # 原来还有 "mates": query_mate / _mate_ready：main 的 d1bfef6 把队友战绩改成独立页走 webui /api/mates，这两个方法删了
}
LIVE_PKG = "app.live"
# 分发入口：被它们调用不说明「属于哪组」（网页动作、热键、快照、每秒的 tick / coach_tick、叠加层视图拼装）
LIVE_DISPATCHERS = {"__init__", "start", "_loop", "action", "snapshot", "publish", "_start_hotkeys", "tick",
                    "coach_tick", "overlay_view"}

NOTES = {
    "格式": "entries 每条：old 旧模块、name 名字（Live 的方法写 Live.方法名）、new 新模块、old_file / new_file 源文件、"
            "tags 为什么进清单（public 公开名 / split 拆分模块的全部名字 / live_method / test_patch / test_assign / "
            "test_ref / cross_module）、imported=true 表示旧模块里这个名字是 import 进来的（测试 patch 它）、"
            "per_module=true 表示每个拆出来的新文件各自定义一份（logger），不查「旧文件不能再定义」。"
            "由 tools/refactor/export_names.py --write-moves 生成。表只追加不删：已有条目不删、不挪顺序（test_moves 核对前 791 条的摘要），"
            "名字搬走以后条目照样留着；新名字追加到末尾。没搬的名字改放法：改脚本里的 SPLIT / LIVE_GROUPS 再跑 --write-moves"
            "（会更新那一条的 new / new_file），或手工改这一条，并在 notes 里写原因。imported 只许 data.DATA_DIR、webui.HTTPServer 两条。",
    "ultcd 调整": "MIN_CD、cooldowns、pick 从 PLAN 表的 domain/ultcd 改放到 kb/cd_ops：kb/cd_ops.update_ops 要用 pick "
                  "挑「对面会给小条的大招」，pick 又要 cooldowns / MIN_CD；按表放会变成 kb → domain 往上导入。三个都是只读知识库条目的纯函数，"
                  "domain/ultcd（cooldown、mods）和 app（live、app/update.fetch_icons）往下引用 cd_ops 即可。",
    "data 调整": "PLAN 表没写到的名字按调用方放：VALVE_HEROLIST / CDN / PORTRAIT_DIR / ICON_PATH / _icon_locks / _icon_failed / "
                 "ITEM_ICON_DIR → sources/assets；IDS_PER_DAY / CHUNK_IDS / MIN_DURATION / _bracket_sql / _chunk_queries / _POSITIONS "
                 "→ sources/stats（_POSITIONS 也被 sources/player 用，同层引用 stats._POSITIONS）；MEDALS / MAIN_POSITION_SHARE → "
                 "sources/player；ROLE_SPLIT 只有 positions_from_lane_roles 用 → app/update；_STRATZ_BRACKETS 跟着 _stratz_brackets → "
                 "sources/http；logger → sources/http（其它新模块各自 logging.getLogger(__name__)）。",
    "data.DATA_DIR": "test_nokey 里 mock.patch.object(data, 'DATA_DIR', d) 之后调 positions_source / load_positions：这三个都在 "
                     "sources/stats，patch 目标改成 stats.DATA_DIR（stats 要 from ..core.config import DATA_DIR）。",
    "patch 目标": "测试 patch 的名字搬家后，调用方必须用「新模块.名字」形式取（不能 from 新模块 import 名字），patch 才生效："
                  "http._get / http._stratz（mates、sources/*、brief、coach、research/lanestate 都要这样调）、"
                  "assets.ICON_DIR / ICONS_DONE / load_heroes / fetch_icon（app/update.fetch_icons 里，test_ultcd patch）、"
                  "assets._download_icon、player._player_matches_stratz（mates 调）、stats.positions_source / load_positions、"
                  "cd_ops.OPS_PATH（test_ultcd 赋值）、lanelog.LOG_FILE（test_webui 赋值；research/lanestate.review 也要用 lanelog.LOG_FILE）、"
                  "app.live.consts.FIX_FILE / DEATH_MIN_SHOW（test_webui 赋值；Live.__init__ 里 self.fix_file = consts.FIX_FILE）。",
    "log / logger": "每个拆出来的新文件各自定义 log = logging.getLogger(__name__)（日志前缀变化是 PLAN 允许的唯一可见差异）。"
                    "清单里 log 只记一个主去处：gsi.log → game/gsi、gamefiles.log → vision/gamefiles、ultcd.log → domain/ultcd、"
                    "live.log → app/live/core。",
    "live 模块级": "PLAN P4：模块级常量和小函数一律 app/live/consts（包括 PLAN 表里写在 settings 的 _check_hotkeys / _check_dota_dir，"
                   "它们是模块级函数不是方法），各 mixin 用 consts.NAME 访问；class Live 和 log 在 core。",
    "kb 与 cli": "kb.py → kb/__init__.py、cli.py → cli/__init__.py：新旧模块名相同，靠 old_file / new_file 区分。",
    "合并 d1bfef6": "重构期间 main 多了 d1bfef6（队友战绩改成独立页）：Live.query_mate / Live._mate_ready 删了（app/live/mates.py 和 MatesMixin 跟着删），"
                   "这两条从表里去掉；mates.py 新加的名字（Service、表现分、整局、STRATZ 评分等 23 个）按 WHOLE 规则去 domain/mates，追加在末尾。"
                   "test_moves 的基线跟着换成 d1bfef6：前 791 条。",
}

LOGGER_NAMES = ("log", "logger")
LAYERS = {"core": 0, "sources": 1, "kb": 2, "ai": 3, "domain": 4, "vision": 5, "game": 5, "research": 6, "app": 7,
          "cli": 8}


# ---------- 读源码 ----------

def old_modules() -> list[str]:
    """dota_assistant/ 下平铺的旧模块（不含 __init__ / __main__，它们不搬）"""
    return sorted(p.stem for p in PKG_DIR.glob("*.py") if p.stem not in ("__init__", "__main__"))


def module_file(mod: str) -> Path:
    """旧模块名（不带包前缀）→ 源文件"""
    return PKG_DIR / f"{mod}.py"


def new_file(new_mod: str) -> str:
    """新模块（相对 dota_assistant）→ 仓库相对路径；kb / cli / app.live 是包"""
    parts = new_mod.split(".")
    if new_mod in ("kb", "cli", LIVE_PKG):
        return "/".join([PKG, *parts, "__init__.py"])
    return "/".join([PKG, *parts]) + ".py"


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _top_statements(body):
    """顶层语句，进 if / try / with 里面（不进 def / class）"""
    for node in body:
        if isinstance(node, (ast.If, ast.Try, ast.With)):
            yield from _top_statements(node.body)
            yield from _top_statements(getattr(node, "orelse", []))
            yield from _top_statements(getattr(node, "finalbody", []))
            for h in getattr(node, "handlers", []):
                yield from _top_statements(h.body)
        else:
            yield node


def _target_names(t) -> list[str]:
    if isinstance(t, ast.Name):
        return [t.id]
    if isinstance(t, (ast.Tuple, ast.List)):
        return [n for e in t.elts for n in _target_names(e)]
    if isinstance(t, ast.Starred):
        return _target_names(t.value)
    return []


def bindings(tree: ast.Module) -> dict[str, str]:
    """顶层绑定的名字 → 种类：def / class / assign / import"""
    out: dict[str, str] = {}
    for node in _top_statements(tree.body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = "def"
        elif isinstance(node, ast.ClassDef):
            out[node.name] = "class"
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                for n in _target_names(t):
                    out.setdefault(n, "assign")
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            for n in _target_names(node.target):
                out.setdefault(n, "assign")
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                if a.name != "*":
                    out.setdefault(a.asname or a.name.split(".")[0], "import")
    return out


def defined_names(tree: ast.Module) -> set[str]:
    """顶层自己定义的名字（不含 import 进来的）"""
    return {n for n, k in bindings(tree).items() if k != "import"}


def class_methods(tree: ast.Module) -> dict[str, set[str]]:
    """顶层每个类直接定义的方法"""
    return {node.name: {m.name for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
            for node in _top_statements(tree.body) if isinstance(node, ast.ClassDef)}


def public_names(mod: str) -> set[str]:
    return {n for n in defined_names(parse(module_file(mod))) if not n.startswith("_")}


def live_methods() -> set[str]:
    path = module_file("live")
    return class_methods(parse(path)).get("Live", set()) if path.exists() else set()


# ---------- import 解析 ----------

def _imports(nodes, current: str | None, modules: set[str]) -> dict[str, tuple]:
    """收集 nodes 里的 import：别名 → ("module", 旧模块) / ("name", 旧模块, 名字) / ("pkg",)。
    current = 正在看的产品模块（解析相对 import）；测试文件传 None"""
    out: dict[str, tuple] = {}
    for node in nodes:
        if isinstance(node, ast.Import):
            for a in node.names:
                parts = a.name.split(".")
                if parts[0] != PKG:
                    continue
                if a.asname:
                    if len(parts) == 2 and parts[1] in modules:
                        out[a.asname] = ("module", parts[1])
                    elif len(parts) == 1:
                        out[a.asname] = ("pkg",)
                else:
                    out[PKG] = ("pkg",)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if current is None or node.level != 1:
                    continue
                base = node.module
            elif node.module and node.module.split(".")[0] == PKG:
                rest = node.module.split(".")[1:]
                base = ".".join(rest) or None
            else:
                continue
            for a in node.names:
                alias = a.asname or a.name
                if base is None:
                    if a.name in modules:
                        out[alias] = ("module", a.name)
                elif base in modules:
                    out[alias] = ("name", base, a.name)
    return out


def _all_imports(tree) -> list:
    return [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]


def _module_of(expr, aliases: dict, modules: set[str]) -> str | None:
    """表达式是不是指向一个 dota_assistant 旧模块：data、ls.data、dota_assistant.data"""
    if isinstance(expr, ast.Name):
        a = aliases.get(expr.id)
        return a[1] if a and a[0] == "module" else None
    if isinstance(expr, ast.Attribute) and expr.attr in modules:
        if isinstance(expr.value, ast.Name) and aliases.get(expr.value.id) == ("pkg",):
            return expr.attr
        if _module_of(expr.value, aliases, modules):
            return expr.attr                    # ls.data：lanestate 里 import 的 data 模块
    return None


class Index:
    """所有旧模块的 ast 和顶层绑定，解析「名字真正定义在哪」"""

    def __init__(self):
        self.modules = set(old_modules())
        self.trees = {m: parse(module_file(m)) for m in self.modules}
        self.binds = {m: bindings(t) for m, t in self.trees.items()}
        self.top_imports = {m: _imports(list(_top_statements(t.body)), m, self.modules) for m, t in self.trees.items()}

    def origin(self, mod: str, name: str, depth: int = 0) -> tuple[str, str] | None:
        """mod.name 真正定义的地方；外部库的名字返回 None"""
        if mod not in self.modules or depth > 10:
            return None
        kind = self.binds[mod].get(name)
        if kind is None:
            return None
        if kind != "import":
            return mod, name
        imp = self.top_imports[mod].get(name)
        if imp and imp[0] == "name":
            return self.origin(imp[1], imp[2], depth + 1)
        return None


# ---------- 测试和跨模块引用 ----------

def _is_patch_object(func) -> bool:
    """mock.patch.object / patch.object"""
    return isinstance(func, ast.Attribute) and func.attr == "object" and (
        (isinstance(func.value, ast.Name) and func.value.id == "patch")
        or (isinstance(func.value, ast.Attribute) and func.value.attr == "patch"))


def _is_patch(func) -> bool:
    return (isinstance(func, ast.Name) and func.id == "patch") or (
        isinstance(func, ast.Attribute) and func.attr == "patch")


def test_refs(idx: Index | None = None) -> dict[tuple[str, str], set[str]]:
    """测试里对旧模块名字的使用：(旧模块, 名字) → {test_patch, test_assign, test_ref}。只收旧模块里确实绑定了的名字"""
    idx = idx or Index()
    out: dict[tuple[str, str], set[str]] = defaultdict(set)

    def add(mod: str | None, name: str, tag: str) -> None:
        if mod and name not in idx.modules and name in idx.binds.get(mod, {}):
            out[(mod, name)].add(tag)

    for path in sorted(TESTS_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = parse(path)
        aliases = _imports(_all_imports(tree), None, idx.modules)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_patch_object(node.func) and len(node.args) >= 2 \
                    and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                add(_module_of(node.args[0], aliases, idx.modules), node.args[1].value, "test_patch")
            elif isinstance(node, ast.Call) and _is_patch(node.func) and node.args \
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                parts = node.args[0].value.split(".")
                if len(parts) == 3 and parts[0] == PKG:
                    add(parts[1], parts[2], "test_patch")
            elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Attribute):
                        add(_module_of(t.value, aliases, idx.modules), t.attr, "test_assign")
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                add(_module_of(node.value, aliases, idx.modules), node.attr, "test_ref")
        for a in aliases.values():               # from dota_assistant.x import y 也算直接引用
            if a[0] == "name":
                add(a[1], a[2], "test_ref")
    return out


def cross_module_private(idx: Index | None = None) -> dict[tuple[str, str], set[str]]:
    """产品代码里跨模块引用的私有名：(定义它的旧模块, 名字) → {引用它的旧模块}"""
    idx = idx or Index()
    out: dict[tuple[str, str], set[str]] = defaultdict(set)
    for mod, tree in idx.trees.items():
        aliases = _imports(_all_imports(tree), mod, idx.modules)
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and (m := _module_of(node.value, aliases, idx.modules)):
                found.append((m, node.attr))
        found += [(a[1], a[2]) for a in aliases.values() if a[0] == "name"]
        for m, name in found:
            if m != mod and name.startswith("_") and not name.startswith("__") and name in idx.binds.get(m, {}):
                out[(m, name)].add(mod)
    return out


def export(idx: Index | None = None) -> dict[str, dict[str, list[str]]]:
    """旧模块 → {名字: [为什么进清单]}；Live 的方法写成 Live.方法名"""
    idx = idx or Index()
    out: dict[str, dict[str, set[str]]] = {m: defaultdict(set) for m in idx.modules}
    for m in idx.modules:
        for n in defined_names(idx.trees[m]):
            if not n.startswith("_"):
                out[m][n].add("public")
            if m in SPLIT or m == "live":
                out[m][n].add("split")
    for (m, n), tags in test_refs(idx).items():
        if idx.binds[m].get(n) == "import":
            if tags & {"test_patch", "test_assign"}:   # import 进来又被测试替换的（data.DATA_DIR）
                out[m][n] |= tags
        else:
            out[m][n] |= tags
    for (m, n), users in cross_module_private(idx).items():
        out[m][n].add("cross_module")
    for meth in live_methods():
        out["live"][f"Live.{meth}"].add("live_method")
    return {m: {n: sorted(t) for n, t in sorted(v.items())} for m, v in sorted(out.items())}


# ---------- 生成 moves.json ----------

def placement(mod: str, name: str, imported: bool = False) -> str:
    """(旧模块, 名字) → 新模块（相对 dota_assistant）"""
    if mod == "live":
        if name.startswith("Live."):
            meth = name.removeprefix("Live.")
            groups = [g for g, ms in LIVE_GROUPS.items() if meth in ms]
            if len(groups) != 1:
                raise SystemExit(f"Live.{meth} 要在 LIVE_GROUPS 里出现且只出现一次（现在 {groups}）")
            return f"{LIVE_PKG}.{groups[0]}"
        return f"{LIVE_PKG}.{LIVE_MODULE_LEVEL.get(name, 'consts')}"
    if imported:
        if (mod, name) not in IMPORTED_BASELINE or (mod, name) not in IMPORTED_PATCHED:
            raise SystemExit(
                f"{mod}.{name} 在旧模块里现在是 import 进来的，测试又 patch / 赋值它。基线里这样的只有 "
                f"data.DATA_DIR、webui.HTTPServer；别的名字变成 import 说明旧模块留了转发（PLAN 2.2 禁止，"
                f"会让 patch 看着生效、其实打空）：删掉旧模块里这行 import，调用方改成「新模块.名字」，"
                f"测试的 patch 目标改到新模块。不许把它加进 IMPORTED_PATCHED / IMPORTED_BASELINE。")
        return IMPORTED_PATCHED[(mod, name)]
    if mod in SPLIT:
        hits = [new for new, names in SPLIT[mod].items() if name in names]
        if len(hits) != 1:
            raise SystemExit(f"{mod}.{name} 要在 SPLIT[{mod!r}] 里出现且只出现一次（现在 {hits}）")
        return hits[0]
    if (mod, name) in WHOLE_EXCEPT:
        return WHOLE_EXCEPT[(mod, name)]
    if mod not in WHOLE:
        raise SystemExit(f"旧模块 {mod} 没有去处：加到 WHOLE 或 SPLIT")
    return WHOLE[mod]


def load_existing_moves() -> dict | None:
    """现有的 moves.json；没有就 None（只有 P0 第一次生成时）"""
    if not MOVES_FILE.is_file():
        return None
    return json.loads(MOVES_FILE.read_text(encoding="utf-8"))


def _defined_in_new(rel: str, name: str) -> bool:
    """新文件里是不是已经自己定义了这个名字（Live.x：任意类里有这个方法）。只用来判断「搬没搬」"""
    path = REPO / rel
    if not path.is_file():
        return False
    tree = parse(path)
    if name.startswith("Live."):
        meth = name.removeprefix("Live.")
        return any(meth in ms for ms in class_methods(tree).values())
    return name in defined_names(tree)


def build_moves(idx: Index | None = None, existing: dict | None = None) -> tuple[dict, dict[str, list[str]]]:
    """在 existing（现有 moves.json）上合并：已有条目一条不删、顺序不动，新名字追加到末尾。
    返回 (新的 moves, {"added": […], "replaced": […]})"""
    idx = idx or Index()
    if set(IMPORTED_PATCHED) != IMPORTED_BASELINE:
        raise SystemExit(f"IMPORTED_PATCHED 只能是基线白名单 {sorted(IMPORTED_BASELINE)}：别的名字变成 import 是留了转发")
    entries = [dict(e) for e in (existing or {}).get("entries", [])]
    pos = {(e["old"], e["name"]): i for i, e in enumerate(entries)}
    if len(pos) != len(entries):
        raise SystemExit("moves.json 里有重复条目（同一个旧模块 + 名字出现两次），先手工查清楚")

    def listed(mod: str, name: str) -> bool:
        return (f"{PKG}.{mod}", name) in pos

    # 已进表的 imported 只能是基线白名单
    bad_imp = [f"{e['old']}.{e['name']}" for e in entries
               if e.get("imported") and (e["old"].removeprefix(PKG + "."), e["name"]) not in IMPORTED_BASELINE]
    if bad_imp:
        raise SystemExit(f"moves.json 里这些条目标了 imported，但不在基线白名单里（等于把转发洗白）：{bad_imp}")
    # 原本自己定义、已进表的名字，现在在旧模块里变成 import 进来：留了转发
    forwards = [f"{e['old']}.{e['name']}" for e in entries
                if not e.get("imported") and not e["name"].startswith("Live.")
                and idx.binds.get(e["old"].removeprefix(PKG + "."), {}).get(e["name"]) == "import"]
    if forwards:
        raise SystemExit("这些名字原本定义在旧模块里，现在旧模块改成 import 进来了（留了转发，PLAN 2.2 禁止，"
                         "会让 mock.patch 打空）：删掉旧模块里的这行 import，调用方改成「新模块.名字」。\n"
                         + "\n".join(forwards))
    # 拆分规则：重复报错；写了但源码里没有的名字，表里已经有（已搬走）不算错，表里也没有才是写错
    for mod, groups in SPLIT.items():
        names_ = [n for ns in groups.values() for n in ns]
        dup = [n for n, c in Counter(names_).items() if c > 1]
        extra = set()
        if mod in idx.modules:
            extra = {n for n in set(names_) - defined_names(idx.trees[mod]) if not listed(mod, n)}
        if dup or extra:
            raise SystemExit(f"SPLIT[{mod!r}] 重复 {dup}，源码和 moves.json 里都没有 {sorted(extra)}（写错了？）")
    live_listed = [m for ms in LIVE_GROUPS.values() for m in ms]
    meths = live_methods()
    extra = {m for m in set(live_listed) - meths if not listed("live", f"Live.{m}")}
    if extra or len(live_listed) != len(set(live_listed)) or meths - set(live_listed):
        raise SystemExit(f"LIVE_GROUPS 和 Live 的方法对不上：源码和表里都没有 {sorted(extra)}，"
                         f"没分组 {sorted(meths - set(live_listed))}，"
                         f"重复 {[m for m, c in Counter(live_listed).items() if c > 1]}")

    report: dict[str, list[str]] = {"added": [], "replaced": []}
    clash = []
    for mod, ns in export(idx).items():
        for name, tags in ns.items():
            key = (f"{PKG}.{mod}", name)
            imported = idx.binds[mod].get(name) == "import"
            new = placement(mod, name, imported)
            if key in pos:
                e = entries[pos[key]]
                if e["new"] == f"{PKG}.{new}" or imported:
                    continue
                # 规则改了去处：还没搬的（新文件里没定义）就更新这一条；已经搬了的报错
                if _defined_in_new(e["new_file"], name):
                    clash.append(f"{mod}.{name}：已经搬到 {e['new_file']}，规则却说去 {new}")
                    continue
                report["replaced"].append(f"{mod}.{name}：{e['new']} → {PKG}.{new}")
                e["new"], e["new_file"] = f"{PKG}.{new}", new_file(new)
                continue
            e = {"old": f"{PKG}.{mod}", "name": name, "new": f"{PKG}.{new}",
                 "old_file": f"{PKG}/{mod}.py", "new_file": new_file(new), "tags": tags}
            if imported:
                e["imported"] = True
            if (mod in SPLIT or mod == "live") and name in LOGGER_NAMES and idx.binds[mod].get(name) == "assign":
                e["per_module"] = True           # 每个新文件各自 getLogger(__name__)，拆到一半时旧文件还留着自己的
            pos[key] = len(entries)
            entries.append(e)
            report["added"].append(f"{mod}.{name} → {PKG}.{new}")
    if clash:
        raise SystemExit("规则和已经搬好的位置对不上（要么把规则改回去，要么真把名字挪过去再手工改 moves.json）：\n"
                         + "\n".join(clash))
    notes = {**(existing or {}).get("notes", {}), **NOTES}
    return ({"version": 1, "generated_by": "tools/refactor/export_names.py --write-moves", "notes": notes,
             "entries": entries}, report)


# ---------- 分层分析 ----------

def layer_of(new_mod: str) -> int:
    return LAYERS[new_mod.split(".")[0]]


def _local_names(fn) -> set[str]:
    """函数里的局部名字（参数 + 赋值过的），用来排除和模块级同名的引用（例如参数 log 和模块级 log）"""
    out = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.arg):
            out.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            out.add(node.id)
    return out


def _units(mod: str, tree: ast.Module):
    """(单元名, 节点)：顶层每个定义一个单元；live 的 Live 按方法拆成 Live.方法名"""
    for node in _top_statements(tree.body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, node
        elif isinstance(node, ast.ClassDef):
            if mod == "live" and node.name == "Live":
                # 类本身只看基类 / 装饰器，方法各算一个单元
                yield "Live", ast.Tuple(elts=[*node.bases, *node.decorator_list], ctx=ast.Load())
                for m in node.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        yield f"Live.{m.name}", m
            else:
                yield node.name, node
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                for n in _target_names(t):
                    yield n, node


def unit_refs(idx: Index) -> dict[tuple[str, str], set[tuple[str, str]]]:
    """(旧模块, 单元) → {(定义在的旧模块, 名字)}：单元里用到的本模块其它名字、别的旧模块的名字（含函数内延迟 import）"""
    out: dict[tuple[str, str], set[tuple[str, str]]] = {}
    live_meths = live_methods()
    for mod, tree in idx.trees.items():
        own = defined_names(tree)
        for unit, node in _units(mod, tree):
            aliases = {**idx.top_imports[mod], **_imports(_all_imports(node), mod, idx.modules)}
            local = _local_names(node) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else set()
            refs: set[tuple[str, str]] = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load) and sub.id not in local:
                    if sub.id in own and sub.id != unit:
                        refs.add((mod, sub.id))
                    elif (a := aliases.get(sub.id)) and a[0] == "name" and (o := idx.origin(a[1], a[2])):
                        refs.add(o)
                elif isinstance(sub, ast.Attribute):
                    if (m := _module_of(sub.value, aliases, idx.modules)) and (o := idx.origin(m, sub.attr)):
                        refs.add(o)
                    elif mod == "live" and isinstance(sub.value, ast.Name) and sub.value.id == "self" \
                            and sub.attr in live_meths and unit.startswith("Live."):
                        refs.add(("live", f"Live.{sub.attr}"))
            refs.discard((mod, unit))
            # 模块级 logger（log / logger）每个新文件各自定义，不算依赖
            out[(mod, unit)] = {r for r in refs if not (r[1] in LOGGER_NAMES and idx.binds[r[0]].get(r[1]) == "assign")}
    return out


def _place_any(mod: str, name: str) -> str:
    return placement(mod, name, False) if not (mod == "live" and name == "Live") else f"{LIVE_PKG}.core"


def layering(idx: Index | None = None) -> dict[str, dict[str, list[str]]]:
    """新模块 → {它要 import 的新模块: [「单元 → 用到的名字」…]}"""
    idx = idx or Index()
    out: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for (mod, unit), refs in unit_refs(idx).items():
        src = _place_any(mod, unit)
        for rmod, rname in refs:
            dst = _place_any(rmod, rname)
            if dst != src:
                out[src][dst].add(f"{mod}.{unit} → {rmod}.{rname}")
    return {s: {d: sorted(v) for d, v in sorted(ds.items())} for s, ds in sorted(out.items())}


def layering_md(idx: Index | None = None) -> str:
    idx = idx or Index()
    deps = layering(idx)
    lines = ["| 新模块 | 层 | import 的新模块（按当前代码，含函数内延迟 import） |", "|---|---|---|"]
    ups = []
    for src, ds in deps.items():
        cells = []
        for dst, why in ds.items():
            mark = ""
            if layer_of(dst) > layer_of(src):
                mark = " **↑往上**"
                ups.append((src, dst, why))
            elif layer_of(dst) == layer_of(src) and dst.split(".")[0] != src.split(".")[0]:
                mark = "（平级）"
            cells.append(f"`{dst.removeprefix(PKG + '.')}`{mark}")
        lines.append(f"| `{src.removeprefix(PKG + '.')}` | {src.split('.')[0]} | {'、'.join(cells)} |")
    lines += ["", "### 往上层的 import（逐条）", ""]
    if not ups:
        lines.append("（无）")
    for src, dst, why in ups:
        lines.append(f"- `{src.removeprefix(PKG + '.')}` → `{dst.removeprefix(PKG + '.')}`：")
        lines += [f"  - `{w}`" for w in why]
    return "\n".join(lines)


def live_group_stats(idx: Index | None = None) -> str:
    """live.py 每个方法：放在哪组、被哪些组调用、调用哪些组；主要调用方不在本组（也不是 core）的标出来"""
    idx = idx or Index()
    refs = unit_refs(idx)
    group = {f"Live.{m}": g for g, ms in LIVE_GROUPS.items() for m in ms}
    callers: dict[str, Counter] = defaultdict(Counter)
    callees: dict[str, Counter] = defaultdict(Counter)
    real_callers: dict[str, Counter] = defaultdict(Counter)   # 去掉分发入口以后的调用方
    for (mod, unit), rs in refs.items():
        if mod != "live" or unit not in group:
            continue
        for rmod, rname in rs:
            if rmod == "live" and rname in group:
                callers[rname][group[unit]] += 1
                callees[unit][group[rname]] += 1
                if unit.removeprefix("Live.") not in LIVE_DISPATCHERS:
                    real_callers[rname][group[unit]] += 1
    lines = ["| 分组 | 方法数 |", "|---|---|"]
    for g, ms in LIVE_GROUPS.items():
        lines.append(f"| {g} | {len(ms)} |")
    lines.append(f"| consts（模块级常量 / 函数） | "
                 f"{sum(1 for n in defined_names(idx.trees['live']) if n not in LIVE_MODULE_LEVEL)} |")
    lines += ["", "| 方法 | 放在 | 被谁调（组: 次数） | 调谁（组: 次数） | 备注 |", "|---|---|---|---|---|"]
    for g, ms in LIVE_GROUPS.items():
        for m in ms:
            u = f"Live.{m}"
            by = callers[u]
            note = ""
            real = real_callers[u]
            outside = {k: v for k, v in real.items() if k != g}
            if g != "core" and outside and max(outside.values()) > real.get(g, 0):
                note = "主要调用方在别组（不算分发入口）"
            elif g != "core" and len(real) >= 3:
                note = "多组共用"
            elif m in LIVE_DISPATCHERS:
                note = "分发入口"
            fmt = lambda c: "、".join(f"{k}:{v}" for k, v in c.most_common()) or "-"
            lines.append(f"| `{m}` | {g} | {fmt(by)} | {fmt(callees[u])} | {note} |")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--write-moves", action="store_true",
                   help="在现有 docs/refactor/moves.json 上合并（只追加，已有条目不删）")
    p.add_argument("--layering", action="store_true", help="打印分层冲突分析（Markdown）")
    p.add_argument("--live-groups", action="store_true", help="打印 live.py 方法分组统计（Markdown）")
    args = p.parse_args(argv)
    idx = Index()
    if args.write_moves:
        existing = load_existing_moves()
        before = len(existing["entries"]) if existing else 0
        moves, report = build_moves(idx, existing)
        MOVES_FILE.write_text(json.dumps(moves, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
        print(f"写好 {MOVES_FILE.relative_to(REPO).as_posix()}：原有 {before} 条，新增 {len(report['added'])} 条，"
              f"改去处 {len(report['replaced'])} 条，共 {len(moves['entries'])} 条")
        for line in report["added"]:
            print(f"  新增 {line}")
        for line in report["replaced"]:
            print(f"  改去处 {line}")
    elif args.layering:
        print(layering_md(idx))
    elif args.live_groups:
        print(live_group_stats(idx))
    else:
        print(json.dumps(export(idx), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
