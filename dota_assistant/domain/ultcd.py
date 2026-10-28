"""对面大招冷却计时：对面 5 个英雄各一个关键大招，小条显示在游戏顶栏买活小条的下面（web/topbar.html）。

玩家 2026-10-26 要的（原话「敌方关键技能的CD，比如末日大招、骷髅王大招」「是否能够计算那些多余的减冷却机制」）。

怎么知道放了：玩家身份下 GSI 拿不到对面的技能（Valve 有意的限制），**玩家看到对面放大招（骷髅王是复活）后点一下小条**，
从点的那一刻开始倒计时；点错了再点一下取消。只记玩家亲眼看到的，不算显示本来看不到的信息。

冷却：知识库（Valve 官方数据）每一级的冷却。对面大招几级看不到确切的（要点开他看），默认按自己英雄等级猜
（大招 6 / 12 / 18 级能升：6~11 级算 1 级、12~17 级算 2 级、18 级以上算 3 级）。

减冷却（右键小条弹菜单，玩家点开对面看到了就勾，这局一直记着；对面有没有，助手自己看不到）：
- 天赋：官方 special_values 里 AbilityCooldown 的 bonuses，operation 2 = 减几秒、4 = 减百分之几（对过天赋原文）；
- 神杖 / 魔晶：datafeed 只给数值、丢了符号（绝冥再生「-10」、激怒「=30 =24 =18」、生命汲取「-50%」都只剩数字），
  所以从游戏文件 scripts/npc/heroes/npc_dota_hero_*.txt 取原样的写法（kb/cd_ops.py 的 `update_ops`，`kb update` 时顺带取）：
  本机装了 Dota 就读游戏资源包，没装就读 SteamDatabase 在 GitHub 上同步的同一份文件；取不到就不给勾这两项；
- 玲珑心：知识库物品属性「+25% 冷却缩减」。别的减冷却物品知识库里没有（2026-10-26 查过 7.41f 全部物品）。
算法：先「=」改成，再减秒，再减百分比（天赋 / 神杖），最后乘玲珑心。几样一起时的先后是实现假设，没在游戏里核对。
没算：命石（例如风行者的「集中火力」命石把冷却改成 70/60/50）、刷新球（刷完再放一次就再点一下，会被当成取消——再点一下重新计时）。

跟踪哪个：大招（官方 type = 1）、不是神杖 / 魔晶给的、1 级冷却 ≥ MIN_CD 秒（kb/cd_ops.py）（狙击手暗杀、帕吉肢解这种几十秒以内的不值得记）；
有两个就取冷却长的。规则是实现假设，玩家没确认过。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .. import kb
from ..kb import cd_ops as kbcd   # 不叫 cd_ops：本模块原来有同名函数（已搬到 kb/cd_ops），旧模块里不再绑定这个名字

READY_SHOW = 30              # 冷却好了亮几秒「大招好了」再变回技能名（实现假设，同买活）
ULT_LEVELS = (6, 12, 18)     # 英雄几级能升大招
OCTARINE = "octarine_core"
log = logging.getLogger(__name__)


def guess_level(hero_level: int | None) -> int:
    """按自己英雄等级猜对面大招几级（没有等级就算 1 级）"""
    return max(1, sum(hero_level >= x for x in ULT_LEVELS)) if hero_level else 1


def _at(values, level: int) -> float:
    return values[min(level, len(values)) - 1]


def cooldown(ab: kb.Entry, level: int, mods: list[Mod] = ()) -> float:
    """按 level 级、勾了的减冷却算出来的冷却（秒）"""
    order = {"=": 0, "-": 1, "%": 2}
    cd = _at(kbcd.cooldowns(ab), level)
    for m in sorted(mods, key=lambda m: (m.key == "octarine", order[m.op])):
        v = _at(m.values, level)
        cd = v if m.op == "=" else cd - v if m.op == "-" else cd * (1 - v / 100)
    return max(0.0, round(cd, 1))


# ---------- 减冷却 ----------

@dataclass(frozen=True)
class Mod:
    key: str                  # talent:<天赋 key> / scepter / shard / octarine
    label: str                # 菜单上显示的
    op: str                   # "=" 改成、"-" 减几秒、"%" 减百分之几
    values: tuple[float, ...]


def parse_op(text: str) -> tuple[str, tuple[float, ...]] | None:
    """游戏文件里的写法 → (op, 各级数值)：「-10」「=30 =24 =18」「-50%」；看不懂的返回 None"""
    parts = text.split()
    pat = re.compile(r"([=+-])(\d+(?:\.\d+)?)(%?)")
    ms = [pat.fullmatch(p) for p in parts]
    if not ms or not all(ms) or len({(m.group(1), m.group(3)) for m in ms}) != 1:
        return None
    sign, pct = ms[0].group(1), ms[0].group(3)
    vals = tuple(float(m.group(2)) for m in ms)
    if sign == "=":
        return None if pct else ("=", vals)
    vals = vals if sign == "-" else tuple(-v for v in vals)       # 「+」是加冷却，按负的减
    return ("%" if pct else "-"), vals


def _describe(op: str, values: tuple[float, ...]) -> str:
    vs = "/".join(f"{v:g}" for v in values)
    return {"=": f"冷却改成 {vs} 秒", "-": f"-{vs}秒 冷却", "%": f"-{vs}% 冷却"}[op]


def mods(h: kb.HeroKB, ab: kb.Entry, base: kb.KnowledgeBase | None, ops: dict) -> list[Mod]:
    """这个大招能勾的减冷却"""
    out = []
    for name, v, op in ab.cd_talents:
        if op in (2, 4):
            lv, text = h.talent_keys.get(name, (None, ""))
            out.append(Mod(f"talent:{name}", f"{lv} 级天赋：{text}" if lv else f"天赋：{text}", "-" if op == 2 else "%", (v,)))
    for key, vals, cn in (("scepter", ab.cd_scepter, "神杖"), ("shard", ab.cd_shard, "魔晶")):
        raw = (ops.get(ab.key) or {}).get(key)
        if vals and raw and (p := parse_op(raw)):
            out.append(Mod(key, f"{cn}：{_describe(*p)}", *p))
    oc = base.items.get(OCTARINE) if base else None
    pct = next((float(m.group(1)) for s in (oc.stats if oc else []) if (m := re.match(r"\+(\d+(?:\.\d+)?)% 冷却缩减", s))), None)
    if pct:
        out.append(Mod("octarine", f"{oc.name}：-{pct:g}% 冷却", "%", (pct,)))
    return out


# ---------- 这一局 ----------

@dataclass
class Tracker:
    casts: dict[tuple[str, int], tuple[float, float]] = field(default_factory=dict)   # (阵营, 顶栏第几格) → (放的游戏时钟, 冷却秒)
    levels: dict[tuple[str, int], int] = field(default_factory=dict)                   # 菜单里选过的大招等级
    mods: dict[tuple[str, int], set[str]] = field(default_factory=dict)                # 菜单里勾了的减冷却（Mod.key）

    def mark(self, side: str, index: int, clock: float, cd: float) -> bool:
        """玩家点了一下：没在冷却就从 clock 开始计 cd 秒，正在冷却就取消（点错了）。返回是不是开始计时"""
        key = (side, index)
        if (c := self.casts.get(key)) and c[0] + c[1] > clock:
            del self.casts[key]
            return False
        self.casts[key] = (clock, cd)
        return True

    def recool(self, side: str, index: int, cd: float) -> None:
        """改了等级 / 减冷却：正在冷却的按新冷却重算（从放的时候算起）"""
        if (c := self.casts.get((side, index))):
            self.casts[(side, index)] = (c[0], cd)

    def end(self, side: str, index: int, clock: float) -> float | None:
        """冷却结束的游戏时钟；好了超过 READY_SHOW 秒的不算（变回技能名）"""
        c = self.casts.get((side, index))
        if c is None:
            return None
        e = c[0] + c[1]
        return e if clock < e + READY_SHOW else None

    def reset(self) -> None:
        self.casts.clear()
        self.levels.clear()
        self.mods.clear()
