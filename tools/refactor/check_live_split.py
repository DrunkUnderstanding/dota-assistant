"""P4 拆 live.py 的核对：拆到 app/live/ 以后，Live 每个方法的源码和拆之前的 live.py 一字不差。

唯一允许的差别（PLAN P4 / 重构任务说明）：
  - 方法体里原来直接写的模块级常量 / 小函数名（搬进 consts.py 的那些）变成 consts.NAME；
  - 方法里的延迟 import 多两个点（from .vision.capture → from ...vision.capture，包深了两层）。
consts.py 里的常量 / 小函数区和原来 live.py 的那一段也要一字不差（中间只少了 log = … 那一行，它留在 core）。

用法：python tools/refactor/check_live_split.py [拆之前的提交，默认 adef2d9]
打印每个文件放了多少方法、哪些方法里限定了哪些 consts 名字；有不一致的退出码 1。
"""
from __future__ import annotations

import ast
import inspect
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BASE = "adef2d9"          # P4.1 提交：live.py 还在包根目录、没拆


def original_source(rev: str) -> str:
    out = subprocess.run(["git", "show", f"{rev}:dota_assistant/live.py"], cwd=REPO, capture_output=True, check=True)
    return out.stdout.decode("utf-8").replace("\r\n", "\n")


def main() -> int:
    rev = sys.argv[1] if len(sys.argv) > 1 else BASE
    src = original_source(rev)
    lines = src.splitlines(keepends=True)
    tree = ast.parse(src)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Live")
    log_node = next(n for n in tree.body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "log")
    imports_end = max(n.end_lineno for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)))
    first = next(n for n in tree.body if n.lineno > imports_end)
    consts_names = set()
    for n in tree.body:
        if first.lineno <= n.lineno < cls.lineno and n is not log_node:
            if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
                consts_names.add(n.name)
            elif isinstance(n, ast.Assign):
                consts_names.update(t.id for t in n.targets)
    region = "".join(lines[first.lineno - 1:log_node.lineno - 1] + lines[log_node.end_lineno:cls.lineno - 1])

    sys.path.insert(0, str(REPO))
    from dota_assistant.app.live import Live, consts

    bad = []
    consts_src = Path(consts.__file__).read_text(encoding="utf-8").replace("\r\n", "\n")
    if region.rstrip("\n") + "\n" not in consts_src:
        bad.append("consts.py 里的常量 / 小函数区和原 live.py 那一段不一致")
    missing = sorted(consts_names - set(vars(consts)))
    if missing:
        bad.append(f"consts.py 里少了：{missing}")

    qualify = re.compile(r"\bconsts\.(" + "|".join(sorted(consts_names, key=len, reverse=True)) + r")\b")
    deferred = re.compile(r"^(\s*from )\.\.(\.+)", re.M)
    per_file = defaultdict(list)
    qualified = {}
    for m in cls.body:
        if not isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        start = m.decorator_list[0].lineno if m.decorator_list else m.lineno
        old = "".join(lines[start - 1:m.end_lineno])
        raw = inspect.getattr_static(Live, m.name, None)
        fn = raw.fget if isinstance(raw, property) else raw
        if fn is None:
            bad.append(f"Live.{m.name}：拆完以后找不到")
            continue
        new = inspect.getsource(fn).replace("\r\n", "\n")
        hits = sorted(set(qualify.findall(new)))
        back = deferred.sub(r"\1\2", qualify.sub(r"\1", new))
        if back != old:
            bad.append(f"Live.{m.name}：方法源码除了 consts. 限定和延迟 import 的层数以外还有改动")
        owner = next(c for c in Live.__mro__ if m.name in vars(c))
        per_file[inspect.getmodule(owner).__name__.rsplit(".", 1)[1]].append(m.name)
        if hits:
            qualified[m.name] = hits

    for f, ms in sorted(per_file.items(), key=lambda kv: -len(kv[1])):
        print(f"{f}.py：{len(ms)} 个方法")
    print(f"共 {sum(map(len, per_file.values()))} 个方法；consts.py：{len(consts_names)} 个模块级名字")
    print("限定成 consts.NAME 的方法：")
    for name, hits in qualified.items():
        print(f"  {name}：{'、'.join(hits)}")
    if bad:
        print("\n不一致：\n" + "\n".join(bad))
        return 1
    print("\n方法源码一致（只差 consts. 限定和延迟 import 的层数）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
