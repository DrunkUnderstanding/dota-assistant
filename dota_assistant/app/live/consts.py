"""实时助手（Live）用的模块级常量和小函数。各 mixin 一律写 consts.NAME（测试只替换这里一处）。"""
from __future__ import annotations

from pathlib import Path

import cv2

from ...ai import llm
from ...domain import brief
from ...core.config import DATA_DIR, ROOT, Config
from ...core.dota_install import is_dota_dir
from ...game.hotkeys import vk_code

SHOTS_DIR = ROOT / "shots"
OVERLAY_FILE = DATA_DIR / "overlay.json"       # 叠加层拖动后的位置 + 是否固定（Electron 那边拖完报过来）
FIX_FILE = DATA_DIR / "fixes.jsonl"            # 玩家帮认 / 改过的格子（一行一次），回头看学到的模板管不管用、哪次填错了
OPACITY_MIN = 0.3                              # 叠加层底色再透就看不清字了
OVERLAY_WIDTH = (300, 4000)                    # 拉宽叠加层时能存的宽度（逻辑像素），Electron 那边还会收到屏幕以内
SIDES = ("radiant", "dire")
DEATH_MIN_SHOW = 3                             # 死亡卡片至少显示几秒（复活很快 / 演示加速时也看得到）
# 死后第几秒截一张 Dota 窗口，和死前推送存在一起（shots/deaths/<时间>-<秒>s.png）。
# 2026-10-26：先看死亡画面上有没有「谁杀的 / 受到的伤害」，有的话再做识别（出装要看这局谁威胁最大）
DEATH_SHOT_DELAYS = (0, 2)
SIDE_MODES = ("auto", "radiant", "dire")       # 阵营：自动（GSI）/ 我在天辉 / 我在夜魇
SIDE_FALLBACK = "radiant"                      # 不知道阵营（自动但 GSI 没连上）时先当左边是我方，卡片和面板上要标出来
ROLE_RETRY = 3.0                               # 认位置：没认出来隔几秒再试
GRID_CONFIRM_MIN = 0.5                         # 顶栏分数至少这么多，再加上网格核对才算认出
SAME_CROP = 0.92                               # 两张顶栏裁图特征相似度高于这个算同一个样子（没换头像 / 重复，不用再记）
# 帮认记模板时，裁图和别的格子 / 开局空位的相似度（recognize.similarity）到这个就当是空位样子、不记。10-22~23 真实截图 140 格：
# 不同英雄之间最多 0.51，英雄和空位最多 0.62；10-29 学坏的那张空位裁图和空位 0.83~1.00 的有 28 格
EMPTY_LIKE = 0.75
# 选人时顶栏这格比这局开头多出这么多彩色像素，才算真有人选了（没认出就显示「?」）。2026-10-23 截图量的：
# 空位是灰底 + 段位小旗（红旗那几格 0.08~0.10），队友预选是半透明灰图（和空位一样 0.00~0.02），选定的头像 0.17~0.91
PICKED_COLOR_GAIN = 0.04
SCAN_MODES = ("auto", "manual")                # 自动识别 / 手动输入
ORPHAN_WINDOW = 30                             # 消息栏认到「买活了」但顶栏没看出死过的：几帧要在这么多游戏秒内
CLOCK_SLACK = 1.5                              # 买活倒计时用的「游戏时钟 0 秒对应的本机时间」差多少秒才改（GSI 时钟是整秒、推送有延迟）


def _side_cn(side: str) -> str:
    return "天辉" if side == "radiant" else "夜魇"


def _names(hs) -> str:
    return "、".join(h.name_cn for h in hs) or "无"


def _slot_text(db, s) -> str:
    """识别结果的一个格子：认出来的写名字，认不出写「?最像的」，后面带相似度"""
    if s.hero_id in db.by_id:
        return f"{db.by_id[s.hero_id].name_cn}({s.score:.2f})"
    guess = db.by_id.get(s.guess)
    return f"?{guess.name_cn if guess else ''}({s.score:.2f})"


def _check_hotkeys(cfg: Config, keys: dict) -> dict:
    """设置里改的热键：键名能注册、几个功能不撞键；返回要写进 config 的字段"""
    updates = {}
    for field, name in keys.items():
        if field not in HOTKEY_ACTIONS:
            raise ValueError(f"不认识的热键项 {field!r}")
        name = str(name or "").strip().upper()
        vk_code(name)                                  # 不支持的键名直接报错
        updates[field] = name
    merged = {f: updates.get(f, getattr(cfg, f)).upper() for f in HOTKEY_ACTIONS}
    seen: dict[str, str] = {}
    for f, name in merged.items():
        if name in seen:
            raise ValueError(f"{name} 同时绑了「{HOTKEY_ACTIONS[seen[name]][0]}」和「{HOTKEY_ACTIONS[f][0]}」，换一个")
        seen[name] = f
    return updates


def _check_dota_dir(text) -> str:
    """设置里填的 Dota 目录：空 = 自动找；选到 game\\dota 这种下一层也认，往上找到「dota 2 beta」那层"""
    text = str(text or "").strip().strip('"').strip()
    if not text:
        return ""
    p = Path(text)
    for d in (p, *list(p.parents)[:2]):
        if is_dota_dir(d):
            return str(d)
    raise ValueError(f"{text} 里没有 game\\dota\\pak01_dir.vpk，不像 Dota 安装目录。"
                     "Steam 里右键 Dota 2 → 管理 → 浏览本地文件，复制打开的那个文件夹地址")


def _colorful(img) -> float:
    """顶栏裁图里彩色像素占多少（下面 15% 是玩家颜色条，不算）"""
    hsv = cv2.cvtColor(img[: max(1, int(img.shape[0] * 0.85))], cv2.COLOR_BGR2HSV)
    return float(((hsv[..., 1] > 89) & (hsv[..., 2] > 51)).mean())


def _view_text(db, view, kinds=("radiant", "dire", "bans")) -> list[str]:
    return [" ".join(_slot_text(db, s) for s in getattr(view, k)) or "-" for k in kinds]


def plan_error(err: Exception) -> str:
    """AI 打法页上的那行说明。调不了 AI（llm.Unavailable）不算出错：说缺什么、怎么办，数据页照常"""
    if isinstance(err, llm.Unavailable):
        return f"AI 打法用不了：{err}。数据页不受影响；控制面板右上角「设置」里换 AI 来源，或者关掉 AI 打法"
    return f"AI 打法没做出来：{err}"


SECRET_FIELDS = {"stratz_token": "STRATZ token", "ai_api_key": "API Key"}   # 不发给页面、不进日志
# 热键：config 字段 → (完整说明, 面板窗口窄时的短说明)；按哪个键在「设置」里改
HOTKEY_ACTIONS = {
    "hotkey_toggle": ("显隐 / 关提醒", "显隐"),
    "hotkey_lock": ("解锁 / 固定位置", "解锁"),
    "hotkey_rescan": ("重识别", "重识别"),
    "hotkey_shot": ("截图", "截图"),
    "hotkey_brief": ("本局要点 / 翻页", "要点"),
}


def _next_stage(page: str) -> str:
    order = [p for p, _ in brief.STAGES]
    return order[(order.index(page) + 1) % len(order)] if page in order else order[0]


def _mask(secret: str) -> dict:
    return {"set": bool(secret), "tail": secret[-4:] if len(secret) >= 8 else ""}
