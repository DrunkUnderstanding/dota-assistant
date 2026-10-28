"""patch 目标核对（重构计划 P1 起一直开着）：防止搬家以后 mock.patch 打到空处、测试看着过了其实没替换。

用 ast 扫 tests/ 下所有测试文件里的三种「替换模块里的东西」：
  1. mock.patch.object(目标, "名字")（也认 patch.object）；
  2. mock.patch("dota_assistant.x.y.名字")（字符串目标，只看 dota_assistant 开头的）；
  3. 直接赋值 模块变量.名字 = …（含 a, 模块.名字 = …、setattr(模块, "名字", …)）。
目标表达式按测试文件里的 import 语句解析成真实对象（文件里任何位置的 import 都算，含函数里的延迟 import；
解析不出来的，例如 self.live 这类实例，跳过）：
  - 目标是 dota_assistant 的模块：名字必须在这个模块**自己的源码顶层**定义（def / class / 赋值；import 进来的不算），
    是函数 / 类的话 __module__ 必须就是这个模块（`x = 别处的函数` 这种转发也算打空）；
  - 目标是类：名字必须在类自己的 __dict__ 里（不是从基类继承来的）。
例外 IMPORTED_OK：模块里 import 进来、测试又 patch 的名字（调用方在本模块全局里查它，patch 照样生效），每条写理由，
而且要求本模块确实有函数在用这个名字（用它的函数搬走了，patch 就打空了）。白名单里每一条都必须还被测试用到。
"""
from __future__ import annotations

import ast
import importlib
import inspect
import sys
import tempfile
import textwrap
import types
import unittest
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"
PKG = "dota_assistant"
MIN_TARGETS = 40            # 扫出来的（能解析的）目标至少这么多，少了说明扫描本身坏了

# (模块, 名字) → 理由。模块名跟着搬家改（只改模块路径，不许拿它把「搬走的名字留转发」洗白）
IMPORTED_OK = {
    ("dota_assistant.sources.stats", "DATA_DIR"):
        "test_nokey patch 后调 positions_source / load_positions，它们在 sources.stats 里按全局名 DATA_DIR 查（从 config import 进来）",
    ("dota_assistant.app.webui", "HTTPServer"):
        "test_webui patch 后起 WebServer，webui 里按全局名 HTTPServer 查（从 log import 进来）",
    ("dota_assistant.live", "recognize"):
        "test_webui 直接赋值换掉 live 里的 recognize（从 vision import 进来，Live.scan 按全局名查）；"
        "P4 拆 live 时要跟着改到真正调 recognize 的那个模块",
}


@dataclass
class Target:
    where: str            # 文件:行
    kind: str             # patch.object / patch / assign / setattr
    obj: object           # 解析出来的目标对象（模块或类）
    name: str


# ---------- 读测试源码 ----------

def _aliases(tree: ast.AST, pkg: str) -> dict[str, set[tuple[str, str | None]]]:
    """文件里所有 import（任何位置）：别名 → {(模块, 属性或 None)}。只收 pkg 开头的"""
    out: dict[str, set[tuple[str, str | None]]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == pkg or a.name.startswith(pkg + "."):
                    if a.asname:
                        out.setdefault(a.asname, set()).add((a.name, None))
                    else:
                        out.setdefault(a.name.split(".")[0], set()).add((a.name.split(".")[0], None))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == pkg or node.module.startswith(pkg + "."):
                for a in node.names:
                    if a.name != "*":
                        out.setdefault(a.asname or a.name, set()).add((node.module, a.name))
    return out


def _load(module: str, attr: str | None):
    mod = importlib.import_module(module)
    if attr is None:
        return mod
    try:
        return importlib.import_module(f"{module}.{attr}")        # from 包 import 子模块
    except ModuleNotFoundError:
        return getattr(mod, attr)


def _chain(expr: ast.AST) -> list[str] | None:
    """a.b.c → ['a', 'b', 'c']；不是纯名字链返回 None"""
    parts = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if not isinstance(expr, ast.Name):
        return None
    return [expr.id, *reversed(parts)]


def _resolve(expr: ast.AST, aliases: dict) -> list[object]:
    """目标表达式 → 可能的真实对象（同一个别名在不同函数里 import 了不同东西时可能有多个）"""
    chain = _chain(expr)
    if not chain or chain[0] not in aliases:
        return []
    out = []
    for module, attr in sorted(aliases[chain[0]], key=str):
        obj = _load(module, attr)
        for part in chain[1:]:
            obj = getattr(obj, part)
        out.append(obj)
    return out


def _resolve_string(target: str) -> tuple[object, str]:
    """'dota_assistant.x.y.名字' → (对象, 名字)：最长能 import 的模块前缀，再逐段 getattr"""
    path, name = target.rsplit(".", 1)
    parts = path.split(".")
    for i in range(len(parts), 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:i]))
        except ModuleNotFoundError:
            continue
        for p in parts[i:]:
            obj = getattr(obj, p)
        return obj, name
    raise ModuleNotFoundError(target)


def _is_patch_object(func: ast.AST) -> bool:
    """mock.patch.object / patch.object"""
    return isinstance(func, ast.Attribute) and func.attr == "object" and (
        (isinstance(func.value, ast.Attribute) and func.value.attr == "patch")
        or (isinstance(func.value, ast.Name) and func.value.id == "patch"))


def _is_patch(func: ast.AST) -> bool:
    """mock.patch / patch"""
    return (isinstance(func, ast.Attribute) and func.attr == "patch") or (isinstance(func, ast.Name) and func.id == "patch")


def _str(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def scan(source: str, where: str, pkg: str = PKG) -> list[Target]:
    tree = ast.parse(source, filename=where)
    aliases = _aliases(tree, pkg)
    out: list[Target] = []

    def add(kind: str, expr: ast.AST, name: str | None, line: int) -> None:
        if name is None:
            return
        for obj in _resolve(expr, aliases):
            out.append(Target(f"{where}:{line}", kind, obj, name))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if _is_patch_object(node.func) and len(node.args) >= 2:
                add("patch.object", node.args[0], _str(node.args[1]), node.lineno)
            elif _is_patch(node.func) and node.args and (s := _str(node.args[0])):
                if s.startswith(pkg + "."):
                    obj, name = _resolve_string(s)
                    out.append(Target(f"{where}:{node.lineno}", "patch", obj, name))
            # setattr(模块, "名字", …)，也认 addCleanup(setattr, 模块, "名字", …)
            args = node.args
            if isinstance(node.func, ast.Name) and node.func.id == "setattr" and len(args) >= 2:
                add("setattr", args[0], _str(args[1]), node.lineno)
            for i, a in enumerate(args[:-2]):
                if isinstance(a, ast.Name) and a.id == "setattr":
                    add("setattr", args[i + 1], _str(args[i + 2]), node.lineno)
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                for e in (t.elts if isinstance(t, (ast.Tuple, ast.List)) else [t]):
                    if isinstance(e, ast.Attribute):
                        for obj in _resolve(e.value, aliases):
                            if isinstance(obj, types.ModuleType):      # 只管模块属性；实例属性不算替换模块里的东西
                                out.append(Target(f"{where}:{node.lineno}", "assign", obj, e.attr))
    return out


# ---------- 读产品源码 ----------

def _top_statements(body):
    """顶层语句，进 if / try / with 里面（不进 def / class）"""
    for node in body:
        yield node
        if isinstance(node, ast.If):
            yield from _top_statements(node.body)
            yield from _top_statements(node.orelse)
        elif isinstance(node, ast.Try):
            for part in (node.body, node.orelse, node.finalbody, *[h.body for h in node.handlers]):
                yield from _top_statements(part)
        elif isinstance(node, ast.With):
            yield from _top_statements(node.body)


def _names(t: ast.AST) -> list[str]:
    if isinstance(t, ast.Name):
        return [t.id]
    if isinstance(t, (ast.Tuple, ast.List)):
        return [n for e in t.elts for n in _names(e)]
    return []


_SRC_CACHE: dict[str, tuple[dict[str, str], ast.Module]] = {}


def module_bindings(mod: types.ModuleType) -> tuple[dict[str, str], ast.Module]:
    """模块源码顶层绑定的名字 → def / class / assign / import"""
    path = mod.__file__
    if path not in _SRC_CACHE:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=path)
        out: dict[str, str] = {}
        for node in _top_statements(tree.body):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out[node.name] = "def"
            elif isinstance(node, ast.ClassDef):
                out[node.name] = "class"
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    for n in _names(t):
                        out.setdefault(n, "assign")
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                for n in _names(node.target):
                    out.setdefault(n, "assign")
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    if a.name != "*":
                        out.setdefault(a.asname or a.name.split(".")[0], "import")
        _SRC_CACHE[path] = (out, tree)
    return _SRC_CACHE[path]


def used_in_function(tree: ast.Module, name: str) -> bool:
    """模块里有没有函数 / 方法按全局名读这个名字"""
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for n in ast.walk(fn):
                if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load):
                    return True
    return False


def check(t: Target, pkg: str = PKG, imported_ok=None) -> str | None:
    """返回问题描述；没问题 / 不归这里管返回 None"""
    imported_ok = IMPORTED_OK if imported_ok is None else imported_ok
    label = f"{t.where} {t.kind}"
    if isinstance(t.obj, types.ModuleType):
        modname = t.obj.__name__
        if not (modname == pkg or modname.startswith(pkg + ".")):
            return None
        binds, tree = module_bindings(t.obj)
        kind = binds.get(t.name)
        if kind is None:
            return f"{label}：{modname} 的源码顶层没有 {t.name}（patch 打空：搬家后漏改目标模块？）"
        if kind == "import":
            if (modname, t.name) not in imported_ok:
                return (f"{label}：{modname}.{t.name} 是 import 进来的，不是这个模块自己定义的"
                        f"（patch 要打到定义它 / 按全局名用它的模块；确实该这样就加进 IMPORTED_OK 并写理由）")
            if not used_in_function(tree, t.name):
                return f"{label}：{modname}.{t.name} 在白名单里，但模块里已经没有函数用它了（用它的代码搬走了，patch 打空）"
            return None
        value = getattr(t.obj, t.name, None)
        if (inspect.isfunction(value) or inspect.isclass(value)) and value.__module__ != modname:
            return f"{label}：{modname}.{t.name} 的 __module__ 是 {value.__module__}（转发别处的定义，patch 打空）"
        return None
    if inspect.isclass(t.obj):
        if t.name not in vars(t.obj):
            return f"{label}：{t.obj.__module__}.{t.obj.__qualname__} 自己没有定义 {t.name}（继承来的或不存在）"
        return None
    return None


def all_targets() -> list[Target]:
    out = []
    for path in sorted(TESTS.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        out += scan(path.read_text(encoding="utf-8"), path.relative_to(REPO).as_posix())
    return out


class PatchTargetsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.targets = all_targets()

    def test_targets_defined_in_their_module(self):
        bad = sorted({p for t in self.targets if (p := check(t))})
        self.assertFalse(bad, f"{len(bad)} 处 patch / 赋值目标不对：\n" + "\n".join(bad))

    def test_scan_finds_targets(self):
        """扫描本身没坏：能解析出足够多的目标，三种写法都有"""
        mods = [t for t in self.targets if isinstance(t.obj, types.ModuleType) and t.obj.__name__.startswith(PKG)]
        self.assertGreaterEqual(len(mods), MIN_TARGETS, f"只扫到 {len(mods)} 个 dota_assistant 模块上的目标")
        kinds = {t.kind for t in mods}
        self.assertLessEqual({"patch.object", "assign"}, kinds)

    def test_whitelist_still_used(self):
        """IMPORTED_OK 里每一条都还有测试在 patch（搬家后模块名改了要跟着改，过时的删掉）"""
        used = {(t.obj.__name__, t.name) for t in self.targets if isinstance(t.obj, types.ModuleType)}
        stale = sorted(f"{m}.{n}" for m, n in IMPORTED_OK if (m, n) not in used)
        self.assertFalse(stale, f"IMPORTED_OK 里这些没有测试用到了：{stale}")


class SelfCheckTest(unittest.TestCase):
    """变异自检：在临时目录里造一个小包，故意写错的 patch 必须报出来，写对的不能误报"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        pkg = root / "ptfake"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "b.py").write_text(textwrap.dedent("""
            X = 1
            def thing():
                return X
            class K:
                def m(self):
                    return 1
            class S(K):
                pass
        """), encoding="utf-8")
        (pkg / "a.py").write_text(textwrap.dedent("""
            from ptfake.b import thing, X
            from ptfake import b
            alias = b.thing
            def use():
                return thing()
        """), encoding="utf-8")
        sys.path.insert(0, str(root))

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(cls.tmp.name)
        for m in [m for m in sys.modules if m == "ptfake" or m.startswith("ptfake.")]:
            del sys.modules[m]
        cls.tmp.cleanup()

    def problems(self, body: str, imported_ok=frozenset()) -> list[str]:
        src = "from unittest import mock\nfrom ptfake import a, b\nfrom ptfake.b import K, S\n" + textwrap.dedent(body)
        targets = scan(src, "fake_test.py", pkg="ptfake")
        self.assertTrue(targets, "一个目标都没扫到")
        return [p for t in targets if (p := check(t, pkg="ptfake", imported_ok=imported_ok))]

    def test_good_targets_pass(self):
        self.assertEqual(self.problems("""
            mock.patch.object(b, "thing")
            mock.patch.object(b, "X", 2)
            mock.patch("ptfake.b.thing")
            mock.patch.object(K, "m")
            mock.patch.object(a.b, "X")
            b.X = 3
            old, b.X = b.X, 4
            setattr(b, "X", 5)
            self.addCleanup(setattr, b, "X", 1)
        """), [])

    def test_imported_name_caught(self):
        self.assertEqual(len(self.problems('mock.patch.object(a, "thing")')), 1)
        self.assertEqual(len(self.problems('mock.patch("ptfake.a.thing")')), 1)
        self.assertEqual(len(self.problems('a.X = 2')), 1)
        self.assertEqual(len(self.problems('self.addCleanup(setattr, a, "thing", None)')), 1)

    def test_whitelist_requires_use(self):
        self.assertEqual(self.problems('mock.patch.object(a, "thing")', {("ptfake.a", "thing")}), [])
        self.assertEqual(len(self.problems('mock.patch.object(a, "X")', {("ptfake.a", "X")})), 1)   # a 里没有函数用 X

    def test_missing_and_forwarded_caught(self):
        self.assertEqual(len(self.problems('mock.patch.object(b, "nope")')), 1)
        self.assertEqual(len(self.problems('mock.patch.object(a, "alias")')), 1)      # alias = b.thing：__module__ 是 b
        self.assertEqual(len(self.problems('mock.patch.object(S, "m")')), 1)          # 继承来的


if __name__ == "__main__":
    unittest.main()
