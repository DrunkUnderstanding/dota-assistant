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
import statistics
import time
from dataclasses import dataclass, field

from . import data, kb
from .config import DATA_DIR, Config

BENCH_DIR = DATA_DIR / "bench"
ITEMS_CONST = DATA_DIR / "opendota_items.json"

# 以下阈值是实现假设（玩家未确认），用模拟结果和玩家反馈再调
BKB_CONTROL_MIN = 3          # 对面至少几个英雄有控制，才提醒黑皇杖
BKB_FALLBACK_MIN = 25        # 同分段数据里没有黑皇杖购买时间时，默认几分钟前该有
CORE_SHARE = 0.4             # 同分段 ≥40% 的局都买的装备算「核心装」
CORE_MIN_COST = 1500
CORE_LATE = 4                # 比同分段中位时间晚几分钟才提醒
RHYTHM_RATIO = 0.80          # 正补 / 经济低于同分段平均的 80% 才提醒
CHECKPOINTS = range(10, 61, 5)
REPEAT_AFTER = 6             # 同一条缺口还没补上，隔几分钟再提醒一次（最多 2 次）
MAX_PER_MOMENT = 2


# ---------- 同分段基准 ----------

def fetch_bench(cfg: Config, hero_id: int, pos: int) -> dict:
    p = BENCH_DIR / f"h{hero_id}_p{pos}.json"
    if p.exists() and time.time() - p.stat().st_mtime < 3 * 86400:
        return json.loads(p.read_text(encoding="utf-8"))
    br = data._stratz_brackets(cfg)
    q = """query($h: Short!, $hs: [Short], $p: [MatchPlayerPositionType], $b: [RankBracketBasicEnum]) { heroStats {
             stats(heroIds: $hs, positionIds: $p, bracketBasicIds: $b, groupByTime: true) { time matchCount cs networth }
             itemFullPurchase(heroId: $h, positionIds: $p, bracketBasicIds: $b) { itemId time matchCount }
             total: stats(heroIds: $hs, positionIds: $p, bracketBasicIds: $b) { matchCount } } }"""
    d = data._stratz(cfg, q, h=hero_id, hs=[hero_id], p=[f"POSITION_{pos}"], b=br)["heroStats"]
    per_min = {str(r["time"]): {"cs": r["cs"], "networth": r["networth"]} for r in d["stats"] if r["matchCount"] >= 100}
    items: dict[str, list[list[int]]] = {}
    for r in d["itemFullPurchase"]:
        items.setdefault(str(r["itemId"]), []).append([r["time"], r["matchCount"]])
    out = {"hero_id": hero_id, "pos": pos, "brackets": br, "matches": (d["total"] or [{}])[0].get("matchCount", 0),
           "per_min": per_min, "items": items}
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out), encoding="utf-8")
    return out


def item_timings(bench: dict) -> dict[int, tuple[float, float]]:
    """itemId → (购买比例, 中位购买分钟)"""
    out = {}
    total = bench.get("matches") or 1
    for iid, rows in bench["items"].items():
        n = sum(c for _, c in rows)
        times = [t for t, c in sorted(rows) for _ in range(min(c, 2000))]
        # STRATZ 的购买统计和总局数口径不完全一致（会超过 100%），封顶
        out[int(iid)] = (min(n / total, 1.0), statistics.median(times) if times else 0)
    return out


# ---------- 装备 ----------

def item_tree() -> dict[str, set[str]]:
    """装备 → 所有直接或间接用到它的成品（买了飓风长戟就等于「有魔龙枪的能力」）"""
    if not ITEMS_CONST.exists():
        import requests
        ITEMS_CONST.write_text(requests.get("https://api.opendota.com/api/constants/items", timeout=60).text,
                               encoding="utf-8")
    raw = json.loads(ITEMS_CONST.read_text(encoding="utf-8"))
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


def plan(me: kb.HeroKB, enemies: list[kb.HeroKB], base: kb.KnowledgeBase, rules: list[kb.Rule],
         bench: dict | None) -> tuple[list[Need], list[str]]:
    """一局开始时就能确定的缺口 + 开局提示"""
    needs, notes = [], []
    my_tags = me.tags()

    # 1. 黑皇杖：对面控制多
    ctrl = [(e, e.with_tag(*kb.CONTROL_TAGS)) for e in enemies]
    ctrl = [(e, hits) for e, hits in ctrl if hits]
    if len(ctrl) >= BKB_CONTROL_MIN and "提供技能免疫" not in my_tags:
        due = BKB_FALLBACK_MIN
        if bench:
            share, med = item_timings(bench).get(base.item_ids.get("item_black_king_bar"), (0, 0))
            if share >= 0.15:
                due = med + CORE_LATE
        who = "、".join(f"{e.name}（{hits[0][0].name}）" for e, hits in ctrl)
        pierce = [f"{e.name}的{ab.name}" for e, hits in ctrl for ab, _, _ in hits if ab.pierces_immunity]
        text = f"对面 {len(ctrl)} 个英雄有控制，你还没有黑皇杖，优先做"
        if pierce:
            text += f"；注意 {'、'.join(dict.fromkeys(pierce))} 无视技能免疫，黑皇杖挡不住"
        needs.append(Need("bkb", text, ("black_king_bar",), due, 0,
                          [f"{e.name}·{ab.name}：{s}" for e, hits in ctrl for ab, _, s in hits[:1]]))

    # 2. 闪避：自己能处理就不推
    eva = [(e, e.with_tag("提供闪避")) for e in enemies]
    eva = [(e, hits) for e, hits in eva if hits]
    if eva:
        names = "、".join(e.name for e, _ in eva)
        player_rule = [r for r in rules if r.hero == me.name and r.item == "金箍棒"]
        if "克敌机先" in my_tags:
            ab, sent = my_tags["克敌机先"]
            notes.append(f"对面{names}有闪避，但你的{ab}自带克敌机先，不用专门出金箍棒")
        elif player_rule:
            notes.append(f"对面{names}有闪避；你的规则：{player_rule[0].text}")
        else:
            needs.append(Need("mkb", f"对面{names}有闪避，考虑金箍棒或血棘", ("monkey_king_bar", "bloodthorn"),
                              30, 2, [f"{e.name}·{hits[0][0].name}：{hits[0][2]}" for e, hits in eva]))

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
    return needs, notes


@dataclass
class State:
    minute: float
    owned: set[str]
    cs: int | None = None
    networth: int | None = None


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

    def __init__(self, needs: list[Need], bench, tree, base):
        self.needs, self.bench, self.tree, self.base = needs, bench, tree, base
        self.said: dict[str, list[float]] = {}

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
            notes.append(f"同分段数据没拉到（{e}），这局只提醒对抗装")
    if not enemies:
        notes.append("没有对面阵容（选人时没识别 / 没填），这局只看出装节奏")
    needs, hero_notes = plan(me, enemies, base, kb.load_rules(), bench)
    return Coach(needs, bench, item_tree(), base), hero_notes + notes


# ---------- 模拟：拿一局录像重放 ----------

def _stratz_position(cfg: Config, match_id: int, account_id: int) -> int | None:
    if not cfg.stratz_token:
        return None
    q = "query($id: Long!) { match(id: $id) { players { steamAccountId position } } }"
    try:
        players = data._stratz(cfg, q, id=match_id)["match"]["players"]
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
    tree = item_tree()
    needs, notes = plan(my, enemies, base, rules, bench)
    coach = Coach(needs, bench, tree, base)

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
    L.append("")
    lh, nw = me.get("lh_t") or [], me.get("networth_t") or me.get("gold_t") or []
    for minute, why in moments:
        owned = {k for t, k in buys if t <= minute}
        i = int(minute)
        st = State(minute, owned, lh[i] if i < len(lh) else None, nw[i] if i < len(nw) else None)
        for n in coach.tick(st, checkpoint=why == "时间点"):
            got = min((t for t in (_when(buys, i, tree) for i in n.items) if t is not None), default=None)
            tail = (f"（实际 {_mmss(got)} 补上）" if got is not None else "（整局没补）") if n.items else ""
            L.append(f"{_mmss(minute)} [{why}] {n.text}{tail}")
    return "\n".join(L)


def _when(buys, key, tree) -> float | None:
    return next((t for t, k in buys if k == key or k in tree.get(key, set())), None)


def _mmss(minute: float) -> str:
    return f"{int(minute):02d}:{int(minute * 60) % 60:02d}"
