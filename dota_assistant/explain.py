"""选人推荐的「为什么」：拿推荐英雄和对面英雄的技能机制对照（知识库，Valve 官方原文），说清楚克在哪、怕在哪。

只认几组机制上的硬对位（克敌机先 ↔ 闪避、显形 ↔ 隐身、破坏 ↔ 被动、无视免疫 ↔ 技能免疫、技能免疫 ↔ 不无视免疫的控制），
每条都带命中的官方原句，方便核对；标签是关键词匹配，可能误判，所以界面上原句一起给。
统计上的克制（实际对位胜率）还是 engine 算的，这里只补「机制上为什么」，不影响排名。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import kb

# (我方标签们, 对面标签们, 说法)：任一对上就算，一个敌人每组只说一次；{me} = 我方技能，{who} = 对面英雄，{ab} = 对面技能
GOOD = (
    (("克敌机先", "无视闪避"), ("提供闪避",), "{me}无视闪避，{who}的{ab}闪避对你无效"),
    (("反隐 / 显形",), ("隐身",), "{me}能显形，{who}的{ab}隐身藏不住"),
    (("施加破坏",), ("怕破坏",), "{me}能施加破坏，{who}的{ab}被破坏后失效"),
    (("无视技能免疫",), ("提供技能免疫",), "{me}无视技能免疫，{who}开{ab}也躲不掉"),
)
# 反过来：对面的机制压我方
BAD = (
    (("提供闪避",), ("克敌机先", "无视闪避"), "{who}的{ab}无视闪避，你的{me}闪避没用"),
    (("隐身",), ("反隐 / 显形",), "{who}的{ab}能显形，你的{me}隐身藏不住"),
    (("怕破坏",), ("施加破坏",), "{who}的{ab}能施加破坏，你的{me}被破坏后失效"),
    (("提供技能免疫",), ("无视技能免疫",), "{who}的{ab}无视技能免疫，你的{me}挡不住"),
)
# 我有技能免疫时，对面不无视免疫的控制能躲开
CONTROL_SAY = "{me}有技能免疫，能躲{who}的{ab}"


@dataclass
class Why:
    kind: str          # good 克 / bad 怕
    text: str
    quote: str         # 依据：官方原句（我方 + 对面）


def _label(ab: kb.Entry, sentence: str) -> str:
    """技能名；原句来自神杖 / 魔晶说明时注明，免得当成白板就有"""
    if sentence in ab.text or any(sentence in n for n in ab.notes):
        return ab.name
    if ab.scepter and sentence in ab.scepter:
        return f"{ab.name}（神杖）"
    if ab.shard and sentence in ab.shard:
        return f"{ab.name}（魔晶）"
    return ab.name


def _first(h: kb.HeroKB, tags: tuple[str, ...]) -> tuple[kb.Entry, str] | None:
    hits = h.with_tag(*tags)
    return (hits[0][0], hits[0][2]) if hits else None


def _unrevealable(invis_sentence: str, tags: tuple[str, ...]) -> bool:
    """官方原文写明「不会被反隐手段发现」的隐身（斯拉克暗影之舞），显形 / 真实视域对它没用"""
    return "隐身" in tags and "不会被反隐" in invis_sentence


def matchups(me: kb.HeroKB, enemies: list[kb.HeroKB], limit: int = 3) -> list[Why]:
    """我方英雄对上已选敌方英雄的机制对位：最多 limit 条克的（每个敌人一条）+ 1 条怕的"""
    good, bad = [], []
    imm = _first(me, ("提供技能免疫",))
    for e in enemies:
        mine: list[Why] = []
        if imm:
            # 官方 immunity 字段明确「不无视技能免疫」的控制才算躲得开
            ctrl = [(ab, s) for ab, _, s in e.with_tag(*kb.CONTROL_TAGS) if ab.pierces_immunity is False]
            if ctrl:
                ab, s = ctrl[0]
                mine.append(Why("good", CONTROL_SAY.format(me=_label(*imm), who=e.name, ab=_label(ab, s)),
                                f"{me.name}·{imm[0].name}：{imm[1]}　{e.name}·{ab.name}：{s}"))
        for rules, out, kind in ((GOOD, mine, "good"), (BAD, bad, "bad")):
            for my_tags, their_tags, say in rules:
                m, t = _first(me, my_tags), _first(e, their_tags)
                if m and t and not _unrevealable(m[1] if kind == "bad" else t[1], my_tags + their_tags):
                    out.append(Why(kind, say.format(me=_label(*m), who=e.name, ab=_label(*t)),
                                   f"{me.name}·{m[0].name}：{m[1]}　{e.name}·{t[0].name}：{t[1]}"))
        good += mine[:1]                             # 一个敌人只说最要紧的一条，免得刷屏
    return good[:limit] + bad[:1]
