"""分层检查（重构计划 PLAN 2.2，P2 起一直开着）：用 ast 扫 dota_assistant 下每个 .py 的 import，不许往上层引用。

层从低到高：core → sources → kb → ai → domain → vision / game（平级）→ research → app → cli。
只能 import 同层或更低层；__main__ 只能 import cli；包根 dota_assistant/__init__.py 算最底层。

扫哪些 import（全用 ast，不 import 产品代码）：
  - 文件里任何位置的 import，含函数 / 方法里的延迟 import；
  - `import dota_assistant.x.y`、`from dota_assistant.x import y`、`from .x import y`、`from . import x`、`from .. import x`
    （相对 import 按文件所在的包解析；`from 包 import 名字` 里的名字是子模块时，目标算那个子模块）。

每个模块属于哪一层：
  - 已经搬进子包的：路径第一段（core / sources / kb / ai / domain / vision / game / research / app / cli）。
  - **还平铺在包根目录的旧模块**（重构中途）：按 docs/refactor/moves.json 里它**还没搬走的名字**的去处算，
    一个旧模块的名字可能去好几层（ultcd：kb/cd_ops + domain/ultcd，lanestate：domain/lanelog + research），所以：
      * 作为 import 的一方：取这些去处里**最高**的层（它里面最高层的那部分能 import 的，都先放过）；
      * 作为被 import 的一方：能看出用了哪些名字时逐个名字按去处查
        （`from .ultcd import Tracker`，或 `from . import ultcd` 以后文件里写的 `ultcd.Tracker`），
        看不出来时取**最低**的层。
    这样平铺模块之间、平铺模块和子包之间都查，只抓「搬完以后肯定是往上 import」的；拿不准的（一个旧模块拆到几层、
    又看不出用了哪个名字）先放过，等它真搬进子包就按真实的层查了。
  - 包根目录下 moves.json 里没有的新平铺模块直接报错（新代码要放进对应的子包）。

例外写在 ALLOWED，每条带理由；新增例外要在重构报告里说明。现在应该为空。
"""
from __future__ import annotations

import ast
import importlib.util
import json
import tempfile
import textwrap
import unittest
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = "dota_assistant"
MOVES_FILE = REPO / "docs" / "refactor" / "moves.json"
EXPORT_SCRIPT = REPO / "tools" / "refactor" / "export_names.py"

# 和 tools/refactor/export_names.LAYERS 一致（下面有测试核对）；vision / game 平级
LAYERS = {"core": 0, "sources": 1, "kb": 2, "ai": 3, "domain": 4, "vision": 5, "game": 5, "research": 6, "app": 7,
          "cli": 8}
MAIN = "__main__"
LOGGER_NAMES = ("log", "logger")

# (import 的模块, 被 import 的模块) → 理由。模块名写完整的（dota_assistant.x.y）
ALLOWED: dict[tuple[str, str], str] = {}


@dataclass
class Edge:
    src: str                  # import 的模块
    dst: str                  # 被 import 的模块
    names: set[str] = field(default_factory=set)   # 能看出来用了 dst 的哪些名字（看不出来就空）
    line: int = 0


# ---------- 读源码 ----------

def module_files(pkg_dir: Path, pkg: str) -> dict[str, Path]:
    """模块名 → 文件；包的 __init__.py 记成包名"""
    out = {}
    for path in sorted(pkg_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        parts = list(path.relative_to(pkg_dir).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        out[".".join([pkg, *parts])] = path
    return out


def _attr_uses(tree: ast.AST, alias: str) -> set[str]:
    """文件里 alias.名字 用到的名字"""
    return {n.attr for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == alias}


def edges_of(mod: str, path: Path, modules: dict[str, Path], pkg: str) -> list[Edge]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    is_pkg = path.name == "__init__.py"
    here = mod if is_pkg else mod.rpartition(".")[0]      # 相对 import 的起点
    out: list[Edge] = []

    def add(dst: str, names: set[str], line: int) -> None:
        if dst == pkg or dst.startswith(pkg + "."):
            out.append(Edge(mod, dst, names, line))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:                               # import dota_assistant.x as y：y.名字
                    add(a.name, _attr_uses(tree, a.asname), node.lineno)
                else:                                      # import dota_assistant.x：只能看出模块
                    add(a.name, set(), node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = here.split(".")
                if node.level > 1:
                    base = base[:-(node.level - 1)]
                base_mod = ".".join(base + ([node.module] if node.module else []))
            else:
                base_mod = node.module or ""
            for a in node.names:
                sub = f"{base_mod}.{a.name}"
                if sub in modules:                         # from 包 import 子模块：看文件里怎么用它
                    add(sub, _attr_uses(tree, a.asname or a.name), node.lineno)
                elif a.name == "*":
                    add(base_mod, set(), node.lineno)
                else:
                    add(base_mod, {a.name}, node.lineno)
    return out


# ---------- 模块 → 层 ----------

@dataclass
class Legacy:
    """还平铺在包根目录的旧模块：名字 → 去处的层（只算还没搬走的名字）"""
    names: dict[str, int]

    @property
    def high(self) -> int:
        return max(self.names.values())

    @property
    def low(self) -> int:
        return min(self.names.values())


def _defined(tree: ast.Module) -> tuple[set[str], set[str]]:
    """顶层自己定义的名字（def / class / 赋值，进 if / try），和 Live 类的方法"""
    names, live = set(), set()

    def walk(body):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(n.name)
            elif isinstance(n, ast.ClassDef):
                names.add(n.name)
                if n.name == "Live":
                    live.update(m.name for m in n.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)))
            elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                    for e in (t.elts if isinstance(t, (ast.Tuple, ast.List)) else [t]):
                        if isinstance(e, ast.Name):
                            names.add(e.id)
            elif isinstance(n, ast.If):
                walk(n.body)
                walk(n.orelse)
            elif isinstance(n, ast.Try):
                for part in (n.body, n.orelse, n.finalbody, *[h.body for h in n.handlers]):
                    walk(part)
    walk(tree.body)
    return names, live


def legacy_layers(entries: list[dict], modules: dict[str, Path], pkg: str) -> dict[str, Legacy]:
    """包根目录下还在的旧模块 → Legacy（moves.json 里没有的旧模块不在结果里）"""
    by_old: dict[str, list[dict]] = {}
    for e in entries:
        by_old.setdefault(e["old"], []).append(e)
    out = {}
    for mod, path in modules.items():
        if mod.count(".") != 1 or mod == f"{pkg}.{MAIN}" or mod.split(".")[1] in LAYERS and path.name == "__init__.py":
            continue
        if mod not in by_old:
            continue
        names, live = _defined(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        layer = {}
        for e in by_old[mod]:
            if e.get("imported") or e.get("per_module") or e["name"] in LOGGER_NAMES:
                continue
            n = e["name"]
            still = n.removeprefix("Live.") in live if n.startswith("Live.") else n in names
            if still:
                layer[n] = LAYERS[e["new"].split(".")[1]]
        if not layer:                                      # 名字都搬走了文件还在：按全部去处算
            layer = {e["name"]: LAYERS[e["new"].split(".")[1]] for e in by_old[mod]}
        out[mod] = Legacy(layer)
    return out


def layer_of(mod: str, pkg: str, legacy: dict[str, Legacy], modules: dict[str, Path]) -> int | str | Legacy | None:
    """层号；__main__ 返回 MAIN；还平铺的旧模块返回 Legacy；认不出来返回 None"""
    if mod == pkg:
        return 0
    parts = mod.split(".")
    if parts[1] == MAIN:
        return MAIN
    if mod in legacy:
        return legacy[mod]
    if parts[1] in LAYERS:                                 # 子包里的模块（或模块名就是层名的平铺 cli.py）
        return LAYERS[parts[1]]
    return None


def layer_name(n: int) -> str:
    return " / ".join(k for k, v in LAYERS.items() if v == n)


def problems(pkg_dir: Path, pkg: str, entries: list[dict], allowed: dict | None = None) -> tuple[list[str], set]:
    """(问题列表, 用到的例外)"""
    allowed = ALLOWED if allowed is None else allowed
    modules = module_files(pkg_dir, pkg)
    legacy = legacy_layers(entries, modules, pkg)
    names_layer = {}                                       # (旧模块, 名字) → 去处的层（作为被 import 方逐个名字查）
    for e in entries:
        names_layer[(e["old"], e["name"])] = LAYERS[e["new"].split(".")[1]]
    bad, used = [], set()
    for mod, path in modules.items():
        src = layer_of(mod, pkg, legacy, modules)
        if src is None:
            bad.append(f"{path.relative_to(pkg_dir.parent).as_posix()}：包根目录下的新模块，不在 moves.json 里（放进对应层的子包）")
            continue
        for e in edges_of(mod, path, modules, pkg):
            if e.dst == mod:
                continue
            where = f"{path.relative_to(pkg_dir.parent).as_posix()}:{e.line} {mod} → {e.dst}"
            if src == MAIN:
                if e.dst != f"{pkg}.cli":
                    bad.append(f"{where}：__main__ 只能 import cli")
                continue
            dst = layer_of(e.dst, pkg, legacy, modules)
            if dst is None:
                continue                                    # 包根目录下的新模块在它自己那一行报
            if dst == MAIN:
                bad.append(f"{where}：不许 import __main__")
                continue
            src_n = src.high if isinstance(src, Legacy) else src
            if isinstance(dst, Legacy):
                known = [n for n in e.names if (e.dst, n) in names_layer and n in dst.names]
                dst_n = max(names_layer[(e.dst, n)] for n in known) if known else dst.low
                what = f"（用到 {', '.join(sorted(known))}）" if known else ""
            else:
                dst_n, what = dst, ""
            if dst_n > src_n:
                if (mod, e.dst) in allowed:
                    used.add((mod, e.dst))
                    continue
                bad.append(f"{where}{what}：{layer_name(src_n)} 层往上 import {layer_name(dst_n)} 层")
    return sorted(set(bad)), used


def load_entries() -> list[dict]:
    return json.loads(MOVES_FILE.read_text(encoding="utf-8"))["entries"]


class LayersTest(unittest.TestCase):
    def test_no_upward_imports(self):
        bad, _ = problems(REPO / PKG, PKG, load_entries())
        self.assertFalse(bad, f"{len(bad)} 处往上层 import（PLAN 2.2）：\n" + "\n".join(bad))

    def test_allowed_still_needed(self):
        """ALLOWED 里每一条都还用得上（代码改好了就删掉），而且写了理由"""
        _, used = problems(REPO / PKG, PKG, load_entries())
        stale = sorted(f"{a} → {b}" for a, b in ALLOWED if (a, b) not in used)
        self.assertFalse(stale, f"这些例外已经用不上了：{stale}")
        self.assertTrue(all(r.strip() for r in ALLOWED.values()), "每条例外都要写理由")

    def test_layers_match_export_script(self):
        spec = importlib.util.spec_from_file_location("export_names", EXPORT_SCRIPT)
        en = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(en)
        self.assertEqual(LAYERS, en.LAYERS)

    def test_scan_sees_subpackages(self):
        """扫描本身没坏：子包里的模块认得出层，延迟 import 也扫到了"""
        modules = module_files(REPO / PKG, PKG)
        legacy = legacy_layers(load_entries(), modules, PKG)
        self.assertEqual(layer_of(f"{PKG}.sources.http", PKG, legacy, modules), LAYERS["sources"])
        self.assertEqual(layer_of(f"{PKG}.kb.cd_ops", PKG, legacy, modules), LAYERS["kb"])
        self.assertEqual(layer_of(f"{PKG}.__main__", PKG, legacy, modules), MAIN)
        edges = edges_of(f"{PKG}.kb", modules[f"{PKG}.kb"], modules, PKG)
        self.assertIn(f"{PKG}.kb.cd_ops", {e.dst for e in edges})        # kb._update_cd_ops 里的延迟 import
        # P3 搬完的层：每层挑一个模块核对认得出层
        for mod, layer in (("ai.llm", "ai"), ("domain.lanelog", "domain"), ("vision.recognize", "vision"),
                           ("game.gsi", "game"), ("research.lanestate", "research")):
            self.assertEqual(layer_of(f"{PKG}.{mod}", PKG, legacy, modules), LAYERS[layer], mod)
        edges = edges_of(f"{PKG}.domain.coach", modules[f"{PKG}.domain.coach"], modules, PKG)
        self.assertIn(f"{PKG}.domain.itembuild", {e.dst for e in edges})  # coach 函数里对 itembuild 的延迟 import

    def test_lower_layers_moved(self):
        """P3 起：app 以下各层的模块都已经在子包里，包根目录下还平铺的旧模块只能去 app / cli（P4 再搬）"""
        modules = module_files(REPO / PKG, PKG)
        legacy = legacy_layers(load_entries(), modules, PKG)
        low = sorted(m for m, lg in legacy.items() if lg.low < LAYERS["app"])
        self.assertFalse(low, f"这些平铺模块还有名字要去 app 以下的层：{low}")

    def test_all_layers_covered(self):
        """P4 起：包根目录只剩 __init__ / __main__，没有平铺旧模块；每个模块都认得出层，九层（含 app、cli）都有模块；
        app 的 mixin import、cli 按命令的延迟 import、__main__ → cli 都扫到了"""
        pkg_dir = REPO / PKG
        self.assertEqual(sorted(p.name for p in pkg_dir.glob("*.py")), ["__init__.py", "__main__.py"],
                         "包根目录下还有平铺的模块")
        modules = module_files(pkg_dir, PKG)
        legacy = legacy_layers(load_entries(), modules, PKG)
        self.assertEqual(legacy, {}, "还有按 moves.json 算层的平铺旧模块")
        seen, unknown = set(), []
        for mod in modules:
            lay = layer_of(mod, PKG, legacy, modules)
            if mod == PKG or lay == MAIN:
                continue
            if not isinstance(lay, int):
                unknown.append(mod)
                continue
            seen.add(mod.split(".")[1])
        self.assertEqual(unknown, [], "认不出层的模块")
        self.assertEqual(seen, set(LAYERS), "有的层一个模块都没有")
        for mod, layer in (("app.live.core", "app"), ("app.live.draft", "app"), ("app.live.consts", "app"),
                           ("app.webui", "app"), ("app.start", "app"), ("app.demo", "app"), ("app.update", "app"),
                           ("cli", "cli")):
            self.assertEqual(layer_of(f"{PKG}.{mod}", PKG, legacy, modules), LAYERS[layer], mod)

        def dst(mod):
            return {e.dst for e in edges_of(mod, modules[mod], modules, PKG)}
        self.assertIn(f"{PKG}.app.live.draft", dst(f"{PKG}.app.live.core"))           # class Live 的 mixin 基类
        self.assertIn(f"{PKG}.app.live.consts", dst(f"{PKG}.app.live.draft"))         # mixin 里 consts.NAME
        self.assertIn(f"{PKG}.app.start", dst(f"{PKG}.cli"))                          # cli 按命令的延迟 import
        self.assertIn(f"{PKG}.research.backtest", dst(f"{PKG}.cli"))
        self.assertEqual(dst(f"{PKG}.{MAIN}"), {f"{PKG}.cli"})


class SelfCheckTest(unittest.TestCase):
    """变异自检：在临时目录造一个小包，故意写错的必须报出来，写对的不能误报"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "lfake"
        files = {
            "__init__.py": "",
            "__main__.py": "from .cli import main\n",
            "cli.py": "from .app import upd\n",
            "core/__init__.py": "",
            "core/config.py": "X = 1\n",
            "sources/__init__.py": "",
            "sources/http.py": "from ..core import config\n",
            "kb/__init__.py": "def f():\n    from . import ops\n",
            "kb/ops.py": "from .. import kb\n",
            "app/__init__.py": "",
            "app/upd.py": "from ..sources import http\nfrom .. import old\n",
            "old.py": "LOW = 1\nHIGH = 2\n",          # 平铺旧模块：LOW 去 core，HIGH 去 app
            "mid.py": "Y = 1\n",                       # 平铺旧模块：去 domain
        }
        for rel, text in files.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        self.entries = [
            {"old": "lfake.old", "name": "LOW", "new": "lfake.core.low"},
            {"old": "lfake.old", "name": "HIGH", "new": "lfake.app.high"},
            {"old": "lfake.mid", "name": "Y", "new": "lfake.domain.mid"},
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def bad(self, rel: str | None = None, text: str = "", allowed=None) -> list[str]:
        if rel:
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(textwrap.dedent(text), encoding="utf-8")
        return problems(self.root, "lfake", self.entries, allowed or {})[0]

    def test_good_tree_passes(self):
        self.assertEqual(self.bad(), [])

    def test_upward_caught(self):
        self.assertEqual(len(self.bad("core/config.py", "from ..sources import http\n")), 1)
        self.assertEqual(len(self.bad("core/config.py", "def f():\n    from lfake.app import upd\n")), 1)   # 延迟 import
        self.assertEqual(len(self.bad("core/config.py", "import lfake.kb.ops\n")), 1)
        self.assertEqual(len(self.bad("core/config.py", "from .. import app\n")), 1)                          # 子包

    def test_app_to_cli_caught(self):
        self.assertEqual(len(self.bad("app/upd.py", "def f():\n    from .. import cli\n")), 1)               # app 往上到 cli

    def test_main_only_cli(self):
        self.assertEqual(len(self.bad("__main__.py", "from .cli import main\nfrom .core import config\n")), 1)

    def test_legacy_by_names(self):
        # 只用旧模块里去 core 的名字：放过；用了去 app 的名字：报
        self.assertEqual(self.bad("sources/http.py", "from ..old import LOW\n"), [])
        self.assertEqual(len(self.bad("sources/http.py", "from ..old import HIGH\n")), 1)
        self.assertEqual(len(self.bad("sources/http.py", "from .. import old\nold.HIGH\n")), 1)
        self.assertEqual(self.bad("sources/http.py", "from .. import old\n"), [])            # 看不出名字：按最低层
        self.assertEqual(len(self.bad("sources/http.py", "from .. import mid\n")), 1)         # 平铺的 domain 模块

    def test_legacy_importer(self):
        self.assertEqual(len(self.bad("mid.py", "Y = 1\nfrom .app import upd\n")), 1)        # 平铺模块往上
        self.assertEqual(self.bad("mid.py", "Y = 1\nfrom .sources import http\n"), [])
        self.assertEqual(self.bad("old.py", "LOW = 1\nHIGH = 2\nfrom .app import upd\n"), [])   # 有名字去 app：按最高层放过

    def test_new_flat_module_caught(self):
        self.assertEqual(len(self.bad("brand_new.py", "")), 1)

    def test_allowed(self):
        self.assertEqual(self.bad("core/config.py", "from ..sources import http\n",
                                  {("lfake.core.config", "lfake.sources.http"): "测试"}), [])


if __name__ == "__main__":
    unittest.main()
