"""一键启动：双击「启动助手.bat」走这里。能自动做的准备工作都自动做，只在缺东西时问一句。

  1. 没有 config.toml → 从 config.example.toml 生成；好友代码、token、AI 来源在控制面板「设置」里填（第一次会自动弹出）
  2. 没装 Electron 界面 → 有 Node.js 就自动 npm install；没有就提示去装（打包版自带，不用装）
  3. 找到 Dota 安装目录 → 自动写 GSI 配置（第一次会提醒加启动项 -gamestateintegration）
  4. 数据过期 → 自动更新（全量 3 天一次，个人数据半天一次）
  5. 启动助手
"""
from __future__ import annotations

import json
import logging
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
log = logging.getLogger(__name__)


def _say(msg: str) -> None:
    """给玩家看的提示：打印到窗口，同时记进日志"""
    print(msg, flush=True)
    log.info(msg.strip())


def ensure_config() -> config.Config:
    path = ROOT / "config.toml"
    if not path.exists():
        config.save({}, path)
        _say(f"已生成 {path.name}。好友代码、STRATZ token、AI 打法用哪家，在控制面板右上角「设置」里填\n")
    return config.load()


def ensure_electron() -> bool:
    from .webui import ELECTRON_DIR, find_electron
    if find_electron():
        return True
    if config.FROZEN:
        _say("界面文件不全（ui 文件夹缺了），重新解压一次完整的压缩包。")
        return False
    npm = shutil.which("npm")
    if not npm:
        _say("界面需要 Node.js：到 https://nodejs.org 下载 LTS 版安装，装完重新双击「启动助手」。")
        return False
    _say("第一次使用，安装界面组件（Electron，约 1~3 分钟）…")
    r = subprocess.run([npm, "install", "--no-fund", "--no-audit"], cwd=ELECTRON_DIR)
    log.info("npm install 返回 %s", r.returncode)
    if r.returncode == 0 and not find_electron() and shutil.which("node"):
        # 实测 npm 11 装完有时没下载 Electron 本体（安装脚本没跑），手动补跑一次
        subprocess.run([shutil.which("node"), "node_modules/electron/install.js"], cwd=ELECTRON_DIR)
    if r.returncode != 0 or not find_electron():
        _say("界面组件安装失败，看上面的报错；网络不好可以多试一次。")
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
    from .gsi import CFG_NAME, CFG_TEMPLATE, install_cfg
    dota = find_dota()
    if dota is None:
        _say("没找到 Dota 2 安装目录，跳过 GSI（阵营要在控制面板手动选）。")
        return
    target = dota / "game" / "dota" / "cfg" / "gamestate_integration" / CFG_NAME
    # 端口或订阅的字段变了都重写（例如死亡复盘要的 abilities）；Dota 启动时才读，开着的话要重开 Dota
    if not target.exists() or target.read_text(encoding="utf-8") != CFG_TEMPLATE.format(port=cfg.gsi_port):
        install_cfg(dota, cfg.gsi_port)
        _say(f"已写入 GSI 配置：{target}（Dota 开着的话要重开才生效）")
    if not FIRST_RUN_MARK.exists():
        _say("提醒（只说一次）：Steam 库里右键 Dota 2 → 属性 → 启动选项，加上  -gamestateintegration\n"
              "              Dota 设置：显示模式改「无边框窗口」，社交里打开「公开比赛数据」。\n")
        DATA_DIR.mkdir(exist_ok=True)
        FIRST_RUN_MARK.write_text("1", encoding="utf-8")


def ensure_grid_portraits() -> None:
    """选人网格认禁用要用游戏自己的英雄图（gamefiles.py）；游戏更新了才重新导出"""
    from . import gamefiles
    dota = find_dota()
    heroes_file = DATA_DIR / "heroes.json"
    if dota is None or not heroes_file.exists():
        return
    try:
        n, missing = gamefiles.extract_selection(dota, json.loads(heroes_file.read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:
        log.warning("网格图导出失败，禁用识别用不了：%s", e)
        return
    if n:
        _say(f"已从游戏资源导出 {n} 张选人网格图（识别禁用用）" + (f"，没取到：{', '.join(missing)}" if missing else ""))


def _age_hours(p: Path) -> float:
    return (time.time() - p.stat().st_mtime) / 3600 if p.exists() else float("inf")


def ensure_data(cfg: config.Config) -> None:
    stats = data.stats_path(cfg)
    try:
        if _age_hours(stats) > FULL_UPDATE_DAYS * 24 or not (DATA_DIR / "heroes.json").exists():
            _say("更新数据（3 天一次，约 5 分钟）…")
            data.update(cfg)
        elif cfg.account_id and _age_hours(DATA_DIR / "player.json") > PLAYER_UPDATE_HOURS:
            data.update_player(cfg)
        if not data.ITEM_ICON_DIR.exists():      # 装备图标是 2026-10-26 加的：老数据不用等全量更新
            data.fetch_item_icons()
    except Exception as e:                       # 没网时用旧数据照样能开
        if not stats.exists():
            raise
        log.info("数据更新失败的详细信息", exc_info=True)
        print(f"数据更新失败（{e}），先用旧数据。")


def no_key_note(cfg: config.Config) -> str:
    """没填 STRATZ token / 调不了 AI 时，启动窗口里说一句哪些照常、少了哪些（不填也能用，只是提醒）"""
    from . import llm
    out = []
    if not cfg.stratz_token:
        out.append("没填 STRATZ token：选人推荐、机制提醒、出装针对装、各种计时照常用；"
                   "少了对线胜负、起手装、强势期、同分段出装节奏")
        src = data.positions_source(cfg)
        if src == "opendota":
            out.append("  分位置数据是用 OpenDota 分路凑的，猜位置比 STRATZ 的准确度低一些")
        elif src is None:
            out.append("  也没有分位置数据：推荐只能按定位标签粗筛位置，对面分路猜不了")
    reason = llm.unavailable(cfg) if cfg.ai_plan else ""
    if reason:
        out.append(f"AI 打法用不了（{reason}），其余功能不受影响")
    if out:
        out.append("要补上就在控制面板右上角「设置」里填\n")
    return "\n".join(out)


def main() -> int:
    cfg = ensure_config()
    if not ensure_electron():
        return 1
    ensure_gsi(cfg)
    ensure_data(cfg)
    ensure_grid_portraits()
    note = no_key_note(cfg)
    if note:
        _say(note)
    _say("启动助手…（关掉控制面板窗口即退出）")
    from .webui import run
    return run(cfg)
