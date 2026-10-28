"""后期团战：对面哪几个大招要等、谁的持续施法要留打断、控制留给对面谁、谁能保人、指向技能别往哪丢。

和对线提示（lanes.tips）一样只用硬规则，不用大模型，每条带 Valve 官方原文或官方数据当依据：
- 大招：官方字段 type = 1；冷却按满级（30 分钟后大招基本满级），短于 KEY_ULT_CD 秒的（脉冲新星、侵蚀雾霭）不算「要等的大招」；
  无视技能免疫用官方 immunity 字段，持续施法用官方 behavior 字段，范围大招看原文关键词；
- 对面核心 = 猜的一号位 / 中单（lanes.guess，按常见位置猜的）；
- 保人技能、跑路 / 保命技能、控制都是原文关键词。
规则、措辞、排序是实现假设，玩家没确认过。
"""
from __future__ import annotations

import re

from .. import kb
from ..death import SAVE_EXCLUDE, SAVE_RE
from .explain import Why
from .lanes import ESCAPE_EXCLUDE, INTERRUPT_TAGS, UNTOUCHABLE

KEY_ULT_CD = 40               # 满级冷却 ≥ 这么多秒的大招才算「交了是窗口」
MAX_TIPS = 6
CORE_POSITIONS = (1, 2)       # 「留控给谁」看对面这两个位置
# 范围大招：没交前别和队友挤在一起
# 「附近所有友军单位获得护甲」不算（淘汰之刃是单体），所以中间不能隔着「友」
AOE_RE = r"(附近|周围|范围内|所有|区域)[^。，友]{0,10}(敌|单位)|漩涡|遮罩|各个方向|竞技场|反射波"
# 能保友方：无敌 / 不会阵亡 / 免疫 / 驱散 / 延后结算伤害 / 传送回去
SAVE_ALLY_RE = r"(友军|友方|队友)[^。]{0,40}(不会阵亡|无敌|免疫|驱散|负面状态|传送回|延缓|结算)"
POS_NAMES = {1: "一号位", 2: "中单"}
OFFENSIVE_RE = r"敌|范围内的单位"
REVIVE_RE = r"复活"                  # 冥魂大帝绝冥再生：要杀两次


def _cd(ab: kb.Entry) -> int | None:
    """满级冷却（秒）"""
    vals = [float(v) for v in re.findall(r"\d+(?:\.\d+)?", ab.cooldown or "")]
    return round(vals[-1]) if vals else None


def _usable(h: kb.HeroKB) -> list[kb.Entry]:
    return [ab for ab in h.abilities if not ab.granted]


def _first_sentence(ab: kb.Entry) -> str:
    return re.split(r"(?<=[。！；\n])", ab.text.strip())[0].strip()


def _quote(*parts: tuple[kb.HeroKB, kb.Entry, str]) -> str:
    return "　".join(f"{h.name}·{ab.name}：{s}" for h, ab, s in parts)


def _hits(h: kb.HeroKB, tags: tuple[str, ...]) -> list[tuple[kb.Entry, str]]:
    """带这些标签的技能（含大招，只看说明正文）：[(技能, 原句)]，每个技能一次"""
    out = []
    for ab in _usable(h):
        sent = next((s for t, s in kb.tag_sentences(ab.text).items() if t in tags), None)
        if sent:
            out.append((ab, sent))
    return out


def key_ults(h: kb.HeroKB) -> list[kb.Entry]:
    """要等它交的大招：主动、满级冷却 ≥ KEY_ULT_CD 秒、作用于敌人（只给自己加攻击 / 变身的不算）"""
    return [ab for ab in _usable(h) if ab.ultimate and (_cd(ab) or 0) >= KEY_ULT_CD
            and (ab.targets_enemy or re.search(OFFENSIVE_RE, ab.text) or re.search(REVIVE_RE, ab.text))]


def _escape(h: kb.HeroKB) -> kb.Entry | None:
    """跑路 / 保命技能（位移、隐身、无敌、技能免疫、强驱散；含大招）。只看技能名和说明第一句"""
    for ab in _usable(h):
        text = f"{ab.name}。{_first_sentence(ab)}"
        if re.search(SAVE_RE, text) and not re.search(SAVE_EXCLUDE, text) and not re.search(ESCAPE_EXCLUDE, text):
            return ab
    return None


def _ally_save(h: kb.HeroKB) -> tuple[kb.Entry, str] | None:
    """能保友方的技能；有几个就取冷却最长的（神谕者有气运之末和虚妄之诺，要紧的是虚妄之诺）"""
    hits = []
    for ab in _usable(h):
        if m := re.search(SAVE_ALLY_RE, ab.text):
            sent = next(s for s in re.split(r"(?<=[。！；\n])", ab.text) if m.group(0) in s.replace("\n", ""))
            hits.append((ab, sent.strip()))
    return max(hits, key=lambda x: _cd(x[0]) or 0) if hits else None


def _ult_line(f: kb.HeroKB, ab: kb.Entry, interrupt, core: bool) -> tuple[int, Why]:
    """对面一个大招 → (排序分, 提示)。分高的先说：能打断的持续施法 > 黑皇杖挡不住的 > 其他"""
    cd = _cd(ab)
    marks, quote = [f"冷却 {cd} 秒"], [(f, ab, _first_sentence(ab))]
    if ab.pierces_immunity:
        marks.append("黑皇杖挡不住")
        quote.append((f, ab, "无视技能免疫（官方技能属性）"))
    head = f"{f.name}的{ab.name}（{'，'.join(marks)}）"
    if ab.channelled and not re.search(UNTOUCHABLE, ab.text) and interrupt:
        h, mab, s, who = interrupt
        quote += [(f, ab, "持续施法（官方技能属性）"), (h, mab, s)]
        return 3, Why("good", f"{head}要持续施法：留{who}的{mab.name}打断", _quote(*quote))
    if re.search(REVIVE_RE, ab.text):
        return 1, Why("bad", f"{head}死了能复活：先打别人，或留够技能杀两次", _quote(*quote))
    score = 2 if ab.pierces_immunity else 1
    if re.search(AOE_RE, ab.text):
        bkb = ""
        if core and ab.pierces_immunity is False and any(a is ab for a, _ in _hits(f, kb.CONTROL_TAGS)):
            bkb = "，开黑皇杖能躲"
            quote.append((f, ab, "不无视技能免疫（官方技能属性）"))
        return score, Why("bad", f"{head}没交前别扎堆{bkb}；交了 {cd} 秒内没有，趁机打", _quote(*quote))
    return score, Why("bad", f"等{head}交了再进；交了 {cd} 秒内没有", _quote(*quote))


def tips(me: kb.HeroKB, allies: list[kb.HeroKB], enemies: list[kb.HeroKB], pos: int,
         enemy_pos: dict[str, int] | None = None, limit: int = MAX_TIPS) -> list[Why]:
    """你（me，打 pos 号位）+ 队友（allies）对上对面五人：后期打团要等什么、留什么、躲什么。
    enemy_pos = 对面英雄名 → 猜的位置（没有就不说「留控给谁」）"""
    if not enemies:
        return []
    ours = [me] + list(allies)
    who = lambda h: "你" if h is me else f"队友{h.name}"
    core = pos in (1, 2, 3)
    # 打断对面持续施法：无视技能免疫的优先（对面开了黑皇杖也能断），其次你自己的
    interrupts = [(h, ab, s, who(h)) for h in ours for ab, s in _hits(h, INTERRUPT_TAGS)]
    interrupt = next((x for x in interrupts if x[1].pierces_immunity), interrupts[0] if interrupts else None)
    control = [(h, ab, s) for h in ours for ab, s in _hits(h, kb.CONTROL_TAGS)]

    # 1. 对面要等的大招：持续施法能打断的 > 无视黑皇杖的 > 其他；同档冷却长的在前
    ults = [(_ult_line(f, ab, interrupt, core), _cd(ab)) for f in enemies for ab in key_ults(f)
            if not re.search(SAVE_ALLY_RE, ab.text)]                 # 保人的大招（守护天使）在第 3 条说
    ults.sort(key=lambda x: (-x[0][0], -(x[1] or 0)))
    ult_tips = [w for (_, w), _ in ults]

    # 2. 控制留给对面核心：他有跑路 / 保命技能就先骗出来
    keep, used = [], set()
    for f in sorted((f for f in enemies if (enemy_pos or {}).get(f.name) in CORE_POSITIONS),
                    key=lambda f: enemy_pos[f.name]):
        if not control:
            break
        left = [c for c in control if id(c[1]) not in used] or control      # 两个核心尽量分给不同的控制
        mine = [c for c in left if c[0] is me] or left
        immune = _hits(f, ("提供技能免疫",))
        piercing = [c for c in mine if c[1].pierces_immunity] or [c for c in left if c[1].pierces_immunity]
        h, ab, s = (piercing or mine)[0] if immune else mine[0]      # 他能自己开免疫，就挑无视免疫的控制
        used.add(id(ab))
        label = f"对面{POS_NAMES[enemy_pos[f.name]]}{f.name}"
        esc = _escape(f)
        if esc:
            cd = _cd(esc)
            text = f"{label}有{esc.name}" + (f"（冷却 {cd} 秒）" if cd else "") + f"保命：骗出来再上{who(h)}的{ab.name}"
            q = [(f, esc, _first_sentence(esc)), (h, ab, s)]
        else:
            text = f"{who(h)}的{ab.name}留给{label}"
            q = [(h, ab, s)]
        if immune and esc is not immune[0][0]:
            iab, isent = immune[0]
            text += f"；{ab.name}无视技能免疫，他开{iab.name}也能控" if ab.pierces_immunity else f"；他开{iab.name}免疫，等结束再控"
            q.append((f, iab, isent))
        keep.append(Why("good", text, _quote(*q)))

    # 3. 对面能保人的：先控住保人的，或者骗掉再集火
    saves = []
    for f in enemies:
        if hit := _ally_save(f):
            ab, sent = hit
            cd = _cd(ab)
            saves.append(Why("bad", f"{f.name}的{ab.name}" + (f"（冷却 {cd} 秒）" if cd else "")
                             + f"能保人：先控{f.name}或等它交了再集火", _quote((f, ab, sent))))

    # 4. 反弹指向技能：别把你的指向技能往他身上丢
    reflect = []
    my_target = [ab for ab in _usable(me) if ab.targets_enemy]
    for f in enemies:
        for ab, sent in _hits(f, ("抵挡 / 反馈指向技能",)):
            if my_target:
                reflect.append(Why("bad", f"{f.name}的{ab.name}会挡 / 弹指向技能：先骗掉再对他放{my_target[0].name}",
                                   _quote((f, ab, sent))))
                break

    # 5. 你自己的大招：持续施法的等对面打断交了；范围大招等对面保人技能交了
    mine_ult = []
    their_interrupt = next(((f, ab, s) for f in enemies for ab, s in _hits(f, INTERRUPT_TAGS)), None)
    for ab in key_ults(me):
        if ab.channelled and not re.search(UNTOUCHABLE, ab.text) and their_interrupt:
            f, fab, s = their_interrupt
            mine_ult.append(Why("bad", f"你的{ab.name}要持续施法，会被{f.name}的{fab.name}打断：等它交了再放",
                                _quote((me, ab, "持续施法（官方技能属性）"), (f, fab, s))))
        elif saves and re.search(AOE_RE, ab.text):
            mine_ult.append(Why("good", f"你的{ab.name}（冷却 {_cd(ab)} 秒）等对面保人技能交了、人挤一起再放",
                                _quote((me, ab, _first_sentence(ab)))))
        break

    # 卡片只显示前几条：最要紧的两个大招 → 留控给谁 → 保人 → 自己的大招 → 反弹 → 其余大招
    ordered = ult_tips[:2] + keep[:1] + saves[:1] + mine_ult + reflect + keep[1:] + saves[1:] + ult_tips[2:]
    return ordered[:limit]
