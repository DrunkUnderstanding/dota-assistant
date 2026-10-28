"""G4「不碰真实目录」：记下运行目录里玩家数据的文件清单（相对路径, 大小, mtime），前后比一比。

tools/check_isolation.py 和 tools/golden/run.py 在跑单测 / 快照前后各记一次。
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
WATCHED = ("data", "shots", "logs", "templates", "config.toml", "calibration.json")
# 已知的现有问题（重构前就这样，P0.3 报告过，按计划不改测试）：报出来但不算 G4 失败
KNOWN = {
    "shots/gsi/": "tests/test_live.py 的 GSI 服务测试推选人阶段 → GameState.log_draft 写真实 ROOT/shots/gsi/",
}


def snapshot(root: Path = REPO) -> dict[str, tuple[int, int]]:
    """{相对路径: (大小, mtime_ns)}；目录本身也记（mtime 变了说明里面有文件被加 / 删过），不存在的不记"""
    out: dict[str, tuple[int, int]] = {}
    for name in WATCHED:
        p = root / name
        if not p.exists():
            continue
        items = [p, *p.rglob("*")] if p.is_dir() else [p]
        for f in items:
            try:
                st = f.stat()
            except OSError:
                continue
            rel = f.relative_to(root).as_posix() + ("/" if f.is_dir() else "")
            out[rel] = (0 if f.is_dir() else st.st_size, st.st_mtime_ns)
    return out


def diff(before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]) -> list[str]:
    """人能看懂的差异行；空列表 = 没变"""
    lines = []
    for k in sorted(before.keys() | after.keys()):
        a, b = before.get(k), after.get(k)
        if a == b:
            continue
        if a is None:
            lines.append(f"+ 新增 {k}")
        elif b is None:
            lines.append(f"- 删除 {k}")
        else:
            lines.append(f"* 改动 {k}  大小 {a[0]}→{b[0]}  mtime {a[1]}→{b[1]}")
    return lines


def split_known(lines: list[str]) -> tuple[list[str], list[str]]:
    """把差异行分成（意外的, 已知现有问题的）"""
    bad, known = [], []
    for ln in lines:
        path = ln.split(" ", 2)[2].split("  ")[0]
        (known if any(path.startswith(k) for k in KNOWN) else bad).append(ln)
    return bad, known
