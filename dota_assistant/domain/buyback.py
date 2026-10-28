"""买活计时：双方 10 个英雄的买活冷却，显示在游戏顶栏每个英雄头像正下方（web/topbar.html）。

玩家 2026-10-26 要的（原话「需要加一个功能就是敌对方买活时间计时」「友方买活时间计时也要加上」
「这个的叠层应该显示在正上方的英雄头像处」）。

怎么知道谁买活了：
- 自己：GSI 的 hero.buyback_cooldown（还剩几秒），自动计时；字段名对照 Dota2GSI，**没在真实推送里见过**，没有这个字段就和别人一样手动点
- 队友 / 对面：玩家身份下 GSI 拿不到别人的数据（Valve 有意的限制）。比赛中每秒截一次 Dota 窗口看顶栏，
  头像由灰变彩色时复活倒计时还没走完 = 买活，自动开始计时（buybackscan.py，未实测）；
  玩家也能点头像下面的小条手动记，点错了 / 认错了再点一下取消。买活是全场公开的：有人买活时全场都听得到买活音效，
  顶栏那个英雄头像下面会出一个金币堆图标、留几秒，敌我都看得见（Fandom「Buyback」），所以不算显示玩家本来看不到的信息

时间：冷却 480 秒（8 分钟，7.06 起；7.41f 没改。Liquipedia / Fandom「Buyback」，不是官方原文，知识库里没有买活）。
只看游戏时钟（GSI map.clock_time），暂停时不走。冷却结束后亮 READY_SHOW 秒「可买活」再变回空条——
对面买不买得起（看身上的钱）玩家看不到，助手也不猜。
"""
from __future__ import annotations

from dataclasses import dataclass, field

COOLDOWN = 480               # 买活冷却（秒）
READY_SHOW = 30              # 冷却结束后亮几秒「可买活」（实现假设）
SELF_RESYNC = 1.5            # GSI 给的自己的冷却和记着的差多少秒才改（推送有延迟，免得每次推送都改、界面跟着刷）
SIDES = ("radiant", "dire")


@dataclass
class Tracker:
    ends: dict[tuple[str, int], float] = field(default_factory=dict)   # (阵营, 顶栏第几格) → 冷却结束的游戏时钟（秒）
    auto: set[tuple[str, int]] = field(default_factory=set)            # 哪几格是 GSI 自动计时的（自己）

    def mark(self, side: str, index: int, clock: float) -> bool:
        """玩家点了一下：没在冷却就从 clock 开始计 8 分钟，正在冷却就取消（点错了）。返回是不是开始计时"""
        key = (side, index)
        if self.ends.get(key, -1e9) > clock:
            del self.ends[key]
            return False
        self.ends[key] = clock + COOLDOWN
        return True

    def start(self, side: str, index: int, clock: float) -> bool:
        """截图认出来买活了（buybackscan.py）：从 clock 开始计；已经在冷却（玩家先点了）就不动。返回是不是新记的"""
        key = (side, index)
        if self.ends.get(key, -1e9) > clock:
            return False
        self.ends[key] = clock + COOLDOWN
        return True

    def sync_self(self, side: str, index: int, clock: float, cooldown: float) -> None:
        """自己：GSI 说还剩 cooldown 秒（0 = 没在冷却）"""
        key = (side, index)
        self.auto = {key}
        if cooldown > 0:
            if abs(self.ends.get(key, -1e9) - (clock + cooldown)) > SELF_RESYNC:
                self.ends[key] = clock + cooldown
        elif self.ends.get(key, -1e9) > clock + SELF_RESYNC:
            del self.ends[key]                   # GSI 说冷却好了（比如之前手动点错了）

    def end(self, side: str, index: int, clock: float) -> float | None:
        """冷却结束的游戏时钟；已经过了 READY_SHOW 秒的不算（变回空条）"""
        e = self.ends.get((side, index))
        return e if e is not None and clock < e + READY_SHOW else None

    def reset(self) -> None:
        self.ends.clear()
        self.auto.clear()
