"""冻结的运行目录：从 D:\\dota 复制一份（只读 D:\\dota），密钥清掉，打包成 home.zip；检查时解压到临时目录。

内容（PLAN.md P0.4）：config.toml（处理过）、config.example.toml、knowledge/、data/、calibration.json、
shots/ 里 14 张选人截图（g_cases.DRAFT_SHOTS）、shots/deaths/*.json 全部、shots/buyback/feed/ 全部，
外加 Live 回放用的一张局内截图（REPLAY_SHOTS）。

config.toml 的处理：stratz_token 换成占位符 DUMMY_TOKEN（见 g_common：走 STRATZ 的代码照样走，真 token 不落盘），
ai_api_key 清空，ai_plan = false，voice = false，dota_dir 清空（不去碰这台机器上的 Dota）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
from pathlib import Path

from g_cases import DRAFT_SHOTS
from g_common import DUMMY_TOKEN, HOME_DIR, HOME_ZIP, SOURCE

# Live 回放（replay_live.py）用的局内画面：比赛 9064593746 第 2 次死亡时的截图（除了自己，顶栏 9 个头像都是彩色的）
REPLAY_SHOTS = ["deaths/20261028-010824-0s.png"]
CONFIG_OVERRIDES = {"stratz_token": DUMMY_TOKEN, "ai_api_key": "", "ai_plan": False, "voice": False, "dota_dir": ""}


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return json.dumps(v)


def sanitize_config(text: str) -> str:
    """只改这几项那一行的值（注释留着）；原来没有的加在末尾"""
    for key, value in CONFIG_OVERRIDES.items():
        line = re.compile(rf'^({re.escape(key)}\s*=\s*)("(?:[^"\\]|\\.)*"|[^#\n]*?)([ \t]*(?:#.*)?)$', re.M)
        text, n = line.subn(lambda m: m.group(1) + _toml_value(value) + m.group(3), text, count=1)
        if not n:
            text = text.rstrip("\n") + f"\n{key} = {_toml_value(value)}\n"
    return text


def build(log=print) -> dict:
    """D:\\dota → D:\\dota-golden\\home（先清空）→ home.zip。返回统计"""
    if HOME_DIR.exists():
        shutil.rmtree(HOME_DIR)
    HOME_DIR.mkdir(parents=True)

    def copy(rel: str) -> None:
        src, dst = SOURCE / rel, HOME_DIR / rel
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.part"))
        elif src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        else:
            log(f"  （没有 {rel}，跳过）")

    raw = (SOURCE / "config.toml").read_text(encoding="utf-8")
    (HOME_DIR / "config.toml").write_text(sanitize_config(raw), encoding="utf-8")
    for rel in ("config.example.toml", "knowledge", "data", "calibration.json", "shots/buyback/feed"):
        copy(rel)
    for s in DRAFT_SHOTS:
        copy(f"shots/{s}.png")
    for p in sorted((SOURCE / "shots" / "deaths").glob("*.json")):
        copy(f"shots/deaths/{p.name}")
    for rel in REPLAY_SHOTS:
        copy(f"shots/{rel}")

    files = [p for p in HOME_DIR.rglob("*") if p.is_file()]
    tmp = HOME_ZIP.with_suffix(".zip.part")
    with zipfile.ZipFile(tmp, "w") as z:
        for p in sorted(files):
            rel = p.relative_to(HOME_DIR).as_posix()
            stored = p.suffix.lower() in (".png", ".jpg")
            z.write(p, rel, compress_type=zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED, compresslevel=1)
    tmp.replace(HOME_ZIP)
    size = sum(p.stat().st_size for p in files)
    log(f"  冻结目录：{len(files)} 个文件，{size / 1e6:.0f} MB → {HOME_ZIP}（{HOME_ZIP.stat().st_size / 1e6:.0f} MB）")
    return {"files": len(files), "bytes": size}


def extract(dest: Path, epoch: float, skip: tuple[str, ...] = ()) -> Path:
    """home.zip → dest（dest 应该是空的新目录）；所有文件 mtime 设成冻结时刻 epoch。skip：不解压的路径前缀"""
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(HOME_ZIP) as z:
        names = [n for n in z.namelist() if not any(n.startswith(s) for s in skip)]
        z.extractall(dest, names)
    for n in names:
        p = dest / n
        if p.is_file():
            os.utime(p, (epoch, epoch))
    return dest
