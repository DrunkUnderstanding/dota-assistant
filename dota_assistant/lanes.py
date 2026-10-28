"""对面分路 + 你这路怎么打。

分路：天梯里一号位 + 五号位走自家优势路，三号位 + 四号位走劣势路，中单走中路；天辉优势路是下路，夜魇优势路是上路。
对面谁打几号位是猜的：每个英雄在本分段打各位置的局数比例（STRATZ，data/positions_*.json），五个人位置互不重复，
每种分法的可能性 = 各人打该位置的比例相乘；显示可能性最大的分法，单人「几成把握」= 他在这个位置的所有分法加起来占多少。

对线提示：只用硬规则，不用大模型，每条带 Valve 官方原文或官方数据当依据（悬停可看）：
射程（近战 / 远程、射程差）、对面控制和冷却、持续施法能不能打断、对面的位移跑路技能、我方两人的控制配合、对面带控制的大招。
只看对线期能用的技能（大招单独说「6 级后」；神杖 / 魔晶给的技能不算）。规则和措辞是实现假设，玩家没确认过。
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field

from . import kb
from .death import SAVE_EXCLUDE
from .domain.engine import POSITION_NAMES
from .domain.explain import Why

# 对线对手：我打几号位 → 对面哪些位置和我同路
LANE_VS = {1: (3, 4), 5: (3, 4), 2: (2,), 3: (1, 5), 4: (1, 5)}
PARTNER = {1: 5, 5: 1, 3: 4, 4: 3}            # 同路队友
SURE = 0.6                                    # 低于这个把握的，界面上标「?」
RANGED_GAP = 100                              # 两边都是远程时，射程差这么多才说
MAX_TIPS = 4                                  # 叠加层地方小，按下面的顺序取前几条
# 能打断持续施法的控制（缠绕 / 定身不打断）
INTERRUPT_TAGS = ("眩晕", "妖术", "沉默")
# 跑路技能：只算位移 / 隐身（死亡复盘的保命关键词去掉了无敌、免疫、驱散——那些是保命，不是「能跑」）
ESCAPE_RE = r"冲到|冲向|冲锋|传送至|传送到|闪烁|跳跃|冲刺|进入隐身|隐身状态|变为隐身|隐匿"
# 只能冲向敌人的突进（幻影冲锋、暗影冲刺、实相裂隙）不算跑路；玛西特快专递传送的是信使
ESCAPE_EXCLUDE = r"向其冲去|盯紧一个敌方单位|与目标连线|信使"
# 持续施法期间自己躲起来的（帕克相位转移、力丸绝杀秘技），打断不了
UNTOUCHABLE = r"躲避|位面|无敌"
# 对面三条路：(敌方位置们, 敌方视角的说法)
ENEMY_LANES = (((1, 5), "safe"), ((2,), "mid"), ((3, 4), "off"))


@dataclass
class Guess:
    hero: int
    pos: int
    conf: float                               # 0~1：他打这个位置的把握


@dataclass
class Lane:
    label: str                                # 上路 / 中路 / 下路（不知道阵营时：对面优势路 / 中路 / 对面劣势路）
    positions: tuple[int, ...]                # 对面在这路的位置
    heroes: list[Guess]
    mine: bool                                # 是不是我这路

    @property
    def missing(self) -> int:                 # 这路对面还有几个位置没人（还没选）
        return len(self.positions) - len(self.heroes)


@dataclass
class Lanes:
    my_pos: int
    side: str | None
    enemies: list[Guess] = field(default_factory=list)
    allies: list[Guess] = field(default_factory=list)       # 队友（不含我）

    def rows(self) -> list[Lane]:
        """按地图从上到下排"""
        by_pos = {g.pos: g for g in self.enemies}
        enemy_side = {"radiant": "dire", "dire": "radiant"}.get(self.side or "")
        names = {"mid": "中路"}
        if enemy_side:
            # 对面的优势路：夜魇在上路、天辉在下路
            names["safe"], names["off"] = ("上路", "下路") if enemy_side == "dire" else ("下路", "上路")
        else:
            names["safe"], names["off"] = "对面优势路", "对面劣势路"
        mine = LANE_VS.get(self.my_pos, ())
        out = [Lane(names[k], ps, [by_pos[p] for p in ps if p in by_pos], bool(set(ps) & set(mine)))
               for ps, k in ENEMY_LANES]
        order = {"上路": 0, "对面劣势路": 0, "中路": 1, "下路": 2, "对面优势路": 2}
        return sorted(out, key=lambda r: order[r.label])

    def opponents(self) -> list[Guess]:
        return [g for g in self.enemies if g.pos in LANE_VS.get(self.my_pos, ())]

    def partner(self) -> Guess | None:
        p = PARTNER.get(self.my_pos)
        return next((g for g in self.allies if g.pos == p), None)

    def mine(self) -> Lane | None:
        return next((r for r in self.rows() if r.mine), None)


# ---------- 猜位置 ----------

def share(positions: dict[int, dict[int, tuple[int, int]]], h: int, p: int) -> float:
    """英雄 h 打 p 号位的局数比例（加一平滑，没数据的英雄五个位置一样）"""
    games = positions.get(h, {})
    total = sum(g for g, _ in games.values())
    return (games.get(p, (0, 0))[0] + 1) / (total + 5)


def guess(hero_ids: list[int], positions: dict[int, dict[int, tuple[int, int]]] | None,
          taken: set[int] = frozenset(), fixed: dict[int, int] | None = None) -> list[Guess]:
    """可能性最大的分法 + 每人的把握，按位置排。taken = 已经确定被别人占的位置；fixed = 已知位置（例如 OCR 认出的队友）"""
    fixed = {h: p for h, p in (fixed or {}).items() if h in hero_ids and 1 <= p <= 5}
    out = [Guess(h, p, 1.0) for h, p in fixed.items()]
    free = [p for p in range(1, 6) if p not in taken and p not in fixed.values()]
    rest = [h for h in hero_ids if h not in fixed][:len(free)]
    if rest and positions:
        weights: dict[tuple[int, ...], float] = {}
        for perm in itertools.permutations(free, len(rest)):
            w = 1.0
            for h, p in zip(rest, perm):
                w *= share(positions, h, p)
            weights[perm] = w
        total = sum(weights.values()) or 1.0
        best = max(weights, key=weights.get)
        for i, (h, p) in enumerate(zip(rest, best)):
            conf = sum(w for perm, w in weights.items() if perm[i] == p) / total
            out.append(Guess(h, p, conf))
    return sorted(out, key=lambda g: g.pos)


def assign(enemy_ids: list[int], ally_ids: list[int], my_pos: int, side: str | None,
           positions: dict[int, dict[int, tuple[int, int]]] | None, ally_pos: dict[int, int] | None = None) -> Lanes:
    return Lanes(my_pos, side, guess(enemy_ids, positions),
                 guess(ally_ids, positions, taken={my_pos}, fixed=ally_pos))


# ---------- 对线提示 ----------

def _lane_abilities(h: kb.HeroKB, ultimate: bool = False) -> list[kb.Entry]:
    return [ab for ab in h.abilities if not ab.granted and ab.ultimate == ultimate]


def _hits(h: kb.HeroKB, tags: tuple[str, ...], ultimate: bool = False) -> list[tuple[kb.Entry, str, str]]:
    """对线期技能（只看说明正文，不看神杖 / 魔晶）里带这些标签的：[(技能, 标签, 原句)]"""
    out = []
    for ab in _lane_abilities(h, ultimate):
        for t, s in kb.tag_sentences(ab.text).items():
            if t in tags:
                out.append((ab, t, s))
                break
    return out


def _escape(h: kb.HeroKB) -> kb.Entry | None:
    """对线期能用的跑路技能。只看技能名和说明第一句：后面的句子常是附带效果（弱化能流「每次跳跃」、司夜刺客「不会打破…隐身状态」）"""
    for ab in _lane_abilities(h):
        text = f"{ab.name}。{_first_sentence(ab)}"
        if re.search(ESCAPE_RE, text) and not re.search(SAVE_EXCLUDE, text) and not re.search(ESCAPE_EXCLUDE, text):
            return ab
    return None


def _first_sentence(ab: kb.Entry) -> str:
    return re.split(r"(?<=[。！；\n])", ab.text.strip())[0].strip()


def _cd(ab: kb.Entry) -> str:
    return f"冷却 {ab.cooldown} 秒" if ab.cooldown else ""


def _quote(*parts: tuple[kb.HeroKB, kb.Entry, str]) -> str:
    return "　".join(f"{h.name}·{ab.name}：{s}" for h, ab, s in parts)


def _range(me: kb.HeroKB, foe: kb.HeroKB) -> Why | None:
    q = f"官方数据：{me.name} {me.attack} {me.attack_range}，{foe.name} {foe.attack} {foe.attack_range}"
    if me.attack == "近战" and foe.attack == "远程":
        return Why("bad", f"{foe.name}远程（射程 {foe.attack_range}）、你近战：补刀时别空站在兵线前白挨普攻", q)
    if me.attack == "远程" and foe.attack == "近战":
        return Why("good", f"你远程（射程 {me.attack_range}）、{foe.name}近战：可以用普攻消耗", q)
    if me.attack == foe.attack == "远程":
        gap = foe.attack_range - me.attack_range
        if gap >= RANGED_GAP:
            return Why("bad", f"{foe.name}射程比你远 {gap}（{foe.attack_range} 对 {me.attack_range}），对拼普攻吃亏", q)
        if -gap >= RANGED_GAP:
            return Why("good", f"你射程比{foe.name}远 {-gap}（{me.attack_range} 对 {foe.attack_range}），站在对方射程外消耗", q)
    return None


def tips(me: kb.HeroKB, foes: list[kb.HeroKB], ally: kb.HeroKB | None = None, limit: int = MAX_TIPS) -> list[Why]:
    """你（me）和同路队友（ally）对上对面同路（foes）：对线期要注意 / 能利用的地方。good = 机会，bad = 小心"""
    if not foes:
        return []
    ours = [me] + ([ally] if ally else [])
    who = lambda h: "你" if h is me else f"队友{h.name}"
    interrupt = [(h, ab, s) for h in ours for ab, _, s in _hits(h, INTERRUPT_TAGS)]
    out: list[Why] = []

    # 1. 对面的控制：技能在的时候别压太前，交了是窗口
    ctrl = [(f, ab, s) for f in foes for ab, _, s in _hits(f, kb.CONTROL_TAGS)[:1]]
    if len(ctrl) >= 2:
        out.append(Why("bad", "对面同路两个控制（" + "、".join(f"{f.name}的{ab.name}" for f, ab, _ in ctrl)
                       + "）：都在时别压到线前，交了一个再换血", _quote(*ctrl)))
    elif ctrl:
        f, ab, s = ctrl[0]
        cd = _cd(ab)
        out.append(Why("bad", f"小心{f.name}的{ab.name}" + (f"（{cd}）" if cd else "") + "，交了以后是你换血 / 补刀的窗口",
                       _quote((f, ab, s))))

    # 2. 持续施法：对面的能打断；我的会被对面打断
    for f in foes:
        for ab in f.abilities:
            if ab.channelled and not ab.granted and not re.search(UNTOUCHABLE, ab.text) and interrupt:
                h, mab, s = interrupt[0]
                when = "6 级后" if ab.ultimate else ""
                out.append(Why("good", f"{when}{f.name}的{ab.name}要持续施法，用{who(h)}的{mab.name}打断",
                               _quote((f, ab, "持续施法（官方技能属性）"), (h, mab, s))))
                break
    their_interrupt = [(f, ab, s) for f in foes for ab, _, s in _hits(f, INTERRUPT_TAGS)]
    for ab in _lane_abilities(me):
        if ab.channelled and not re.search(UNTOUCHABLE, ab.text) and their_interrupt:
            f, fab, s = their_interrupt[0]
            out.append(Why("bad", f"你的{ab.name}要持续施法，会被{f.name}的{fab.name}打断，等{fab.name}交了再放",
                           _quote((me, ab, "持续施法（官方技能属性）"), (f, fab, s))))
            break

    # 3. 射程
    for f in foes:
        if w := _range(me, f):
            out.append(w)
            break

    # 4. 我方能控：对面有跑路技能就先骗出来
    mine = [(h, ab, s) for h in ours for ab, _, s in _hits(h, kb.CONTROL_TAGS)[:1]]
    for f in foes:
        if mine and (esc := _escape(f)):
            h, ab, s = mine[0]
            out.append(Why("good", f"{f.name}有{esc.name}能跑" + (f"（{_cd(esc)}）" if esc.cooldown else "")
                           + f"：等{esc.name}交了再上{who(h)}的{ab.name}", _quote((f, esc, _first_sentence(esc)), (h, ab, s))))
            break

    # 5. 我和队友都有控制：能配合打一套
    if len(mine) >= 2:
        out.append(Why("good", f"你的{mine[0][1].name}和队友{mine[1][0].name}的{mine[1][1].name}都能控，"
                       "两个技能都好的时候找机会一起打", _quote(*mine)))

    # 6. 对面带控制的大招
    for f in foes:
        if ult := _hits(f, kb.CONTROL_TAGS, ultimate=True):
            ab, t, s = ult[0]
            out.append(Why("bad", f"6 级后小心{f.name}的{ab.name}（{t}）", _quote((f, ab, s))))
            break
    return out[:limit]


# ---------- 文字（本局要点 / 给大模型的资料）----------

def lane_line(lanes: Lanes, name) -> str:
    """一行说清三路：上路 斧王（3号位）、… / 中路 …"""
    parts = []
    for r in lanes.rows():
        hs = "、".join(f"{name(g.hero)}（{g.pos}号位{'' if g.conf >= SURE else '?'}）" for g in r.heroes)
        if r.missing:
            hs += ("、" if hs else "") + f"{r.missing} 人未选"
        parts.append(f"{r.label}{'（你这路）' if r.mine else ''} {hs}")
    if p := lanes.partner():
        parts.append(f"你这路队友 {name(p.hero)}（{p.pos}号位{'' if p.conf >= SURE else '?'}）")
    return "；".join(parts)


def view(lanes: Lanes, hero_ref, tips: list[Why] = ()) -> dict:
    """叠加层用：三路（地图从上到下）+ 同路队友 + 我这路的提示"""
    out = {"my_pos": lanes.my_pos, "my_pos_name": POSITION_NAMES.get(lanes.my_pos, ""),
           "rows": [{"label": r.label, "mine": r.mine, "missing": r.missing,
                     "heroes": [{**hero_ref(g.hero), "pos": g.pos, "conf": round(g.conf, 2), "sure": g.conf >= SURE}
                                for g in r.heroes]} for r in lanes.rows()],
           "partner": None, "tips": [w.__dict__ for w in tips]}
    if p := lanes.partner():
        out["partner"] = {**hero_ref(p.hero), "pos": p.pos, "conf": round(p.conf, 2), "sure": p.conf >= SURE}
    return out


# ---------- 回测：猜的分路对不对（python -m dota_assistant lanes-test）----------

def evaluate(matches: list[dict], positions: dict[int, dict[int, tuple[int, int]]]) -> str:
    """matches = backtest.fetch_matches 的结果（STRATZ：选人顺序 + 每人实际位置）。
    看三件事：五人选完时单人猜对多少、「几成把握」准不准、选到一半（对面只选了 k 个）时猜对多少，以及我这路对手全猜对的比例"""
    single, lane1 = [0, 0], [0, 0]
    calib = {b: [0, 0] for b in range(5)}                  # 把握 0~20%、…、80~100%：[猜对, 总数]
    partial = {k: [0, 0] for k in range(1, 6)}             # 对面选了 k 个时
    lane_ok = {p: [0, 0] for p in range(1, 6)}             # 我打 p 号位：猜的对面同路是哪几个人，和实际完全一样
    lane_of = lambda p: next(k for ps, k in ENEMY_LANES if p in ps)
    for m in matches:
        actual = {(pl["isRadiant"], pl["heroId"]): int(pl["position"][-1]) for pl in m["players"]}
        order = sorted((p for p in m["pickBans"] if p["isPick"]), key=lambda p: p["order"])
        for radiant in (True, False):
            team = [p["heroId"] for p in order if p["isRadiant"] == radiant]
            for k in range(1, len(team) + 1):
                for g in guess(team[:k], positions):
                    ok = g.pos == actual.get((radiant, g.hero))
                    partial[k][0] += ok
                    partial[k][1] += 1
                    if k == len(team):
                        single[0] += ok
                        single[1] += 1
                        lane1[0] += lane_of(g.pos) == lane_of(actual.get((radiant, g.hero)))
                        lane1[1] += 1
                        b = calib[min(4, int(g.conf * 5))]
                        b[0] += ok
                        b[1] += 1
            full = {g.hero: g.pos for g in guess(team, positions)}
            for my_pos, vs in LANE_VS.items():
                real = {h for h in team if actual.get((radiant, h)) in vs}
                lane_ok[my_pos][0] += real == {h for h, p in full.items() if p in vs}
                lane_ok[my_pos][1] += 1
    pct = lambda a: f"{a[0] / a[1]:.1%}（{a[1]}）" if a[1] else "-"
    L = [f"{len(matches)} 场，每场两队各猜一次（括号里是样本数）",
         f"五人选完，单人位置猜对：{pct(single)}；走哪条路猜对（3 / 4 号位互换不算错）：{pct(lane1)}",
         "「几成把握」分档 → 实际猜对（准的话两边应该差不多）："]
    L += [f"  {b * 20}~{b * 20 + 20}%：{pct(calib[b])}" for b in range(5) if calib[b][1]]
    L.append("对面只选了 k 个时，已选的人位置猜对：")
    L += [f"  k={k}：{pct(partial[k])}" for k in range(1, 6)]
    L.append("我打几号位 → 对面和我同路的是哪几个人，猜得完全对：")
    L += [f"  {POSITION_NAMES[p]}：{pct(lane_ok[p])}" for p in range(1, 6)]
    return "\n".join(L)
