"""出装推荐：这局按什么顺序出、下一件买什么、还差多少钱。

- 骨架：STRATZ 同分段、同英雄、同位置的核心装（≥40% 的局会买、≥1500 金），按中位购买时间排
- 针对这局：coach.plan() 的对抗装（黑皇杖 / 金箍棒 / 林肯 / 白银之锋……，知识库原文对原文），
  排在它在同分段的中位购买时间；黑皇杖、金箍棒以外的只推这个英雄会出的
- 之后常见：15%~40% 的局会买的，补几件在后面
- 下一件：顺序里第一件还没有的（合成后的也算有）；还差多少 = 成品价 - 已有零件价 - 身上的钱

不按胜率排：买得晚的大件（蝴蝶、撒旦）胜率天然高——买得起说明这局已经领先，不能说明装备本身好。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from . import coach, kb

# 以下阈值是实现假设（玩家未确认），看回放和反馈再调
COMMON_SHARE = 0.15          # 同分段 15%~40% 的局会买的算「之后常见」
COMMON_MAX = 2               # 「之后常见」最多补几件
NEXT_SHOW = 2                # 「下一件」后面再列几件


@dataclass
class Step:
    key: str                  # OpenDota / Valve 装备 key（去掉 item_）
    name: str
    minute: float             # 同分段中位购买时间；针对装买的人太少时是开始提醒的时间
    kind: str                 # core 核心 / counter 针对这局 / common 之后常见
    why: str = ""             # 针对装的理由
    quote: str = ""           # 依据原文（界面上悬停看）
    share: float = 0          # 同分段多少比例的局会买
    urgent: bool = False      # 黑皇杖这类优先级最高的针对装


@dataclass
class Build:
    steps: list[Step] = field(default_factory=list)

    def todo(self, owned: set[str], tree: dict[str, set[str]]) -> list[Step]:
        return [s for s in self.steps if not coach.has(owned, s.key, tree)]


def make(needs: list[coach.Need], bench: dict | None, base: kb.KnowledgeBase) -> Build:
    timings = coach.item_timings(bench) if bench else {}

    def stat(k: str) -> tuple[float, float]:
        return timings.get(base.item_ids.get(f"item_{k}"), (0, 0))

    steps: dict[str, Step] = {}
    common = []
    for k, e in base.items.items():
        share, med = stat(k)
        if e.cost < coach.CORE_MIN_COST or k.startswith("recipe") or e.neutral_tier:
            continue
        if share >= coach.CORE_SHARE:
            steps[k] = Step(k, e.name, med, "core", share=share)
        elif share >= COMMON_SHARE:
            common.append(Step(k, e.name, med, "common", share=share))
    for n in needs:
        k = next((k for k in n.items if k in base.items), None)
        if k is None:
            continue
        why, quote = n.brief or n.text, "\n".join(n.evidence)
        if k in steps:                       # 本来就是核心装：标成针对，带上理由
            s = steps[k]
            s.kind, s.why, s.quote, s.urgent = "counter", why, quote, n.priority == 0
            s.minute = n.minute or s.minute
            continue
        share, med = stat(k)
        minute = n.minute or (med if share >= coach.MENU_SHARE and med else n.due)
        steps[k] = Step(k, base.items[k].name, minute, "counter", why, quote, share, n.priority == 0)
    for s in sorted(common, key=lambda s: -s.share):
        if sum(x.kind == "common" for x in steps.values()) >= COMMON_MAX:
            break
        if s.key not in steps:
            steps[s.key] = s
    # 「之后常见」排在核心和针对装后面：不是必出的，别让它挡住「下一件」
    return Build(sorted(steps.values(), key=lambda s: (s.kind == "common", s.minute)))


def remaining_cost(key: str, owned: set[str], const: dict) -> int:
    """合成 key 还要花多少钱：已有的零件（递归找）不用再买，每件只抵一次"""
    pool = Counter(owned)

    def need(k: str) -> int:
        if pool[k] > 0:
            pool[k] -= 1
            return 0
        it = const.get(k) or {}
        comps = it.get("components") or []
        cost = int(it.get("cost") or 0)
        if not comps:
            return cost
        recipe = cost - sum(int((const.get(c) or {}).get("cost") or 0) for c in comps)
        return max(0, recipe) + sum(need(c) for c in comps)
    return need(key)


def next_line(b: Build, owned: set[str], gold: int | None, tree: dict[str, set[str]], const: dict) -> str | None:
    """「下一件：黑皇杖，还差 850 金；之后 蝴蝶、白银之锋」"""
    todo = b.todo(owned, tree)
    if not todo:
        return None
    s = todo[0]
    left = remaining_cost(s.key, owned, const)
    name = f"{s.name}（针对）" if s.kind == "counter" else s.name
    if gold is None:
        money = f"还要 {left} 金"
    elif gold >= left:
        money = f"钱够了（要 {left}，身上 {gold}）"
    else:
        money = f"还差 {left - gold} 金"
    after = "、".join(x.name for x in todo[1:1 + NEXT_SHOW])
    return f"下一件：{name}，{money}" + (f"；之后 {after}" if after else "")


def brief_lines(b: Build) -> list[tuple]:
    """本局要点「出装」一节：顺序一行 + 每件针对装的理由（悬停看原文）"""
    if not b.steps:
        return []
    mark = {"core": "", "counter": "★", "common": "·"}
    lines = [("row", " → ".join(f"{mark[s.kind]}{s.name} {s.minute:.0f}′" for s in b.steps))]
    lines += [("warn" if s.urgent else "row", f"★{s.name}：{s.why}", s.quote) for s in b.steps if s.kind == "counter"]
    lines.append(("dim", "′ = 同分段中位购买时间；无标记 = 核心（≥40% 的局买）；★ = 针对这局阵容；· = 之后常见"))
    return lines
