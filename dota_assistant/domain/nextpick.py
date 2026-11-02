"""猜对面最后一手：我是我方最后一个选的（队友 4 个都选了），对面还剩 1 手在我后面。

2026-11-02 玩家：「对手最后一手选择也可能拿出一些针对性的英雄，导致我最后一手选择可能没有特别好……
对方有我方场上 4 个英雄的信息，你可以根据这一点推算出对方的选择，然后再来推理我当前的选择应该如何」。

模型（research/nextpick.py 回测定的，见 HANDOFF 第 50 节）：
  对面还空着的位置 p 的概率 q(p)：对面已选的 4 个人分位置（和分路同一个排列模型，lanes.share），剩下哪个位置；
  在位置 p 选 e 的权重 = e 打 p 号位的选取次数（流行度）× exp(W_ADV × e 对我方 4 人的克制合计 + W_SYN × e 和对面 4 人的配合合计)，
  每个位置内归一化，再按 q(p) 加起来。
不看我这手：对面最后一手在我之后选、看得到我选了谁，但回测里把我这手也算进去并没有猜得更准（天梯里最后一手多半早就想好了）。
回测（3978 场样本外）：最可能的那个猜中约 9%，前 3 个约 24%，前 5 个约 34%。所以只是「对面大概会补什么」的参考。
"""
from __future__ import annotations

import itertools
import math

from .engine import MIN_GAMES, Engine
from .lanes import share

# 回测（research/nextpick.py，一半对局网格搜、一半验证）：克制 2、配合 0.5 最好；英雄自身强度再加一项没有更好（流行度里已经有了）
W_ADV = 2.0
W_SYN = 0.5
SHOW = 5          # 叠加层列几个猜的


def open_positions(their: list[int], positions: dict | None, fixed: dict[int, int] | None = None) -> dict[int, float]:
    """对面已选的人（< 5 个）→ 还空着的位置的概率 {位置: 概率}。fixed = 玩家在叠加层上定了的位置（{英雄: 位置}），
    这些人不再猜，其余的人在剩下的位置里按 lanes.share 排列（没有分位置数据时每种排法一样）"""
    fixed = {h: p for h, p in (fixed or {}).items() if h in their and 1 <= p <= 5}
    rest = [h for h in their if h not in fixed]
    slots = [p for p in range(1, 6) if p not in fixed.values()]
    free = {p: 0.0 for p in range(1, 6)}
    for perm in itertools.permutations(slots, min(len(rest), len(slots))):
        w = math.prod(share(positions, h, p) if positions else 1.0 for h, p in zip(rest, perm))
        left = set(slots) - set(perm)
        for p in left:
            free[p] += w / len(left)
    total = sum(free.values()) or 1.0
    return {p: w / total for p, w in free.items()}


def features(eng: Engine, their: list[int], ours: list[int], taken: set[int]) -> list[tuple[int, float, float]]:
    """每个对面还能选的英雄：(英雄 id, 对我方已选的人克制合计, 和对面已选的人配合合计)，logit"""
    return [(h, sum(eng.advantage(h, o) for o in ours), sum(eng.synergy(h, t) for t in their))
            for h in eng.base if h not in taken and eng.games.get(h, 0) >= MIN_GAMES]


def distribution(eng: Engine, feats: list[tuple[int, float, float]], q: dict[int, float],
                 w_adv: float = W_ADV, w_syn: float = W_SYN) -> dict[int, float]:
    """{英雄 id: 对面最后一手选它的概率}"""
    out = {h: 0.0 for h, _, _ in feats}
    pos = eng.positions or {}
    for p, qp in q.items():
        if qp <= 0:
            continue
        w = {h: eng.games[h] * (share(pos, h, p) if pos else 1.0) * math.exp(w_adv * a + w_syn * s) for h, a, s in feats}
        total = sum(w.values()) or 1.0
        for h, x in w.items():
            out[h] += qp * x / total
    return out


def predict(eng: Engine, their: list[int], ours: list[int], taken: set[int],
            fixed: dict[int, int] | None = None) -> list[tuple[int, float]]:
    """对面最后一手最可能选谁：[(英雄 id, 概率)]，从高到低。their = 对面已选，ours = 我方已选（不含我这手），
    taken = 已选 + 禁用（不会再被选的），fixed = 玩家定了的对面位置（见 open_positions）"""
    dist = distribution(eng, features(eng, their, ours, taken), open_positions(their, eng.positions, fixed))
    return sorted(dist.items(), key=lambda t: -t[1])
