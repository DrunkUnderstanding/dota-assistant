"""搬家清单核对（重构计划 P0.7）：docs/refactor/moves.json 里每个（旧模块, 名字, 新模块）要么已经搬好、要么还在原处。

全程用 ast 读源码，不 import 产品代码。

- 新文件存在且顶层自己定义了这个名字（def / class / 赋值，import 不算）→ 算搬好。这时旧文件要是还在，
  旧文件里**不能再绑定**这个名字：自己定义、import 进来、赋值转发都算（PLAN 2.2：不留转发，转发会让 patch 打空）。
  凡是已经有名字搬走的旧文件，出现 `import *` 或模块级 `__getattr__` 直接算失败。
- 否则旧文件必须还在、而且自己定义着这个名字（还没搬）。
- "Live.方法名"：搬好 = 新文件里 Live 本身或 app/live/core.py 里 class Live 的基类（mixin）定义了这个方法
  （core.py 还没有时，只认 Live 和以 Mixin 结尾的类）；旧文件看 live.py 的 Live 类。
- 不许多抄一份：同一个旧模块拆出去的各个文件（含 app/live/*.py）里，只有 new_file 能定义这个名字
  （别的旧模块的同名名字落在那个文件的不算）；测试会 patch / 赋值的名字，兄弟文件连 import 都不许绑定。
  app/live/ 下除了 consts.py，任何文件都不许绑定 consts 的名字（PLAN P4：mixin 一律 consts.NAME）。
- imported=true：旧模块里本来就是 import 进来的、测试 patch 它的名字，只许基线的 data.DATA_DIR、webui.HTTPServer 两条。
- per_module=true：logger 这类每个新文件各自定义一份的，只许 log / logger。
- 表只追加不删：前 BASELINE_COUNT 条的（旧模块, 名字）摘要固定，删条目、挪顺序都会失败。
  重构以后功能上删掉的名字不删条目，加 "removed": "日期 + 理由"，不再核对它在不在。
- 完整性：tools/refactor/export_names.py 现在从还存在的旧模块里导出的每个名字都要在清单里。
- ALL_MOVED：P4 结束、旧模块全删以后改成 True，打开「旧模块文件都不存在」的断言。
"""
import ast
import hashlib
import importlib.util
import json
import unittest
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
MOVES_FILE = ROOT / "docs" / "refactor" / "moves.json"
EXPORT_SCRIPT = ROOT / "tools" / "refactor" / "export_names.py"
LIVE_DIR = "dota_assistant/app/live"
LIVE_CONSTS = "dota_assistant.app.live.consts"
ALL_MOVED = True            # P4 结束：旧模块全删了

# 基线：只能在后面追加，不许删、不许挪顺序。b1113e8 生成 770 条；合并 main 的 d1bfef6 时换成它的基线：
# 删掉它去掉的 Live.query_mate / Live._mate_ready 两条，末尾追加 mates.py 新加的 23 个名字，共 791 条
BASELINE_COUNT = 791
BASELINE_DIGEST = "187585bd2c11ba27f9230f42c4f9d2f78f759ff02ec7c1a42313fc059bc2749b"
# 旧模块里本来就是 import 进来、测试又 patch 的名字（基线白名单，写死，不跟着脚本走）
IMPORTED_BASELINE = {("dota_assistant.data", "DATA_DIR"), ("dota_assistant.webui", "HTTPServer")}
PATCHED_TAGS = {"test_patch", "test_assign"}


def load_export_names():
    """按文件路径加载导出脚本（tools/ 不是包）"""
    spec = importlib.util.spec_from_file_location("export_names", EXPORT_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


EN = load_export_names()


def load_moves() -> dict:
    return json.loads(MOVES_FILE.read_text(encoding="utf-8"))


class Src(NamedTuple):
    binds: dict           # 顶层绑定 {名字: def / class / assign / import}
    classes: dict         # {类名: 方法集合}
    star: bool            # 有 from X import *
    module_getattr: bool  # 有模块级 def __getattr__
    tree: ast.Module


@lru_cache(maxsize=None)
def source_info(rel: str) -> Src | None:
    """文件不存在返回 None"""
    path = ROOT / rel
    if not path.is_file():
        return None
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    binds = EN.bindings(tree)
    star = any(isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names) for n in ast.walk(tree))
    return Src(binds, EN.class_methods(tree), star, binds.get("__getattr__") == "def", tree)


@lru_cache(maxsize=None)
def live_hosts() -> frozenset | None:
    """能放 Live 方法的类：Live + app/live/core.py 里 class Live 的基类（解析 from .x import Y as Z 的别名）。
    core.py 或 class Live 还没有时返回 None"""
    info = source_info(f"{LIVE_DIR}/core.py")
    if info is None:
        return None
    alias = {}
    for n in ast.walk(info.tree):
        if isinstance(n, ast.ImportFrom):
            for a in n.names:
                alias[a.asname or a.name] = a.name
    for node in info.tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Live":
            bases = set()
            for b in node.bases:
                if isinstance(b, ast.Name):
                    bases.add(alias.get(b.id, b.id))
                elif isinstance(b, ast.Attribute):
                    bases.add(b.attr)
            return frozenset({"Live", *bases})
    return None


def is_live_host(cls: str) -> bool:
    hosts = live_hosts()
    return cls in hosts if hosts is not None else (cls == "Live" or cls.endswith("Mixin"))


def has_name(rel: str, name: str, *, allow_import: bool = False, old_live: bool = False) -> bool:
    """rel 这个文件里有没有 name。
    普通名字：顶层自己定义（allow_import=True 时 import 绑定也算）。
    Live.x：old_live=True 只看 Live 类（旧 live.py）；否则看 Live 或它的 mixin 基类"""
    info = source_info(rel)
    if info is None:
        return False
    if name.startswith("Live."):
        meth = name.removeprefix("Live.")
        if old_live:
            return meth in info.classes.get("Live", set())
        return any(meth in ms for cls, ms in info.classes.items() if is_live_host(cls))
    kind = info.binds.get(name)
    return kind is not None and (allow_import or kind != "import")


def module_to_file(module: str, package: bool) -> str:
    parts = module.split(".")
    return "/".join(parts) + ("/__init__.py" if package else ".py")


def live_files() -> list[str]:
    return sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / LIVE_DIR).glob("*.py"))


class MovesTableTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.moves = load_moves()
        cls.entries = cls.moves["entries"]

    def test_entries_well_formed(self):
        """字段齐、不重复、新模块落在计划的层里、文件路径和模块名对得上、新旧文件不同"""
        seen = set()
        bad = []
        for e in self.entries:
            key = (e["old"], e["name"])
            if key in seen:
                bad.append(f"重复：{key}")
            seen.add(key)
            if not (e["old"].startswith("dota_assistant.") and e["new"].startswith("dota_assistant.")):
                bad.append(f"模块名不对：{e}")
                continue
            if e["old_file"] != module_to_file(e["old"], False):
                bad.append(f"old_file 和 old 对不上：{e}")
            top = e["new"].split(".")[1]
            if top not in EN.LAYERS:
                bad.append(f"新模块不在计划的层里：{e['new']}")
            if e["new_file"] not in (module_to_file(e["new"], False), module_to_file(e["new"], True)):
                bad.append(f"new_file 和 new 对不上：{e}")
            if e["new_file"] == e["old_file"]:
                bad.append(f"新旧文件相同，核对不了：{e}")
            if not e.get("tags"):
                bad.append(f"没写为什么进清单：{e}")
        self.assertFalse(bad, "\n".join(bad[:50]))
        self.assertGreater(len(self.entries), 500)

    def test_baseline_entries_kept(self):
        """表只追加不删：基线的 791 条一条不少、顺序不变（已经搬走的名字靠这些条目继续核对新模块里有它）"""
        self.assertGreaterEqual(len(self.entries), BASELINE_COUNT, "moves.json 条目比基线少：不许删条目")
        keys = "\n".join(f"{e['old']}:{e['name']}" for e in self.entries[:BASELINE_COUNT])
        self.assertEqual(hashlib.sha256(keys.encode("utf-8")).hexdigest(), BASELINE_DIGEST,
                         "moves.json 前 791 条的（旧模块, 名字）变了：只能在末尾追加，不许删、不许挪顺序、不许改名"
                         "（改去处只改 new / new_file）")

    def test_flags_whitelisted(self):
        """imported 只许基线两条（不能拿它把转发洗白）；per_module 只许 logger"""
        imported = {(e["old"], e["name"]) for e in self.entries if e.get("imported")}
        self.assertLessEqual(imported, IMPORTED_BASELINE, "imported 只许 data.DATA_DIR、webui.HTTPServer")
        self.assertEqual({(f"dota_assistant.{m}", n) for m, n in EN.IMPORTED_PATCHED}, IMPORTED_BASELINE,
                         "export_names.IMPORTED_PATCHED 只能是基线白名单")
        per_module = sorted(f"{e['old']}.{e['name']}" for e in self.entries
                            if e.get("per_module") and e["name"] not in EN.LOGGER_NAMES)
        self.assertFalse(per_module, f"per_module 只许 log / logger：{per_module}")

    def test_each_name_moved_or_still_in_place(self):
        bad = []
        moved_from = set()
        for e in self.entries:
            if e.get("removed"):                    # 重构以后功能上删掉的（条目留着，写了理由）
                continue
            name, imported = e["name"], bool(e.get("imported"))
            old_live = name.startswith("Live.")
            if has_name(e["new_file"], name, allow_import=imported):
                if imported or e.get("per_module"):
                    continue
                moved_from.add(e["old_file"])
                # 旧文件里任何形式的绑定都不许留（import 转发会让 mock.patch.object(旧模块, 名字) 打空）
                if has_name(e["old_file"], name, allow_import=True, old_live=old_live):
                    kind = "Live 方法" if old_live else source_info(e["old_file"]).binds.get(name)
                    bad.append(f"{e['old']}.{name} 已经在 {e['new']}，旧文件里还绑定着（{kind}；不许留转发 / 两份）")
            elif not has_name(e["old_file"], name, allow_import=imported, old_live=old_live):
                info = source_info(e["old_file"])
                if info is None:
                    where = "旧文件已删"
                elif info.binds.get(name) == "import":
                    where = "旧文件里是 import 进来的（转发），新文件里又没定义"
                else:
                    where = "旧文件里没有"
                bad.append(f"{e['old']}.{name}：{e['new_file']} 里没有（自己定义），{where}（漏搬、改了名或搬错文件）")
        for f in sorted(moved_from):
            info = source_info(f)
            if info and info.star:
                bad.append(f"{f}：已经有名字搬走了，旧文件里不许 import *（转发空壳）")
            if info and info.module_getattr:
                bad.append(f"{f}：已经有名字搬走了，旧文件里不许模块级 __getattr__（转发空壳）")
        self.assertFalse(bad, f"{len(bad)} 条不对：\n" + "\n".join(bad[:50]))

    def test_no_duplicates_in_sibling_files(self):
        """同一个旧模块拆出去的各个文件里，只有 new_file 能定义这个名字；patch 目标连 import 都不许；
        app/live/ 下除了 consts.py 不许绑定 consts 的名字"""
        siblings = defaultdict(set)      # 旧模块 → 它的所有去处文件
        allowed = defaultdict(set)       # 名字 → 合法定义它的文件（别的旧模块的同名名字落在那里也合法）
        for e in self.entries:
            siblings[e["old"]].add(e["new_file"])
            allowed[e["name"]].add(e["new_file"])
        siblings["dota_assistant.live"] |= set(live_files())
        bad = []
        for e in self.entries:
            name = e["name"]
            if e.get("imported") or e.get("per_module") or name in EN.LOGGER_NAMES:
                continue
            patched = bool(PATCHED_TAGS & set(e["tags"]))
            for f in sorted(siblings[e["old"]] - allowed[name]):
                if has_name(f, name):
                    bad.append(f"{e['old']}.{name}：去处是 {e['new_file']}，{f} 里又定义了一份")
                elif patched and has_name(f, name, allow_import=True):
                    bad.append(f"{e['old']}.{name}：测试会 patch 它，{f} 不许 import 绑定（要写「模块.名字」）")
        consts_names = {e["name"] for e in self.entries if e["new"] == LIVE_CONSTS}
        for f in live_files():
            if f.endswith("/consts.py"):
                continue
            info = source_info(f)
            hits = sorted(n for n in consts_names if n in info.binds)
            if hits:
                bad.append(f"{f} 绑定了 consts 的名字 {hits}（PLAN P4：用 consts.NAME，不许 from .consts import）")
            if info.star:
                bad.append(f"{f}：app/live 下不许 import *")
        self.assertFalse(bad, f"{len(bad)} 条不对：\n" + "\n".join(bad[:50]))

    def test_old_modules_gone_after_p4(self):
        """ALL_MOVED = True（P4 结束）以后：清单里的旧模块文件一个都不能剩"""
        if not ALL_MOVED:
            self.assertIs(ALL_MOVED, False)
            return
        left = sorted({e["old_file"] for e in self.entries if (ROOT / e["old_file"]).is_file()})
        self.assertFalse(left, f"旧模块还在：{left}")

    def test_every_exported_name_listed(self):
        """导出脚本从还存在的旧模块里导出的名字（公开名、测试 patch / 引用的、跨模块私有名、Live 方法）都在清单里"""
        listed = {(e["old"], e["name"]) for e in self.entries}
        missing = []
        for mod, names in EN.export().items():
            for name, tags in names.items():
                if (f"dota_assistant.{mod}", name) not in listed:
                    missing.append(f"dota_assistant.{mod}.{name}（{','.join(tags)}）")
        self.assertFalse(missing, "这些名字没进 docs/refactor/moves.json：在 tools/refactor/export_names.py 的规则里"
                                  "定好去处后跑 --write-moves（只在末尾追加，已有条目不删），或手工在末尾加条目：\n"
                         + "\n".join(missing[:50]))

    def test_split_modules_fully_assigned(self):
        """要拆的模块（data gsi gamefiles ultcd lanestate）和 live.py：还在的话顶层每个名字都有去处"""
        listed = {(e["old"], e["name"]) for e in self.entries}
        missing = []
        for mod in [*EN.SPLIT, "live"]:
            info = source_info(f"dota_assistant/{mod}.py")
            if info is None:
                continue
            names = {n for n, k in info.binds.items() if k != "import"}
            if mod == "live":
                names |= {f"Live.{m}" for m in info.classes.get("Live", set())}
            missing += [f"{mod}.{n}" for n in sorted(names) if (f"dota_assistant.{mod}", n) not in listed]
        self.assertFalse(missing, "\n".join(missing))

    def test_live_groups_are_planned_files(self):
        """Live 的方法只分到 P4 表里的 mixin 文件；模块级名字除了 Live / log 都在 consts"""
        allowed = {f"dota_assistant.app.live.{g}" for g in
                   ("core", "draft", "ingame", "timers", "cards", "overlay", "settings", "mates")}
        bad = []
        for e in self.entries:
            if e["old"] != "dota_assistant.live":
                continue
            if e["name"].startswith("Live."):
                if e["new"] not in allowed:
                    bad.append(f"{e['name']} → {e['new']}")
            elif e["name"] in ("Live", "log"):
                if e["new"] != "dota_assistant.app.live.core":
                    bad.append(f"{e['name']} → {e['new']}")
            elif e["new"] != LIVE_CONSTS:
                bad.append(f"{e['name']} → {e['new']}（模块级名字一律 consts）")
        self.assertFalse(bad, "\n".join(bad))


if __name__ == "__main__":
    unittest.main()
