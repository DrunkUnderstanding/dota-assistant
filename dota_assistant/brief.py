"""本局要点：选完人（策略时间）弹一张卡片，告诉你这局的关键点。

全部来自统计数据和知识库原文，不用大模型（大模型凭自身理解排选人不如统计引擎，见 HANDOFF 第 10 节）：
- 对线：对面分路（lanes.py，按常见位置猜）+ STRATZ laneOutcome（同分段、你这个英雄这个位置，对上对面某个英雄时的对线胜 / 平 / 负）
  + 你这路的对线提示（lanes.tips，知识库原文的硬规则）
- 起手装：STRATZ itemStartingPurchase（同分段同英雄同位置多数局会买的）
- 强势期：STRATZ 按比赛时长的胜率（groupByTime），双方 5 个英雄相对各自平均胜率的偏差
- 出装：itembuild.make()（同分段核心装的中位购买时间 + coach.plan() 的针对装，知识库原文对原文）
- 对面要防的：coach.plan() 的提示（隐身要带粉、不用专门出金箍棒、提醒核心出黑皇杖……）
对面的位置是猜的（lanes.guess：每个英雄在该分段打各位置的比例，五个人不重复），对线对手据此推出来。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from . import coach, data, itembuild, kb, lanes
from .config import DATA_DIR, Config
from .engine import POSITION_NAMES

log = logging.getLogger(__name__)
BRIEF_DIR = DATA_DIR / "brief"
CACHE_DAYS = 3

# 以下阈值是实现假设（玩家未确认），用回放和反馈再调
PHASES = ((0, 30, "30 分钟前"), (30, 40, "30~40 分钟"), (40, 61, "40 分钟后"))
PHASE_GAP = 1.5            # 双方强势期差距（百分点）超过这个才下结论
LANE_MIN_GAMES = 40        # 对线数据少于这么多局不说
LANE_GAP = 0.08            # 对线「胜 - 负」比你这个英雄平时高 / 低这么多，才说好打 / 难打
START_SHARE = 0.4          # 同分段 ≥40% 的局会带的才算常见起手
LANE_VS = lanes.LANE_VS


# ---------- 数据（缓存在 data/brief/）----------

def _cached(name: str, fetch):
    p = BRIEF_DIR / name
    if p.exists() and time.time() - p.stat().st_mtime < CACHE_DAYS * 86400:
        return json.loads(p.read_text(encoding="utf-8"))
    out = fetch()
    BRIEF_DIR.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out), encoding="utf-8")
    return out


def fetch_curves(cfg: Config) -> dict[str, dict[str, list[int]]]:
    """{英雄 id: {"n": [...], "w": [...]}}：第 t 个数 = 打到第 t 分钟还没结束的局数 / 其中赢的局数（t = 0~60）。
    不分位置（对面的位置是猜的，统一口径）。"""
    br = data._stratz_brackets(cfg)

    def fetch():
        q = """query($b: [RankBracketBasicEnum], $lo: Int, $hi: Int) { heroStats {
                 stats(bracketBasicIds: $b, groupByTime: true, minTime: $lo, maxTime: $hi) { heroId time matchCount winCount } } }"""
        out: dict[str, dict[str, list[int]]] = {}
        for lo, hi in ((0, 30), (31, 60)):          # 一次拉全部英雄 × 61 分钟太大，分两段
            for r in data._stratz(cfg, q, b=br, lo=lo, hi=hi)["heroStats"]["stats"]:
                c = out.setdefault(str(r["heroId"]), {"n": [0] * 61, "w": [0] * 61})
                if 0 <= r["time"] <= 60:
                    c["n"][r["time"]], c["w"][r["time"]] = r["matchCount"], r["winCount"]
        return out
    return _cached(f"curves_{'-'.join(br)}.json", fetch)


def fetch_hero(cfg: Config, hero_id: int, pos: int) -> dict:
    """你这个英雄这个位置：对线（按对面英雄）+ 起手装"""
    br = data._stratz_brackets(cfg)

    def fetch():
        q = """query($h: Short!, $hs: [Short], $p: [MatchPlayerPositionType], $b: [RankBracketBasicEnum]) { heroStats {
                 laneOutcome(heroId: $h, isWith: false, positionIds: $p, bracketBasicIds: $b) {
                   heroId2 winCount drawCount lossCount }
                 itemStartingPurchase(heroId: $h, positionIds: $p, bracketBasicIds: $b) { itemId instance matchCount }
                 total: stats(heroIds: $hs, positionIds: $p, bracketBasicIds: $b) { matchCount } } }"""
        d = data._stratz(cfg, q, h=hero_id, hs=[hero_id], p=[f"POSITION_{pos}"], b=br)["heroStats"]
        start: dict[str, int] = {}
        for r in d["itemStartingPurchase"]:          # 同一件的第几份（instance）分开算；送的 / 买的合起来
            k = f"{r['itemId']}:{r['instance']}"
            start[k] = start.get(k, 0) + r["matchCount"]
        return {"lane": {str(r["heroId2"]): [r["winCount"], r["drawCount"], r["lossCount"]] for r in d["laneOutcome"]},
                "start": start, "matches": (d["total"] or [{}])[0].get("matchCount", 0)}
    return _cached(f"h{hero_id}_p{pos}_{'-'.join(br)}.json", fetch)


# ---------- 计算 ----------

def phase_delta(curve: dict[str, list[int]], lo: int, hi: int) -> float | None:
    """在 [lo, hi) 分钟结束的局里的胜率 - 这个英雄的总胜率（百分点）；局数太少返回 None"""
    n, w = curve["n"], curve["w"]
    if not n[0]:
        return None
    end_n = n[lo] - (n[hi] if hi < len(n) else 0)
    end_w = w[lo] - (w[hi] if hi < len(w) else 0)
    if end_n < 200:
        return None
    return (end_w / end_n - w[0] / n[0]) * 100


def team_phases(curves: dict, hero_ids: list[int]) -> list[float | None]:
    """每个阶段：这几个英雄的偏差平均（缺数据的英雄不算）"""
    out = []
    for lo, hi, _ in PHASES:
        ds = [d for h in hero_ids if (c := curves.get(str(h))) and (d := phase_delta(c, lo, hi)) is not None]
        out.append(sum(ds) / len(ds) if ds else None)
    return out


def guess_positions(hero_ids: list[int], positions: dict[int, dict[int, tuple[int, int]]] | None,
                    taken: set[int] = frozenset()) -> dict[int, int]:
    """{英雄: 位置}：每人的位置占比相乘最大、互不重复（taken = 已经确定被别人占的位置）。带把握的版本见 lanes.guess"""
    return {g.hero: g.pos for g in lanes.guess(hero_ids, positions, taken)}


@dataclass
class Brief:
    title: str
    # (小标题, [(kind, 文字) 或 (kind, 文字, 依据原文)])，依据在界面上悬停显示
    sections: list[tuple[str, list[tuple]]] = field(default_factory=list)
    needs: list[coach.Need] = field(default_factory=list)                              # 给局内提醒接着用
    lane_ids: list[int] = field(default_factory=list)                                  # 猜的同路英雄：对线对手 + 同路队友
    plan: object = None           # gameplan.Plan：AI 打法（第二页），没做 / 没做完是 None
    plan_note: str = ""           # 打法那页没内容时显示什么（生成中 / 出错 / 没开）

    def add(self, title: str, lines: list[tuple]) -> None:
        if not lines:
            return
        for t, ls in self.sections:
            if t == title:                       # 同名小节合并（例如几条「说明」）
                ls.extend(lines)
                return
        self.sections.append((title, lines))

    def view(self, page: str = "data", hint: str = "") -> dict:
        """两页：data = 统计数据（本局要点），plan = AI 打法"""
        if page == "plan":
            if self.plan and self.plan.sections:
                hint = "AI 依据本局数据和官方原文写的，未验证 · " + hint
                sections = [{"title": p, "lines": [{"kind": "warn" if p == "对线劣势时" else "row", "text": t.text,
                                                     "note": t.basis} for t in ts]} for p, ts in self.plan.sections]
            else:
                sections = [{"title": "打法", "lines": [{"kind": "dim", "text": self.plan_note or "AI 打法没做出来"}]}]
        else:
            sections = [{"title": t, "lines": [{"kind": k, "text": x, **({"quote": q[0]} if q else {})} for k, x, *q in ls]}
                        for t, ls in self.sections]
        return {"kind": "brief", "title": self.title, "closable": True, "page": page, "hint": hint,
                "tabs": [{"label": "数据", "page": "data", "on": page == "data"},
                         {"label": "打法", "page": "plan", "on": page == "plan"}],
                "sections": sections}

    def text(self) -> str:
        L = [self.title]
        for t, ls in self.sections:
            L.append(f"【{t}】")
            L += [f"  {'⚠ ' if k == 'warn' else ''}{x}" for k, x, *_ in ls]
        return "\n".join(L)


def build(cfg: Config, my_id: int, ally_ids: list[int], enemy_ids: list[int], pos: int,
          base: kb.KnowledgeBase | None = None, side: str | None = None, ally_pos: dict[int, int] | None = None) -> Brief:
    """要联网（有缓存），放后台线程调。拿不到的部分跳过，不编。
    side = 我方阵营（分路写成上 / 中 / 下路）；ally_pos = 已知的队友位置（选人界面 OCR 认出来的）"""
    base = base or kb.KnowledgeBase.load()
    if base is None:
        raise RuntimeError("还没有知识库，先运行 python -m dota_assistant kb update")
    name = lambda h: base.heroes[h].name if h in base.heroes else str(h)
    item_name = {v: base.items[k.removeprefix("item_")].name for k, v in base.item_ids.items()
                 if k.removeprefix("item_") in base.items}
    me = base.heroes[my_id]
    b = Brief(f"本局要点 · {me.name} {POSITION_NAMES.get(pos, '')}")
    if not enemy_ids:
        b.add("说明", [("dim", "没有对面阵容（选人时没识别 / 没填），只给你自己的数据")])

    hero = bench = curves = None
    if cfg.stratz_token:
        for what, fn in (("hero", lambda: fetch_hero(cfg, my_id, pos)), ("bench", lambda: coach.fetch_bench(cfg, my_id, pos)),
                         ("curves", lambda: fetch_curves(cfg))):
            try:
                v = fn()
            except Exception as e:
                log.warning("本局要点：STRATZ %s 没拉到：%s", what, e, exc_info=not isinstance(e, data.NETWORK_ERRORS))
                b.add("说明", [("dim", f"STRATZ 数据没拉到（{what}：{e}）")])
                continue
            hero, bench, curves = (v, bench, curves) if what == "hero" else (hero, v, curves) if what == "bench" \
                else (hero, bench, v)
    else:
        b.add("说明", [("dim", "config.toml 里没有 STRATZ token，只能给对面机制提醒")])

    # 1. 对线：三路分路 → 我这路的对手 / 队友 → 对线数据 → 硬规则提示
    lane = lanes.assign(enemy_ids, ally_ids, pos, side, data.load_positions(cfg), ally_pos)
    guessed = {g.hero: g.pos for g in lane.enemies}
    opp = [g.hero for g in lane.opponents()]
    partner = lane.partner()
    b.lane_ids = opp + ([partner.hero] if partner else [])
    lines = []
    if lane.enemies:
        lines.append(("dim", "分路（按常见位置猜，? = 没把握）：" + lanes.lane_line(lane, name)))
    if opp:
        lines.append(("row", f"你这路对面：{'、'.join(f'{name(h)}（{guessed[h]}号位）' for h in opp)}"
                      + (f"；队友 {name(partner.hero)}" if partner else "")))
    if hero and hero["lane"]:
        tot = [sum(v[i] for v in hero["lane"].values()) for i in range(3)]
        base_edge = (tot[0] - tot[2]) / max(1, sum(tot))
        for h in opp:
            wdl = hero["lane"].get(str(h))
            if not wdl or sum(wdl) < LANE_MIN_GAMES:
                continue
            n = sum(wdl)
            edge = (wdl[0] - wdl[2]) / n
            verdict = "比平时好打" if edge - base_edge >= LANE_GAP else "比平时难打" if base_edge - edge >= LANE_GAP else "和平时差不多"
            lines.append(("warn" if verdict == "比平时难打" else "row",
                          f"对上{name(h)}对线 胜 {wdl[0] / n:.0%} / 平 {wdl[1] / n:.0%} / 负 {wdl[2] / n:.0%}，{verdict}（{n} 局）"))
    foes = [base.heroes[h] for h in opp if h in base.heroes]
    ally = base.heroes.get(partner.hero) if partner else None
    lines += [("warn", w.text, w.quote) if w.kind == "bad" else ("row", f"✓ {w.text}", w.quote)
              for w in lanes.tips(me, foes, ally)]
    b.add("对线", lines)

    # 2. 起手装
    if hero and hero["matches"]:
        picks = []
        for k, c in sorted(hero["start"].items(), key=lambda kv: -kv[1]):
            iid, inst = map(int, k.split(":"))
            if c / hero["matches"] >= START_SHARE and iid in item_name:
                picks.append((iid, inst))
        counts: dict[int, int] = {}
        for iid, _ in picks:
            counts[iid] = counts.get(iid, 0) + 1
        if counts:
            b.add("起手", [("row", "、".join(f"{item_name[i]}{f' ×{c}' if c > 1 else ''}" for i, c in counts.items())
                            + f"（同分段 ≥{START_SHARE:.0%} 的局会带）")])

    # 3. 强势期
    if curves and enemy_ids:
        mine, theirs = team_phases(curves, [my_id, *ally_ids]), team_phases(curves, enemy_ids)
        diffs = [(m - t) if m is not None and t is not None else None for m, t in zip(mine, theirs)]
        lines = [("dim", "　".join(f"{label} {d:+.1f}" for (_, _, label), d in zip(PHASES, diffs) if d is not None)
                  + "（在这段时间结束的局里，我方英雄胜率偏差 - 对面，百分点）")]
        if diffs[0] is not None and diffs[-1] is not None:
            swing = diffs[-1] - diffs[0]
            if swing >= PHASE_GAP:
                lines.insert(0, ("row", f"我方越拖越强：{PHASES[0][2]}对面占优，{PHASES[-1][2]}我方占优。前期别硬接团，守住发育"))
            elif swing <= -PHASE_GAP:
                lines.insert(0, ("warn", f"我方前期强、后期弱：趁 {PHASES[0][2]}抱团推进，拖到 {PHASES[-1][2]}对面占优"))
            else:
                lines.insert(0, ("row", "双方强势期差不多，按装备节奏打"))
        b.add("强势期", lines)

    # 4. 出装：同分段核心装 + 针对这局阵容的装备（局内提醒用的同一套判断），针对装悬停看原文
    enemies = [base.heroes[h] for h in enemy_ids if h in base.heroes]
    needs, notes = coach.plan(me, enemies, base, kb.load_rules(), bench, pos)
    b.needs = needs
    b.add("出装", itembuild.brief_lines(itembuild.make(needs, bench, base)))
    b.add("对面要防", [("row", n) for n in notes])
    return b


# ---------- 回放：拿一局录像看当时会给什么 ----------

def simulate(cfg: Config, match_id: int, account_id: int | None = None, ai: bool = False) -> str:
    import requests
    account_id = account_id or cfg.account_id
    m = requests.get(f"https://api.opendota.com/api/matches/{match_id}", timeout=60).json()
    me = next((p for p in m.get("players", []) if p.get("account_id") == account_id), None)
    if me is None:
        return f"比赛 {match_id} 里找不到好友代码 {account_id}"
    actual = {}                                   # 实际位置（STRATZ），用来对照猜的对线对手
    if cfg.stratz_token:
        try:
            q = "query($id: Long!) { match(id: $id) { players { heroId position } } }"
            actual = {p["heroId"]: int(p["position"][-1]) for p in data._stratz(cfg, q, id=match_id)["match"]["players"]
                      if (p.get("position") or "")[-1:].isdigit()}
        except Exception:
            pass
    pos = actual.get(me["hero_id"]) or cfg.position
    allies = [p["hero_id"] for p in m["players"] if p["isRadiant"] == me["isRadiant"] and p is not me]
    enemies = [p["hero_id"] for p in m["players"] if p["isRadiant"] != me["isRadiant"]]
    b = build(cfg, me["hero_id"], allies, enemies, pos, side="radiant" if me["isRadiant"] else "dire",
              ally_pos={h: actual[h] for h in allies if h in actual})      # 实时版队友位置也是认出来的（选人界面 OCR）
    won = (me["player_slot"] < 128) == m["radiant_win"]
    L = [f"比赛 {match_id}", b.text(), "", f"实际：{'赢' if won else '输'}，{m['duration'] / 60:.0f} 分钟"]
    if actual:
        base = kb.KnowledgeBase.load()
        opp = [h for h in enemies if actual.get(h) in LANE_VS.get(pos, ())]
        L.append("实际同路：" + "、".join(f"{base.heroes[h].name}（{actual[h]}号位）" for h in opp))
    if ai:
        from . import gameplan
        plan = gameplan.generate(cfg, me["hero_id"], allies, enemies, b.lane_ids, b.text())
        L += ["", f"AI 打法（{plan.seconds:.0f} 秒，折合 API ${plan.cost:.2f}；缓存命中时为 0）", plan.text()]
    return "\n".join(L)
