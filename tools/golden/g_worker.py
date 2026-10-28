"""行为快照的子进程：run.py 用固定的 PYTHONHASHSEED / COLUMNS / DOTA_ASSISTANT_HOME 起它，跑一组快照，写到 --out。

  python tools/golden/g_worker.py --group cli|update|slow|replay --mode record|check --home <冻结目录> --out <文件>
                                  --epoch <冻结时刻> [--shift-days N]

先装假时钟（要在 import dota_assistant 之前），再装网络 / OCR 录放和各道闸，最后才 import 产品代码。
"""
from __future__ import annotations

import argparse
import io
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(HERE.parent.parent))     # 仓库根：import dota_assistant

from g_clock import Clock  # noqa: E402
from g_common import CASSETTES, real_token, utf8_stdio  # noqa: E402


def main() -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", required=True)
    ap.add_argument("--mode", choices=("record", "check"), required=True)
    ap.add_argument("--home", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--epoch", type=float, required=True)
    ap.add_argument("--shift-days", type=int, default=0)
    args = ap.parse_args()

    # 第三方库先 import（绑定真的 datetime 类），再装假时钟，再 import 产品代码
    import logging  # noqa: F401
    import tomllib  # noqa: F401

    import cv2  # noqa: F401
    import numpy  # noqa: F401
    import requests  # noqa: F401
    import urllib3  # noqa: F401

    clock = Clock(args.epoch, "virtual" if args.group == "replay" else "cli", args.shift_days,
                  sleep="real" if args.mode == "record" else "skip").install()

    from g_cassette import HttpCassette, OcrCassette, install_guards
    from g_normalize import Normalizer
    guard_log: list[str] = []
    install_guards(args.mode, guard_log)
    cas_dir = CASSETTES / args.group                # 每组自己一份：重录一组不影响别的组
    http = HttpCassette(cas_dir, args.mode, real_token() if args.mode == "record" else "").install()
    ocr = OcrCassette(cas_dir, args.mode)
    norm = Normalizer(args.home)

    if args.group == "replay":
        import replay_live
        text = replay_live.run(args.home, clock, ocr, norm)
    else:
        from g_cases import CASES
        text = run_cli(CASES[args.group], args.home, norm, args.epoch)

    tail = [f"# 网络请求 {http.calls} 次；cassette 里没有的 {len(http.misses)} 个"]
    tail += [f"#   缺：{m}" for m in sorted(set(http.misses))]
    tail += [f"# {norm.text(x)}" for x in http.summary()]
    if ocr.misses:
        tail.append(f"# OCR cassette 里没有的 {ocr.misses} 次")
    tail += [f"# {norm.text(g)}" for g in guard_log]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text.rstrip("\n") + "\n\n" + "\n".join(tail) + "\n", encoding="utf-8")
    return 0


def _log_sizes(home: Path) -> dict[Path, int]:
    d = home / "logs"
    return {p: p.stat().st_size for p in d.glob("*.log*")} if d.exists() else {}


def _new_log_text(home: Path, before: dict[Path, int]) -> str:
    parts = []
    for p, size in sorted(_log_sizes(home).items()):
        start = before.get(p, 0)
        if size > start:
            with open(p, "rb") as f:
                f.seek(start)
                parts.append(f.read().decode("utf-8", "replace"))
    return "".join(parts)


def run_cli(cases: list[list[str]], home: Path, norm, epoch: float) -> str:
    """进程内依次调 dota_assistant.cli.main(argv)，记 stdout / stderr / 退出码 / 这条命令新写的日志 / 写了冻结目录里哪些文件"""
    import g_files
    from g_normalize import sort_pool_runs
    scrub = g_files.Scrubber(epoch)

    from dota_assistant import cli
    out_lines: list[str] = []
    for case in cases:
        argv = [a.replace("{HOME}", str(home)) for a in case]
        sys.argv = ["dota_assistant", *argv]             # 日志开头「==== 启动：…」用的是 sys.argv
        before = _log_sizes(home)
        files_before = g_files.scan(home)
        so, se = io.StringIO(), io.StringIO()
        with redirect_stdout(so), redirect_stderr(se):
            try:
                rc = cli.main(argv)
            except SystemExit as e:
                rc = e.code
            except BaseException as e:                   # noqa: BLE001 —— 产品代码抛出来的也是行为的一部分
                rc = f"异常 {type(e).__name__}: {e}"
                traceback.print_exc(file=se)
        logtext = sort_pool_runs(norm.text(_new_log_text(home, before)))
        touched = g_files.report(home, files_before, g_files.scan(home), norm, scrub)
        out_lines.append("=" * 100)
        out_lines.append("$ dota_assistant " + " ".join(case))
        out_lines.append(f"[退出码] {norm.text(str(rc))}")
        out_lines.append("[stdout]")
        out_lines.append(norm.text(so.getvalue()).rstrip("\n"))
        if se.getvalue().strip():
            out_lines.append("[stderr]")
            out_lines.append(norm.text(se.getvalue()).rstrip("\n"))
        if logtext.strip():
            out_lines.append("[日志]")
            out_lines.append(logtext.rstrip("\n"))
        if touched:
            out_lines.append("[文件变动]（冻结目录里这条命令写过的文件；logs/ 不算）")
            out_lines += touched
    return "\n".join(out_lines)


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        traceback.print_exc()
        code = 3
    sys.exit(code)
