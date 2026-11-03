"""野区计时提醒：封野、拉野（双拉）、屯野、抢莲花，到点前几秒在叠加层顶上弹一行，过了自己收起。

谁提醒什么：
- 四号位：开局封对面优势路的拉野点；之后每分钟屯野，劣势路拉野（拉对面野区边上那个大野）
- 五号位：开局看自家拉野点有没有被封；每分钟优势路拉野（小野 + 大野）和屯野
- 三号位：劣势路拉野（玩家 2026-11-03：「劣势路其实也要拉野，拉大野」；三号位也算进来是实现假设）
- 一 / 二 / 三号位：只有这个英雄这个位置同分段 10 分钟平均屯野 ≥ STACK_MIN_AVG 组才提醒屯野
  （STRATZ heroStats.campsStacked，2026-10-26 查：中单炸弹人 3.4、光法 2.8、天涯墨客 2.8、炼金 1.5、米波 1.1、
  修补匠 0.8、圣堂 0.7；一 / 三号位只有炼金过 0.5；辅助平均 0.5）
- 三 / 四 / 五号位：3:00、6:00 抢莲花（玩家 2026-10-26 要求的这两个时间点）。和 2:53 / 5:53 的屯野同时亮在一张卡片上：
  玩家原话「屯野和抢莲花都可以亮，是两种不同的选择而已，如果要说具体情况应该按照当时对线的优势劣势情况来分配」。
  对线优劣助手判断不了（GSI 只有自己的数据），卡片只写怎么按对线选，由玩家自己定

只看游戏时钟（玩家自己也看得到），不读别的东西。时间来源：
- 野怪 1:00 第一次刷，之后每个整分钟刷，刷新框里有单位（英雄 / 眼 / 召唤物）就不刷（Liquipedia / Fandom「Neutral Creeps」）
- 屯野 x:53~x:55 打一下往外拉（TeamSmurf 7.41d：小野 :53~:55，大野 :52~:54，远古 :51~:53）
- 拉野是一段时间（玩家 2026-11-03：「拉野应该有一个时间范围而不是一个时间点」）：每个野点、每个阵营各自的起止秒数
  来自游戏文件 creep_pull_timings.txt（camps.pull_plan，Dota 按住 Alt 显示的就是它），例如天辉优势路小野 :19~:21、
  大野 :27~:29，劣势路拉对面大野 :19~:20。每分钟两波兵、两次（:19 和 :49 这种，玩家 2026-11-03 要两次都提醒）。
  读不到游戏文件时退回 config 的 pull_second ~ +5 秒、每分钟一次、只提醒五号位
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
OFFLANE_PULLERS = (3, 4)     # 劣势路拉野提醒谁（三号位是实现假设，玩家 2026-11-03 确认要）
PULL_GROUP_GAP = 15          # 两次拉野隔不到这么多秒就合成一张卡片
LABELS = {"block": "封野", "stack": "屯野", "pull": "拉野", "lotus": "抢莲花"}
PULL_ROLES = {"small": "拉野点（小野）", "big": "大野", "off": "对面大野"}


@dataclass(frozen=True)
class Pull:
    """一次拉野：哪个野点（camps.CAMPS 的名字，没有游戏文件时是空）、哪种（small 自家小野 / big 自家大野 /
    off 劣势路对面野区边上的大野）、每分钟第几秒到第几秒拉"""
    camp: str
    role: str
    start: int
    end: int


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
    pulls: tuple[Pull, ...] = ()

    def cue(self, clock: float, muted=()) -> Cue | None:
        """游戏时钟 clock（秒，号角前是负数）这一刻该显示哪条；muted 是本局关掉的种类"""
        kinds = self.kinds - set(muted)
        if "block" in kinds and BLOCK_FROM <= clock < FIRST_SPAWN:
            return self._block()
        lotus = next((m for m in LOTUS_MINUTES if "lotus" in kinds
                      and m * 60 - LOTUS_LEAD <= clock <= m * 60 + LOTUS_LINGER), None)
        routine = None
        windows = [("stack", self.stack_second, self.stack_second, STACK_UNTIL, ())]
        for group in self.pull_groups():             # 挨着的几次拉野（小野 :19 + 大野 :27）合成一张：最早那次之前亮，最晚那次过完收起
            windows.append(("pull", group[0].start, max(p.end for p in group), PULL_UNTIL, group))
        active = []
        for kind, start, end, until, group in windows:
            if kind not in kinds:
                continue
            m = int((clock + LEAD - start) // 60)    # 最近一个还没过完的时间段是第几分钟的
            if 1 <= m < until and m * 60 + start - LEAD <= clock <= m * 60 + end + LINGER:
                due = [m * 60 + p.start for p in group if clock <= m * 60 + p.end] if group else \
                    ([m * 60 + start] if clock <= m * 60 + end else [])
                active.append((not due, min(due, default=0), (kind, m, group)))
        if active:                                   # 同时亮好几样（五号位 :49 / :57 拉野夹着 :53 屯野）：下一件要做的先到的那个
            routine = min(active, key=lambda a: a[:2])[2]
        if lotus and routine and routine[0] == "stack":
            return self._choice(lotus, routine[1])
        if lotus:
            return self._lotus(lotus)
        if routine:
            return self._stack(routine[1]) if routine[0] == "stack" else self._pull(routine[1], routine[2])
        return None

    def pull_groups(self) -> list[tuple[Pull, ...]]:
        """一分钟里的几次拉野按时间分组：前后隔不到 PULL_GROUP_GAP 秒的算一组（一张卡片）。
        游戏文件里每分钟两波兵（:19 和 :49 这种），所以通常是两组"""
        groups: list[list[Pull]] = []
        for p in sorted(self.pulls, key=lambda p: p.start):
            if groups and p.start - max(q.end for q in groups[-1]) <= PULL_GROUP_GAP:
                groups[-1].append(p)
            else:
                groups.append([p])
        return [tuple(g) for g in groups]

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
                pull = next((p for p in self.pulls if p.role == "small"), None)
                if pull:
                    lines.append(("dim", f"屯拉野点的话，下一分钟 :{pull.start:02d}~:{pull.end:02d} 再拉过来，兵要打更久"))
            elif self.pos not in SUPPORTS and self.stacks10 is not None:
                lines.append(("dim", f"你这英雄这个位置同分段 10 分钟平均屯 {self.stacks10:.1f} 组"))
        return Cue(f"stack:{m}", "stack", f"屯野 · {m}:{s}", tuple(lines))

    def _pull(self, m: int, group: tuple[Pull, ...]) -> Cue:
        what = {"small": "打一下自家拉野点（小野），把路过的自家兵引过去",
                "big": "拉旁边的大野：兵要打更久，这波回不了线",
                "off": "劣势路拉大野（对面野区边上那个）：把路过的自家兵引过去，兵线不往对面塔下推"}
        lines = [("row", f"{m}:{p.start:02d}~{m}:{p.end:02d} {what[p.role]}") for p in group]
        first = group[0]
        if m == 1 and first is self.pull_groups()[0][0]:
            if first.role == "off":
                lines.append(("dim", "大野在对面野区边上，对面辅助可能在附近；兵线已经在自家塔下就先别拉"))
            else:
                lines.append(("dim", "兵线已经在自家塔下就先别拉"))
        title = "劣势路拉野" if first.role == "off" else "拉野"
        when = f"{m}:{first.start:02d}~{m}:{first.end:02d}" if len(group) == 1 else " / ".join(f"{m}:{p.start:02d}" for p in group)
        return Cue(f"pull:{m}:{first.start}", "pull", f"{title} · {when}", tuple(lines))


def make(pos: int, bench: dict | None, stack_second: int = 53, pull_second: int = 15,
         pulls: tuple[Pull, ...] | None = None) -> Plan:
    """bench 是 coach.fetch_bench 的同分段数据（没拉到是 None：辅助照样提醒，核心位不提醒屯野）。
    pulls 是 camps.pull_plan 按游戏文件给的这个位置该拉哪几次；None（读不到游戏文件 / 不知道阵营）时
    五号位按 pull_second ~ +5 秒拉自家拉野点，别的位置不提醒拉野"""
    if pulls is None:
        pulls = (Pull("", "small", pull_second, pull_second + 5),) if pos == 5 else ()
    ref = ((bench or {}).get("per_min") or {}).get("10") or {}
    stacks10 = ref.get("stacks")
    kinds = set()
    if pos in SUPPORTS:
        kinds |= {"block", "stack"}
    elif stacks10 is not None and stacks10 >= STACK_MIN_AVG:
        kinds.add("stack")
    if pos in LOTUS_POSITIONS:
        kinds.add("lotus")
    if pulls:
        kinds.add("pull")
    return Plan(pos, frozenset(kinds), stacks10, stack_second, pull_second, tuple(pulls))
