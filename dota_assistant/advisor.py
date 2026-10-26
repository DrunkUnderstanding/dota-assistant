"""把「当前选人局面」变成要显示 / 要念的建议。不依赖界面，方便测试。"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from . import data, explain, kb, lanes
from .config import Config
from .engine import POSITION_NAMES, Engine, Suggestion, pct
from .heroes import Hero, HeroDB

TEAM_PICKS = 2          # 剩余位置每个推荐几个英雄
TARGETED_TITLE = "针对这局"


@dataclass
class Draft:
    allies: list[Hero] = field(default_factory=list)     # 我方已选（不含我自己）
    enemies: list[Hero] = field(default_factory=list)
    bans: list[Hero] = field(default_factory=list)
    my_hero: Hero | None = None                         # 我已经选了的英雄（GSI 告诉我们）
    ally_pos: dict[int, int] = field(default_factory=dict)   # 队友英雄 id → 位置（选人界面 OCR 认出来的）

    def key(self) -> tuple:
        ids = lambda hs: tuple(sorted(h.id for h in hs))
        return ids(self.allies), ids(self.enemies), ids(self.bans), self.my_hero.id if self.my_hero else 0


@dataclass
class TeamSlot:
    """我方一个位置：有人占了（ally，me = 是我自己选好的英雄），或者还空着
    （picks = 这个位置的前几个推荐：我还没选时是给我挑位置的，我选好以后是给队友的）"""
    position: int
    ally: lanes.Guess | None = None
    picks: list[Suggestion] = field(default_factory=list)
    me: bool = False

    @property
    def open(self) -> bool:
        return self.ally is None


@dataclass
class Advice:
    position: int
    draft: Draft
    picks: list[Suggestion]               # 综合推荐：按综合胜率排的前 cfg.top 个
    targeted: list[Suggestion]            # 针对这局：剩下的里按克制 + 配合排的前 cfg.top_other 个（见 Advisor.advise）
    threat: tuple[Hero, float] | None     # 对我方威胁最大的敌方英雄（够显著才有）
    why: dict[int, list[explain.Why]] = field(default_factory=dict)   # 推荐英雄 id → 机制上克 / 怕谁（知识库原文）
    lanes: lanes.Lanes | None = None      # 对面分路（猜的）
    lane_tips: list[explain.Why] = field(default_factory=list)       # 我选了英雄后：我这路怎么打（知识库原文）
    team: list[TeamSlot] = field(default_factory=list)   # 我方五个位置：谁占了哪几个、剩下的每个位置推荐谁（有队友选了 / 我选好了才有）

    def best_open(self) -> TeamSlot | None:
        """剩余位置里第一推荐的英雄胜率最高的那个位置（我还没选时才有：给我挑位置用）"""
        if self.draft.my_hero:
            return None
        return max((t for t in self.team if t.open and t.picks), key=lambda t: t.picks[0].score, default=None)


class Advisor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db: HeroDB = data.load_heroes()
        self.engine = Engine(self.db, data.load_stats(cfg), data.load_positions(cfg))
        self.me = data.load_player(cfg)
        self.pool = self.db.find_many(cfg.pool)
        self.kb = kb.KnowledgeBase.load()      # 没有知识库也能推荐，只是没有机制说明

    def _suggest(self, draft: Draft, position: int) -> list[Suggestion]:
        history, by_pos = (data.player_history(self.me, position, self.engine.positions)
                           if self.me else ({}, False))
        return self.engine.suggest(position=position, allies=draft.allies, enemies=draft.enemies,
                                   bans=draft.bans, pool=self.pool, only_pool=self.cfg.only_pool,
                                   history=history, history_by_position=by_pos,
                                   position_filter=self.cfg.position_filter, top=None)

    def team_slots(self, draft: Draft, position: int, current: list[Suggestion]) -> list[TeamSlot]:
        """队友已选的英雄占了哪几个位置（选人界面认出的优先，其余按常打位置猜），剩下的位置各推荐前 TEAM_PICKS 个。
        我还没选：不先替我占位，推荐按「我打这个位置」算（英雄池、个人战绩都算）——帮我挑打哪个位置；current = 当前位置已经算好的推荐。
        我选好了：我占当前位置，空位置推荐给队友（算上和我的配合，不算我的英雄池 / 战绩），一直显示到进游戏。"""
        me = draft.my_hero
        guesses = {g.pos: g for g in lanes.guess([a.id for a in draft.allies], self.engine.positions,
                                                 taken={position} if me else frozenset(), fixed=draft.ally_pos)}
        out = []
        for p in POSITION_NAMES:
            if me and p == position:
                out.append(TeamSlot(p, lanes.Guess(me.id, p, 1.0), me=True))
            elif p in guesses:
                out.append(TeamSlot(p, guesses[p]))
            elif me:
                picks = self.engine.suggest(position=p, allies=[*draft.allies, me], enemies=draft.enemies, bans=draft.bans,
                                            position_filter=self.cfg.position_filter, top=TEAM_PICKS)
                out.append(TeamSlot(p, picks=picks))
            else:
                out.append(TeamSlot(p, picks=(current if p == position else self._suggest(draft, p))[:TEAM_PICKS]))
        return out

    def advise(self, draft: Draft, position: int) -> Advice:
        team = draft.allies + ([draft.my_hero] if draft.my_hero else [])
        threat = None
        if draft.enemies and team:
            e, v = self.engine.threats(draft.enemies, team)[0]
            if pct(v) >= 1.5:
                threat = (e, v)
        lane = lanes.assign([e.id for e in draft.enemies], [a.id for a in draft.allies], position, None,
                            self.engine.positions, draft.ally_pos) if draft.enemies and self.engine.positions else None
        if draft.my_hero:
            return Advice(position, draft, [], [], threat, lanes=lane, lane_tips=self.lane_tips(draft.my_hero, lane),
                          team=self.team_slots(draft, position, []))
        res = self._suggest(draft, position)
        picks, targeted = split_targeted(res, self.cfg.top, self.cfg.top_other, bool(draft.enemies or draft.allies))
        slots = self.team_slots(draft, position, res) if draft.allies else []
        return Advice(position, draft, picks, targeted, threat, self.explain(picks + targeted, draft.enemies), lane,
                      team=slots)

    def lane_tips(self, me: Hero, lane: lanes.Lanes | None) -> list[explain.Why]:
        if not (self.kb and lane and me.id in self.kb.heroes):
            return []
        foes = [self.kb.heroes[g.hero] for g in lane.opponents() if g.hero in self.kb.heroes]
        partner = lane.partner()
        return lanes.tips(self.kb.heroes[me.id], foes, self.kb.heroes.get(partner.hero) if partner else None)

    def explain(self, picks: list[Suggestion], enemies: list[Hero]) -> dict[int, list[explain.Why]]:
        if not (self.kb and enemies):
            return {}
        foes = [self.kb.heroes[e.id] for e in enemies if e.id in self.kb.heroes]
        return {s.hero.id: explain.matchups(self.kb.heroes[s.hero.id], foes, limit=2)
                for s in picks if s.hero.id in self.kb.heroes}


def split_targeted(res: list[Suggestion], top: int, targeted: int, has_draft: bool) -> tuple[list, list]:
    """(综合推荐, 针对这局)。res 已按综合分排好。
    版本强势的英雄自身强度领先 3~5 个百分点，单个敌人的克制中位数才 1 个百分点，只看综合分每局前几名都差不多
    （2026-10-26 玩家反馈「推荐趋同」）。所以综合前 top 个之外，再从剩下的里按「克制 + 配合」挑 targeted 个，
    只要综合胜率不低于 50%（免得挑出自身太弱、克一个人也救不回来的）。
    还没有人选（没有克制 / 配合可言）时只有综合推荐，一共 top + targeted 个。"""
    if not has_draft:
        return res[:top + targeted], []
    rest = sorted((s for s in res[top:] if s.score >= 0), key=lambda s: -s.situational)
    return res[:top], rest[:targeted]


def best(advice: Advice) -> list[Suggestion]:
    """播报用的前两名（综合推荐）"""
    return (advice.picks + advice.targeted)[:2]


def groups(advice: Advice) -> list[tuple[str, list[Suggestion]]]:
    return [("推荐", advice.picks), (TARGETED_TITLE, advice.targeted)]


def speech(prev: Draft | None, advice: Advice, force: bool = False) -> str | None:
    """局面变化时要念的一句话；没什么新东西就返回 None。force=True（热键）时总是念当前推荐。"""
    d = advice.draft
    new_enemies = [h for h in d.enemies if not prev or h.id not in {e.id for e in prev.enemies}]
    parts = []
    if new_enemies:
        parts.append("对面拿了" + "、".join(h.name_cn for h in new_enemies))
    if d.my_hero:
        if advice.threat and (force or new_enemies):
            parts.append(f"小心{advice.threat[0].name_cn}")
        return "。".join(parts) if parts else None
    if not (force or new_enemies or prev is None):
        return None
    top = best(advice)
    if top:
        parts.append("推荐" + "，或者".join(s.hero.name_cn for s in top))
    return "。".join(parts) if parts else None


def render_lines(advice: Advice, side: str | None) -> list[tuple[str, str]]:
    """叠加层 / 控制面板显示的内容：[(样式, 文本)]，样式 = title / dim / head / row / warn"""
    d = advice.draft
    side_cn = {"radiant": "天辉", "dire": "夜魇"}.get(side or "", "阵营未知")
    out = [("title", f"选人助手 · {POSITION_NAMES[advice.position]} · {side_cn}")]
    if d.enemies:
        out.append(("dim", "敌：" + " ".join(h.name_cn for h in d.enemies)))
    if d.allies:
        out.append(("dim", "友：" + " ".join(h.name_cn for h in d.allies)))
    if advice.lanes:
        out.append(("dim", "分路（猜）：" + lanes.lane_line(replace(advice.lanes, side=side), _namer(d))))
    if advice.team:
        out.append(("dim", "剩余位置：" + team_line(advice)))
    if d.my_hero:
        out.append(("head", f"你已选：{d.my_hero.name_cn}"))
        out += [("dim", f"　{'✓' if w.kind == 'good' else '✗'} {w.text}") for w in advice.lane_tips]
    for title, group in groups(advice):
        if group:
            out.append(("head", title + ("（克制 + 配合最多的）" if title == TARGETED_TITLE else "")))
            for s in group:
                out.append(("row", f"{s.hero.name_cn}  {s.winrate:.1f}%　{s.reason()}"))
                if s.vs:
                    out.append(("dim", "　对位 " + "  ".join(f"{e.name_cn} {pct(v):+.1f}" for e, v in s.vs)))
                out += [("dim", f"　{'✓' if w.kind == 'good' else '✗'} {w.text}") for w in advice.why.get(s.hero.id, [])]
    if advice.threat:
        e, v = advice.threat
        out.append(("warn", f"注意：{e.name_cn} 对我方威胁最大（{pct(v):+.1f}%）"))
    return out


def team_line(advice: Advice) -> str:
    """一行字的剩余位置：「一号位 敌法师 52.1% / 幽鬼 51.8%　四号位 …　（队友占了：五号位 水晶室女?）推荐打一号位」；
    我选好以后：「（给队友）中单 … （已占：一号位 你 敌法师、…）」"""
    names = {"open": [], "taken": []}
    for t in advice.team:
        mark = "（当前）" if t.position == advice.position and not advice.draft.my_hero else ""
        if t.me:
            names["taken"].append(f"{POSITION_NAMES[t.position]} 你 {advice.draft.my_hero.name_cn}")
        elif t.open:
            picks = " / ".join(f"{s.hero.name_cn} {s.winrate:.1f}%" for s in t.picks) or "无候选"
            names["open"].append(f"{POSITION_NAMES[t.position]}{mark} {picks}")
        else:
            h = _namer(advice.draft)(t.ally.hero)
            names["taken"].append(f"{POSITION_NAMES[t.position]}{mark} {h}{'' if t.ally.conf >= lanes.SURE else '?'}")
    text = ("（给队友）" if advice.draft.my_hero else "") + ("　".join(names["open"]) or "无")
    if names["taken"]:
        text += ("　（已占：" if advice.draft.my_hero else "　（队友占了：") + "、".join(names["taken"]) + "）"
    best = advice.best_open()
    if best and best.position != advice.position:
        text += f"　→ 推荐打{POSITION_NAMES[best.position]}"
    return text


def hero_ref(h: Hero) -> dict:
    return {"id": h.id, "name": h.name_cn}


def _namer(d: Draft):
    """英雄 id → 中文名（这局阵容里的）"""
    names = {h.id: h.name_cn for h in [*d.allies, *d.enemies, *([d.my_hero] if d.my_hero else [])]}
    return lambda i: names.get(i, str(i))


def render_view(advice: Advice, side: str | None) -> dict:
    """和 render_lines 同样的内容，但保留结构（英雄 id、胜率、理由分段），给网页叠加层画头像 / 卡片用。可直接转 JSON。"""
    d = advice.draft
    out = []
    for title, group in groups(advice):
        if group:
            out.append({"title": title, "items": [
                {**hero_ref(s.hero), "winrate": round(s.winrate, 1),
                 "tags": [{"kind": k, "text": t} for k, t in s.reason_parts()],
                 # 对每个已选敌人的克制（百分点，正 = 这个推荐克他），按敌方选人顺序，各卡片列对齐
                 "vs": [{**hero_ref(e), "delta": round(pct(v), 1)} for e, v in s.vs],
                 "why": [w.__dict__ for w in advice.why.get(s.hero.id, [])]} for s in group]})
    threat = None
    if advice.threat:
        e, v = advice.threat
        threat = {**hero_ref(e), "delta": round(pct(v), 1)}
    return {
        "kind": "draft",
        "position": advice.position,
        "position_name": POSITION_NAMES[advice.position],
        "side": side,
        "side_name": {"radiant": "天辉", "dire": "夜魇"}.get(side or "", "阵营未知"),
        "enemies": [hero_ref(h) for h in d.enemies],
        "allies": [hero_ref(h) for h in d.allies],
        "bans": [hero_ref(h) for h in d.bans],
        "my_hero": hero_ref(d.my_hero) if d.my_hero else None,
        "groups": out,
        "threat": threat,
        # 对面分路（猜的）+ 我选好英雄后我这路的对线提示
        "lanes": lanes.view(replace(advice.lanes, side=side), lambda i: {"id": i, "name": _namer(d)(i)},
                            advice.lane_tips) if advice.lanes else None,
        "team": team_view(advice),
    }


def team_view(advice: Advice) -> list[dict] | None:
    """我方五个位置：有人占了的（猜的带把握，me = 我自己）/ 空着的（前几个推荐）。我还没选时点空位置切过去"""
    if not advice.team:
        return None
    best = advice.best_open()
    namer = _namer(advice.draft)
    return [{
        "position": t.position,
        "name": POSITION_NAMES[t.position],
        "current": t.position == advice.position,
        "me": t.me,
        "best": best is not None and t is best,
        "ally": {"id": t.ally.hero, "name": namer(t.ally.hero), "conf": round(t.ally.conf, 2),
                 "sure": t.ally.conf >= lanes.SURE} if t.ally else None,
        "picks": [{**hero_ref(s.hero), "winrate": round(s.winrate, 1)} for s in t.picks],
    } for t in advice.team]
