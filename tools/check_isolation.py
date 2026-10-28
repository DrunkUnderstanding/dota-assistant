"""G4：跑单测前后对比运行目录里的玩家数据（data/ shots/ logs/ templates/ config.toml calibration.json），有变化就报出来。

用法：python tools/check_isolation.py [--root 目录 ...] [-- 传给 unittest 的参数]
--root 默认是本仓库根；可以多给几个（例如顺便盯着 D:\\dota）。退出码：单测失败 / 目录有变化都非 0。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests.tools import sentinel  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    argv = list(sys.argv[1:] if argv is None else argv)
    rest: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, rest = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", action="append", type=Path, help="要盯的运行目录（默认本仓库根）")
    args = ap.parse_args(argv)
    roots = [r.resolve() for r in (args.root or [REPO])]

    before = {r: sentinel.snapshot(r) for r in roots}
    rc = subprocess.run([sys.executable, "-m", "unittest", *rest], cwd=REPO).returncode
    changed = False
    for r in roots:
        bad, known = sentinel.split_known(sentinel.diff(before[r], sentinel.snapshot(r)))
        if known:
            print(f"[G4] {r} 已知现有问题（不算失败）：")
            print("\n".join(known))
        if bad:
            changed = True
            print(f"\n[G4] {r} 里的玩家数据被改动了（{len(bad)} 处）：")
            print("\n".join(bad))
        else:
            print(f"[G4] {r}：没有意外改动（{len(before[r])} 项）")
    if rc:
        print(f"[G4] 单测退出码 {rc}")
    return 1 if (rc or changed) else 0


if __name__ == "__main__":
    sys.exit(main())
