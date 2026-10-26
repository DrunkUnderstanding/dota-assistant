"""野区计时提醒：封野、拉野（双拉）、屯野、抢莲花，到点前几秒在叠加层顶上弹一行，过了自己收起。

谁提醒什么：
- 四号位：开局封对面优势路的拉野点；之后每分钟屯野
- 五号位：开局看自家拉野点有没有被封；每分钟拉野（附双拉）和屯野
- 一 / 二 / 三号位：只有这个英雄这个位置同分段 10 分钟平均屯野 ≥ STACK_MIN_AVG 组才提醒屯野
  （STRATZ heroStats.campsStacked，2026-10-26 查：中单炸弹人 3.4、光法 2.8、天涯墨客 2.8、炼金 1.5、米波 1.1、
  修补匠 0.8、圣堂 0.7；一 / 三号位只有炼金过 0.5；辅助平均 0.5）
- 三 / 四 / 五号位：3:00、6:00 抢莲花（玩家 2026-10-26 要求的这两个时间点）。和 2:53 / 5:53 的屯野同时亮在一张卡片上：
  玩家原话「屯野和抢莲花都可以亮，是两种不同的选择而已，如果要说具体情况应该按照当时对线的优势劣势情况来分配」。
  对线优劣助手判断不了（GSI 只有自己的数据），卡片只写怎么按对线选，由玩家自己定

只看游戏时钟（玩家自己也看得到），不读别的东西。时间来源：
- 野怪 1:00 第一次刷，之后每个整分钟刷，刷新框里有单位（英雄 / 眼 / 召唤物）就不刷（Liquipedia / Fandom「Neutral Creeps」）
- 屯野 x:53~x:55 打一下往外拉（TeamSmurf 7.41d：小野 :53~:55，大野 :52~:54，远古 :51~:53）
- 拉野约 x:15~x:20（同一来源，只有这一个，不同野点 / 阵营差几秒，**没实测**；config.toml 的 pull_second 能调）
- 莲花池：上下路各一个，在那条路劣势路一塔旁边（上路天辉塔、下路夜魇塔，7.41 又往塔边挪了）；每 3 分钟整（3:00、6:00…）
  各长一朵治疗莲花（回 125 血 125 蓝），最多攒 6 朵，两边都看得见池子里有几朵；站进池子 350 范围、有空格子就自动拾取，
  第一朵 1.5 秒，对面英雄也在时进度倒退（dota2-tools.com「Lotus Pool Timer」、neznakov.com 莲花 / 前哨指南；加速模式 90 秒一朵，没管）
"""
from __future__ import annotations

from dataclasses import dataclass

FIRST_SPAWN = 60             # 野怪第一次刷（秒）
LEAD = 8                     # 提前几秒弹
LINGER = 3                   # 过了时间点再留几秒
LOTUS_LEAD = 15              # 莲花：要走过去、到点前站好，提前久一点
LOTUS_LINGER = 5
BLOCK_FROM = -60             # 封野：号角前 1 分钟弹，到第一次刷野收起
# 以下是实现假设（玩家未确认）
STACK_UNTIL = 12             # 屯野 / 拉野提醒到第几分钟（对线期）
PULL_UNTIL = 10
STACK_MIN_AVG = 0.5          # 核心位：同分段 10 分钟平均屯野 ≥ 这么多组才提醒
LOTUS_MINUTES = (3, 6)       # 抢莲花提醒哪几分钟（玩家指定 3、6 分钟）
LOTUS_POSITIONS = (3, 4, 5)  # 谁去抢：劣势路两人的池子就在自家一塔边上；五号位那边的池子在对面一塔边上
SUPPORTS = (4, 5)
LABELS = {"block": "封野", "stack": "屯野", "pull": "拉野", "lotus": "抢莲花"}


@dataclass(frozen=True)
class Cue:
    key: str                 # 种类:分钟，换了就是新的一条
    kind: str                # block / stack / pull / lotus
    title: str
    lines: tuple[tuple[str, str], ...]


@dataclass
class Plan:
    pos: int
    kinds: frozenset[str]
    stacks10: float | None = None    # 同分段这个英雄这个位置 10 分钟平均屯几组（没数据是 None）
    stack_second: int = 53
    pull_second: int = 15

    def cue(self, clock: float, muted=()) -> Cue | None:
        """游戏时钟 clock（秒，号角前是负数）这一刻该显示哪条；muted 是本局关掉的种类"""
        kinds = self.kinds - set(muted)
        if "block" in kinds and BLOCK_FROM <= clock < FIRST_SPAWN:
            return self._block()
        lotus = next((m for m in LOTUS_MINUTES if "lotus" in kinds
                      and m * 60 - LOTUS_LEAD <= clock <= m * 60 + LOTUS_LINGER), None)
        routine = None
        for kind, sec, until in (("stack", self.stack_second, STACK_UNTIL), ("pull", self.pull_second, PULL_UNTIL)):
            if kind not in kinds:
                continue
            m = int((clock + LEAD - sec) // 60)      # 最近一个还没过完的时间点是第几分钟的
            if 1 <= m < until and m * 60 + sec - LEAD <= clock <= m * 60 + sec + LINGER:
                routine = (kind, m)
                break
        if lotus and routine and routine[0] == "stack":
            return self._choice(lotus, routine[1])
        if lotus:
            return self._lotus(lotus)
        if routine:
            return self._stack(routine[1]) if routine[0] == "stack" else self._pull(routine[1])
        return None

    def _block(self) -> Cue:
        if self.pos == 4:
            lines = [("row", "1:00 第一次刷野前，站进对面优势路拉野点的刷新框（或插个眼），这个点就不刷"),
                     ("dim", "对面五号位拉不了野，兵线留在你们三号位这边；对面会拿真眼来扫")]
        else:
            lines = [("row", "1:00 看一眼自家拉野点刷没刷：没刷多半被对面封了（站人 / 插眼），带真眼去扫"),
                     ("dim", "有空也可以去封对面劣势路的野点，站进刷新框或插眼")]
        return Cue("block", "block", "封野 · 1:00 前", tuple(lines))

    def _lotus_row(self, m: int) -> str:
        if self.pos == 5:
            where = "你们这路的莲花池在对面一塔旁边，和对面三、四号位抢"
        else:
            where = "你们这路的莲花池在你们一塔旁边，站进池子就自动拾取（要空格子）"
        return f"{m}:00 莲花池长一朵治疗莲花（回 125 血 125 蓝）。{where}"

    def _lotus(self, m: int) -> Cue:
        lines = [("row", self._lotus_row(m))]
        if m == LOTUS_MINUTES[0]:
            lines.append(("dim", "对面英雄也站在池子里时进度倒退：先到先拿，或者把对面赶走；两边都看得见池子里有几朵"))
        else:
            lines.append(("dim", f"{LOTUS_MINUTES[0]}:00 那朵没人拿的话现在有 2 朵"))
        return Cue(f"lotus:{m}", "lotus", f"抢莲花 · {m}:00", tuple(lines))

    def _choice(self, lotus: int, m: int) -> Cue:
        """屯野和抢莲花同时到点：两个都亮，怎么选看对线（助手不判断）。种类记成 lotus：点 × 关的是抢莲花，屯野照常"""
        s = self.stack_second
        lines = [("row", f"屯野：{m}:{s}~{m}:{s + 2} 打一下野怪往外拉，{m + 1}:00 前全拉出刷新框"),
                 ("row", f"莲花：{self._lotus_row(lotus)}"),
                 ("dim", "两个都能做，按这会儿对线的优势劣势来分：线上吃紧、缺血缺蓝，莲花回状态更要紧；"
                         "线上稳、能离线一会儿，就屯一组给核心")]
        return Cue(f"choice:{lotus}", "lotus", f"屯野 {m}:{s} / 抢莲花 {lotus}:00", tuple(lines))

    def _stack(self, m: int) -> Cue:
        s = self.stack_second
        lines = [("row", f"{m}:{s}~{m}:{s + 2} 打一下野怪往外拉，{m + 1}:00 前全拉出刷新框（大野 / 远古早 1~2 秒）")]
        if m == 1:                                   # 第一张多说一句，之后只留一行
            if self.pos == 5:
                lines.append(("dim", f"屯拉野点的话，下一分钟 :{self.pull_second} 左右再拉过来，兵要打更久"))
            elif self.pos not in SUPPORTS and self.stacks10 is not None:
                lines.append(("dim", f"你这英雄这个位置同分段 10 分钟平均屯 {self.stacks10:.1f} 组"))
        return Cue(f"stack:{m}", "stack", f"屯野 · {m}:{s}", tuple(lines))

    def _pull(self, m: int) -> Cue:
        s = self.pull_second
        lines = [("row", f"大约 {m}:{s:02d}~{m}:{s + 5:02d} 打拉野点，把路过的兵引过去")]
        if m == 1:
            lines += [("dim", "兵还在打小野时把它们带到旁边的大野，就是双拉：这波兵回不了线，兵线退回你们塔前"),
                      ("dim", "兵线已经在自家塔下就先别拉")]
        return Cue(f"pull:{m}", "pull", f"拉野 · {m}:{s:02d}", tuple(lines))


def make(pos: int, bench: dict | None, stack_second: int = 53, pull_second: int = 15) -> Plan:
    """bench 是 coach.fetch_bench 的同分段数据（没拉到是 None：辅助照样提醒，核心位不提醒屯野）"""
    ref = ((bench or {}).get("per_min") or {}).get("10") or {}
    stacks10 = ref.get("stacks")
    kinds = set()
    if pos in SUPPORTS:
        kinds |= {"block", "stack"}
        if pos == 5:
            kinds.add("pull")
    elif stacks10 is not None and stacks10 >= STACK_MIN_AVG:
        kinds.add("stack")
    if pos in LOTUS_POSITIONS:
        kinds.add("lotus")
    return Plan(pos, frozenset(kinds), stacks10, stack_second, pull_second)
