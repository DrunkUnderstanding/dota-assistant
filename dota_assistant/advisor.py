"""把「当前选人局面」变成要显示 / 要念的建议。不依赖界面，方便测试。"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import data
from .config import Config
from .engine import POSITION_NAMES, Engine, Suggestion, pct
from .heroes import Hero, HeroDB


@dataclass
class Draft:
    allies: list[Hero] = field(default_factory=list)     # 我方已选（不含我自己）
    enemies: list[Hero] = field(default_factory=list)
    bans: list[Hero] = field(default_factory=list)
    my_hero: Hero | None = None                         # 我已经选了的英雄（GSI 告诉我们）

    def key(self) -> tuple:
        ids = lambda hs: tuple(sorted(h.id for h in hs))
        return ids(self.allies), ids(self.enemies), ids(self.bans), self.my_hero.id if self.my_hero else 0


@dataclass
class Advice:
    position: int
    draft: Draft
    mine: list[Suggestion]
    other: list[Suggestion]
    threat: tuple[Hero, float] | None     # 对我方威胁最大的敌方英雄（够显著才有）


class Advisor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db: HeroDB = data.load_heroes()
        self.engine = Engine(self.db, data.load_stats(cfg), data.load_positions(cfg))
        self.me = data.load_player(cfg)
        self.pool = self.db.find_many(cfg.pool)

    def advise(self, draft: Draft, position: int) -> Advice:
        history, by_pos = (data.player_history(self.me, position, self.engine.positions)
                           if self.me else ({}, False))
        team = draft.allies + ([draft.my_hero] if draft.my_hero else [])
        threat = None
        if draft.enemies and team:
            e, v = self.engine.threats(draft.enemies, team)[0]
            if pct(v) >= 1.5:
                threat = (e, v)
        if draft.my_hero:
            return Advice(position, draft, [], [], threat)
        res = self.engine.suggest(position=position, allies=draft.allies, enemies=draft.enemies,
                                  bans=draft.bans, pool=self.pool, only_pool=self.cfg.only_pool,
                                  history=history, history_by_position=by_pos,
                                  position_filter=self.cfg.position_filter, top=None)
        mine = [s for s in res if s.in_pool][:self.cfg.top]
        other = [s for s in res if not s.in_pool][:self.cfg.top_other]
        return Advice(position, draft, mine, other, threat)


def best(advice: Advice) -> list[Suggestion]:
    """播报用的前两名：先你的英雄，不够再补其他"""
    return (advice.mine + advice.other)[:2]


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
    if d.my_hero:
        out.append(("head", f"你已选：{d.my_hero.name_cn}"))
    for title, group in (("你的英雄", advice.mine), ("其他选择", advice.other)):
        if group:
            out.append(("head", title))
            out += [("row", f"{s.hero.name_cn}  {s.winrate:.1f}%　{s.reason()}") for s in group]
    if advice.threat:
        e, v = advice.threat
        out.append(("warn", f"注意：{e.name_cn} 对我方威胁最大（{pct(v):+.1f}%）"))
    return out
