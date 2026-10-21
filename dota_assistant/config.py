"""读取 config.toml；不存在时用默认值。"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


@dataclass
class Config:
    position: int = 1                 # 我打几号位（1~5）
    rank_min: int = 0                 # 统计数据的段位下限（1=先锋 … 8=冠绝），0=不限
    rank_max: int = 0                 # 段位上限，0=不限
    days: int = 7                     # 统计最近几天的天梯全英雄选择对局
    account_id: int = 0               # Steam 好友代码，0=未设置
    history_matches: int = 300        # 用最近多少局天梯判断「你的英雄」
    stratz_token: str = ""            # STRATZ API token：分位置数据 + 你每局打的位置
    pool: list[str] = field(default_factory=list)   # 我的英雄池（中文名 / 英文名 / 别名）
    only_pool: bool = False           # True=只在英雄池里推荐
    position_filter: bool = True      # True=按位置粗筛候选
    top: int = 3                      # 「你的英雄」里推荐几个
    top_other: int = 2                # 另外再给几个池外的选择

    # ---- 实时助手（python -m dota_assistant run）----
    monitor: int = 1                  # 截哪块屏幕（mss 编号，1=主屏）
    scan_interval: float = 1.0        # 识别间隔（秒）
    match_threshold: float = 0.70     # 头像相似度低于此视为空位 / 认不出（用 recognize 看实际分数再调）
    gsi_port: int = 3000              # Game State Integration 本地端口
    voice: bool = True
    voice_rate: int = 1               # 语速 -10 ~ 10
    overlay_x: float = 0.80           # 叠加层左上角位置（占屏幕宽 / 高的比例）
    overlay_y: float = 0.22
    overlay_width: int = 420          # 叠加层宽度（逻辑像素）
    overlay_opacity: float = 0.85
    hotkey_toggle: str = "F8"         # 显示 / 隐藏叠加层
    hotkey_rescan: str = "F9"         # 立即重新识别并播报
    hotkey_shot: str = "F10"          # 保存一张全屏截图到 shots/（收集校准 / 学习素材）


def load(path: Path | None = None) -> Config:
    path = path or ROOT / "config.toml"
    if not path.exists():
        return Config()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    known = Config.__dataclass_fields__.keys()
    return Config(**{k: v for k, v in raw.items() if k in known})
