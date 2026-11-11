"""出装推荐：这局按什么顺序出、下一件买什么、还差多少钱。

- 骨架：STRATZ 同分段、同英雄、同位置的核心装（≥40% 的局会买、≥1500 金），按中位购买时间排
- 针对这局：coach.plan() 的对抗装（黑皇杖 / 金箍棒 / 林肯 / 白银之锋……，知识库原文对原文）。
  还没有「出了更容易赢」的大数据（Need.tip 非空）的只作提示（Build.tips），不进顺序、不当「下一件」；
  以后有数据支持的（tip 为空）才排进顺序，排在它在同分段的中位购买时间
- 之后常见：15%~40% 的局会买的，补几件在后面
- 下一件：顺序里第一件还没有的（合成后的也算有）；还差多少 = 成品价 - 已有零件价 - 身上的钱
- 按死法补装（death_pick）：死亡复盘（death.py）看出是被控死 / 瞬间打爆 / 跑不掉……对应一件装备；
  同一种死法本局超过两次（第 3 次起）才说，同样只作提示（coach.Coach.on_death）

不按胜率排：买得晚的大件（蝴蝶、撒旦）胜率天然高——买得起说明这局已经领先，不能说明装备本身好。

界面上装备显示成图标（data/items/<key>.png，assets.fetch_item_icons）：带装备的一行写成「片段」列表（parts），
str 原样显示，chip() 是一件装备，group() 是并列的几件；text() 拼回文字版（语音 / 日志 / 命令行 / 给 AI 的资料）。
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from . import coach
from .. import kb
from ..sources import assets

# 以下阈值是实现假设（玩家未确认），看回放和反馈再调
COMMON_SHARE = 0.15          # 同分段 15%~40% 的局会买的算「之后常见」
COMMON_MAX = 2               # 「之后常见」最多补几件
NEXT_SHOW = 2                # 「下一件」后面再列几件
DONE_SHOW = 4                # 「下一件」卡片上列几件已经出了的（灰掉打勾，只在界面上显示）
DEATH_MIN = 3                # 同一种死法本局第几次起才推对应装备（玩家 2026-10-25：超过两次才推）
DEATH_SILENCE = 2            # 被沉默至少几秒才算「被沉默死」
DEATH_HARD = 2               # 没归到别的死法（瞬间打爆 / 残血拖太久…）时，眩晕 / 妖术至少几秒也按被控处理
BUYBACK_FROM = 30            # 第几分钟起「下一件」要提醒买了还够不够买活（和本局要点「后期」那页同一时间）


# ---------- 一行里的装备（界面显示图标）----------

MARK = {"core": "", "counter": "★", "common": "·", "death": "✚", "avoid": "✗", "got": "✓"}


def chip(key: str, name: str, sub: str = "", mark: str = "", pct: float | None = None, big: bool = False) -> dict:
    """一件装备：界面上是图标（悬停看名字），sub 是小字（15′、×3），mark 是 MARK 里的种类；
    pct = 同分段多少比例的局会买（0~1，只在界面上显示在图标下面，文字版不写）；big = 界面上放大（「下一件」）"""
    return {"item": key, "name": name, **({"sub": sub} if sub else {}), **({"mark": mark} if MARK.get(mark) else {}),
            **({"pct": round(pct * 100)} if pct else {}), **({"big": True} if big else {})}


def group(chips: list[dict]) -> dict:
    """并列的几件：界面上图标挨着排，文字版用顿号隔开"""
    return {"group": chips}


def tiles(chips: list[dict]) -> dict:
    """出装顺序：界面上一排大图标、每件下面写时间和购买比例；文字版「A 10′ → B 15′」"""
    return {"tiles": chips}


def hero(h, ab=None) -> dict:
    """对面 / 我方英雄头像（kb.HeroKB），带技能（kb.Entry）就在头像后面接技能图标；文字版「帕吉的肢解」"""
    return {"hero": h.id, "name": h.name, **({"ab": ab.key, "ab_name": ab.name} if ab is not None else {})}


def ability(ab) -> dict:
    """单独一个技能图标（kb.Entry），文字版是技能名"""
    return {"ab": ab.key, "name": ab.name}


def tag(t: str, tone: str = "") -> dict:
    """几个字的小标签；tone：good 绿 / bad 红 / dim 灰 / 空 = 普通"""
    return {"tag": t, **({"tone": tone} if tone else {})}


def bars(values: list[tuple[str, float]]) -> dict:
    """几根正负柱子（强势期）：[(标签, 数值)]；文字版「标签 +1.3」"""
    return {"bars": [{"label": label, "v": round(v, 1)} for label, v in values]}


def meter(label: str, value: float, ref: float) -> dict:
    """进度条：value 对 ref（补刀 120 / 196）；文字版「补刀 120 / 196」"""
    return {"meter": {"label": label, "v": round(value), "ref": round(ref)}}


def hero_abs(pairs) -> list[dict]:
    """[(英雄, 技能)] → 头像 + 技能图标；同一个英雄几个技能只放一次头像（凯三个技能都无视免疫）"""
    out, seen = [], set()
    for h, ab in pairs:
        out.append(ability(ab) if h.id in seen and ab is not None else hero(h, ab))
        seen.add(h.id)
    return out


class Note(str):
    """一句提示（开局提示 / 「对面要防」）：本身就是那句话（str，原来的用法都不变），另外带卡片上的精简图标 icons"""
    icons: list


def note(t: str, icons: list) -> Note:
    n = Note(t)
    n.icons = icons
    return n


def short(full, icons: list) -> dict:
    """一行的两种说法：卡片上显示 icons（图标 + 几个字），悬停看 full 整句；文字版（语音 / 日志 / 命令行 / 给 AI）用 full。
    玩家 2026-11-10：局内叠加层要像 gg-go-next 那样少字多图"""
    return {"short": icons, "full": full}


def text(t) -> str:
    """片段列表 → 文字版；本来就是 str 的原样返回"""
    if isinstance(t, str):
        return t
    if isinstance(t, dict):
        if "short" in t:
            return text(t["full"])
        if "group" in t:
            return "、".join(text(x) for x in t["group"])
        if "tiles" in t:
            return " → ".join(text(x) for x in t["tiles"])
        if "hero" in t:
            return t["name"] + (f"的{t['ab_name']}" if t.get("ab_name") else "")
        if "tag" in t:
            return t["tag"]
        if "bars" in t:
            return "　".join(f"{b['label']} {b['v']:+.1f}" for b in t["bars"])
        if "meter" in t:
            m = t["meter"]
            return f"{m['label']} {m['v']} / {m['ref']}"
        if "item" not in t:
            return t["name"]                     # 单独的技能
        return MARK.get(t.get("mark", ""), "") + t["name"] + (f" {t['sub']}" if t.get("sub") else "")
    return "".join(text(x) for x in t)


def line_view(t) -> dict:
    """界面用的一行：{"text": 文字版}，有图标的再带 {"parts": 片段}；short() 的那种显示精简版，
    "short": True 告诉界面把整句（text）放进悬停提示"""
    if isinstance(t, str):
        if getattr(t, "icons", None):            # Note：一句话 + 精简图标
            return {"text": str(t), "parts": t.icons, "short": True}
        return {"text": str(t)}
    if isinstance(t, dict) and "short" in t:
        return {"text": text(t["full"]), "parts": t["short"], "short": True}
    return {"text": text(t), "parts": t if isinstance(t, list) else [t]}


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
    who: list = field(default_factory=list)      # 针对谁：头像 + 技能图标片段（coach.Need.who）
    label: str = ""           # 卡片上的几个字（coach.Need.label）
    stat: str = ""            # 验证过的大数据数字（coach.Need.stat），只有它会在卡片上写胜率


@dataclass
class Build:
    steps: list[Step] = field(default_factory=list)
    tips: list[Step] = field(default_factory=list)      # 只作提示的针对装（缺大数据依据），不进顺序

    def todo(self, owned: set[str], tree: dict[str, set[str]]) -> list[Step]:
        return [s for s in self.steps if not coach.has(owned, s.key, tree)]


def make(needs: list[coach.Need], bench: dict | None, base: kb.KnowledgeBase) -> Build:
    timings = coach.item_timings(bench) if bench else {}

    def stat(k: str) -> tuple[float, float]:
        return timings.get(base.item_ids.get(f"item_{k}"), (0, 0))

    steps: dict[str, Step] = {}
    tips: list[Step] = []
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
        share, med = stat(k)
        extra = {"who": n.who, "label": n.label, "stat": n.stat}
        if n.avoid:                          # 「别买」（coach.AVOID_TIP）：从顺序里拿掉，列成 ✗ 提示
            steps.pop(k, None)
            common = [s for s in common if s.key != k]
            tips.append(Step(k, base.items[k].name, med or 0, "avoid", why, quote, share, **extra))
            continue
        if n.tip:
            minute = n.minute or (med if share >= coach.MENU_SHARE and med else n.due)
            tips.append(Step(k, base.items[k].name, minute, "counter", why, quote, share, n.priority == 0, **extra))
            continue
        if k in steps:                       # 本来就是核心装：标成针对，带上理由
            s = steps[k]
            s.kind, s.why, s.quote, s.urgent = "counter", why, quote, n.priority == 0
            s.minute = n.minute or s.minute
            s.who, s.label, s.stat = n.who, n.label, n.stat
            continue
        minute = n.minute or (med if share >= coach.MENU_SHARE and med else n.due)
        steps[k] = Step(k, base.items[k].name, minute, "counter", why, quote, share, n.priority == 0, **extra)
    for s in sorted(common, key=lambda s: -s.share):
        if sum(x.kind == "common" for x in steps.values()) >= COMMON_MAX:
            break
        if s.key not in steps:
            steps[s.key] = s
    # 「之后常见」排在核心和针对装后面：不是必出的，别让它挡住「下一件」
    return Build(sorted(steps.values(), key=lambda s: (s.kind == "common", s.minute)),
                 sorted(tips, key=lambda s: s.minute))


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


def sell_pick(slots: list[str] | None, next_key: str, tree: dict[str, set[str]], const: dict) -> tuple[str, int] | None:
    """身上 6 格满了，买下一件之前卖哪件：价值（买价）最低的那件（玩家 2026-10-26：「六个格子满了一般会卖掉价值最小的那件」）。
    身上有下一件的零件就不用卖（合成会空出格子）。吃掉 / 用掉的消耗品（树枝除外）和没有买价的（不朽之守护）不算——
    这条是实现假设，玩家没确认。返回 (装备 key, 买价)"""
    if not slots or len(slots) < 6 or not all(slots):
        return None
    if any(next_key in tree.get(k, ()) for k in slots):
        return None
    menu = []
    for k in slots:
        it = const.get(k) or {}
        cost = int(it.get("cost") or 0)
        if cost > 0 and (it.get("qual") != "consumable" or k == "branches"):
            menu.append((cost, k))
    if not menu:
        return None
    cost, k = min(menu)
    return k, cost


def next_line(b: Build, owned: set[str], gold: int | None, tree: dict[str, set[str]], const: dict,
              slots: list[str] | None = None, buyback: tuple[int, float] | None = None, minute: float = 0,
              names: dict[str, str] | None = None) -> str | None:
    """next_parts 的文字版"""
    parts = next_parts(b, owned, gold, tree, const, slots, buyback, minute, names)
    return text(parts) if parts else None


def next_parts(b: Build, owned: set[str], gold: int | None, tree: dict[str, set[str]], const: dict,
               slots: list[str] | None = None, buyback: tuple[int, float] | None = None, minute: float = 0,
               names: dict[str, str] | None = None) -> dict | None:
    """「下一件：黑皇杖，还差 850 金；之后 蝴蝶、白银之锋」。返回 short()：卡片上是放大的下一件 + 「还差 850」+ 之后两件小图标
    + 已经出了的（灰掉打勾），整句在悬停 / 文字版里。
    格子满了加一句先卖哪件；BUYBACK_FROM 分钟后钱够了但买完不够买活，也说一声"""
    todo = b.todo(owned, tree)
    if not todo:
        return None
    s = todo[0]
    left = remaining_cost(s.key, owned, const)
    why = "（针对）" if s.kind == "counter" else f"（{s.why}）" if s.kind == "death" else ""
    no_bb = False
    if gold is None:
        money, tip = f"还要 {left} 金", tag(f"要 {left}")
    elif gold >= left:
        money, tip = f"钱够了（要 {left}，身上 {gold}）", tag("钱够了", "good")
        if buyback and minute >= BUYBACK_FROM and buyback[1] <= 0 and gold - left < buyback[0]:
            money += f"，但买完只剩 {gold - left}，不够买活（{buyback[0]}）"
            no_bb = True
    else:
        money, tip = f"还差 {left - gold} 金", tag(f"还差 {left - gold}")
    parts = ["下一件：", chip(s.key, s.name), f"{why}，{money}"]
    icons = [chip(s.key, s.name, mark="counter" if s.kind == "counter" else "", big=True), tag("下一件", "dim"), tip]
    if no_bb:
        icons.append(tag("买完不够买活", "bad"))
    if sell := sell_pick(slots, s.key, tree, const):
        name = (names or {}).get(sell[0], sell[0])
        parts += ["；格子满了，先卖", chip(sell[0], name), f"（{sell[1]} 金，身上最便宜）"]
        icons += [tag("先卖", "bad"), chip(sell[0], name)]
    if after := todo[1:1 + NEXT_SHOW]:
        parts += ["；之后 ", group([chip(x.key, x.name) for x in after])]
        icons += [tag("→", "dim"), group([chip(x.key, x.name, mark="common") for x in after])]
    done = [x for x in b.steps if coach.has(owned, x.key, tree)][-DONE_SHOW:]
    if done:
        icons.append(group([chip(x.key, x.name, mark="got") for x in done]))
    return short(parts, icons)


def brief_lines(b: Build, lo: float = 0, hi: float | None = None) -> list[tuple]:
    """本局要点「出装」一节：[lo, hi) 分钟的顺序一行 + 每件针对装的理由（悬停看原文）+ 只作提示的针对装"""
    inside = lambda s: s.minute >= lo and (hi is None or s.minute < hi)
    steps, tips = [s for s in b.steps if inside(s)], [s for s in b.tips if inside(s) and s.kind != "avoid"]
    avoid = [s for s in b.tips if s.kind == "avoid"] if lo == 0 else []     # 「别买」只在第一页说一次，不分时间段
    if not (steps or tips or avoid):
        return []
    order = tiles([chip(s.key, s.name, f"{s.minute:.0f}′", s.kind, pct=s.share) for s in steps])
    lines = [("row", order)] if steps else []
    # 卡片上：装备图标 + 针对谁（头像 + 技能图标）+ 几个字；整句理由悬停看。胜率只写验证过的（Step.stat，HANDOFF 第 75 节）
    lines += [("warn" if s.urgent else "row",
               short([chip(s.key, s.name, mark="counter"), f"：{s.why}"],
                     [chip(s.key, s.name, mark="counter"), *([tag(s.label)] if s.label else []), *s.who]), s.quote)
              for s in steps if s.kind == "counter"]
    lines += [("row", short(["提示 · ", chip(s.key, s.name), f"：{s.why}（{coach.NO_DATA}）"],
                            [tag("提示", "dim"), chip(s.key, s.name), *([tag(s.label)] if s.label else []), *s.who]), s.quote)
              for s in tips]
    lines += [("warn", short(["这局别出 ", chip(s.key, s.name, mark="avoid"), f"：{s.why}"],
                             [chip(s.key, s.name, mark="avoid"), *([tag(s.stat, "bad")] if s.stat else []),
                              *([tag(s.label)] if s.label else []), *s.who]), s.quote) for s in avoid]
    if steps:
        lines.append(("dim", "′ 同分段中位购买时间 · % 同分段多少局买 · "
                      + ("金框 针对这局 · " if any(s.kind == "counter" for s in steps) else "") + "淡 之后常见 · 悬停看理由"))
    return lines


def late_lines(b: Build, late: float) -> list[tuple]:
    """本局要点「后期出装」一节：late 分钟以后的顺序 + 针对装；前面的针对装再列一遍（后期还没有就该补了）；
    再加卖东西 / 买活两条规矩（局内「下一件」会按身上的格子和钱具体说）。没有出装数据就不出这一节"""
    if not (b.steps or b.tips):
        return []
    lines = brief_lines(b, lo=late)
    early = [chip(s.key, s.name) for s in b.steps if s.kind == "counter" and s.minute < late]
    if early:
        lines.insert(1 if lines else 0, ("row", short(["针对这局的", group(early), "前面就该有，到后期还没有先补"],
                                                      [group(early), tag("还没有先补")])))
    early_tips = [chip(s.key, s.name) for s in b.tips if s.minute < late and s.kind != "avoid"]
    if early_tips:
        lines.insert(1 if lines else 0, ("row", short(["提示 · 前面提过的", group(early_tips), f"，到后期还没有可以补（{coach.NO_DATA}）"],
                                                      [tag("提示", "dim"), group(early_tips), tag("还没有可以补")])))
    if not lines:
        lines.append(("row", "同分段到后期没有多数人都出的大件，看对面阵容补针对装"))
    lines.append(("dim", f"格子满了先卖身上最便宜的那件；{late:.0f} 分钟后买大件前留够买活钱"))
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
    from . import death
    from ..sources import stats as stats_mod
    base = kb.KnowledgeBase.load()
    if base is None:
        return "还没有知识库，先运行 python -m dota_assistant kb update"
    positions = stats_mod.load_positions(cfg) or {}
    const = assets.items_const()
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
        L += [f"    → {text(t)}" for _, t in c.on_death(rec, st)]
        if nxt := c.next_line(st):
            L.append(f"    → {nxt}")
    return "\n".join(L).strip()
