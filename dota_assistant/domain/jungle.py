"""野区计时提醒：封野、拉野（双拉）、屯野，到点前几秒在叠加层顶上弹一行，过了自己收起；Dota 时钟正下方一块倒数（Plan.timeline，web/topbar.html）。

谁提醒什么：
- 四号位：开局封对面优势路的拉野点；之后每分钟屯野，劣势路拉野（拉对面野区边上那个大野）
- 五号位：开局看自家拉野点有没有被封；每分钟优势路拉野（小野 + 大野）和屯野
- 三号位：劣势路拉野（玩家 2026-11-03：「劣势路其实也要拉野，拉大野」；三号位也算进来是实现假设）
- 一 / 二 / 三号位：只有这个英雄这个位置同分段 10 分钟平均屯野 ≥ STACK_MIN_AVG 组才提醒屯野
  （STRATZ heroStats.campsStacked，2026-10-26 查：中单炸弹人 3.4、光法 2.8、天涯墨客 2.8、炼金 1.5、米波 1.1、
  修补匠 0.8、圣堂 0.7；一 / 三号位只有炼金过 0.5；辅助平均 0.5）
- 抢莲花不提醒了（玩家 2026-11-03：「莲花提示也可以去掉」；原来三 / 四 / 五号位 3:00、6:00 提醒，见 HANDOFF 第 27 节）

只看游戏时钟（玩家自己也看得到），不读别的东西。时间来源：
- 野怪 1:00 第一次刷，之后每个整分钟刷，刷新框里有单位（英雄 / 眼 / 召唤物）就不刷（Liquipedia / Fandom「Neutral Creeps」）
- 屯野 x:53~x:55 打一下往外拉（TeamSmurf 7.41d：小野 :53~:55，大野 :52~:54，远古 :51~:53）
- 拉野是一段时间（玩家 2026-11-03：「拉野应该有一个时间范围而不是一个时间点」）：每个野点、每个阵营各自的起止秒数
  来自游戏文件 creep_pull_timings.txt（camps.pull_plan，Dota 按住 Alt 显示的就是它），例如天辉优势路小野 :19~:21、
  大野 :27~:29，劣势路拉对面大野 :19~:20。每分钟两波兵、两次（:19 和 :49 这种，玩家 2026-11-03 要两次都提醒）。
  读不到游戏文件时退回 config 的 pull_second ~ +5 秒、每分钟一次、只提醒五号位
"""
from __future__ import annotations

from dataclasses import dataclass

FIRST_SPAWN = 60             # 野怪第一次刷（秒）
LEAD = 8                     # 提前几秒弹
LINGER = 3                   # 过了时间点再留几秒
BLOCK_FROM = -60             # 封野：号角前 1 分钟弹，到第一次刷野收起
# 以下是实现假设（玩家未确认）
STACK_UNTIL = 12             # 屯野 / 拉野提醒到第几分钟（对线期）
PULL_UNTIL = 10
STACK_MIN_AVG = 0.5          # 核心位：同分段 10 分钟平均屯野 ≥ 这么多组才提醒
SUPPORTS = (4, 5)
OFFLANE_PULLERS = (3, 4)     # 劣势路拉野提醒谁（三号位是实现假设，玩家 2026-11-03 确认要）
PULL_GROUP_GAP = 15          # 两次拉野隔不到这么多秒就合成一张卡片
# 顶栏倒数（玩家 2026-11-04：打起来不看叠加层、不要声音、高频截图怕 VAC）：只靠游戏时钟，在 Dota 时钟正下方挂一小块「下一件事 + 几秒」
BAR_WORDS = {"small": "拉野", "big": "拉大野", "off": "拉大野"}
LABELS = {"block": "封野", "stack": "屯野", "pull": "拉野"}
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
    kind: str                # block / stack / pull
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
        if not active:
            return None
        kind, m, group = min(active, key=lambda a: a[:2])[2]   # 同时亮好几样（五号位 :49 / :57 拉野夹着 :53 屯野）：下一件要做的先到的那个
        return self._stack(m) if kind == "stack" else self._pull(m, group)

    def timeline(self, muted=()) -> list[tuple[int, int, str, str]]:
        """这局对线期要做的野区动作 [(开始, 结束, 种类, 词)]（游戏时钟秒），按开始排。顶栏那块倒数用：
        整局一次给全，页面按时钟自己挑下一件（快照不用每秒变）。封野：四号位 1:00 刷野前站进去；五号位 1:00 刷野后看自家拉野点"""
        kinds = self.kinds - set(muted)
        out = []
        if "block" in kinds:
            out.append((FIRST_SPAWN, FIRST_SPAWN, "block", "封野") if self.pos == 4
                       else (FIRST_SPAWN, FIRST_SPAWN + 5, "block", "看拉野点"))
        if "stack" in kinds:
            s = self.stack_second
            out += [(m * 60 + s, m * 60 + s + 2, "stack", "屯野") for m in range(1, STACK_UNTIL)]
        if "pull" in kinds:
            out += [(m * 60 + p.start, m * 60 + p.end, "pull", BAR_WORDS[p.role]) for m in range(1, PULL_UNTIL) for p in self.pulls]
        return sorted(out)

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
    if pulls:
        kinds.add("pull")
    return Plan(pos, frozenset(kinds), stacks10, stack_second, pull_second, tuple(pulls))
