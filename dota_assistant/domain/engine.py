"""选人推荐打分。

所有量都在 logit（对数几率）空间里相加，最后换回胜率：
  得分(h) = 英雄自身强度 + Σ 对位权重(e) × 对每个敌方英雄的克制优势 + w_syn × Σ 与每个队友的配合
          − 被反制风险 + 熟练度加成

对位权重：大概率和我对线的敌方英雄（按它的主位置推断）权重更高。
被反制风险：对面还没选的英雄里最克 h 的几个的平均优势 × 对面剩余手数；选得越早，越怕后手被针对。

克制优势 = 实际对位胜率 与「按双方自身强度推算的期望胜率」之差。
样本少的对位用贝叶斯收缩拉回期望值，避免 30 局 70% 胜率这种噪声霸榜。
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from ..core.heroes import Hero, HeroDB

# 收缩强度 = 单局噪声方差 / 真实效应方差：单局方差 0.25（=2500 个百分点²），
# 真实克制效应大致在 ±2.5 个百分点 → 2500 / 6.25 = 400；配合效应更小（±1.5）→ 约 1100。
# 样本 ≈ K 局时实际与期望各占一半。
K_VS = 400
K_SYN = 1100
W_SYN = 0.7
# 2026-10-25 玩家要求只按英雄本身（强度、克制、配合）排，不看自己打了几局：下面两项加分关掉（原 0.04 / 0.08）
POOL_BONUS = 0.0        # 手写英雄池里的英雄加分（0.04 ≈ 1 个百分点）
FAMILIAR_BONUS = 0.0    # 近期天梯打满 FAMILIAR_FULL 局的英雄加分（0.08 ≈ 2 个百分点），按局数线性
FAMILIAR_FULL = 20
FAMILIAR_MIN = 3        # 近期少于 3 局不算「你的英雄」
K_ME = 40               # 个人胜率噪声很大（还掺着队友），收缩得很狠：20 局 70% 也只算一点点
W_ME = 0.0              # 2026-10-24 玩家要求不看个人胜率（原 0.5）
MIN_GAMES = 300         # 统计期内总场次低于此的英雄（刚出的新英雄等）不推荐
POS_SHARE_MIN = 0.10    # 有分位置数据时：英雄至少 10% 的局打这个位置才推荐
K_POS = 300             # 分位置胜率的收缩强度
# 英雄打某位置的强度按「这个英雄打这个位置的局数」向 50% 收缩（2026-10-25 玩家要求）：冷门英雄 / 冷门位置
# （米波中单、维萨吉、一号位拉席克）场次少、多是绝活玩家在打，胜率虚高，换个人不一定赢。局数 ≈ K_PICKS 时只算一半。
# 回测（3001 场样本外）：区分度 0.0554 → 0.0501（2000 / 5000 / 20000 分别 0.0535 / 0.0516 / 0.0486），在噪声内略降；
# 回测样本里选米波的多半就是米波玩家，本来就偏向绝活英雄，所以这点下降是预期的代价。
# 只影响「自身强度」这一项，克制 / 配合的期望值仍用原始胜率（否则冷门英雄对谁都会显得「克」）。
K_PICKS = 10000
# 下面两项 2026-10-21 回测调参：0.5 / 1.0 的对线加权、0.03~0.3 的反制扣分都没有带来可测的提升
# （区分度变化 ±0.003，噪声约 ±0.01），所以默认关闭，只保留「易被反制」的文字提示。
W_LANE = 0.0            # 对线对手的额外克制权重
W_RISK = 0.0            # 被反制风险的权重
RISK_TOP = 3            # 取最克我的前几个还没被选的英雄
RISK_HINT = 4.0         # 对面还剩 ≥3 手、且有英雄克我 ≥ 4 个百分点时，在理由里提示
# 我的位置 → 和我对线的敌方位置（优势路 1+5 对劣势路 3+4，中路对中路）
LANE_OPPONENTS = {1: (3, 4), 2: (2,), 3: (1, 5), 4: (1, 5), 5: (3, 4)}

POSITION_NAMES = {1: "一号位", 2: "中单", 3: "三号位", 4: "四号位", 5: "五号位"}


def logit(p: float) -> float:
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def pct(x: float) -> float:
    """logit 增量 → 近似胜率百分点（在 50% 附近）"""
    return x * 25


@dataclass
class Suggestion:
    hero: Hero
    score: float                       # 综合 logit
    base: float                        # 英雄自身强度（logit）
    vs: list[tuple[Hero, float]] = field(default_factory=list)    # (敌方, 优势 logit)
    syn: list[tuple[Hero, float]] = field(default_factory=list)   # (队友, 配合 logit)
    in_pool: bool = False
    risk: float = 0.0                  # 被反制风险扣分（logit，正数）
    counters: list[tuple[Hero, float]] = field(default_factory=list)  # 对面还能选的、最克我的英雄 (英雄, 优势)
    enemies_left: int = 5
    my_games: int = 0                  # 我近期天梯用这个英雄的局数 / 胜场
    my_wins: int = 0

    @property
    def winrate(self) -> float:
        return sigmoid(self.score) * 100

    @property
    def situational(self) -> float:
        """这局阵容带来的加减（克制 + 配合 − 反制风险，logit）：综合分去掉英雄自身强度"""
        return self.score - self.base

    def reason_parts(self) -> list[tuple[str, str]]:
        """推荐理由的各段：[(种类, 文字)]，种类 = good 克 / syn 配 / bad 怕 / risk 易被反制 / meta 版本强势 / me 个人 / pool 英雄池"""
        parts = []
        good = [(h, v) for h, v in sorted(self.vs, key=lambda t: -t[1]) if pct(v) >= 0.8][:2]
        bad = [(h, v) for h, v in sorted(self.vs, key=lambda t: t[1]) if pct(v) <= -1.5][:1]
        team = [(h, v) for h, v in sorted(self.syn, key=lambda t: -t[1]) if pct(v) >= 1.0][:1]
        if good:
            parts.append(("good", "克 " + "、".join(f"{h.name_cn}({pct(v):+.1f}%)" for h, v in good)))
        if team:
            parts.append(("syn", "配 " + "、".join(f"{h.name_cn}({pct(v):+.1f}%)" for h, v in team)))
        if bad:
            parts.append(("bad", "但怕 " + "、".join(f"{h.name_cn}({pct(v):+.1f}%)" for h, v in bad)))
        hard = [(h, v) for h, v in self.counters if pct(v) >= RISK_HINT]
        if self.enemies_left >= 3 and hard:
            parts.append(("risk", "易被反制：" + "、".join(f"{h.name_cn}({pct(v):+.1f}%)" for h, v in hard[:2])))
        if not parts and pct(self.situational) >= 1.0:      # 单个都不显著，但几个小克制 / 配合加起来不少
            parts.append(("good", f"克制 + 配合合计 {pct(self.situational):+.1f}%"))
        if not parts:
            parts.append(("meta", f"版本强势（自身 {sigmoid(self.base) * 100:.1f}%）"))
        if self.my_games:
            parts.append(("me", f"你近期 {self.my_games} 局 {self.my_wins / self.my_games:.0%}"))
        elif self.in_pool:
            parts.append(("pool", "你的英雄池"))
        return parts

    def reason(self) -> str:
        return "；".join(t for _, t in self.reason_parts())


class Engine:
    def __init__(self, db: HeroDB, stats: dict,
                 positions: dict[int, dict[int, tuple[int, int]]] | None = None):
        """positions = {英雄 id: {位置: (局数, 胜场)}}（STRATZ），没有时用定位标签粗筛"""
        self.db = db
        self.positions = positions
        self.matches = stats["matches"]
        self._vs = {(a, b): (g, w) for a, b, g, w in stats["vs"]}
        self._syn = {(a, b): (g, w) for a, b, g, w in stats["syn"]}

        games, wins = defaultdict(int), defaultdict(int)
        for (a, _), (g, w) in self._vs.items():
            games[a] += g
            wins[a] += w
        # 每局里一个英雄对上 5 个敌人，所以这里的局数是真实局数的 5 倍
        self.games = {h: g // 5 for h, g in games.items()}
        self.base = {h: logit((wins[h] + 50) / (games[h] + 100)) for h in games}
        self._adv: dict[tuple[int, int], float] = {}

    def advantage(self, h: int, e: int) -> float:
        """h 对上 e 的克制优势（logit，正 = h 克 e）"""
        key = (h, e)
        if key not in self._adv:
            self._adv[key] = self._advantage(h, e)
        return self._adv[key]

    def _advantage(self, h: int, e: int) -> float:
        expect = self.base.get(h, 0) - self.base.get(e, 0)
        g, w = self._vs.get((h, e), (0, 0))
        wr = (w + K_VS * sigmoid(expect)) / (g + K_VS)
        return logit(wr) - expect

    def synergy(self, a: int, b: int) -> float:
        expect = self.base.get(a, 0) + self.base.get(b, 0)
        g, w = self._syn.get((min(a, b), max(a, b)), (0, 0))
        wr = (w + K_SYN * sigmoid(expect)) / (g + K_SYN)
        return logit(wr) - expect

    def base_at(self, h: int, position: int) -> float:
        """英雄打这个位置的强度：整体强度 + 该位置相对整体的胜率差（来自 STRATZ，收缩过），
        再按这个英雄打这个位置的局数（总局数 × 位置占比）向 50% 收缩，见 K_PICKS"""
        base = self.base.get(h, 0)
        share = self.position_share(h, position)
        n = self.games.get(h, 0) * (1 if share is None else share)    # 没有分位置数据时按总局数
        per = (self.positions or {}).get(h)
        if per:
            g_all = sum(g for g, _ in per.values())
            w_all = sum(w for _, w in per.values())
            overall = (w_all + 50) / (g_all + 100)
            g, w = per.get(position, (0, 0))
            base += logit((w + K_POS * overall) / (g + K_POS)) - logit(overall)
        return base * n / (n + K_PICKS)

    def position_share(self, h: int, position: int) -> float | None:
        per = (self.positions or {}).get(h)
        if not per:
            return None
        total = sum(g for g, _ in per.values())
        return per.get(position, (0, 0))[0] / total if total else 0.0

    def personal(self, h: int, games: int, wins: int, position: int | None = None) -> float:
        """我用这个英雄比该分段平均水平好多少（logit），没局数时为 0"""
        if not games:
            return 0.0
        expect = self.base_at(h, position) if position else self.base.get(h, 0)
        wr = (wins + K_ME * sigmoid(expect)) / (games + K_ME)
        return logit(wr) - expect

    def lane_weight(self, e: int, position: int) -> float:
        """敌方 e 对「打 position 的我」的对位权重：1 + W_LANE × e 打我对线位置的概率"""
        if not W_LANE:
            return 1.0
        share = sum(self.position_share(e, p) or 0 for p in LANE_OPPONENTS[position])
        return 1 + W_LANE * share

    def counter_risk(self, h: int, taken: set[int], remaining: int) -> tuple[float, list[tuple[int, float]]]:
        """(扣分, [(最克 h 的还没被选的英雄 id, 优势)])。remaining = 对面还剩几手"""
        pool = [(self.advantage(e, h), e) for e in self.base
                if e != h and e not in taken and self.games.get(e, 0) >= MIN_GAMES]
        top = sorted(pool, reverse=True)[:RISK_TOP]
        counters = [(e, v) for v, e in top]
        if not top or remaining <= 0:
            return 0.0, counters
        return W_RISK * remaining * sum(v for v, _ in top) / len(top), counters

    def fits(self, hero: Hero, position: int) -> bool:
        share = self.position_share(hero.id, position)
        if share is not None:
            return share >= POS_SHARE_MIN
        return self.fits_by_roles(hero, position)

    @staticmethod
    def fits_by_roles(hero: Hero, position: int) -> bool:
        """没有分位置数据时的粗筛。OpenDota 的定位标签按重要性排序但很宽（斧王也挂着 Carry），
        所以主要看前两个标签。"""
        r = hero.roles
        support = "Support" in r[:2]
        if position == 1:
            return r[0] == "Carry" and not support
        if position == 2:
            return not support and bool({"Carry", "Nuker"} & set(r[:2]))
        if position == 3:
            return not support and ("Initiator" in r[:3] or (r[0] != "Carry" and "Durable" in r[:2]))
        if position == 4:
            return "Support" in r or (r[0] in ("Initiator", "Disabler", "Nuker") and "Carry" not in r[:2])
        return r[0] == "Support" or (support and r[0] != "Carry")

    def suggest(self, *, position: int, allies: list[Hero] = (), enemies: list[Hero] = (),
                bans: list[Hero] = (), pool: list[Hero] = (), only_pool: bool = False,
                history: dict[int, tuple[int, int]] | None = None, history_by_position: bool = False,
                position_filter: bool = True, top: int | None = 5) -> list[Suggestion]:
        """pool = 手写英雄池（不受位置筛选限制）；history = {英雄 id: (近期局数, 胜场)}，
        近期打过 FAMILIAR_MIN 局以上的也算你的英雄。history_by_position=True 表示 history
        已经只含你在这个位置的局，这些英雄你真打过这个位置，不再过位置筛选；否则仍要过筛。"""
        taken = {h.id for h in (*allies, *enemies, *bans)}
        manual = {h.id for h in pool}
        history = {h: gw for h, gw in (history or {}).items() if gw[0] >= FAMILIAR_MIN}
        out = []
        for hero in self.db:
            if hero.id in taken or self.games.get(hero.id, 0) < MIN_GAMES:
                continue
            trusted = hero.id in manual or (history_by_position and hero.id in history)
            if position_filter and not trusted and not self.fits(hero, position):
                continue
            mine = hero.id in manual or hero.id in history
            if only_pool and (manual or history) and not mine:
                continue
            g, w = history.get(hero.id, (0, 0))
            s = Suggestion(hero, 0.0, self.base_at(hero.id, position), in_pool=mine, my_games=g, my_wins=w)
            s.vs = [(e, self.advantage(hero.id, e.id)) for e in enemies]
            s.syn = [(a, self.synergy(hero.id, a.id)) for a in allies]
            s.enemies_left = 5 - len(enemies)
            s.risk, counters = self.counter_risk(hero.id, taken, s.enemies_left)
            s.counters = [(self.db.by_id[e], v) for e, v in counters if e in self.db.by_id]
            s.score = (s.base + sum(self.lane_weight(e.id, position) * v for e, v in s.vs)
                       + W_SYN * sum(v for _, v in s.syn) - s.risk
                       + (POOL_BONUS if hero.id in manual else 0)
                       + FAMILIAR_BONUS * min(g, FAMILIAR_FULL) / FAMILIAR_FULL
                       + W_ME * self.personal(hero.id, g, w, position))
            out.append(s)
        out.sort(key=lambda s: -s.score)
        return out[:top]

    def threats(self, enemies: list[Hero], allies: list[Hero]) -> list[tuple[Hero, float]]:
        """敌方英雄对我方当前阵容的总克制（正 = 对我们威胁大），用于提示"""
        res = [(e, sum(self.advantage(e.id, a.id) for a in allies)) for e in enemies]
        return sorted(res, key=lambda t: -t[1])
