"""行为快照（重构门禁 G3）：CLI 快照 + Live 回放快照，逐字节比对。说明见同目录 README.md。

  python tools/golden/run.py record [--reuse-home [--group G ...]] [--no-slow]
                                                                从 D:\\dota 重建冻结目录、真联网录 cassette、再生成快照
  python tools/golden/run.py check [--slow] [--shift-days N] [--group G ...] [--keep]
                                                                只读 cassette 跑一遍，和存着的快照逐字节比；不一致打印 diff、退出码 1
  python tools/golden/run.py update-snapshot [--slow] [--group G ...]   按当前代码重写快照（确认差异是该有的才用）
  python tools/golden/run.py scan-token                         确认 cassette / 快照 / 冻结目录里没有真 STRATZ token

每组快照在一个子进程里跑（固定 PYTHONHASHSEED、COLUMNS，DOTA_ASSISTANT_HOME 指向这次新解压的冻结目录），
check 前后用 tests/tools/sentinel.py 记一次仓库根的玩家目录（G4：快照不许碰真实目录）。
"""
from __future__ import annotations

import argparse
import difflib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(HERE.parent.parent))

import g_home  # noqa: E402
from g_common import (CASSETTES, COLUMNS, DIFFS, GOLDEN, GROUPS, HASH_SEED, HOME_ZIP, REPO,  # noqa: E402
                      SLOW_GROUPS, SNAPSHOTS, load_meta, real_token, save_meta, utf8_stdio)

from tests.tools import sentinel  # noqa: E402

# 各组不需要解压的东西（省时间）：只有回放要用买活消息栏的截图
SKIP = {"cli": ("shots/buyback/",), "update": ("shots/",), "slow": ("shots/",), "replay": ()}
MAX_DIFF_LINES = 300


def run_group(group: str, mode: str, epoch: float, out: Path, shift_days: int = 0, keep: bool = False) -> tuple[int, float]:
    """起子进程跑一组；返回（退出码, 秒）"""
    t0 = time.monotonic()
    tmp = Path(tempfile.mkdtemp(prefix=f"dota-golden-{group}-"))
    home = g_home.extract(tmp / "home", epoch, SKIP.get(group, ()))
    env = {k: v for k, v in os.environ.items() if k not in ("ELECTRON_RUN_AS_NODE",)}
    env.update(PYTHONHASHSEED=HASH_SEED, COLUMNS=COLUMNS, DOTA_ASSISTANT_HOME=str(home), PYTHONIOENCODING="utf-8",
               PYTHONUTF8="1")
    cmd = [sys.executable, "-X", "utf8", str(HERE / "g_worker.py"), "--group", group, "--mode", mode,
           "--home", str(home), "--out", str(out), "--epoch", str(epoch), "--shift-days", str(shift_days)]
    rc = subprocess.run(cmd, cwd=REPO, env=env).returncode
    if keep:
        print(f"  （保留了临时目录 {tmp}）")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return rc, time.monotonic() - t0


def compare(group: str, new: Path) -> bool:
    old = SNAPSHOTS / f"{group}.txt"
    if not old.exists():
        print(f"[{group}] 还没有快照：先跑 record 或 update-snapshot")
        return False
    a, b = old.read_bytes(), new.read_bytes()
    if a == b:
        print(f"[{group}] 一致（{len(a):,} 字节）")
        return True
    diff = list(difflib.unified_diff(a.decode("utf-8").splitlines(), b.decode("utf-8").splitlines(),
                                     f"快照/{group}.txt", f"这次/{group}.txt", n=3, lineterm=""))
    DIFFS.mkdir(parents=True, exist_ok=True)
    (DIFFS / f"{group}.diff").write_text("\n".join(diff) + "\n", encoding="utf-8")
    shutil.copyfile(new, DIFFS / f"{group}.new.txt")
    print(f"[{group}] 不一致：{len(diff)} 行 diff（完整的在 {DIFFS / (group + '.diff')}）")
    print("\n".join(diff[:MAX_DIFF_LINES]))
    if len(diff) > MAX_DIFF_LINES:
        print(f"…（只显示前 {MAX_DIFF_LINES} 行）")
    return False


def need_home() -> float:
    meta = load_meta()
    if not (HOME_ZIP.exists() and meta.get("epoch")):
        sys.exit(f"没有冻结目录 {HOME_ZIP}：先跑 python tools/golden/run.py record")
    return float(meta["epoch"])


def cmd_record(args) -> int:
    meta = load_meta()
    if args.reuse_home and HOME_ZIP.exists() and meta.get("epoch"):
        epoch = float(meta["epoch"])
        print(f"沿用冻结目录 {HOME_ZIP}")
    else:
        print(f"从 {g_home.SOURCE} 复制冻结目录（只读）…")
        g_home.build()
        epoch = float(int(time.time()))
        meta = {"epoch": epoch, "built": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))}
    meta["recorded"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_meta(meta)
    if args.group and not args.reuse_home:
        sys.exit("--group 只能和 --reuse-home 一起用（重建冻结目录以后所有组都要重录）")
    groups = args.group or (list(GROUPS) + ([] if args.no_slow else list(SLOW_GROUPS)))
    rec_dir = GOLDEN / "record-output"
    for g in groups:
        shutil.rmtree(CASSETTES / g, ignore_errors=True)
        print(f"[{g}] 录 cassette（真联网）…", flush=True)
        rc, secs = run_group(g, "record", epoch, rec_dir / f"{g}.txt")
        print(f"[{g}] 退出码 {rc}，{secs:.0f} 秒")
        if rc:
            return rc
    if scan_token() != 0:
        return 1
    print("按 cassette 生成快照（check 模式）…")
    return cmd_update(argparse.Namespace(slow=not args.no_slow, group=args.group))


def _groups(args) -> list[str]:
    if args.group:
        return args.group
    return list(GROUPS) + (list(SLOW_GROUPS) if args.slow else [])


def cmd_update(args) -> int:
    epoch = need_home()
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    for g in _groups(args):
        out = SNAPSHOTS / f"{g}.txt"
        rc, secs = run_group(g, "check", epoch, out)
        print(f"[{g}] 快照已写 {out}（退出码 {rc}，{secs:.0f} 秒）")
        if rc:
            return rc
    return scan_token()


def cmd_check(args) -> int:
    epoch = need_home()
    t0 = time.monotonic()
    before = sentinel.snapshot(REPO)
    shutil.rmtree(DIFFS, ignore_errors=True)        # 上一次的 diff 先清掉，一致了也不留旧的误导人
    work = Path(tempfile.mkdtemp(prefix="dota-golden-check-"))
    ok = True
    for g in _groups(args):
        out = work / f"{g}.txt"
        rc, secs = run_group(g, "check", epoch, out, args.shift_days, args.keep)
        print(f"[{g}] 跑完（{secs:.0f} 秒）", flush=True)
        if rc:
            print(f"[{g}] 子进程出错，退出码 {rc}")
            ok = False
            continue
        ok = compare(g, out) and ok
    # 不用 sentinel.split_known 的「已知问题」豁免：那是给单测开的；golden 子进程只该写临时冻结目录，仓库根有任何变化都算失败
    bad = sentinel.diff(before, sentinel.snapshot(REPO))
    if bad:
        ok = False
        print(f"[G4] 仓库根的玩家目录被改动了（{len(bad)} 处）：\n" + "\n".join(bad))
    else:
        print(f"[G4] {REPO} 的玩家目录没有改动（{len(before)} 项）")
    shutil.rmtree(work, ignore_errors=True)
    print(f"{'全部一致' if ok else '有不一致 / 出错'}，共 {time.monotonic() - t0:.0f} 秒")
    return 0 if ok else 1


def scan_token() -> int:
    """真 token（读 D:\\dota\\config.toml，不打印）不许出现在 D:\\dota-golden 下任何文件里"""
    token = real_token()
    if len(token) < 8:
        print("[token] D:\\dota\\config.toml 里没有 STRATZ token，跳过检查")
        return 0
    needle = token.encode("utf-8")
    hits, n = [], 0
    for p in GOLDEN.rglob("*"):
        if not p.is_file() or p.suffix.lower() == ".zip":
            continue
        n += 1
        if needle in p.read_bytes():
            hits.append(p)
    import zipfile
    if HOME_ZIP.exists():
        with zipfile.ZipFile(HOME_ZIP) as z:
            for name in z.namelist():
                n += 1
                if needle in z.read(name):
                    hits.append(Path(f"{HOME_ZIP}!{name}"))
    if hits:
        print(f"[token] 有 {len(hits)} 个文件含真 token：\n" + "\n".join(str(h) for h in hits))
        return 1
    print(f"[token] 查了 {n} 个文件（cassette / 快照 / 冻结目录 / home.zip 里的），没有真 token")
    return 0


def main() -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--reuse-home", action="store_true", help="不重建冻结目录，沿用现有 home.zip 和冻结时刻")
    r.add_argument("--no-slow", action="store_true", help="不录慢快照（backtest 那些）")
    r.add_argument("--group", action="append", choices=list(GROUPS) + list(SLOW_GROUPS),
                   help="只重录这几组（要带 --reuse-home）")
    for name in ("check", "update-snapshot"):
        c = sub.add_parser(name)
        c.add_argument("--slow", action="store_true", help="加上慢快照（backtest / lanes-test / lane-test）")
        c.add_argument("--group", action="append", choices=list(GROUPS) + list(SLOW_GROUPS), help="只跑这几组")
        if name == "check":
            c.add_argument("--shift-days", type=int, default=0, help="模拟隔 N 天再跑（datetime 往后拨，见 g_clock）")
            c.add_argument("--keep", action="store_true", help="保留解压出来的临时冻结目录（排查用）")
    sub.add_parser("scan-token")
    args = ap.parse_args()
    if args.cmd == "record":
        return cmd_record(args)
    if args.cmd == "check":
        return cmd_check(args)
    if args.cmd == "update-snapshot":
        return cmd_update(args)
    return scan_token()


if __name__ == "__main__":
    sys.exit(main())
