"""把「当前选人局面」变成要显示 / 要念的建议。不依赖界面，方便测试。"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from .. import kb
from . import explain, lanes
from ..core.config import Config
from .engine import POSITION_NAMES, RADIANT_EDGE, SIDE_NAMES, Engine, Suggestion, pct, sigmoid
from ..core.heroes import Hero, HeroDB
from ..sources import assets, player
from ..sources import stats as stats_mod

TARGETED_TITLE = "针对这局"
TEAM_TOP, TEAM_TARGETED = 3, 3   # 剩余位置每个空位置：推荐几个 + 针对这局几个（玩家要求 3 + 3，不跟 config 的 top / top_other）


@dataclass
class Draft:
    allies: list[Hero] = field(default_factory=list)     # 我方已选（不含我自己）
    enemies: list[Hero] = field(default_factory=list)
    bans: list[Hero] = field(default_factory=list)
    my_hero: Hero | None = None                         # 我已经选了的英雄（GSI 告诉我们）
    ally_pos: dict[int, int] = field(default_factory=dict)   # 队友英雄 id → 位置（选人界面 OCR 认出来的）
    # 我这局不想玩的（叠加层推荐卡片上点 ×）：只从给我的推荐里去掉、后面的顺位补上来，不算禁用（队友照样能被推荐）。
    # 不进 key()：局面没变，「如果选它」的卡片不该因此算过期
    skip: list[Hero] = field(default_factory=list)

    def key(self) -> tuple:
        ids = lambda hs: tuple(sorted(h.id for h in hs))
        return ids(self.allies), ids(self.enemies), ids(self.bans), self.my_hero.id if self.my_hero else 0


@dataclass
class TeamSlot:
    """我方一个位置：有人占了（ally，me = 是我自己选好的英雄），或者还空着
    （picks / targeted = 给队友的「推荐 + 针对这局」，TEAM_TOP + TEAM_TARGETED 个；
    mine = 这是我还没选的当前位置，不在这里列英雄，我的推荐看下面的大卡片）"""
    position: int
    ally: lanes.Guess | None = None
    picks: list[Suggestion] = field(default_factory=list)
    targeted: list[Suggestion] = field(default_factory=list)
    me: bool = False
    mine: bool = False

    @property
    def open(self) -> bool:
        """空着、给队友推荐的位置（我还没选的当前位置不算）"""
        return self.ally is None and not self.mine


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
    familiar_min: int = 0                 # 「推荐」只放我生涯打过这么多局的英雄；0 = 没按熟练度分（没生涯数据 / 门槛关了）
    duo: str | None = None                # 空位置（给队友的）是按这位双排队友会玩的英雄分的（他的名字）
    rest: tuple[float, float] | None = None   # 已选阵容（含选好的我）的 (我方, 对面) logit，Engine.draft_rest；还没人选时 None


class Advisor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db: HeroDB = assets.load_heroes()
        self.engine = Engine(self.db, stats_mod.load_stats(cfg), stats_mod.load_positions(cfg))
        self.me = player.load_player(cfg)
        self.career = self.me["career"] if self.me else None   # 我生涯每个英雄 (局数, 胜场)；None = 不知道，推荐不按熟练度分
        self.duo = player.load_duo(cfg)                        # 双排队友 {name, career}；没填 = None
        self.pool = self.db.find_many(cfg.pool)
        self.kb = kb.KnowledgeBase.load()      # 没有知识库也能推荐，只是没有机制说明

    def _suggest(self, draft: Draft, position: int) -> list[Suggestion]:
        history, by_pos = (player.player_history(self.me, position, self.engine.positions)
                           if self.me else ({}, False))
        return self.engine.suggest(position=position, allies=draft.allies, enemies=draft.enemies,
                                   bans=[*draft.bans, *draft.skip], pool=self.pool, only_pool=self.cfg.only_pool,
                                   history=history, history_by_position=by_pos,
                                   position_filter=self.cfg.position_filter, top=None)

    def familiar(self, career: dict[int, tuple[int, int]] | None, pool: list[Hero] = ()) -> set[int] | None:
        """会玩的英雄：生涯打过 cfg.familiar_games 局以上，或者写进了手写英雄池；None = 不分（没生涯数据 / 门槛设成 0）"""
        if not career or self.cfg.familiar_games <= 0:
            return None
        return {h for h, (g, _) in career.items() if g >= self.cfg.familiar_games} | {h.id for h in pool}

    def _split(self, res: list[Suggestion], top: int, targeted: int, has_draft: bool,
               career: dict[int, tuple[int, int]] | None, pool: list[Hero] = ()) -> tuple[list, list]:
        """split_targeted + 按 career 的主人会不会玩分：不熟的标上生涯局数（推荐理由里提示）"""
        familiar = self.familiar(career, pool)
        if familiar is not None:
            for s in res:
                if s.hero.id not in familiar:
                    s.unfamiliar = career.get(s.hero.id, (0, 0))
        return split_targeted(res, top, targeted, has_draft, familiar)

    def team_slots(self, draft: Draft, position: int, shown: list[Suggestion] = ()) -> list[TeamSlot]:
        """队友已选的英雄占了哪几个位置（选人界面认出的优先，其余按常打位置猜），剩下的空位置各给队友「推荐 + 针对这局」
        （split_targeted，TEAM_TOP + TEAM_TARGETED 个；算上和已选队友、和我（选好了的话）的配合，不算我的英雄池 / 战绩；
        填了双排队友就按他会玩的分——不知道哪个空位置是他，所以每个空位置都按他分）。
        我还没选时，我的当前位置不列英雄（mine，我的推荐看下面的大卡片），也不再「帮我挑位置」
        （2026-10-30 玩家：「只针对自己的就行，别在自己的位置上推荐……同行队友的推荐英雄在这里能看到已经足够了」）。
        同一个英雄只出现一次：shown（下面大卡片里给我的）不再列，几个空位置都能推荐的，归到它在哪个位置综合分最高的那个。"""
        me = draft.my_hero
        guesses = {g.pos: g for g in lanes.guess([a.id for a in draft.allies], self.engine.positions,
                                                 taken={position} if me else frozenset(), fixed=draft.ally_pos)}
        allies = [*draft.allies, *([me] if me else [])]
        career = self.duo and self.duo["career"]
        slots, res_of = {}, {}
        for p in POSITION_NAMES:
            if me and p == position:
                slots[p] = TeamSlot(p, lanes.Guess(me.id, p, 1.0), me=True)
            elif p in guesses:
                slots[p] = TeamSlot(p, guesses[p])
            elif p == position:
                slots[p] = TeamSlot(p, mine=True)
            else:
                slots[p] = TeamSlot(p)
                res_of[p] = self.engine.suggest(position=p, allies=allies, enemies=draft.enemies, bans=draft.bans,
                                                position_filter=self.cfg.position_filter, top=None)
        used = {s.hero.id for s in shown}
        home = {}                                            # 英雄 → 它综合分最高的那个空位置
        for p, res in res_of.items():
            for s in res:
                if s.hero.id not in used and (s.hero.id not in home or s.score > home[s.hero.id][1]):
                    home[s.hero.id] = (p, s.score)
        for p, res in res_of.items():
            mine_only = [s for s in res if home.get(s.hero.id, (None,))[0] == p]
            slots[p].picks, slots[p].targeted = self._split(mine_only, TEAM_TOP, TEAM_TARGETED, True, career)
        return list(slots.values())

    def advise(self, draft: Draft, position: int) -> Advice:
        team = draft.allies + ([draft.my_hero] if draft.my_hero else [])
        threat = None
        if draft.enemies and team:
            e, v = self.engine.threats(draft.enemies, team)[0]
            if pct(v) >= 1.5:
                threat = (e, v)
        lane = lanes.assign([e.id for e in draft.enemies], [a.id for a in draft.allies], position, None,
                            self.engine.positions, draft.ally_pos) if draft.enemies and self.engine.positions else None
        duo = (self.duo["name"] or "双排队友") if self.duo and self.familiar(self.duo["career"]) is not None else None
        rest = self.draft_rest(draft, position) if draft.allies or draft.enemies or draft.my_hero else None
        if draft.my_hero:
            return Advice(position, draft, [], [], threat, lanes=lane, lane_tips=self.lane_tips(draft.my_hero, lane),
                          team=self.team_slots(draft, position), duo=duo, rest=rest)
        res = self._suggest(draft, position)
        picks, targeted = self._split(res, self.cfg.top, self.cfg.top_other, bool(draft.enemies or draft.allies),
                                      self.career, self.pool)
        slots = self.team_slots(draft, position, picks + targeted) if draft.allies else []
        gated = self.cfg.familiar_games if self.familiar(self.career, self.pool) is not None else 0
        return Advice(position, draft, picks, targeted, threat, self.explain(picks + targeted, draft.enemies), lane,
                      team=slots, familiar_min=gated, duo=duo, rest=rest)

    def draft_rest(self, draft: Draft, position: int) -> tuple[float, float]:
        """已选阵容的 (我方, 对面) logit（Engine.draft_rest）：选好的我按当前位置，队友用选人界面认出的位置，
        其余按常打位置猜（和分路同一个猜法）"""
        pos = self.engine.positions
        mine = {g.hero: g.pos for g in lanes.guess([a.id for a in draft.allies], pos, taken={position}, fixed=draft.ally_pos)}
        theirs = {g.hero: g.pos for g in lanes.guess([e.id for e in draft.enemies], pos)}
        allies = [(a.id, mine.get(a.id)) for a in draft.allies] + ([(draft.my_hero.id, position)] if draft.my_hero else [])
        return self.engine.draft_rest(allies, [(e.id, theirs.get(e.id)) for e in draft.enemies])

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


def split_targeted(res: list[Suggestion], top: int, targeted: int, has_draft: bool,
                   familiar: set[int] | None = None) -> tuple[list, list]:
    """(综合推荐, 针对这局)。res 已按综合分排好。
    版本强势的英雄自身强度领先 3~5 个百分点，单个敌人的克制中位数才 1 个百分点，只看综合分每局前几名都差不多
    （2026-10-26 玩家反馈「推荐趋同」）。所以综合前 top 个之外，再从剩下的里按「克制 + 配合」挑 targeted 个，
    只要综合胜率不低于 50%（免得挑出自身太弱、克一个人也救不回来的）。
    还没有人选（没有克制 / 配合可言）时只有综合推荐，一共 top + targeted 个。
    familiar = 会玩的英雄 id（Advisor.familiar）：「推荐」只从这些里按综合分挑，不熟的只可能进「针对这局」
    （2026-10-30 玩家反馈：克制英雄排进推荐，拿了却不会玩；胜率是会玩的人打出来的）。
    这个位置一个会玩的都没有时不分，免得「推荐」整组空着。None = 不分。"""
    mine = res if familiar is None else [s for s in res if s.hero.id in familiar] or res
    if not has_draft:
        return mine[:top + targeted], []
    picks = mine[:top]
    chosen = {s.hero.id for s in picks}
    rest = sorted((s for s in res if s.hero.id not in chosen and s.score >= 0), key=lambda s: -s.situational_pct)   # 按显示的百分点排，和卡片上的数字顺序一致
    return picks, rest[:targeted]


def best(advice: Advice) -> list[Suggestion]:
    """播报用的前两名（综合推荐）"""
    return (advice.picks + advice.targeted)[:2]


def groups(advice: Advice) -> list[tuple[str, str, list[Suggestion]]]:
    """[(标题, 小字说明, 英雄)]"""
    return [("推荐", f"你打过 {advice.familiar_min} 局以上的" if advice.familiar_min else "", advice.picks),
            (TARGETED_TITLE, "克制 + 配合最多的" + ("，不管熟不熟" if advice.familiar_min else ""), advice.targeted)]


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
    rate = draft_rate(advice, side)
    if rate:
        out.append(("dim", f"阵容胜率 {rate['winrate']:.1f}%（{rate['note']}）"))
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
    for title, note, group in groups(advice):
        if group:
            out.append(("head", title + (f"（{note}）" if note else "")))
            for s in group:
                # 「针对这局」按克制 + 配合挑的，胜率后面分开写这两项（玩家 2026-10-30：一眼看到这个选项的克制值，克制 / 配合分开）
                b = {k: v for k, _, v in s.breakdown()}
                situ = f"（克制 {b['counter']:+.1f} 配合 {b['syn']:+.1f}）" if title == TARGETED_TITLE else ""
                out.append(("row", f"{s.hero.name_cn}  {s.winrate:.1f}%{situ}　{s.reason()}"))
                out.append(("dim", "　算法 " + s.calc_text(POSITION_NAMES[advice.position])))
                if s.vs:
                    out.append(("dim", "　对位 " + "  ".join(f"{e.name_cn} {pct(v):+.1f}" for e, v in s.vs)))
                if s.syn:          # 和每个已选队友的配合，负的也列出来（相互拖累，例如抢同一个位置）
                    out.append(("dim", "　配合 " + "  ".join(f"{a.name_cn} {pct(v):+.1f}" for a, v in s.syn)))
                out += [("dim", f"　{'✓' if w.kind == 'good' else '✗'} {w.text}") for w in advice.why.get(s.hero.id, [])]
    if advice.threat:
        e, v = advice.threat
        out.append(("warn", f"注意：{e.name_cn} 对我方威胁最大（{pct(v):+.1f}%）"))
    return out


def team_line(advice: Advice) -> str:
    """一行字的剩余位置（空位置都是给队友的）：「（给队友）中单 帕吉 55.7% / … ｜针对 …　四号位 …
    （队友占了：五号位 水晶室女?、你：一号位）」；我选好以后「（已占：一号位 你 敌法师、…）」"""
    names = {"open": [], "taken": []}
    for t in advice.team:
        mark = "（当前）" if t.position == advice.position and not advice.draft.my_hero else ""
        if t.me:
            names["taken"].append(f"{POSITION_NAMES[t.position]} 你 {advice.draft.my_hero.name_cn}")
        elif t.mine:
            continue                                         # 我的位置不列英雄，看上面的推荐
        elif t.open:
            names_of = lambda ss: " / ".join(f"{s.hero.name_cn} {s.winrate:.1f}%{'（不熟）' if s.unfamiliar else ''}"
                                             for s in ss)
            picks = (names_of(t.picks) + (f" ｜针对 {names_of(t.targeted)}" if t.targeted else "")) or "无候选"
            names["open"].append(f"{POSITION_NAMES[t.position]}{mark} {picks}")
        else:
            h = _namer(advice.draft)(t.ally.hero)
            names["taken"].append(f"{POSITION_NAMES[t.position]}{mark} {h}{'' if t.ally.conf >= lanes.SURE else '?'}")
    who = f"（给队友，按{advice.duo}会玩的分）" if advice.duo else "（给队友）"
    text = who + ("　".join(names["open"]) or "无")
    if names["taken"]:
        text += ("　（已占：" if advice.draft.my_hero else "　（队友占了：") + "、".join(names["taken"]) + "）"
    mine = next((t for t in advice.team if t.mine), None)
    if mine:
        text += f"　你：{POSITION_NAMES[mine.position]}"
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
    for title, note, group in groups(advice):
        if group:
            # targeted：「针对这局」卡片上把 calc 里的克制、配合两项单独标出来
            out.append({"title": title, "note": note, "targeted": title == TARGETED_TITLE, "items": [
                {**hero_ref(s.hero), "winrate": round(s.winrate, 1),
                 "tags": [{"kind": k, "text": t} for k, t in s.reason_parts()],
                 # 对每个已选敌人的克制（百分点，正 = 这个推荐克他），按敌方选人顺序，各卡片列对齐
                 "vs": [{**hero_ref(e), "delta": round(pct(v), 1)} for e, v in s.vs],
                 # 和每个已选队友的配合（百分点，正 = 配得好，负 = 相互拖累），按队友选人顺序
                 "syn": [{**hero_ref(a), "delta": round(pct(v), 1)} for a, v in s.syn],
                 # 胜率怎么来的：整体 → 位置 → 冷门打折 → 自身（小计）→ 克制 → 配合 → 其他，依次加起来 = winrate
                 "calc": calc_view(s, advice.position),
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
        "draft_rate": draft_rate(advice, side),   # 叠加层标题栏的「阵容 xx%」；还没人选时 None
        "enemies": [hero_ref(h) for h in d.enemies],
        "allies": [hero_ref(h) for h in d.allies],
        "bans": [hero_ref(h) for h in d.bans],
        "my_hero": hero_ref(d.my_hero) if d.my_hero else None,
        "groups": out,
        "skipped": [] if d.my_hero else [hero_ref(h) for h in d.skip],   # 这局不想玩的，叠加层列出来、点名字恢复
        "threat": threat,
        # 对面分路（猜的）+ 我选好英雄后我这路的对线提示
        "lanes": lanes.view(replace(advice.lanes, side=side), lambda i: {"id": i, "name": _namer(d)(i)},
                            advice.lane_tips) if advice.lanes else None,
        "team": team_view(advice),
        "duo": advice.duo,
    }


def team_view(advice: Advice) -> list[dict] | None:
    """我方五个位置：有人占了的（猜的带把握，me = 我自己选好的）/ 我的当前位置（mine，不列英雄）/
    空着的（给队友的前几个推荐）。我还没选时点空位置仍能把我的位置切过去"""
    if not advice.team:
        return None
    namer = _namer(advice.draft)
    return [{
        "position": t.position,
        "name": POSITION_NAMES[t.position],
        "current": t.position == advice.position,
        "me": t.me,
        "mine": t.mine,
        "ally": {"id": t.ally.hero, "name": namer(t.ally.hero), "conf": round(t.ally.conf, 2),
                 "sure": t.ally.conf >= lanes.SURE} if t.ally else None,
        "picks": [team_pick(s, t.position) for s in t.picks],
        "targeted": [team_pick(s, t.position) for s in t.targeted],
    } for t in advice.team]


def team_pick(s: Suggestion, position: int) -> dict:
    """剩余位置里一个推荐：头像 + 胜率 + 胜率怎么来的（和推荐卡片同一份 calc_view，直接显示）；不熟的带生涯 [局数, 胜场]"""
    return {**hero_ref(s.hero), "winrate": round(s.winrate, 1), "calc": calc_view(s, position),
            **({"unfamiliar": list(s.unfamiliar)} if s.unfamiliar is not None else {})}


def draft_rate(advice: Advice, side: str | None) -> dict | None:
    """已选阵容的胜率：{winrate（%）, note（说明一句）}。我方已选（含选好的我）+ 对面已选 + 阵营（天辉 +RADIANT_EDGE / 夜魇 −），
    没选的人当平均水平；对所有候选都一样，所以只在顶上放一次（玩家 2026-10-30：每张卡片一行「全队」太挤）。还没人选时 None"""
    if advice.rest is None:
        return None
    edge = {"radiant": RADIANT_EDGE, "dire": -RADIANT_EDGE}.get(side or "", 0.0)
    me = "含你" if advice.draft.my_hero else "你这手按平均算"
    return {"winrate": round(sigmoid(sum(advice.rest) + edge) * 100, 1),
            "note": f"已选的英雄 + {SIDE_NAMES.get(side or '', '阵营未知')}，{me}"}


def calc_view(s: Suggestion, position: int) -> list[dict]:
    """Suggestion.breakdown 给网页：位置那步换成具体位置名，数值保留一位"""
    return [{"kind": k, "name": POSITION_NAMES[position] if k == "pos" else n, "value": round(v, 1)}
            for k, n, v in s.breakdown()]
