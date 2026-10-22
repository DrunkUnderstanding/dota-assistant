"""一键启动：双击「启动助手.bat」走这里。能自动做的准备工作都自动做，只在缺东西时问一句。

  1. 没有 config.toml → 问好友代码和 STRATZ token，其余用默认值
  2. 没装 Electron 界面 → 有 Node.js 就自动 npm install；没有就提示去装
  3. 找到 Dota 安装目录 → 自动写 GSI 配置（第一次会提醒加启动项 -gamestateintegration）
  4. 数据过期 → 自动更新（全量 3 天一次，个人数据半天一次）
  5. 启动助手
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path

from . import config, data
from .config import DATA_DIR, ROOT

FULL_UPDATE_DAYS = 3
PLAYER_UPDATE_HOURS = 12
FIRST_RUN_MARK = DATA_DIR / ".gsi_hint_shown"


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def ensure_config() -> config.Config:
    path = ROOT / "config.toml"
    if not path.exists():
        print("第一次使用，需要两样东西（直接回车跳过，之后可以在 config.toml 里补）")
        acc = _ask("  Steam 好友代码（游戏里个人资料能看到的那串数字）：")
        tok = _ask("  STRATZ token（stratz.com/api 登录后复制，没有就回车）：")
        text = (ROOT / "config.example.toml").read_text(encoding="utf-8")
        if acc.isdigit():
            text = re.sub(r"^account_id = 0", f"account_id = {acc}", text, flags=re.M)
        if tok:
            text = re.sub(r'^stratz_token = ""', f'stratz_token = "{tok}"', text, flags=re.M)
        path.write_text(text, encoding="utf-8")
        print(f"  已生成 {path.name}\n")
    return config.load()


def ensure_electron() -> bool:
    from .webui import ELECTRON_DIR, find_electron
    if find_electron():
        return True
    npm = shutil.which("npm")
    if not npm:
        print("界面需要 Node.js：到 https://nodejs.org 下载 LTS 版安装，装完重新双击「启动助手」。")
        return False
    print("第一次使用，安装界面组件（Electron，约 1~3 分钟）…")
    r = subprocess.run([npm, "install", "--no-fund", "--no-audit"], cwd=ELECTRON_DIR)
    if r.returncode == 0 and not find_electron() and shutil.which("node"):
        # 实测 npm 11 装完有时没下载 Electron 本体（安装脚本没跑），手动补跑一次
        subprocess.run([shutil.which("node"), "node_modules/electron/install.js"], cwd=ELECTRON_DIR)
    if r.returncode != 0 or not find_electron():
        print("界面组件安装失败，看上面的报错；网络不好可以多试一次。")
        return False
    return True


def find_dota() -> Path | None:
    """从注册表找 Steam，再从 libraryfolders.vdf 找装了 Dota 的库"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = Path(winreg.QueryValueEx(k, "SteamPath")[0])
    except OSError:
        return None
    libs = [steam]
    vdf = steam / "steamapps" / "libraryfolders.vdf"
    if vdf.exists():
        libs += [Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8",
                                                                                                    errors="ignore"))]
    for lib in libs:
        d = lib / "steamapps" / "common" / "dota 2 beta"
        if (d / "game" / "dota").exists():
            return d
    return None


def ensure_gsi(cfg: config.Config) -> None:
    from .gsi import CFG_NAME, install_cfg
    dota = find_dota()
    if dota is None:
        print("没找到 Dota 2 安装目录，跳过 GSI（阵营要在控制面板手动选）。")
        return
    target = dota / "game" / "dota" / "cfg" / "gamestate_integration" / CFG_NAME
    if not target.exists() or f":{cfg.gsi_port}/" not in target.read_text(encoding="utf-8"):
        install_cfg(dota, cfg.gsi_port)
        print(f"已写入 GSI 配置：{target}")
    if not FIRST_RUN_MARK.exists():
        print("提醒（只说一次）：Steam 库里右键 Dota 2 → 属性 → 启动选项，加上  -gamestateintegration\n"
              "              Dota 设置：显示模式改「无边框窗口」，社交里打开「公开比赛数据」。\n")
        DATA_DIR.mkdir(exist_ok=True)
        FIRST_RUN_MARK.write_text("1", encoding="utf-8")


def _age_hours(p: Path) -> float:
    return (time.time() - p.stat().st_mtime) / 3600 if p.exists() else float("inf")


def ensure_data(cfg: config.Config) -> None:
    stats = data.stats_path(cfg)
    try:
        if _age_hours(stats) > FULL_UPDATE_DAYS * 24 or not (DATA_DIR / "heroes.json").exists():
            print("更新数据（3 天一次，约 5 分钟）…")
            data.update(cfg)
        elif cfg.account_id and _age_hours(DATA_DIR / "player.json") > PLAYER_UPDATE_HOURS:
            data.update_player(cfg)
    except Exception as e:                       # 没网时用旧数据照样能开
        if not stats.exists():
            raise
        print(f"数据更新失败（{e}），先用旧数据。")


def main() -> int:
    cfg = ensure_config()
    if not ensure_electron():
        return 1
    ensure_gsi(cfg)
    ensure_data(cfg)
    print("启动助手…（关掉控制面板窗口即退出）")
    from .webui import run
    return run(cfg)
