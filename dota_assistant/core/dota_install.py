"""找 Dota 2 安装目录：config.toml 的 dota_dir，或者到本机各个 Steam 库里找（只读注册表 / 磁盘，不碰游戏进程）。

GSI 配置（game/gsi）、大招冷却读游戏文件（kb/cd_ops）、启动检查（start）、控制面板设置（live）都要用。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)


def is_dota_dir(d: Path) -> bool:
    """真装了 Dota 的目录（...\\steamapps\\common\\dota 2 beta）：要有主资源包 game\\dota\\pak01_dir.vpk。
    只看 game\\dota 文件夹在不在不够：挪过 Steam 库 / 卸载过会留下只剩 cfg 的空壳。2026-10-27 朋友的电脑上
    e:\\steam 下的空壳被当成了 Dota，GSI 配置写进去游戏根本不读，阵营一直按默认的天辉算"""
    return (d / "game" / "dota" / "pak01_dir.vpk").is_file()


def steam_libraries() -> list[Path]:
    """本机所有 Steam 库：注册表里的 Steam 目录 + libraryfolders.vdf 里登记的其他库"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = Path(winreg.QueryValueEx(k, "SteamPath")[0])
    except OSError:
        return []
    libs = [steam]
    vdf = steam / "steamapps" / "libraryfolders.vdf"
    if vdf.exists():
        text = vdf.read_text(encoding="utf-8", errors="ignore")
        libs += [Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', text)]
    return libs


def find_dota(configured: str | None = None, libraries: list[Path] | None = None) -> Path | None:
    """Dota 安装目录：先用 config.toml 的 dota_dir（configured=None 就读 config.toml），不对再到各个 Steam 库里找。
    libraries 给测试用，默认 steam_libraries()"""
    if configured is None:
        from . import config
        configured = config.load().dota_dir
    if configured:
        if is_dota_dir(Path(configured)):
            return Path(configured)
        log.warning("设置里的 Dota 目录 %s 不对（没有 game\\dota\\pak01_dir.vpk），改成自动找", configured)
    for lib in steam_libraries() if libraries is None else libraries:
        d = lib / "steamapps" / "common" / "dota 2 beta"
        if is_dota_dir(d):
            return d
        if (d / "game" / "dota").exists():
            log.info("跳过 %s：有 game\\dota 文件夹但没有 pak01_dir.vpk，不是完整的 Dota（多半是挪库 / 卸载留下的）", d)
    return None
