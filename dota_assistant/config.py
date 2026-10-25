"""读取 config.toml；不存在时用默认值。"""
from __future__ import annotations

import json
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# 打包成 exe（PyInstaller，见 packaging/）后，config.toml、data/、web/ 等都放在 exe 旁边
FROZEN = getattr(sys, "frozen", False)
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent
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
    top: int = 3                      # 推荐总数 = top + top_other（不再分「你的英雄 / 其他」）
    top_other: int = 2                # 见上一行

    # ---- 实时助手（python -m dota_assistant run）----
    monitor: int = 1                  # 截哪块屏幕（mss 编号，1=主屏）
    scan_interval: float = 1.0        # 识别间隔（秒）
    match_threshold: float = 0.70     # 头像相似度低于此视为空位 / 认不出（用 recognize 看实际分数再调）
    gsi_port: int = 3000              # Game State Integration 本地端口
    voice: bool = False               # 语音播报先关掉、面板开关也隐藏了（玩家 2026-10-22 决定），这里改 true 能打开
    voice_rate: int = 1               # 语速 -10 ~ 10
    coach_voice: bool = False         # 局内缺口提醒要不要念出来（先只弹卡片）
    ai_plan: bool = True              # 本局要点里加「AI 打法」（每局一次，几十秒）
    ai_provider: str = "claude_code"  # AI 来源：claude_code（本机 Claude Code 订阅）/ anthropic（自己的 API Key）/ openai（OpenAI 兼容接口）
    ai_model: str = "claude-opus-5-5"
    ai_api_key: str = ""              # anthropic / openai 用；和 stratz_token 一样不进 git、不外传
    ai_base_url: str = ""             # openai 用，例如 https://api.deepseek.com/v1
    overlay_x: float = 0.80           # 叠加层左上角位置（占屏幕宽 / 高的比例）；拖动过以后以 data/overlay.json 为准
    overlay_y: float = 0.22
    overlay_width: int = 420          # 叠加层宽度（逻辑像素）
    overlay_opacity: float = 0.95     # 叠加层卡片底色的不透明度（0.3~1，1 = 完全不透明）；控制面板「叠加层」下面能直接调
    overlay_color: str = "#0a0d12"    # 叠加层卡片底色；文字是浅色，底色要深
    hotkey_toggle: str = "F8"         # 显示 / 隐藏叠加层
    hotkey_lock: str = "F7"           # 解锁（可以拖动叠加层）/ 固定位置
    hotkey_rescan: str = "F9"         # 立即重新识别并播报
    hotkey_shot: str = "F10"          # 保存一张全屏截图到 shots/（收集校准 / 学习素材）
    hotkey_brief: str = "F6"          # 再看一次本局要点（号角响后会自动收起）
    log_level: str = "INFO"           # 日志详细程度（logs/ 下）：INFO / DEBUG（排查问题时用，会多记每秒一次的识别分数和每次 GSI 推送）


def load(path: Path | None = None) -> Config:
    path = path or ROOT / "config.toml"
    if not path.exists():
        return Config()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    known = Config.__dataclass_fields__.keys()
    return Config(**{k: v for k, v in raw.items() if k in known})


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(str(v))                       # JSON 字符串转义和 TOML 基本字符串兼容


def save(updates: dict, path: Path | None = None) -> Path:
    """把几项设置写回 config.toml：只改对应那一行的值，注释和其他行原样保留；没有这一项就加在末尾。
    还没有 config.toml 时从 config.example.toml 复制一份再改。"""
    path = path or ROOT / "config.toml"
    if path.exists():
        text = path.read_text(encoding="utf-8")
    else:
        example = ROOT / "config.example.toml"
        text = example.read_text(encoding="utf-8") if example.exists() else ""
    for key, value in updates.items():
        if key not in Config.__dataclass_fields__:
            raise KeyError(key)
        line = re.compile(rf'^({re.escape(key)}\s*=\s*)("(?:[^"\\]|\\.)*"|[^#\n]*?)([ \t]*(?:#.*)?)$', re.M)
        new = _toml_value(value)
        text, n = line.subn(lambda m: m.group(1) + new + m.group(3), text, count=1)
        if not n:
            text = text.rstrip("\n") + f"\n{key} = {new}\n"
    tomllib.loads(text)                              # 写坏了宁可报错也不落盘
    path.write_text(text, encoding="utf-8")
    return path
