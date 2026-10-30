"""G5 冒烟 + 接口清单（docs/refactor/PLAN.md 的 P0.5）。重构搬家时最容易「平时不报错、一用就坏」的几类问题在这里兜底：

1. 每个模块都能 import（pkgutil.walk_packages 递归，子包建好以后自动覆盖）；import 不起线程、不写运行目录；
2. 「运行到那一行才会炸」的引用静态核对（--help 走不到命令分支，单测和快照也覆盖不全，搬家后会假通过）：
   a. 函数里的延迟 import（`from .x import a` / `import dota_assistant.x`）目标模块和名字都存在；
   b. `模块别名.名字`（例如 cli 里的 `data.update`）这个名字在那个模块里存在；
   c. 每个函数用到的全局名字在它所在模块里有定义（方法挪进新文件、忘了带 import 的情况）；
   每一项都有变异自检：故意写错一处必须报出来；
3. 每个子命令 `python -m dota_assistant <命令> --help` 退出码 0（命令列表从 cli 的 argparse 动态取，新命令自动覆盖），
   顺带跑几组 suggest，并用 tests/tools/sentinel.py 证明它不写运行目录；
4. Live 的全部方法 / 属性（含私有方法）名字 + 签名和 tests/data/live_api.json 一字不差（P4 把 live.py 拆成 app/live/ 包以后也必须一样）；
   Live 各方法里用到的 self.xxx 都有着落（类上有，或者某个方法里赋过值）；模拟一次 mixin 拆分验证这些检查本身靠得住；
5. 网页 / Electron 发给后端的动作名都在 Live.action 的分发表里；分发表本身也和基线（d1bfef6）一致；
   前端每一处 act / run / send 调用都必须被归类（字面量 / 登记过的动态调用 / 函数定义），漏抽的会报出来；
   前端请求的每个 /api/xxx 接口 webui 都有，队友战绩页（mates.js）发的 op webui 都认。

更新 live_api.json（只在确实有意改了 Live 的接口时）：
    set UPDATE_LIVE_API=1 && python -m unittest tests.test_smoke
注意：仓库根 .gitignore 的 `data/` 也会匹配 tests/data/，提交时要 `git add -f tests/data/live_api.json`
（或者把 .gitignore 改成 `/data/`），否则干净检出后这个文件不在、test_live_api_unchanged 失败。
"""
from __future__ import annotations

import argparse
import ast
import builtins
import contextlib
import copy
import dis
import importlib
import importlib.util
import inspect
import io
import json
import os
import pkgutil
import re
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import dota_assistant
from tests.tools import sentinel

REPO = Path(__file__).resolve().parent.parent
LIVE_API = REPO / "tests" / "data" / "live_api.json"
PKG = "dota_assistant"
TIMEOUT = 120
# 除了包本身，还要核对这些脚本里对 dota_assistant 的 import（打包入口不在单测里跑）
EXTRA_SCRIPTS = ("packaging/*.py",)

# 基线 Live.action 的完整分发表。PLAN 第 1 节（b1113e8）列了 24 个；main 上 d1bfef6 把队友战绩改成独立页
# （web/mates.html 直接请求 webui 的 /api/mates、/api/match，不走 Live），去掉了 mate_query / mate_remove，剩 22 个
ACTIONS_BASE = frozenset(
    "set_slot set_bans set refresh clear rescan learn shot dismiss_coach show_brief explain_pick brief_page buyback ult "
    "name_slot ult_set toggle_overlay toggle_lock close_overlay overlay_moved save_settings open_logs"
    " upgrade_check upgrade_install"           # 2026-10-29 更新助手（app/upgrade.py）
    " skip_hero unskip_hero"                   # 2026-10-30 推荐卡片上点 × 这局不想玩 / 点名字恢复
    .split())
# 一定要有的子命令（列表本身是动态取的，这里只防「取到的是空的 / 取错了 parser」）
KEY_COMMANDS = {"suggest", "kb", "run", "start", "update", "demo", "recognize", "logs"}
SUGGEST_CASES = (    # (参数, 输出里必须有的片段)
    # 填了好友代码、有生涯数据时标题是「【推荐（你打过 N 局以上的）】」，没有时是「【推荐】」，所以只认前缀
    (["--enemy", "PA,莱恩,风行者", "--ally", "敌法师,冰女", "--pos", "3"], ["【推荐", "【针对这局", "算法 自身", "＋克制"]),
    (["--enemy", "末日,沉默", "--pos", "1", "--me", "卓尔", "--side", "radiant"], ["【你已选：卓尔游侠】"]),
    (["--pos", "1"], ["【推荐", "算法 自身"]),
    (["--pos", "5", "--ban", "小小,斧王"], ["【推荐"]),
)
# logs.setup 之后控制台 handler 是 brief 格式：崩溃时 stderr 只有这一句、没有 Traceback（见 log.py 的 excepthook）
CRASH_MARKS = ("Traceback", "程序出错退出")


# ---------- 公用 ----------

def load_live():
    """新路径优先（P4 后 Live 在 dota_assistant.app.live 包里），旧路径兜底。
    只有「新路径这个模块本身不存在」才退回旧路径；新包里面 import 出错要直接报出来，不能悄悄测旧的。"""
    for name in (f"{PKG}.app.live", f"{PKG}.live"):
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError as e:
            if e.name and (name == e.name or name.startswith(e.name + ".")):
                continue
            raise
        return mod.Live
    raise ImportError("找不到 Live（dota_assistant.app.live / dota_assistant.live 都没有）")


def all_modules() -> list[str]:
    """dota_assistant 下所有模块（递归子包）；__main__ 除外（import 它就是跑一遍命令行）"""
    def boom(name):
        raise ImportError(f"walk_packages 导入子包 {name} 失败")
    return sorted(m.name for m in pkgutil.walk_packages(dota_assistant.__path__, PKG + ".", onerror=boom)
                  if m.name.rsplit(".", 1)[-1] != "__main__")


class _Captured(Exception):
    pass


def cli_subcommands() -> list[str]:
    """从 cli.main 里建的 argparse 取子命令：拦住顶层 parser 的 parse_args，在真正解析前把 parser 拿出来。
    不写死命令列表，以后加命令自动覆盖；也不会执行到 config.load / logs.setup。"""
    from dota_assistant import cli                         # P4 后 cli 是包，dota_assistant.cli.main 路径不变
    got = {}

    def capture(self, *args, **kwargs):
        got.setdefault("parser", self)
        raise _Captured

    with mock.patch.object(argparse.ArgumentParser, "parse_args", capture), \
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
            contextlib.suppress(_Captured):
        cli.main(["--help"])
    subs = [a for a in got["parser"]._actions if isinstance(a, argparse._SubParsersAction)]
    return list(subs[0].choices) if subs else []


def run_cli(args: list[str], home: Path | None) -> subprocess.CompletedProcess:
    """子进程跑 python -m dota_assistant …；home 给了就把运行目录指到那里（G4：不碰真实目录），None = 仓库根"""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    env.pop("DOTA_ASSISTANT_HOME", None)
    if home is not None:
        env["DOTA_ASSISTANT_HOME"] = str(home)
    return subprocess.run([sys.executable, "-m", PKG, *args], cwd=REPO, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT)


def run_many(jobs: list[list[str]], home: Path | None) -> list[subprocess.CompletedProcess]:
    with ThreadPoolExecutor(8) as pool:
        return list(pool.map(lambda a: run_cli(a, home), jobs))


def _out(r: subprocess.CompletedProcess) -> str:
    return f"退出码 {r.returncode}\n--- stdout ---\n{r.stdout[-3000:]}\n--- stderr ---\n{r.stderr[-3000:]}"


def _listdir(p: Path) -> list[str]:
    return sorted(x.relative_to(p).as_posix() for x in p.rglob("*"))


# ---------- 1. import ----------

class ImportTest(unittest.TestCase):
    def test_every_module_imports(self):
        mods = all_modules()
        self.assertIn(f"{PKG}.cli", mods)
        self.assertGreaterEqual(len(mods), 30, mods)       # 现在 38 个（不含 __main__ 和包本身）；搬家只会多不会少
        for name in mods:
            with self.subTest(module=name):
                importlib.import_module(name)

    def test_import_has_no_side_effects(self):
        """新进程里把所有模块 import 一遍：不起线程、运行目录（临时）里什么都没写、仓库根的玩家数据也没动"""
        code = textwrap.dedent(f"""
            import importlib, json, pkgutil, threading
            import dota_assistant
            before = set(threading.enumerate())
            names = [m.name for m in pkgutil.walk_packages(dota_assistant.__path__, "{PKG}.")
                     if m.name.rsplit(".", 1)[-1] != "__main__"]
            for n in names:
                importlib.import_module(n)
            print(json.dumps({{"n": len(names), "threads": [t.name for t in threading.enumerate() if t not in before]}}))
        """)
        repo_before = sentinel.snapshot(REPO)
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "PYTHONIOENCODING": "utf-8", "DOTA_ASSISTANT_HOME": tmp}
            r = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT)
            self.assertEqual(r.returncode, 0, _out(r))
            res = json.loads(r.stdout.strip().splitlines()[-1])
            self.assertGreaterEqual(res["n"], 30)
            self.assertEqual(res["threads"], [], "import 时起了线程")
            self.assertEqual(_listdir(Path(tmp)), [], "import 时往运行目录里写了东西")
        bad, _ = sentinel.split_known(sentinel.diff(repo_before, sentinel.snapshot(REPO)))
        self.assertEqual(bad, [], "import 时动了仓库根的玩家数据")


# ---------- 2. 运行时才会炸的引用（静态核对） ----------

class Source:
    """一个要核对的源文件：模块名（包外脚本为 None）、是不是包的 __init__、语法树、每个节点的父节点"""

    def __init__(self, path: Path, modname: str | None, tree: ast.Module | None = None):
        self.path, self.modname = path, modname
        self.is_pkg = path.name == "__init__.py"
        self.tree = tree if tree is not None else ast.parse(path.read_text(encoding="utf-8"), str(path))
        self.parent = {c: p for p in ast.walk(self.tree) for c in ast.iter_child_nodes(p)}

    @property
    def where(self) -> str:
        try:
            return self.path.relative_to(REPO).as_posix()
        except ValueError:
            return str(self.path)

    @property
    def package(self) -> str | None:
        """相对 import 的基准包"""
        if self.modname is None:
            return None
        return self.modname if self.is_pkg else self.modname.rpartition(".")[0]

    def scope(self, node: ast.AST):
        """最近的外层函数（None = 模块顶层）"""
        p = self.parent.get(node)
        while p is not None and not isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            p = self.parent.get(p)
        return p


def pkg_sources() -> list[Source]:
    out = [Source(Path(importlib.import_module(n).__file__), n) for n in [PKG, *all_modules()]]
    out += [Source(p, None) for g in EXTRA_SCRIPTS for p in sorted(REPO.glob(g))]
    return out


def _submodule(modname: str, name: str) -> types.ModuleType | None:
    """modname.name 是子模块就返回它，否则 None（`from . import kb` 这种名字本身是子模块的情况）"""
    mod = importlib.import_module(modname)
    if hasattr(mod, "__path__") and importlib.util.find_spec(f"{modname}.{name}") is not None:
        return importlib.import_module(f"{modname}.{name}")
    v = getattr(mod, name, None)
    return v if isinstance(v, types.ModuleType) else None


def check_imports(src: Source) -> tuple[list[str], dict, int, int]:
    """核对文件里所有指向 dota_assistant 的 import（顶层的 + 函数里延迟的）。
    返回（问题列表, {作用域: {别名: 模块名}}, 核对的 import 条数, 其中函数里的条数）"""
    problems, aliases, n_all, n_func = [], {}, 0, 0
    for node in ast.walk(src.tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0 and (node.module or "").split(".")[0] != PKG:
                continue
            if node.level and src.package is None:
                continue                                    # 包外脚本的相对 import，不归这里管
            n_all += 1
            n_func += src.scope(node) is not None
            stmt = ast.unparse(node)
            try:
                target = (importlib.util.resolve_name("." * node.level + (node.module or ""), src.package)
                          if node.level else node.module)
                importlib.import_module(target)
            except ImportError as e:
                problems.append(f"{src.where}:{node.lineno} `{stmt}`：模块不存在（{e}）")
                continue
            for a in node.names:
                if a.name == "*":
                    continue
                sub = _submodule(target, a.name)
                if sub is None and not hasattr(importlib.import_module(target), a.name):
                    problems.append(f"{src.where}:{node.lineno} `{stmt}`：{target} 里没有 {a.name}")
                elif sub is not None and sub.__name__.split(".")[0] == PKG:
                    aliases.setdefault(src.scope(node), {})[a.asname or a.name] = sub.__name__
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] != PKG:
                    continue
                n_all += 1
                n_func += src.scope(node) is not None
                try:
                    importlib.import_module(a.name)
                except ImportError as e:
                    problems.append(f"{src.where}:{node.lineno} `import {a.name}`：模块不存在（{e}）")
                    continue
                if a.asname:
                    aliases.setdefault(src.scope(node), {})[a.asname] = a.name
    return problems, aliases, n_all, n_func


def _local_names(fn: ast.AST) -> set[str]:
    """函数里（含嵌套）除 import 以外绑定的名字：参数、赋值、for / with / except 的变量、嵌套 def / class。
    宁可多算（少核对几处），不误报"""
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n is not fn:
            out.add(n.name)
    return out


def check_module_attrs(src: Source, aliases: dict) -> tuple[list[str], list[ast.Attribute]]:
    """`别名.名字`：别名是 import 进来的 dota_assistant 模块时，这个名字在模块里必须存在。返回（问题列表, 核对过的节点）"""
    problems, checked, local_cache = [], [], {}
    for node in ast.walk(src.tree):
        if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
            continue
        name, s, target = node.value.id, src.scope(node), None
        while True:                                          # 由内往外找这个别名绑定的模块，被局部变量遮住就算了
            if name in aliases.get(s, {}):
                target = aliases[s][name]
                break
            if s is None:
                break
            if s not in local_cache:
                local_cache[s] = _local_names(s)
            if name in local_cache[s]:
                break
            s = src.scope(s)
        if target is None:
            continue
        checked.append(node)
        if not hasattr(importlib.import_module(target), node.attr) and _submodule(target, node.attr) is None:
            problems.append(f"{src.where}:{node.lineno} `{ast.unparse(node)}`：{target} 里没有 {node.attr}")
    return problems, checked


def _code_objects(co: types.CodeType):
    yield co
    for c in co.co_consts:
        if isinstance(c, types.CodeType):
            yield from _code_objects(c)


def global_loads(co: types.CodeType) -> set[str]:
    """函数（含嵌套）按全局名字读的名字"""
    return {i.argval for c in _code_objects(co) for i in dis.get_instructions(c) if i.opname == "LOAD_GLOBAL"}


def undefined_globals(module_code: types.CodeType, namespace: dict, where: str) -> list[str]:
    """模块里每个函数（含方法、嵌套函数、lambda）用到的全局名字，都要在模块命名空间 / builtins 里，
    或者被哪个函数用 global 赋过值。模块顶层代码不查（import 时就执行了，import 测试兜着）"""
    stored = {i.argval for co in _code_objects(module_code) for i in dis.get_instructions(co)
              if i.opname in ("STORE_GLOBAL", "DELETE_GLOBAL")}
    out = []
    for co in _code_objects(module_code):
        if co is module_code:
            continue
        for ins in dis.get_instructions(co):
            if (ins.opname == "LOAD_GLOBAL" and ins.argval not in namespace and ins.argval not in stored
                    and not hasattr(builtins, ins.argval)):
                out.append(f"{where}:{co.co_firstlineno} {co.co_qualname}() 用到的全局名字 {ins.argval} 没有定义")
    return out


class DeferredRefsTest(unittest.TestCase):
    """--help 在 argparse 里就退出，走不到命令分支里的 `from .xxx import`；这些引用路径写错了平时不报错，搬家后会假通过"""

    @classmethod
    def setUpClass(cls):
        cls.sources = pkg_sources()

    def test_package_imports_resolve(self):
        problems, n_all, n_func = [], 0, 0
        for src in self.sources:
            p, _, a, f = check_imports(src)
            problems += p
            n_all, n_func = n_all + a, n_func + f
        self.assertEqual(problems, [], "指向不存在的模块 / 名字的 import")
        self.assertGreaterEqual(n_func, 40, "函数里的延迟 import 一条都没找到？扫描本身坏了")   # P0 时 53 条
        self.assertGreater(n_all, n_func)

    def test_module_attrs_exist(self):
        problems, n = [], 0
        for src in self.sources:
            _, aliases, _, _ = check_imports(src)
            p, k = check_module_attrs(src, aliases)
            problems += p
            n += len(k)
        self.assertEqual(problems, [], "`模块.名字` 引用了模块里不存在的名字")
        self.assertGreaterEqual(n, 50, "`模块.名字` 一处都没核对到？扫描本身坏了")

    def test_global_names_defined(self):
        problems = []
        for src in self.sources:
            if src.modname is None:
                continue
            code = compile(src.path.read_text(encoding="utf-8"), str(src.path), "exec")
            problems += undefined_globals(code, vars(importlib.import_module(src.modname)), src.where)
        self.assertEqual(problems, [], "函数里用到、模块里没定义的全局名字")

    # ---- 变异自检：故意写错，必须报出来（防检查本身失效、搬家后假通过） ----

    def _first(self, pred):
        """包里第一个满足 pred(src, node) 的节点，返回 (src, 节点在 ast.walk 里的序号)"""
        for src in self.sources:
            if src.modname is None:
                continue
            for i, node in enumerate(ast.walk(src.tree)):
                if pred(src, node):
                    return src, i
        self.fail("找不到可以拿来变异的节点")

    @staticmethod
    def _mutated(src: Source, index: int, change) -> Source:
        tree = copy.deepcopy(src.tree)
        change(list(ast.walk(tree))[index])
        return Source(src.path, src.modname, tree)

    def test_mutation_bad_deferred_import_caught(self):
        def is_deferred(src, n):
            return isinstance(n, ast.ImportFrom) and n.level > 0 and src.scope(n) is not None

        src, i = self._first(is_deferred)
        self.assertEqual(check_imports(src)[0], [])

        def bad_module(n):                                  # from .start import main → from .start_moved import main
            if n.module:
                n.module += "_moved"
            else:
                n.names[0].name += "_moved"

        def bad_name(n):                                    # from .start import main → from .start import main_moved
            n.names[0].name += "_moved"

        for change in (bad_module, bad_name):
            with self.subTest(change=change.__name__):
                self.assertTrue(check_imports(self._mutated(src, i, change))[0], "改坏了延迟 import 却没报")

    def test_mutation_bad_module_attr_caught(self):
        checked = {}

        def is_checked(src, n):
            if src not in checked:
                checked[src] = check_module_attrs(src, check_imports(src)[1])[1]
            return n in checked[src]

        src, i = self._first(is_checked)
        bad = self._mutated(src, i, lambda n: setattr(n, "attr", n.attr + "_moved"))   # data.update → data.update_moved
        self.assertTrue(check_module_attrs(bad, check_imports(bad)[1])[0], "改坏了 模块.名字 却没报")

    def test_mutation_missing_global_caught(self):
        """模拟 P4：把 Live 的一个方法原样挪进一个新文件、没带它用到的模块级名字，必须报出来"""
        Live = load_live()
        ns = vars(sys.modules[Live.__module__])
        for name, fn in sorted(vars(Live).items()):
            if inspect.isfunction(fn) and any(n in ns and not hasattr(builtins, n) for n in global_loads(fn.__code__)):
                break
        else:
            self.fail("Live 里找不到用到模块级名字的方法")
        code = compile(textwrap.dedent(inspect.getsource(fn)), "<moved>", "exec")
        self.assertTrue(undefined_globals(code, {"__builtins__": builtins}, "<moved>"), f"{name} 挪走后缺名字却没报")
        self.assertEqual(undefined_globals(code, ns, "<moved>"), [], f"{name} 在原模块里应该不缺名字")


# ---------- 3. 子命令 ----------

class CliSmokeTest(unittest.TestCase):
    def test_subcommands_found(self):
        cmds = cli_subcommands()
        self.assertTrue(KEY_COMMANDS <= set(cmds), f"子命令列表不对：{cmds}")

    def test_every_subcommand_help(self):
        """--help 在 argparse 里就退出了，不读配置不写日志；还是把运行目录指到临时目录，并断言里面什么都没写。
        注意 --help 走不到命令分支里的延迟 import，那部分由 DeferredRefsTest 静态核对"""
        cmds = cli_subcommands()
        self.assertTrue(KEY_COMMANDS <= set(cmds), cmds)
        with tempfile.TemporaryDirectory() as tmp:
            results = run_many([["--help"]] + [[c, "--help"] for c in cmds], Path(tmp))
            top, rest = results[0], results[1:]
            self.assertEqual(top.returncode, 0, _out(top))
            for c in cmds:
                self.assertIn(c, top.stdout, "顶层 --help 里没列出这个命令")
            for c, r in zip(cmds, rest):
                with self.subTest(cmd=c):
                    self.assertEqual(r.returncode, 0, _out(r))
                    self.assertIn(f"usage: {PKG} {c}", r.stdout, _out(r))
                    for mark in CRASH_MARKS:
                        self.assertNotIn(mark, r.stderr, _out(r))
            self.assertEqual(_listdir(Path(tmp)), [], "--help 往运行目录里写了东西")

    def test_suggest(self):
        """在仓库根真跑（要 data/ 里的统计数据和 config.toml 的段位设置）；前后对比玩家数据，证明 suggest 只读不写。
        缺数据才 skip，而且只看仓库里的文件、不看产品输出：免得「ROOT 算错找不到 data」这种真故障被当成缺数据跳过"""
        data = REPO / "data"
        need = {"heroes.json": [data / "heroes.json"] if (data / "heroes.json").exists() else [],
                "stats_*.json": list(data.glob("stats_*.json")), "positions_*.json": list(data.glob("positions_*.json"))}
        missing = [k for k, v in need.items() if not v]
        if missing:
            self.skipTest(f"data/ 里没有 {'、'.join(missing)}（先 update）")
        before = sentinel.snapshot(REPO)
        results = run_many([["suggest", *a] for a, _ in SUGGEST_CASES] + [["suggest", "--enemy", "没有这个英雄"]], None)
        for (args, wants), r in zip(SUGGEST_CASES, results):
            with self.subTest(args=" ".join(args)):
                self.assertEqual(r.returncode, 0, _out(r))
                self.assertTrue(r.stdout.startswith("数据："), _out(r))
                for w in wants:
                    self.assertIn(w, r.stdout, _out(r))
                for mark in CRASH_MARKS:
                    self.assertNotIn(mark, r.stderr, _out(r))
        bad_name = results[-1]                           # 认不出的名字：提示一句、退出码 1，不是崩溃
        self.assertEqual(bad_name.returncode, 1, _out(bad_name))
        self.assertIn("没有这个英雄", bad_name.stdout, _out(bad_name))
        for mark in CRASH_MARKS:
            self.assertNotIn(mark, bad_name.stderr, _out(bad_name))
        bad, _ = sentinel.split_known(sentinel.diff(before, sentinel.snapshot(REPO)))
        self.assertEqual(bad, [], "suggest 改动了运行目录里的玩家数据")


# ---------- 4. Live 接口 ----------

_MOD_PATH = re.compile(rf"\b{PKG}(?:\.\w+)*\.")            # 'dota_assistant.config.Config' → 'Config'（搬家会变的部分）
_CLASS_REPR = re.compile(r"<class '(?:[\w]+\.)*(\w+)'>")    # 没开 from __future__ annotations 时注解是类对象
_FUNC_KINDS = (property, classmethod, staticmethod, types.FunctionType)


def _sig(fn) -> str:
    """签名字符串。live.py 开了 from __future__ import annotations，注解就是源码里写的字符串，搬家不变；
    万一哪个 mixin 文件没开、注解变成类对象，去掉模块路径，免得只因为搬家误报（参数名 / 默认值 / 注解名变了照样报）"""
    s = str(inspect.signature(fn))
    return _MOD_PATH.sub("", _CLASS_REPR.sub(r"\1", s))


def _own_classes(cls) -> list[type]:
    return [c for c in cls.__mro__ if c is not object]


def live_api(Live) -> dict[str, dict[str, str]]:
    """Live 上的全部名字（沿 MRO 找，mixin 里的也算），含私有方法：P4 拆 mixin 时漏挪 / 挪错一个私有方法，
    只有走到那条分支才会 AttributeError，单测和快照没走到的路径就漏了。
    dunder 只收函数类的（__init__ 等），__dict__ / __weakref__ / __module__ 这类类自带的不收"""
    out = {}
    names = {n for c in _own_classes(Live) for n in vars(c)}
    for name in sorted(names):
        raw = inspect.getattr_static(Live, name)
        if name.startswith("__") and name.endswith("__") and not isinstance(raw, _FUNC_KINDS):
            continue
        if isinstance(raw, property):
            out[name] = {"kind": "property", "signature": _sig(raw.fget)}
        elif isinstance(raw, classmethod):
            out[name] = {"kind": "classmethod", "signature": _sig(raw.__func__)}
        elif isinstance(raw, staticmethod):
            out[name] = {"kind": "staticmethod", "signature": _sig(raw.__func__)}
        elif inspect.isfunction(raw):
            out[name] = {"kind": "method", "signature": _sig(raw)}
        else:
            out[name] = {"kind": "attribute", "signature": type(raw).__name__}
    return out


def live_api_text(Live) -> str:
    return json.dumps(live_api(Live), sort_keys=True, indent=1, ensure_ascii=False) + "\n"


def _functions(cls):
    """类（沿 MRO）上所有函数对象：方法、property 的 getter / setter / deleter、classmethod / staticmethod 包着的函数"""
    for c in _own_classes(cls):
        for raw in vars(c).values():
            if isinstance(raw, property):
                yield from (f for f in (raw.fget, raw.fset, raw.fdel) if f is not None)
            elif isinstance(raw, (classmethod, staticmethod)):
                yield raw.__func__
            elif inspect.isfunction(raw):
                yield raw


def self_refs(cls) -> tuple[dict[str, set[str]], set[str]]:
    """（{self.名字: 在哪些方法里读}, 有着落的名字 = 类上的 ∪ 任何方法里 self.名字 = … 赋过值的）。
    用 inspect.getsource 取每个函数的源码，P4 拆到多个文件以后照样能取"""
    loads: dict[str, set[str]] = {}
    defined = set(dir(cls))
    for fn in _functions(cls):
        fdef = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
        args = fdef.args.posonlyargs + fdef.args.args
        if not args:
            continue                                          # staticmethod
        me = args[0].arg
        for n in ast.walk(fdef):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == me:
                if isinstance(n.ctx, (ast.Store, ast.Del)):
                    defined.add(n.attr)
                else:
                    loads.setdefault(n.attr, set()).add(fn.__qualname__)
    return loads, defined


def self_attr_problems(cls) -> list[str]:
    loads, defined = self_refs(cls)
    return sorted(f"self.{a}（{'、'.join(sorted(q))} 里用到）" for a, q in loads.items() if a not in defined)


def simulate_split(Live, drop: str | None = None) -> type:
    """模拟 P4 的 mixin 拆分：把 Live 的全部成员摊平，一半挪进 SplitMixin，另一半留在新的 Live 里（函数对象原样用）。
    drop 给了就「漏挪」这个名字"""
    skip = {"__dict__", "__weakref__", "__module__", "__qualname__", "__doc__", "__firstlineno__", "__static_attributes__"}
    flat = {}
    for c in reversed(_own_classes(Live)):
        flat.update({k: v for k, v in vars(c).items() if k not in skip})
    flat.pop(drop, None)
    moved = set(sorted(flat)[::2])
    mixin = type("SplitMixin", (), {k: v for k, v in flat.items() if k in moved})
    return type("Live", (mixin,), {k: v for k, v in flat.items() if k not in moved})


class LiveApiTest(unittest.TestCase):
    def test_live_api_unchanged(self):
        text = live_api_text(load_live())
        if os.environ.get("UPDATE_LIVE_API") == "1":
            LIVE_API.parent.mkdir(exist_ok=True)
            LIVE_API.write_text(text, encoding="utf-8", newline="\n")
        if not LIVE_API.exists():
            self.fail(f"{LIVE_API.relative_to(REPO).as_posix()} 不存在：多半是 .gitignore 的 `data/` 把它挡在 git 外面了，"
                      "提交时要 git add -f；本地可以 UPDATE_LIVE_API=1 在重构前的版本上重新生成")
        saved = LIVE_API.read_text(encoding="utf-8").replace("\r\n", "\n")   # git 可能把换行转成 CRLF
        if saved != text:
            old, new = json.loads(saved), json.loads(text)
            diff = [f"- {k}: {old[k]}" for k in sorted(old.keys() - new.keys())]
            diff += [f"+ {k}: {new[k]}" for k in sorted(new.keys() - old.keys())]
            diff += [f"* {k}: {old[k]} → {new[k]}" for k in sorted(old.keys() & new.keys()) if old[k] != new[k]]
            self.fail("Live 接口和 tests/data/live_api.json 不一致（有意改的话 UPDATE_LIVE_API=1 重生成）：\n"
                      + "\n".join(diff or ["（内容一样、格式不同）"]))

    def test_live_api_has_private_methods(self):
        api = live_api(load_live())
        self.assertIn("__init__", api)
        self.assertGreaterEqual(sum(k.startswith("_") and not k.startswith("__") for k in api), 40, "私有方法没收进清单")

    def test_self_attrs_defined(self):
        """Live 各方法里读的 self.xxx：要么类上有（方法 / property / 类属性，沿 MRO），要么某个方法里赋过值。
        拆 mixin 时漏挪的私有方法、挪的时候改错的名字，走不到那条分支也能查出来"""
        Live = load_live()
        loads, _ = self_refs(Live)
        self.assertGreaterEqual(len(loads), 100, "self.xxx 一个都没扫到？扫描本身坏了")    # P0 时 194 个
        self.assertEqual(self_attr_problems(Live), [], "Live 方法里用到、却没有着落的 self 属性")

    def test_split_simulation(self):
        """拆成 mixin 以后：清单、分发表、self 属性检查都不变；漏挪一个私有方法，清单和 self 属性检查都要报出来"""
        Live = load_live()
        split = simulate_split(Live)
        self.assertEqual(live_api(split), live_api(Live))
        self.assertEqual(action_table(split), action_table(Live))
        self.assertEqual(self_attr_problems(split), [])
        loads, _ = self_refs(Live)
        victim = next(k for k in sorted(loads) if k.startswith("_") and inspect.isfunction(
            inspect.getattr_static(Live, k, None)))                # 一个被别的方法用 self 调用的私有方法
        broken = simulate_split(Live, drop=victim)
        self.assertNotEqual(live_api(broken), live_api(Live))
        self.assertTrue(any(p.startswith(f"self.{victim}（") for p in self_attr_problems(broken)), victim)


# ---------- 5. 网页动作 ----------

FRONT_FILES = ("web/*.js", "web/*.html", "electron/*.js")
_NAME = r"([^'\"`\\]+)"                                       # 动作名：引号里的任何东西（含数字 / 大写 / 连字符），不认识的让 Live.action 那关报
# act('x') / run('x') / send('x')：web/api.js 的 act 发 POST /api/action {type: x}；panel.js 的 run、topbar.html 的 send 是它的包装
ANY_CALL = re.compile(r"(?<![\w.$])(?:act|run|send)\(")
LITERAL_CALL = re.compile(rf"(?<![\w.$])(?:act|run|send)\(\s*(['\"`]){_NAME}\1")
# 控制面板按钮 <button data-act="x">，panel.js 统一 run(b.dataset.act)
ANY_DATA_ACT = re.compile(r"\bdata-act\s*=")
DATA_ACT = re.compile(rf"\bdata-act=(['\"]){_NAME}\1")
# electron/main.js 直接 fetch('/api/action', { body: JSON.stringify({ type: 'x', … }) })
ANY_JSON_TYPE = re.compile(r"JSON\.stringify\(\s*\{\s*type\b")
JSON_TYPE = re.compile(rf"JSON\.stringify\(\s*\{{\s*type:\s*(['\"`]){_NAME}\1")
# JSON.stringify({ type, … }) 简写（type 是变量）：和动态调用一样要登记
DYNAMIC_JSON = re.compile(r"JSON\.stringify\(\s*\{\s*type\s*[,}]")
# 第一个参数不是字面量的调用（函数定义除外）：每一处都要在 DYNAMIC_CALLS 里说明发的是什么
DYNAMIC_CALL = re.compile(r"(?<![\w.$])(?:act|run|send)\(\s*[A-Za-z_$][\w.$]*\s*[,)]")
DYNAMIC_CALLS = {
    ("web/panel.js", "run(b.dataset.act)"): (),                 # 按钮的 data-act，DATA_ACT 已经收了
    ("web/panel.js", "act(type,"): (),                          # run() 转发给 act()
    ("web/topbar.html", "act(type,"): (),                       # send() 转发给 act()
    ("web/topbar.html", "send(kind,"): ("buyback", "ult"),      # 顶栏小条：kind 在 ['buyback', 'ult', 'ask'] 里循环，'ask' 走 toggleMenu 不发
    ("web/api.js", "JSON.stringify({type,"): (),                # act() 本体：把调用方给的 type 原样发出去
}
# 直接请求 /api/action 的地方（不经过 act()）：每个文件出现几次。新增的请求点要在这里登记，并确认上面的正则抽得到它发的动作
API_ACTION_SITES = {"web/api.js": 1, "electron/main.js": 1}
# set 的 key：act('set', { key: 'x' })、bindSeg('seg-…', 'x')（控制面板分段按钮）、setStyle('x', …)（叠加层底色 / 透明度）
SET_KEY = (re.compile(r"(?<![\w.$])(?:act|run|send)\(\s*'set',\s*\{\s*key:\s*'(\w+)'"),
           re.compile(r"\bbindSeg\(\s*'[^']*',\s*'(\w+)'"),
           re.compile(r"\bsetStyle\(\s*'(\w+)'"))


def front_files() -> list[Path]:
    return sorted(p for g in FRONT_FILES for p in REPO.glob(g))


def front_actions() -> tuple[dict[str, set[str]], set[tuple[str, str]], set[str], list[str], dict[str, int]]:
    """（{动作名: 出现在哪些文件}, 动态调用点 {(文件, 调用文本)}, set 的 key,
       没归上类的调用点（字面量 / 动态 / 函数定义都不是，正则漏抽的）, {文件: /api/action 出现次数}）"""
    acts: dict[str, set[str]] = {}
    dyn: set[tuple[str, str]] = set()
    keys: set[str] = set()
    unclassified: list[str] = []
    api_sites: dict[str, int] = {}
    for p in front_files():
        rel, text = p.relative_to(REPO).as_posix(), p.read_text(encoding="utf-8")

        def at(pos: int) -> str:
            return f"{rel}:{text.count(chr(10), 0, pos) + 1} {text[pos:pos + 60].splitlines()[0]}"

        for rx in (LITERAL_CALL, DATA_ACT, JSON_TYPE):
            for m in rx.finditer(text):
                acts.setdefault(m.group(2), set()).add(rel)
        for m in DYNAMIC_CALL.finditer(text):
            if re.search(r"\bfunction\s+$", text[:m.start()]):
                continue
            call = re.sub(r"\s+", "", m.group(0))
            dyn.add((rel, call))
            for name in DYNAMIC_CALLS.get((rel, call), ()):
                acts.setdefault(name, set()).add(rel)
        for m in DYNAMIC_JSON.finditer(text):
            dyn.add((rel, re.sub(r"\s+", "", m.group(0))))
        # 对账：每一处 act / run / send 调用、data-act、JSON.stringify({type…}) 都要被上面某个正则收下
        for m in ANY_CALL.finditer(text):
            if not (re.search(r"\bfunction\s+$", text[:m.start()])
                    or LITERAL_CALL.match(text, m.start()) or DYNAMIC_CALL.match(text, m.start())):
                unclassified.append(at(m.start()))
        for any_rx, rx in ((ANY_DATA_ACT, DATA_ACT), (ANY_JSON_TYPE, JSON_TYPE)):
            for m in any_rx.finditer(text):
                if not (rx.match(text, m.start()) or (any_rx is ANY_JSON_TYPE and DYNAMIC_JSON.match(text, m.start()))):
                    unclassified.append(at(m.start()))
        if (k := text.count("/api/action")):
            api_sites[rel] = k
        for rx in SET_KEY:
            keys.update(m.group(1) for m in rx.finditer(text))
    return acts, dyn, keys, unclassified, api_sites


def _field_vars(fn: ast.FunctionDef, field: str) -> set[str]:
    """函数里被赋值成 a.get("field") / a["field"] 的变量名（含 key, value = a.get("key"), a.get("value") 这种）"""
    def is_field(v) -> bool:
        if isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr == "get" and v.args:
            return isinstance(v.args[0], ast.Constant) and v.args[0].value == field
        return isinstance(v, ast.Subscript) and isinstance(v.slice, ast.Constant) and v.slice.value == field

    out = set()
    for n in ast.walk(fn):
        if not isinstance(n, ast.Assign):
            continue
        for tgt in n.targets:
            if isinstance(tgt, ast.Name) and is_field(n.value):
                out.add(tgt.id)
            elif isinstance(tgt, ast.Tuple) and isinstance(n.value, ast.Tuple):
                out.update(t.id for t, v in zip(tgt.elts, n.value.elts) if isinstance(t, ast.Name) and is_field(v))
    return out


def _compared(fn: ast.FunctionDef, names: set[str]) -> set[str]:
    """names 里的变量和哪些字符串比过：x == "a" / x in ("a", "b")"""
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


def action_table(Live) -> tuple[set[str], set[str]]:
    """Live.action 能处理的（动作名, set 的 key）。action 是 if / elif 链，最后 else 抛 ValueError；
    用 inspect.getsource(Live.action) 取源码，P4 拆到 app/live/core.py 以后照样能取"""
    fn = ast.parse(textwrap.dedent(inspect.getsource(Live.action))).body[0]
    return _compared(fn, _field_vars(fn, "type")), _compared(fn, _field_vars(fn, "key"))


class WebActionTest(unittest.TestCase):
    def setUp(self):
        self.Live = load_live()
        self.handled, self.set_keys = action_table(self.Live)
        self.acts, self.dyn, self.front_keys, self.unclassified, self.api_sites = front_actions()

    def test_action_table_unchanged(self):
        self.assertEqual(self.handled, ACTIONS_BASE, "Live.action 的分发表变了")

    def test_unknown_action_raises(self):
        """分发表以外的动作：抛 ValueError（webui 回 400 + 消息），不是悄悄忽略"""
        with self.assertRaisesRegex(ValueError, "不认识的操作"):
            self.Live.action(mock.MagicMock(), {"type": "没有这个操作"})

    def test_front_actions_handled(self):
        self.assertTrue(front_files())
        self.assertGreaterEqual(len(self.acts), 20, sorted(self.acts))     # 正则没抽到东西也会「全过」，先防这个
        missing = {a: sorted(f) for a, f in self.acts.items() if a not in self.handled}
        self.assertEqual(missing, {}, "前端发了、Live.action 不处理的动作")

    def test_every_call_site_classified(self):
        """前端每一处 act / run / send 调用、data-act、JSON.stringify({type…}) 都归上了类：
        字面量（名字抽走）、DYNAMIC_CALLS 登记过的动态调用、函数定义。写法变了正则抽不到的，在这里报，不会静默漏掉"""
        self.assertEqual(self.unclassified, [], "正则没收下的调用点（改正则或登记到 DYNAMIC_CALLS）")
        self.assertEqual(self.api_sites, API_ACTION_SITES, "直接请求 /api/action 的地方变了（确认新请求发的动作抽得到，再登记）")

    def test_dynamic_calls_documented(self):
        """第一个参数是变量的调用，正则抽不到名字：每一处都要在 DYNAMIC_CALLS 里写明；表里写了但代码里没了也要删"""
        self.assertEqual(self.dyn, set(DYNAMIC_CALLS), "动态调用点和 DYNAMIC_CALLS 对不上")

    def test_set_keys_handled(self):
        self.assertGreaterEqual(len(self.front_keys), 5, self.front_keys)
        self.assertEqual(self.front_keys - self.set_keys, set(), "前端 set 的 key，Live.action 不认")

    def test_plan_actions_reachable(self):
        """PLAN 第 1 节的每个动作都在 Live.action 里；前端不发的（只有热键 / 别处调用）单独列在这里，变了要更新"""
        self.assertEqual(ACTIONS_BASE - self.handled, set())
        self.assertEqual(ACTIONS_BASE - set(self.acts), {"toggle_lock"})   # F7 热键直接调 self.toggle_lock；叠加层锁按钮发 set overlay_locked


# ---------- 6. 前端直接请求的 /api 接口（不经过 Live.action 的：队友战绩页等）----------

FRONT_API = re.compile(r"/api/([a-z_]+)")                    # '/api/mates'、`/api/match/${mid}`、`${SERVER}/api/config`
# webui 里认的接口：path == "/api/x"、re.fullmatch(r"/api/x/(\d+)", path)
SERVED_API = re.compile(r"""['"]/api/([a-z_]+)(?=['"/])""")
# 队友战绩页 op({ op: 'query', … }) → webui 的 _mates_op 里 body.get("op") == "query"
MATES_OP = re.compile(r"(?<![\w.$])op\(\s*\{\s*op:\s*'(\w+)'")
HANDLED_OP = re.compile(r"""body\.get\(\s*["']op["']\s*\)\s*==\s*["'](\w+)["']""")


def load_webui_source() -> str:
    """新路径优先（P4 后在 dota_assistant.app.webui），旧路径兜底（同 load_live）"""
    for name in (f"{PKG}.app.webui", f"{PKG}.webui"):
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError as e:
            if e.name and (name == e.name or name.startswith(e.name + ".")):
                continue
            raise
        return inspect.getsource(mod)
    raise ImportError("找不到 webui（dota_assistant.app.webui / dota_assistant.webui 都没有）")


class FrontApiTest(unittest.TestCase):
    def setUp(self):
        self.src = load_webui_source()
        self.front: dict[str, set[str]] = {}
        self.ops: set[str] = set()
        for p in front_files():
            rel, text = p.relative_to(REPO).as_posix(), p.read_text(encoding="utf-8")
            for m in FRONT_API.finditer(text):
                self.front.setdefault(m.group(1), set()).add(rel)
            if rel == "web/mates.js":
                self.ops.update(MATES_OP.findall(text))

    def test_front_api_paths_served(self):
        """前端请求的每个 /api/xxx 在 webui 里都有分支（没有的会落到「没有这个接口」/ 静态文件 404）"""
        self.assertGreaterEqual(set(self.front), {"action", "events", "heroes", "config", "mates", "match"},
                                "正则没抽到前端的接口？")
        served = set(SERVED_API.findall(self.src))
        missing = {k: sorted(v) for k, v in self.front.items() if k not in served}
        self.assertEqual(missing, {}, "前端请求了、webui 没有的接口")
        # 变异自检：webui 里把 /api/mates 改个名，必须查得出来
        self.assertNotIn("mates", set(SERVED_API.findall(self.src.replace('"/api/mates"', '"/api/mates_moved"'))))

    def test_mates_ops_handled(self):
        """队友战绩页 POST /api/mates 的 op（查 / 删）webui 都认（不认的回 400「不认识的操作」）"""
        self.assertEqual(self.ops, {"query", "remove"}, "mates.js 发的 op 变了（新 op 要确认 webui 认，再改这里）")
        self.assertEqual(self.ops - set(HANDLED_OP.findall(self.src)), set(), "mates.js 发了、webui 不认的 op")

    def test_match_network_error_502(self):
        """GET /api/match 联网失败走 502 分支（test_mates / 回放都没走到）：
        webui 里的 `except http.NETWORK_ERRORS` 要真指向 sources.http，断网 / 数据源报错都回 502「联网失败」"""
        import urllib.error
        import urllib.request

        import requests

        from dota_assistant.app.webui import WebServer
        from dota_assistant.sources import http

        for err in (requests.ConnectionError("断网了"), http.APIError("数据源报错")):
            svc = types.SimpleNamespace(match=mock.Mock(side_effect=err), list=lambda: [])
            srv = WebServer(types.SimpleNamespace(cfg=None), mates_service=svc).start()
            try:
                with self.assertLogs("dota_assistant.app.webui", "WARNING"):
                    with self.assertRaises(urllib.error.HTTPError) as cm:
                        urllib.request.urlopen(srv.url + "/api/match/77?p=5", timeout=5)
                body = json.loads(cm.exception.read())
            finally:
                srv.stop()
            self.assertEqual((cm.exception.code, body), (502, {"ok": False, "message": f"联网失败：{err}"}))
            svc.match.assert_called_once_with(77, 5)


if __name__ == "__main__":
    unittest.main()
