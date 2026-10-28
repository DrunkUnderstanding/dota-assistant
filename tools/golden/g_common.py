"""行为快照（G3）用到的路径和小工具。数据都放在仓库外的 D:\\dota-golden\\（有个人数据，不进 git）。

环境变量可改：DOTA_GOLDEN_DIR（快照 / cassette / home.zip 放哪）、DOTA_GOLDEN_SOURCE（冻结目录从哪复制，默认 D:\\dota）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
GOLDEN = Path(os.environ.get("DOTA_GOLDEN_DIR") or r"D:\dota-golden")
SOURCE = Path(os.environ.get("DOTA_GOLDEN_SOURCE") or r"D:\dota")
HOME_ZIP = GOLDEN / "home.zip"
HOME_DIR = GOLDEN / "home"               # 解压后的样子（留着方便看；检查时每次另解压到临时目录）
CASSETTES = GOLDEN / "cassettes"         # 网络录放，每组一个子目录：<组>/http/ 一个请求一个文件，<组>/ocr.json 是 Windows OCR 的
SNAPSHOTS = GOLDEN / "snapshots"
DIFFS = GOLDEN / "last-diff"             # 最近一次 check 不一致时的完整 diff
META = GOLDEN / "meta.json"              # 冻结时刻 epoch（假时钟用）等

# 冻结目录里 config.toml 的 STRATZ token 换成这个占位符：走 STRATZ 的代码路径照样会走，真 token 不落盘。
# record 时请求发出去之前（只在内存里）换成 D:\dota\config.toml 里的真 token；cassette 不存请求头。
DUMMY_TOKEN = "golden-dummy-stratz-token"

# 快照分组：每组一个子进程、各自一份新解压的冻结目录（互不影响写进去的东西）
GROUPS = ("cli", "replay", "update")
SLOW_GROUPS = ("slow",)

HASH_SEED = "0"
COLUMNS = "100"                          # argparse --help 按终端宽度换行，固定住


def load_meta() -> dict:
    try:
        return json.loads(META.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_meta(meta: dict) -> None:
    GOLDEN.mkdir(parents=True, exist_ok=True)
    META.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")


def real_token() -> str:
    """D:\\dota\\config.toml 里的 STRATZ token（只在 record 时读进内存，不打印、不落盘）"""
    import tomllib
    try:
        return str(tomllib.loads((SOURCE / "config.toml").read_text(encoding="utf-8")).get("stratz_token") or "")
    except (OSError, ValueError):
        return ""


def utf8_stdio() -> None:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
