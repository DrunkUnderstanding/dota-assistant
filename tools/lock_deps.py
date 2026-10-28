"""生成 requirements.lock：python tools/lock_deps.py

只锁 requirements.txt 里列的包和它们的传递依赖（按当前 Python 装的版本），不像 pip freeze 那样把无关的包也带进来。
依赖关系从已装包的元数据（importlib.metadata）递归求；环境标记（例如 `; sys_platform == "win32"`）按本机求值，
extras 依赖（`; extra == "..."`）不算。
"""
from __future__ import annotations

import importlib.metadata as md
import sys
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent
REQ = ROOT / "requirements.txt"
LOCK = ROOT / "requirements.lock"


def top_level() -> list[Requirement]:
    reqs = []
    for line in REQ.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            reqs.append(Requirement(line))
    return reqs


def resolve() -> dict[str, tuple[str, str]]:
    """{规范名: (元数据里的名字, 版本)}"""
    found: dict[str, tuple[str, str]] = {}
    todo = top_level()
    while todo:
        req = todo.pop()
        key = canonicalize_name(req.name)
        if key in found:
            continue
        try:
            dist = md.distribution(req.name)
        except md.PackageNotFoundError:
            sys.exit(f"没装 {req.name}：先 pip install -r requirements.txt")
        found[key] = (dist.metadata["Name"], dist.version)
        for spec in dist.requires or []:
            sub = Requirement(spec)
            if sub.marker is not None and not sub.marker.evaluate({"extra": ""}):
                continue
            todo.append(sub)
    return found


def main() -> None:
    found = resolve()
    lines = [
        "# 由 python tools/lock_deps.py 生成：requirements.txt 里的包及其传递依赖，当前能跑的版本",
        f"# Python {sys.version_info.major}.{sys.version_info.minor}，{sys.platform}",
    ]
    lines += [f"{name}=={ver}" for _, (name, ver) in sorted(found.items())]
    LOCK.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"写了 {LOCK.name}：{len(found)} 个包")


if __name__ == "__main__":
    main()
