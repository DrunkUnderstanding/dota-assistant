"""打包成免安装版：python packaging/build.py

产物在 build/ 下：
  build/Dota选人助手/            解压即用的目录（双击 Dota选人助手.exe）
  build/Dota选人助手-<版本>.zip  上面目录的压缩包，拷到别的电脑解压就能用

目录结构：
  Dota选人助手.exe + _internal/   PyInstaller 打的 Python 部分（不用装 Python）
  ui/                            Electron 本体 + resources/app（main.js、preload.js），不用装 Node.js
  web/ knowledge/ data/          页面、知识库规则、数据缓存（英雄表、头像、克制统计、知识库）
  config.example.toml            第一次启动复制成 config.toml；账号和 AI 在控制面板「设置」里填

不打进去的：config.toml（个人 token / key）、logs/、shots/、data 里的个人数据（player.json、overlay.json、lane_log.jsonl、mates.json）
和开发用的回测数据。打包前先把头像、装备常量 / 图标、顶栏图标这些死数据补全（要联网），用的人第一次启动不用再下。
最后会扫一遍产物，确认本机 config.toml 里的密钥没混进去。

需要：pip install pyinstaller；electron/ 下装过一次 npm install（要它的 Electron 本体）。
"""
from __future__ import annotations

import datetime
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
NAME = "Dota选人助手"
OUT = BUILD / NAME
ELECTRON_DIST = ROOT / "electron" / "node_modules" / "electron" / "dist"
KEEP_LOCALES = {"zh-CN.pak", "en-US.pak"}                 # 其余 50 多种语言包 ~45MB，用不到
DATA_SKIP = {"player.json", "overlay.json", ".gsi_hint_shown", "lane_log.jsonl", "mates.json",   # 个人数据（lane_log / mates 里有 Steam ID / 名字）
             "bench", "backtest_matches.json"}                                       # 开发用的回测数据
SECRET_KEYS = ("stratz_token", "ai_api_key")


def say(msg: str) -> None:
    print(msg, flush=True)


def version() -> str:
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
                               capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.strip()
    except OSError:
        rev, dirty = "", ""
    return f"{datetime.date.today():%Y%m%d}-{rev or 'nogit'}{'-改动未提交' if dirty else ''}"


def prefetch() -> None:
    """死数据打包前补全：只补缺的，已有的不重下（装备常量重下一份，跟上版本）"""
    say("== 补全头像、装备、顶栏图标 …")
    sys.path.insert(0, str(ROOT))
    from dota_assistant import kb
    from dota_assistant.app import update
    from dota_assistant.core.config import DATA_DIR
    from dota_assistant.sources import assets
    if kb.KnowledgeBase.load() is None or not (DATA_DIR / "heroes.json").exists():
        sys.exit("还没有英雄表 / 知识库：先运行 python -m dota_assistant update")
    assets.fetch_portraits(say)
    assets.fetch_items_const(say)
    assets.fetch_item_icons(say)
    update.fetch_icons(say)
    if not assets.ICONS_DONE.exists():
        sys.exit("顶栏图标没下到（断网？），停止")


def pyinstaller() -> None:
    say("== PyInstaller 打 Python 部分 …")
    work = BUILD / "pyinstaller"
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--console",
           "--name", NAME, "--distpath", str(BUILD / "pyi-dist"), "--workpath", str(work / "work"),
           "--specpath", str(work), "--paths", str(ROOT),
           "--collect-submodules", "dota_assistant",       # cli 里按命令延迟 import 的模块也带上
           "--hidden-import", "anthropic",
           "--exclude-module", "tkinter", "--exclude-module", "matplotlib", "--exclude-module", "IPython",
           str(ROOT / "packaging" / "launcher.py")]
    subprocess.run(cmd, cwd=ROOT, check=True)
    shutil.copytree(BUILD / "pyi-dist" / NAME, OUT)


def electron() -> None:
    say("== Electron 界面 …")
    if not (ELECTRON_DIST / "electron.exe").exists():
        sys.exit(f"没找到 {ELECTRON_DIST / 'electron.exe'}：先在 electron/ 下 npm install")
    ui = OUT / "ui"
    shutil.copytree(ELECTRON_DIST, ui, ignore=shutil.ignore_patterns("default_app.asar"))
    for pak in (ui / "locales").iterdir():
        if pak.name not in KEEP_LOCALES:
            pak.unlink()
    app = ui / "resources" / "app"
    app.mkdir(parents=True)
    for f in ("main.js", "preload.js", "package.json"):
        shutil.copy2(ROOT / "electron" / f, app / f)


def resources(ver: str) -> None:
    say("== 页面、知识库、数据 …")
    shutil.copytree(ROOT / "web", OUT / "web")
    shutil.copytree(ROOT / "knowledge", OUT / "knowledge")
    shutil.copytree(ROOT / "data", OUT / "data", ignore=lambda d, names: [n for n in names if n in DATA_SKIP]
                    if Path(d) == ROOT / "data" else [])
    for f in ("config.example.toml", "README.md", "calibration.json"):
        if (ROOT / f).exists():
            shutil.copy2(ROOT / f, OUT / f)
    (OUT / "VERSION.txt").write_text(ver, encoding="utf-8")


def check_secrets() -> None:
    """本机 config.toml 里的密钥不能出现在产物的任何文件里"""
    if (OUT / "config.toml").exists():
        sys.exit("产物里有 config.toml，停止")
    cfg = ROOT / "config.toml"
    secrets = []
    if cfg.exists():
        raw = tomllib.loads(cfg.read_text(encoding="utf-8"))
        secrets = [str(raw[k]).encode() for k in SECRET_KEYS if len(str(raw.get(k) or "")) >= 8]
    if not secrets:
        return
    for f in OUT.rglob("*"):
        if f.is_file() and f.suffix.lower() in {".json", ".toml", ".txt", ".md", ".js", ".html", ".css", ".py", ".log"}:
            data = f.read_bytes()
            if any(s in data for s in secrets):
                sys.exit(f"{f.relative_to(OUT)} 里有本机 config.toml 的密钥，停止")
    say(f"== 密钥检查通过（扫了 {len(secrets)} 个密钥）")


def make_zip(ver: str) -> Path:
    say("== 压缩 …")
    dest = BUILD / f"{NAME}-{ver}.zip"
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted(OUT.rglob("*")):
            if f.is_file():
                z.write(f, Path(NAME) / f.relative_to(OUT))
    return dest


def size_mb(p: Path) -> float:
    if p.is_file():
        return p.stat().st_size / 2**20
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 2**20


def main() -> None:
    ver = version()
    prefetch()
    for d in (OUT, BUILD / "pyi-dist"):
        if d.exists():
            shutil.rmtree(d)
    pyinstaller()
    electron()
    resources(ver)
    check_secrets()
    z = make_zip(ver)
    say(f"\n完成（版本 {ver}）\n  目录：{OUT}（{size_mb(OUT):.0f} MB）\n  压缩包：{z}（{size_mb(z):.0f} MB）")


if __name__ == "__main__":
    main()
