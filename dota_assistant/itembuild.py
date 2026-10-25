"""出装推荐：这局按什么顺序出、下一件买什么、还差多少钱。

- 骨架：STRATZ 同分段、同英雄、同位置的核心装（≥40% 的局会买、≥1500 金），按中位购买时间排
- 针对这局：coach.plan() 的对抗装（黑皇杖 / 金箍棒 / 林肯 / 白银之锋……，知识库原文对原文），
  排在它在同分段的中位购买时间；黑皇杖、金箍棒以外的只推这个英雄会出的
- 之后常见：15%~40% 的局会买的，补几件在后面
- 下一件：顺序里第一件还没有的（合成后的也算有）；还差多少 = 成品价 - 已有零件价 - 身上的钱
- 按死法补装（death_pick）：死亡复盘（death.py）看出是被控死 / 瞬间打爆 / 跑不掉……对应一件装备；
  同一种死法本局超过两次（第 3 次起）才推，推的时候直接提到「下一件」最前面

不按胜率排：买得晚的大件（蝴蝶、撒旦）胜率天然高——买得起说明这局已经领先，不能说明装备本身好。
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from . import coach, kb

# 以下阈值是实现假设（玩家未确认），看回放和反馈再调
COMMON_SHARE = 0.15          # 同分段 15%~40% 的局会买的算「之后常见」
COMMON_MAX = 2               # 「之后常见」最多补几件
NEXT_SHOW = 2                # 「下一件」后面再列几件
DEATH_MIN = 3                # 同一种死法本局第几次起才推对应装备（玩家 2026-10-25：超过两次才推）
DEATH_SILENCE = 2            # 被沉默至少几秒才算「被沉默死」
DEATH_HARD = 2               # 没归到别的死法（瞬间打爆 / 残血拖太久…）时，眩晕 / 妖术至少几秒也按被控处理


@dataclass
class Step:
    key: str                  # OpenDota / Valve 装备 key（去掉 item_）
    name: str
    minute: float             # 同分段中位购买时间；针对装买的人太少时是开始提醒的时间
    kind: str                 # core 核心 / counter 针对这局 / common 之后常见 / death 按死法补的
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
    name = f"{s.name}（针对）" if s.kind == "counter" else f"{s.name}（{s.why}）" if s.kind == "death" else s.name
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
    mark = {"core": "", "counter": "★", "common": "·", "death": "✚"}
    lines = [("row", " → ".join(f"{mark[s.kind]}{s.name} {s.minute:.0f}′" for s in b.steps))]
    lines += [("warn" if s.urgent else "row", f"★{s.name}：{s.why}", s.quote) for s in b.steps if s.kind == "counter"]
    lines.append(("dim", "′ = 同分段中位购买时间；无标记 = 核心（≥40% 的局买）；★ = 针对这局阵容；· = 之后常见"))
    return lines


# ---------- 按死法补装 ----------

@dataclass(frozen=True)
class DeathRule:
    """一种死法 → 能应对的装备。装备那句原文（正则）必须在当前版本说明里找得到；
    只从这个英雄同分段会出的（≥ coach.MENU_SHARE）里挑，by_share = 挑买得多的，否则按列表顺序挑第一件"""
    key: str                                         # 死法，同一个 key 的规则一起数次数
    label: str                                       # 「本局第 3 次被控死」
    test: Callable                                   # death.Recap → bool
    items: tuple[tuple[str, str, str], ...]          # (装备 key, 说明里要有的原文, 理由)
    by_share: bool = False


def _hard(rec) -> float:
    return rec.secs.get("stunned", 0) + rec.secs.get("hexed", 0)


DEATH_RULES = (
    # 被控死，但黑皇杖是好的、还没开就被控住了：再出一件黑皇杖没用（和下一条算同一种死法）
    DeathRule("control", "被控死", lambda r: "black_king_bar" in r.saves.get("blocked", []), (
        ("sphere", r"抵挡.{0,6}指向性技能", "林肯法球被动挡一次指向性技能，给你开黑皇杖的时间"),
        ("aeon_disk", r"生命值低于70%.{0,12}强驱散", "永恒之盘低血时自动强驱散，伤害归零"),
    )),
    DeathRule("control", "被控死", lambda r: (r.cause == "control" or not r.cause and _hard(r) >= DEATH_HARD)
              and r.immune < 0.5, (
        ("black_king_bar", r"获得减益免疫", "黑皇杖开了以后控不住你"),
        ("sphere", r"抵挡.{0,6}指向性技能", "林肯法球被动挡一次指向性技能"),
        ("aeon_disk", r"生命值低于70%.{0,12}强驱散", "永恒之盘低血时自动强驱散，伤害归零"),
    )),
    # 锁闭（muted）时物品也用不了，那就不是幻影斧能解决的
    DeathRule("silence", "被沉默死", lambda r: r.secs.get("silenced", 0) >= DEATH_SILENCE and r.secs.get("muted", 0) < 0.5
              and r.cause != "control", (
        ("manta", r"移除多种负面效果", "幻影斧能移除多种负面效果（能驱散的沉默）"),
        ("disperser", r"驱散佩戴者", "散魂剑能驱散自己（能驱散的沉默）"),
        ("black_king_bar", r"施加弱驱散", "黑皇杖开启时弱驱散并获得减益免疫"),
    )),
    DeathRule("burst", "被瞬间打爆", lambda r: r.cause == "burst", (
        ("aeon_disk", r"生命值低于70%.{0,12}强驱散", "永恒之盘低血时自动强驱散，伤害归零"),
        ("glimmer_cape", r"吸收最多\d+点伤害的魔法护盾", "微光披风给魔法护盾并隐身"),
    )),
    # 残血拖太久 / 一直没脱离：缺跑路装。跳刀挨打 3 秒内用不了，不推
    DeathRule("escape", "残血跑不掉", lambda r: r.cause in ("low", "chased"), (
        ("force_staff", r"推进600距离", "原力法杖把自己推走"),
        ("hurricane_pike", r"推进600距离", "飓风长戟把自己推走"),
        ("glimmer_cape", r"进入隐身状态", "微光披风隐身跑"),
        ("invis_sword", r"进入隐身", "影刃隐身跑"),
        ("silver_edge", r"进入隐身", "白银之锋隐身跑"),
    ), by_share=True),
)


@dataclass
class DeathPick:
    key: str                  # 装备
    name: str
    why: str
    quote: str                # 装备说明里的原文


def death_cause(rec) -> DeathRule | None:
    """这次是什么死法（第一条匹配的规则）；看不出来返回 None"""
    return next((r for r in DEATH_RULES if r.test(rec)), None)


def death_pick(rec, owned: set[str], timings: dict, base: kb.KnowledgeBase, tree: dict[str, set[str]]) -> DeathPick | None:
    """这种死法该补什么装备（同一死法的规则依次找，第一条没得推再看下一条）。
    保命装备 / 技能当时是好的却没用上（魔杖除外），先说操作，不推装备。
    timings 为空（没有同分段数据）时不按「会不会出」筛"""
    cause = death_cause(rec)
    if cause is None or [k for k in rec.saves.get("unused", []) if k not in ("magic_wand", "magic_stick")]:
        return None
    for rule in DEATH_RULES:
        if rule.key != cause.key or not rule.test(rec):
            continue
        menu = []
        for i, (k, pattern, why) in enumerate(rule.items):
            e = base.items.get(k)
            if e is None or coach.has(owned, k, tree):
                continue
            proof = next((x.strip() for x in re.split(r"(?<=[。！；\n])", e.full_text()) if re.search(pattern, x)), None)
            share = timings.get(base.item_ids.get(f"item_{k}"), (0, 0))[0]
            if proof and (not timings or share >= coach.MENU_SHARE):
                menu.append((-share if rule.by_share else i, k, why, proof))
        if menu:
            _, k, why, proof = min(menu)
            return DeathPick(k, base.items[k].name, why, proof)
    return None


def death_sim(cfg, paths: list) -> str:
    """拿 shots/deaths/ 里存下的真实死亡推送重放：每次死亡会说什么、按死法补什么。
    位置用这个英雄在本分段最常打的位置（推送里没有）；同一场（matchid）的死亡按顺序累计次数"""
    import json
    from pathlib import Path
    from . import data, death
    base = kb.KnowledgeBase.load()
    if base is None:
        return "还没有知识库，先运行 python -m dota_assistant kb update"
    positions = data.load_positions(cfg) or {}
    const = coach.items_const()
    tree = coach.item_tree(const)
    coaches: dict[str, coach.Coach] = {}
    L = []
    for f in sorted(Path(x) for x in paths):
        raw = json.loads(f.read_text(encoding="utf-8"))
        history = [(x.pop("t"), x) for x in raw["pushes"]]
        last = history[-1][1]
        hero = base.heroes.get(int((last.get("hero") or {}).get("id") or 0))
        if hero is None:
            continue
        names, saves = death.names_from_kb(base, hero.key)
        rec = death.analyze(history, (last.get("player") or {}).get("team_name"), names, saves)
        if rec is None:
            continue
        match = str((last.get("map") or {}).get("matchid") or f.stem)
        if match not in coaches:
            pos = max(positions.get(hero.id, {1: (1, 0)}).items(), key=lambda kv: kv[1][0])[0]
            bench = coach.fetch_bench(cfg, hero.id, pos) if cfg.stratz_token else None
            coaches[match] = coach.Coach([], bench, tree, base, make([], bench, base), const)
            L.append(f"\n比赛 {match}：{hero.name}（按 {pos} 号位）")
        c = coaches[match]
        gold = (history[-2][1].get("player") or {}).get("gold") if len(history) > 1 else None
        st = coach.State(rec.clock / 60, rec.owned, gold=gold)
        L.append(f"  {rec.title}　{rec.lines[0][1] if rec.lines else ''}")
        L += [f"    → {t}" for _, t in c.on_death(rec, st)]
        if nxt := c.next_line(st):
            L.append(f"    → {nxt}")
    return "\n".join(L).strip()
