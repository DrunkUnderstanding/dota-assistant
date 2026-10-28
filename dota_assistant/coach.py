"""局内缺口提醒：对照对面阵容和同分段同英雄同位置的节奏，告诉你「这局缺什么」。

判断全部是数字对数字、原文对原文，不用大模型：
- 对抗装缺口：敌方技能的机制标签（知识库，Valve 官方原文）× 你身上的装备
- 不需要的装备：你自己英雄的技能（例如射手天赋自带克敌机先）+ knowledge/rules.toml
- 节奏：STRATZ 同分段、同英雄、同位置每分钟的平均正补 / 经济，和核心装的购买时间分布

实时版的输入来自 GSI（自己的装备、正补、时间），在死亡 / 固定时间点提醒；
现在先有模拟版：拿一局已解析的录像，逐时间点重放，看当时会提醒什么（simulate）。
"""
from __future__ import annotations

import json
import logging
import re
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import kb
from .core.config import DATA_DIR, Config
from .sources import http

log = logging.getLogger(__name__)
BENCH_DIR = DATA_DIR / "bench"
ITEMS_CONST = DATA_DIR / "opendota_items.json"

# 以下阈值是实现假设（玩家未确认），用模拟结果和玩家反馈再调
BKB_CONTROL_MIN = 3          # 对面至少几个英雄有控制，才提醒黑皇杖
BKB_FALLBACK_MIN = 25        # 同分段数据里没有黑皇杖购买时间时，默认几分钟前该有
BKB_EARLY_MIN = 4            # 对面至少几个英雄有控制，黑皇杖按同分段「出得早的 1/4」的时间算（玩家 2026-10-21 复盘：控制多时黑皇杖前移）
CORE_SHARE = 0.4             # 同分段 ≥40% 的局都买的装备算「核心装」
CORE_MIN_COST = 1500
MENU_SHARE = 0.03            # 同分段 ≥3% 的局买过，才算「这个英雄会出的装备」，针对装只从这里面挑
COUNTER_FALLBACK_MIN = 30    # 针对装没有同分段购买时间（买的人少）时，默认几分钟后才催
CORE_LATE = 4                # 比同分段中位时间晚几分钟才提醒
RHYTHM_RATIO = 0.80          # 正补 / 经济低于同分段平均的 80% 才提醒
CHECKPOINTS = range(10, 61, 5)
REPEAT_AFTER = 6             # 同一条缺口还没补上，隔几分钟再提醒一次（最多 2 次）
MAX_PER_MOMENT = 2
# 玩家 2026-10-26：出装建议要「大数据 + 本局」两种依据，缺一种只算提示。针对装（看对面机制）和按死法补装
# 都还没有「出了更容易赢」的数据（HANDOFF 第 33 节：剑圣对上幻象英雄，哪件装备都测不出效果），先都算提示
NO_DATA = "还没有大数据说明出了更容易赢"
SUPPORT_POSITIONS = (4, 5)   # 辅助：对抗装（黑皇杖 / 金箍棒）不催你出，改成「提醒核心」


# ---------- 同分段基准 ----------

# 2：itemFullPurchase 加 matchLimit: 0（不加只返回前中期最常见的几件，黑皇杖都没有）
# 3：只算第一次购买（instance 0），分母改成「买鞋最多的那种鞋的局数」（见 item_timings）
# 4：每分钟多存平均屯野数（stacks），野区提醒（jungle.py）看要不要提醒屯野
# 5：每分钟数据拉到 BENCH_MAX_MIN（以前没写 maxTime，STRATZ 默认只给到 35 分钟）；总局数改用第 0 分钟的局数
BENCH_VERSION = 5
# STRATZ heroStats.stats 按时间分组：不写 maxTime 默认只给 0~35 分钟（2026-10-26 实测），最多给到 75 分钟。
# 第 t 分钟 = 打到第 t 分钟还没结束的局的平均值；局数少于 PER_MIN_MIN_MATCHES 的分钟不要，冷门英雄就到它数据够的最后一分钟为止
BENCH_MAX_MIN = 90
PER_MIN_MIN_MATCHES = 100


def fetch_bench(cfg: Config, hero_id: int, pos: int) -> dict:
    p = BENCH_DIR / f"h{hero_id}_p{pos}.json"
    if p.exists() and time.time() - p.stat().st_mtime < 3 * 86400:
        cached = json.loads(p.read_text(encoding="utf-8"))
        if cached.get("v") == BENCH_VERSION:
            return cached
    br = http._stratz_brackets(cfg)
    q = """query($h: Short!, $hs: [Short], $p: [MatchPlayerPositionType], $b: [RankBracketBasicEnum], $mx: Int) { heroStats {
             stats(heroIds: $hs, positionIds: $p, bracketBasicIds: $b, groupByTime: true, maxTime: $mx) {
               time matchCount cs networth campsStacked }
             itemFullPurchase(heroId: $h, positionIds: $p, bracketBasicIds: $b, matchLimit: 0) { itemId instance time matchCount }
             itemBootPurchase(heroId: $h, positionIds: $p, bracketBasicIds: $b) { itemId instance matchCount } } }"""
    d = http._stratz(cfg, q, h=hero_id, hs=[hero_id], p=[f"POSITION_{pos}"], b=br, mx=BENCH_MAX_MIN)["heroStats"]
    per_min = {str(r["time"]): {"cs": r["cs"], "networth": r["networth"], "stacks": r["campsStacked"]}
               for r in d["stats"] if r["matchCount"] >= PER_MIN_MIN_MATCHES}
    # 第 0 分钟的局数 = 全部局数。不分时间的 stats 其实给的是「打到 maxTime（默认 35）分钟的局数」，不能当总数
    total = next((r["matchCount"] for r in d["stats"] if r["time"] == 0), 0)
    items: dict[str, list[list[int]]] = {}
    for r in d["itemFullPurchase"]:
        if not r["instance"]:                    # 同一件买第二次（instance 1+）不算
            items.setdefault(str(r["itemId"]), []).append([r["time"], r["matchCount"]])
    boots: dict[int, int] = {}
    for r in d["itemBootPurchase"]:
        if not r["instance"]:
            boots[r["itemId"]] = boots.get(r["itemId"], 0) + r["matchCount"]
    out = {"v": BENCH_VERSION, "hero_id": hero_id, "pos": pos, "brackets": br,
           "matches": total, "boots_max": max(boots.values(), default=0),
           "per_min": per_min, "items": items}
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out), encoding="utf-8")
    return out


def item_timings(bench: dict) -> dict[int, tuple[float, float]]:
    """itemId → (购买比例, 中位购买分钟)。
    分母：取「总局数」和「买得最多的那种鞋的局数」里大的那个——几乎每局都买鞋。
    （2026-10-25 卓尔一号位 stats 2513 局、动力鞋却有 3533 局买，是因为当时的总局数其实是「打到 35 分钟的局数」，
    BENCH_VERSION 5 已改成真正的总局数；鞋的局数留着兜底）"""
    out = {}
    total = max(bench.get("matches") or 0, bench.get("boots_max") or 0) or 1
    for iid, rows in bench["items"].items():
        n = sum(c for _, c in rows)
        times = [t for t, c in sorted(rows) for _ in range(min(c, 2000))]
        out[int(iid)] = (min(n / total, 1.0), statistics.median(times) if times else 0)
    return out


def item_quantile(bench: dict, item_id: int | None, q: float) -> float | None:
    """同分段买这件的局里，第 q 分位的购买分钟（q = 0.25：出得早的那 1/4）"""
    rows = bench["items"].get(str(item_id)) or []
    times = [t for t, c in sorted(rows) for _ in range(min(c, 2000))]
    return times[int(q * (len(times) - 1))] if times else None


# ---------- 装备 ----------

def items_const() -> dict:
    """OpenDota 装备常量：key → {cost, components, …}"""
    if not ITEMS_CONST.exists():
        import requests
        ITEMS_CONST.write_text(requests.get("https://api.opendota.com/api/constants/items", timeout=60).text,
                               encoding="utf-8")
    return json.loads(ITEMS_CONST.read_text(encoding="utf-8"))


def item_tree(raw: dict | None = None) -> dict[str, set[str]]:
    """装备 → 所有直接或间接用到它的成品（买了飓风长戟就等于「有魔龙枪的能力」）"""
    raw = raw if raw is not None else items_const()
    parents: dict[str, set[str]] = {}
    for k, v in raw.items():
        for c in v.get("components") or []:
            parents.setdefault(c, set()).add(k)
    up: dict[str, set[str]] = {}

    def walk(k: str) -> set[str]:
        if k not in up:
            up[k] = set()
            for p in parents.get(k, ()):
                up[k] |= {p} | walk(p)
        return up[k]
    for k in raw:
        walk(k)
    return up


def has(owned: set[str], key: str, tree: dict[str, set[str]]) -> bool:
    return key in owned or bool(owned & tree.get(key, set()))


# ---------- 缺口判断 ----------

@dataclass
class Need:
    key: str                      # 去重用
    text: str                     # 要说的话
    items: tuple[str, ...] = ()   # 满足任意一件即可（OpenDota 装备 key）
    due: float = 0                # 几分钟之后才开始提醒
    priority: int = 1             # 越小越重要
    evidence: list[str] = field(default_factory=list)   # 依据（原句 / 数字），复盘时显示
    brief: str = ""               # 开局（本局要点卡片）的说法：那时还没到点，不能说「你还没有」
    minute: float = 0             # 建议几分钟出（出装顺序用）；0 = 按同分段中位购买时间
    tip: str = ""                 # 非空 = 只算提示，写缺哪种依据（不进出装顺序 / 「下一件」）


@dataclass(frozen=True)
class Counter:
    """针对装：对面有某种机制 → 某件装备能应对。
    装备那句原文（proof，正则）必须在当前版本说明里找得到，作为依据；版本改了找不到就不推。
    和黑皇杖 / 金箍棒不同，只推你这个英雄同分段确实会出的（≥ MENU_SHARE），不给法核推白银之锋"""
    key: str
    find: Callable[[kb.HeroKB], list[tuple[str, str]]]   # 对面英雄 → [(技能名, 原句)]
    min_heroes: int                                      # 对面至少几个英雄有
    items: tuple[tuple[str, str], ...]                   # (装备 key, 说明里要有的原文)；都会出就挑买得多的
    self_skip: str                                       # 自己英雄有这个标签就不用专门出
    reason: str                                          # {who} = 对面英雄（技能），{item} = 装备名


def _base_hits(e: kb.HeroKB, *tags: str) -> list[tuple[kb.Entry, str, str]]:
    """带这些标签的技能，去掉只在神杖 / 魔晶说明里出现的（狼人的狼要魔晶才有闪避，不值得为它出金箍棒）"""
    return [(ab, t, sent) for ab, t, sent in e.with_tag(*tags) if sent not in ab.scepter and sent not in ab.shard]


def _tagged(*tags: str):
    return lambda e: [(ab.name, sent) for ab, _, sent in _base_hits(e, *tags)]


def _break_hits(e: kb.HeroKB) -> list[tuple[str, str]]:
    """被破坏后整个被动失效的（敌法师法术反制、孽主衰退光环…）；只是「无法获得新的叠加效果」的不算，不值得专门出装"""
    return [(ab.name, sent) for ab, _, sent in _base_hits(e, "怕破坏") if "叠加效果" not in sent]


def _targeted_ult(e: kb.HeroKB) -> list[tuple[str, str]]:
    """能指向敌方单位的大招（官方 behavior / target_team 字段），林肯能挡、清莲能反"""
    return [(ab.name, ab.text.split("\n")[0]) for ab in e.abilities if ab.ultimate and ab.targets_enemy and not ab.granted]


# 自己能造出一群可控幻象的英雄（幻影长矛手、娜迦海妖、混沌骑士、恐怖利刃）。
# 「无法控制」的（幽鬼如影随形）、造别人幻象的（暗影恶魔崩裂禁锢「该单位的」）、百戏大王的道具（【使用】）不算
ILLUSION_RE = re.compile(r"(制造|创造|召唤|产生)[^。；]{0,14}(幻象|镜像)")
ILLUSION_SKIP = re.compile(r"无法控制|该单位的|【使用")


def _illusions(e: kb.HeroKB) -> list[tuple[str, str]]:
    out = []
    for ab in e.abilities:
        if ab.granted:
            continue
        sents = (x.strip() for x in re.split(r"(?<=[。！；\n])", ab.text))
        sent = next((x for x in sents if ILLUSION_RE.search(x) and not ILLUSION_SKIP.search(x)
                     and x not in ab.scepter and x not in ab.shard), None)
        if sent:
            out.append((ab.name, sent))
    return out


COUNTERS = (
    Counter("linken", _targeted_ult, 2, (("sphere", r"抵挡.{0,6}指向性技能"), ("lotus_orb", r"指向性技能反馈给施法者")),
            "抵挡 / 反馈指向技能", "对面{who}是指向性大招，可以出{item}"),
    Counter("break", _break_hits, 1, (("silver_edge", r"被动技能无效"),),
            "施加破坏", "对面{who}怕破坏，{item}的破隐一击能让被动失效"),
    Counter("heal", _tagged("吸血"), 1, (("spirit_vessel", r"生命回复降低"), ("skadi", r"降低.{0,16}生命回复")),
            "", "对面{who}能吸血，{item}能降低回复"),
    Counter("pierce", _tagged("提供技能免疫"), 1, (("abyssal_blade", r"无视减益免疫"),),
            "无视技能免疫", "对面{who}能技能免疫，{item}的击晕无视减益免疫"),
    # 2026-10-26 玩家：后期出装要看对面英雄。范围伤害一下打一片幻象（实现假设，玩家没确认装备选哪几件）
    Counter("illusion", _illusions, 1, (("mjollnir", r"\d+个目标之间跳跃"), ("bfury", r"锥形区域内敌人造成"),
                                        ("radiance", r"每秒对敌军造成"), ("shivas_guard", r"寒冷的波动，对敌人造成")),
            "", "对面{who}会造幻象，{item}是范围伤害，一下打一片"),
)


def _proof(e: kb.Entry, pattern: str) -> str | None:
    """物品说明里命中的那句原文"""
    return next((x.strip() for x in re.split(r"(?<=[。！；\n])", e.full_text()) if re.search(pattern, x)), None)


def _player_rule(rules: list[kb.Rule], me: kb.HeroKB, key: str, base: kb.KnowledgeBase) -> kb.Rule | None:
    """玩家对「这个英雄 + 这件装备」写过的规则（knowledge/rules.toml）"""
    for r in rules:
        if r.hero == me.name and r.item:
            try:
                if base.item(r.item).key == key:
                    return r
            except KeyError:
                continue
    return None


def _who(hits: list[tuple[kb.HeroKB, list]], limit: int = 3) -> str:
    return "、".join(f"{e.name}（{f[0][0]}）" for e, f in hits[:limit]) + ("等" if len(hits) > limit else "")


def plan(me: kb.HeroKB, enemies: list[kb.HeroKB], base: kb.KnowledgeBase, rules: list[kb.Rule],
         bench: dict | None, pos: int = 1) -> tuple[list[Need], list[str]]:
    """一局开始时就能确定的缺口 + 开局提示"""
    needs, notes = [], []
    support = pos in SUPPORT_POSITIONS
    my_tags = me.tags()
    timings = item_timings(bench) if bench else {}

    # 1. 黑皇杖：对面控制多
    ctrl = [(e, e.with_tag(*kb.CONTROL_TAGS)) for e in enemies]
    ctrl = [(e, hits) for e, hits in ctrl if hits]
    share, med = (item_timings(bench).get(base.item_ids.get("item_black_king_bar"), (0, 0)) if bench else (0, 0))
    # 自带技能免疫的英雄（噬魂鬼狂暴）一般不用黑皇杖；但同分段多数人照样做的（哈斯卡跳跃只免疫一下）还是提醒
    self_immune = "提供技能免疫" in my_tags and share < 0.3
    if len(ctrl) >= BKB_CONTROL_MIN and not self_immune:
        minute = med if share >= 0.15 else 0
        if minute and len(ctrl) >= BKB_EARLY_MIN:
            minute = item_quantile(bench, base.item_ids.get("item_black_king_bar"), 0.25) or minute
        due = minute + CORE_LATE if minute else BKB_FALLBACK_MIN
        who = "、".join(f"{e.name}（{hits[0][0].name}）" for e, hits in ctrl)
        pierce = [f"{e.name}的{ab.name}" for e, hits in ctrl for ab, _, _ in hits if ab.pierces_immunity]
        text = f"对面 {len(ctrl)} 个英雄有控制，你还没有黑皇杖"
        when = (f"比同分段一般（{med:.0f} 分钟）早，{minute:.0f} 分钟左右出" if minute and minute < med
                else f"{due:.0f} 分钟前要有")
        early = f"对面 {len(ctrl)} 个英雄有控制（{who}），可以考虑黑皇杖，{when}"
        tail = f"；注意 {'、'.join(dict.fromkeys(pierce))} 无视技能免疫，黑皇杖挡不住" if pierce else ""
        if support:
            notes.append(f"对面 {len(ctrl)} 个英雄有控制（{who}），提醒核心早出黑皇杖{tail}")
        else:
            needs.append(Need("bkb", text + tail, ("black_king_bar",), due, 0,
                              [f"{e.name}·{ab.name}：{s}" for e, hits in ctrl for ab, _, s in hits[:1]], early + tail,
                              minute, NO_DATA))

    # 2. 闪避：自己能处理就不推
    eva = [(e, _base_hits(e, "提供闪避")) for e in enemies]
    eva = [(e, hits) for e, hits in eva if hits]
    if eva:
        names = "、".join(e.name for e, _ in eva)
        player_rule = _player_rule(rules, me, "monkey_king_bar", base)
        if "克敌机先" in my_tags:
            ab, sent = my_tags["克敌机先"]
            notes.append(f"对面{names}有闪避，但你的{ab}自带克敌机先，不用专门出金箍棒")
        elif player_rule:
            notes.append(f"对面{names}有闪避；你的规则：{player_rule.text}")
        elif support:
            notes.append(f"对面{names}有闪避，提醒核心考虑金箍棒或血棘")
        else:
            # 金箍棒 / 血棘哪件这个英雄出得多就先说哪件
            keys = sorted(("monkey_king_bar", "bloodthorn"),
                          key=lambda k: -timings.get(base.item_ids.get(f"item_{k}"), (0, 0))[0])
            first, second = (base.items[k].name for k in keys)
            needs.append(Need("mkb", f"对面{names}有闪避，考虑{first}或{second}", tuple(keys),
                              30, 2, [f"{e.name}·{hits[0][0].name}：{hits[0][2]}" for e, hits in eva],
                              f"对面{names}有闪避，后期考虑{first}或{second}", tip=NO_DATA))

    # 3. 隐身：提醒队伍反隐，自己有反隐也说一声
    inv = [(e, e.with_tag("隐身")) for e in enemies]
    inv = [(e, hits) for e, hits in inv if hits]
    if inv:
        # 官方原文写明「不会被反隐手段发现」的（斯拉克暗影之舞），买粉没用
        immune = [f"{e.name}的{ab.name}" for e, hits in inv for ab, _, sent in hits if "不会被反隐" in sent]
        mine = my_tags.get("反隐 / 显形")
        notes.append(f"对面{'、'.join(e.name for e, _ in inv)}能隐身，提醒辅助带粉 / 岗哨"
                     + (f"；你的{mine[0]}也能显形" if mine else "")
                     + (f"（{'、'.join(immune)}不会被反隐发现，粉对它没用）" if immune else ""))

    # 4. 其他针对装：只从这个英雄同分段会出的装备里挑（没有同分段数据就不推，宁缺毋滥）
    for c in COUNTERS:
        hits = [(e, f) for e in enemies if (f := c.find(e))]
        if len(hits) < c.min_heroes or not timings:
            continue
        menu = []
        for k, pattern in c.items:
            share, med = timings.get(base.item_ids.get(f"item_{k}"), (0, 0))
            proof = _proof(base.items[k], pattern) if k in base.items else None
            if proof is None and k in base.items:
                log.warning("针对装 %s：%s 的说明里找不到「%s」，版本可能改了", c.key, k, pattern)
            if share >= MENU_SHARE and proof:
                menu.append((share, med, k, proof))
        if not menu:
            continue
        share, med, k, proof = max(menu)
        name = base.items[k].name
        if c.self_skip and c.self_skip in my_tags:
            ab, _ = my_tags[c.self_skip]
            notes.append(f"{c.reason.format(who=_who(hits), item=name)}；但你的{ab}自带这个效果，不用专门出")
            continue
        if rule := _player_rule(rules, me, k, base):
            notes.append(f"{c.reason.format(who=_who(hits), item=name)}；你的规则：{rule.text}")
            continue
        text = c.reason.format(who=_who(hits), item=name)
        due = med + CORE_LATE if med else COUNTER_FALLBACK_MIN
        needs.append(Need(c.key, text, (k,), due, 2,
                          [f"{e.name}·{f[0][0]}：{f[0][1]}" for e, f in hits] + [f"{name}：{proof}"],
                          f"{text}（同分段 {share:.0%} 的局会出，一般 {med:.0f} 分钟）", tip=NO_DATA))
    return needs, notes


@dataclass
class State:
    minute: float
    owned: set[str]
    cs: int | None = None
    networth: int | None = None
    gold: int | None = None       # 身上的钱（GSI player.gold）
    slots: list[str] | None = None            # 身上 6 格（GSI slot0~5），格子满了要卖东西
    buyback: tuple[int, float] | None = None  # (买活价, 买活冷却剩余秒)


def check(needs: list[Need], st: State, bench: dict | None, tree, base: kb.KnowledgeBase,
          checkpoint: bool) -> list[Need]:
    """这个时刻还缺什么（按优先级）"""
    out = [n for n in needs if st.minute >= n.due and not any(has(st.owned, i, tree) for i in n.items)]
    if bench:
        # 核心装：同分段大多数人这时候已经有了
        timings = item_timings(bench)
        late = []
        for e in base.items.values():
            share, med = timings.get(base.item_ids.get(e.key), (0, 0))
            k = e.key.removeprefix("item_")
            if share >= CORE_SHARE and e.cost >= CORE_MIN_COST and st.minute >= med + CORE_LATE \
                    and not has(st.owned, k, tree):
                late.append((med, e.name, k, share))
        if late:
            med, name, k, share = min(late)
            out.append(Need(f"core:{k}", f"{name}还没出，同分段大多数局会买，一般 {med:.0f} 分钟出",
                            (k,), 0, 1))
        # 节奏：只在固定时间点比
        ref = bench["per_min"].get(str(int(st.minute)))
        if checkpoint and ref and st.cs is not None:
            if st.cs < ref["cs"] * RHYTHM_RATIO:
                out.append(Need(f"cs:{int(st.minute)}", f"{int(st.minute)} 分钟正补 {st.cs}，同分段平均 {ref['cs']:.0f}，"
                                f"落后 {1 - st.cs / ref['cs']:.0%}", (), 0, 1))
            elif st.networth is not None and st.networth < ref["networth"] * RHYTHM_RATIO:
                out.append(Need(f"nw:{int(st.minute)}", f"{int(st.minute)} 分钟经济 {st.networth}，同分段平均 "
                                f"{ref['networth']:.0f}", (), 0, 1))
    return sorted(out, key=lambda n: n.priority)


class Coach:
    """记住说过什么，避免重复"""

    def __init__(self, needs: list[Need], bench, tree, base, build=None, const: dict | None = None):
        self.needs, self.bench, self.tree, self.base = needs, bench, tree, base
        self.build, self.const = build, const or {}      # itembuild.Build：出装顺序，「下一件」用
        self.said: dict[str, list[float]] = {}
        self.deaths: dict[str, int] = {}                 # 死法（itembuild.DeathRule.key）→ 本局几次

    def on_death(self, rec, st: State) -> list[tuple[str, list]]:
        """死亡复盘（death.Recap）→ 数这种死法本局第几次；超过两次（第 DEATH_MIN 次起）才说对应装备（片段列表，见 itembuild.text）。
        只有本局依据、没有大数据依据，所以只算提示，不动「下一件」。保命手段没用上的那次照样算次数，只是那次不说"""
        from . import itembuild
        cause = itembuild.death_cause(rec)
        if cause is None or self.base is None:
            return []
        n = self.deaths[cause.key] = self.deaths.get(cause.key, 0) + 1
        if n < itembuild.DEATH_MIN or self.build is None:
            return []
        timings = item_timings(self.bench) if self.bench else {}
        pick = itembuild.death_pick(rec, st.owned, timings, self.base, self.tree)
        if pick is None:
            return []
        return [("row", [f"提示：本局第 {n} 次{cause.label}，可以考虑", itembuild.chip(pick.key, pick.name),
                         f"：{pick.why}（{NO_DATA}）"])]

    def next_line(self, st: State) -> str | None:
        """next_parts 的文字版（命令行回放用）"""
        from . import itembuild
        parts = self.next_parts(st)
        return itembuild.text(parts) if parts else None

    def next_parts(self, st: State) -> list | None:
        """「下一件：黑皇杖，还差 850 金；之后 …」，死了回家买东西时最有用；格子满了说卖哪件，后期说买了够不够买活。
        片段列表（装备在界面上是图标），文字版用 next_line"""
        if self.build is None:
            return None
        from . import itembuild
        names = {k: e.name for k, e in self.base.items.items()} if self.base else {}
        return itembuild.next_parts(self.build, st.owned, st.gold, self.tree, self.const,
                                    slots=st.slots, buyback=st.buyback, minute=st.minute, names=names)

    def tick(self, st: State, checkpoint: bool) -> list[Need]:
        out = []
        for n in check(self.needs, st, self.bench, self.tree, self.base, checkpoint):
            times = self.said.get(n.key, [])
            if times and (len(times) >= 2 or st.minute - times[-1] < REPEAT_AFTER):
                continue
            out.append(n)
            if len(out) >= MAX_PER_MOMENT:
                break
        for n in out:
            self.said.setdefault(n.key, []).append(st.minute)
        return out


# ---------- 实时版：开局建一个 Coach，之后由 live.py 在死亡 / 每 5 分钟喂 GSI 状态 ----------

def prepare(cfg: Config, my_id: int, enemy_ids: list[int], pos: int) -> tuple[Coach, list[str]]:
    """要读知识库、可能联网拉同分段数据（有缓存），放后台线程调。返回 (coach, 开局提示)"""
    base = kb.KnowledgeBase.load()
    if base is None:
        raise RuntimeError("还没有知识库，先运行 python -m dota_assistant kb update")
    me = base.heroes[my_id]
    enemies = [base.heroes[i] for i in enemy_ids if i in base.heroes]
    bench = None
    notes = []
    if cfg.stratz_token:
        try:
            bench = fetch_bench(cfg, my_id, pos)
        except Exception as e:                   # 没网也照样提醒对抗装，只是没有节奏对比
            log.warning("局内提醒：同分段数据没拉到：%s", e, exc_info=not isinstance(e, http.NETWORK_ERRORS))
            notes.append(f"同分段数据没拉到（{e}），这局只提醒对抗装")
    if not enemies:
        notes.append("没有对面阵容（选人时没识别 / 没填），这局只看出装节奏")
    needs, hero_notes = plan(me, enemies, base, kb.load_rules(), bench, pos)
    from . import itembuild
    const = items_const()
    return Coach(needs, bench, item_tree(const), base, itembuild.make(needs, bench, base), const), hero_notes + notes


# ---------- 模拟：拿一局录像重放 ----------

def _stratz_position(cfg: Config, match_id: int, account_id: int) -> int | None:
    if not cfg.stratz_token:
        return None
    q = "query($id: Long!) { match(id: $id) { players { steamAccountId position } } }"
    try:
        players = http._stratz(cfg, q, id=match_id)["match"]["players"]
    except Exception:
        return None
    pos = next((p["position"] for p in players if p["steamAccountId"] == account_id), None)
    return int(pos[-1]) if pos and pos[-1].isdigit() else None


def simulate(cfg: Config, match_id: int, account_id: int | None = None) -> str:
    import requests
    account_id = account_id or cfg.account_id
    m = requests.get(f"https://api.opendota.com/api/matches/{match_id}", timeout=60).json()
    me = next((p for p in m.get("players", []) if p.get("account_id") == account_id), None)
    if me is None:
        return f"比赛 {match_id} 里找不到好友代码 {account_id}"
    if not me.get("purchase_log"):
        return f"比赛 {match_id} 还没有解析（没有出装时间线），先在 OpenDota 网页上点「请求解析」"
    base = kb.KnowledgeBase.load()
    if base is None:
        return "还没有知识库，先运行 python -m dota_assistant kb update"
    rules = kb.load_rules()
    heroes = base.heroes
    my = heroes[me["hero_id"]]
    enemies = [heroes[p["hero_id"]] for p in m["players"] if p["isRadiant"] != me["isRadiant"]]
    pos = _stratz_position(cfg, match_id, account_id) or cfg.position
    bench = fetch_bench(cfg, my.id, pos) if cfg.stratz_token else None
    const = items_const()
    tree = item_tree(const)
    needs, notes = plan(my, enemies, base, rules, bench, pos)
    from . import itembuild
    build = itembuild.make(needs, bench, base)
    coach = Coach(needs, bench, tree, base, build, const)

    deaths = sorted(d["time"] for d in me.get("deaths_log") or [])
    end = m["duration"] / 60
    moments = sorted({(t / 60, "死亡") for t in deaths} | {(float(c), "时间点") for c in CHECKPOINTS if c <= end})
    buys = [(x["time"] / 60, x["key"]) for x in me["purchase_log"]]

    L = [f"比赛 {match_id}：你 {my.name}（{pos}号位）{'赢' if (me['player_slot'] < 128) == m['radiant_win'] else '输'}，"
         f"{end:.0f} 分钟；对面 " + "、".join(e.name for e in enemies)]
    if bench:
        L.append(f"对照：STRATZ {'/'.join(bench['brackets'])} {my.name} {pos}号位最近一周 {bench['matches']} 局")
    L += [f"开局提示：{n}" for n in notes]
    for n in needs:
        L.append(f"本局缺口：{n.text}（{n.due:.0f} 分钟后开始提醒）")
        L += [f"    依据 {e}" for e in n.evidence[:5]]
    if build.steps:
        L.append("出装顺序：" + " → ".join(f"{s.name} {s.minute:.0f}′" for s in build.steps))
        L.append("实际出装：" + " → ".join(f"{base.items[k].name} {_mmss(t)}" for t, k in buys
                                         if k in base.items and base.items[k].cost >= CORE_MIN_COST))
    L.append("")
    lh, nw = me.get("lh_t") or [], me.get("networth_t") or me.get("gold_t") or []
    for minute, why in moments:
        owned = {k for t, k in buys if t <= minute}
        i = int(minute)
        st = State(minute, owned, lh[i] if i < len(lh) else None, nw[i] if i < len(nw) else None)
        for n in coach.tick(st, checkpoint=why == "时间点"):
            got = min((t for t in (_when(buys, i, tree) for i in n.items) if t is not None), default=None)
            tail = (f"（实际 {_mmss(got)} 补上）" if got is not None else "（整局没补）") if n.items else ""
            L.append(f"{_mmss(minute)} [{why}] {'提示：' if n.tip else ''}{n.text}{tail}")
        if why == "死亡" and (nxt := coach.next_line(st)):
            L.append(f"{_mmss(minute)} [死亡] {nxt}（录像里没有当时的钱）")
    return "\n".join(L)


def _when(buys, key, tree) -> float | None:
    return next((t for t, k in buys if k == key or k in tree.get(key, set())), None)


def _mmss(minute: float) -> str:
    return f"{int(minute):02d}:{int(minute * 60) % 60:02d}"
