"""配合权重 W_SYN 取 0.5 还是 1.0：样本外回测同一批对局配对比较。

和 `python -m dota_assistant backtest` 用同一套样本（research/backtest.fetch_matches，统计截止点之后的新对局）和同一套打分（Tables），
只把配合那一项的系数换掉：
- 选人重放：推荐区分度（排名越靠前 ↔ 越赢的相关系数），全部 / 有队友时（配合才起作用）
- 阵容预测：只看最终阵容预测胜负的准确率、对数损失
两种权重的差用「按对局重抽样」的 bootstrap 给 95% 区间（同一批对局配对，噪声比两次独立回测小）。
用法：python docs/research/syn-weight/scripts/syn_weight.py [对局数，默认 3000]"""
import math
import random
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from dota_assistant.core import config  # noqa: E402
from dota_assistant.research import backtest as bt  # noqa: E402
from dota_assistant.sources import assets  # noqa: E402
from dota_assistant.sources import stats as stats_mod  # noqa: E402
from dota_assistant.domain.engine import Engine, sigmoid  # noqa: E402

WEIGHTS = (0.5, 1.0)
BOOT = 1000


def per_match(t, matches, w):
    """每场：[(位置排名分 x, 赢没赢 y, 有没有队友)], 阵容预测 logit, 天辉赢没赢"""
    bt.W_SYN = w
    rows, drafts = [], []
    for m in matches:
        r = bt.replay_picks(t, [m])
        rows.append([(1 - (p.rank - 1) / max(p.candidates - 1, 1), p.won, p.order >= 2) for p in r])
        d = bt.predict_drafts(t, [m])
        full = list(d.values())[-1] if d else []
        drafts.append(full[0] if full else None)
    return rows, drafts


def sep(rows, only_allies=False):
    xs = [(x, y) for g in rows for x, y, a in g if a or not only_allies]
    if len(xs) < 10:
        return float("nan")
    x, y = np.array(xs, float).T
    return float(np.corrcoef(x, y)[0, 1])


def draft_scores(drafts, edge=0.13):
    d = [p for p in drafts if p]
    acc = sum((x + edge > 0) == w for x, w in d) / len(d)
    ll = -sum(math.log(sigmoid(x + edge) if w else 1 - sigmoid(x + edge)) for x, w in d) / len(d)
    return acc, ll


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    cfg = config.load()
    stats = stats_mod.load_stats(cfg)
    eng = Engine(assets.load_heroes(), stats, stats_mod.load_positions(cfg))
    matches = bt.fetch_matches(cfg, stats["hi_id"], n)
    random.Random(0).shuffle(matches)
    print(f"样本外 {len(matches)} 场（match_id > {stats['hi_id']}）")
    t = bt.Tables(eng)
    res = {w: per_match(t, matches, w) for w in WEIGHTS}
    for w in WEIGHTS:
        rows, drafts = res[w]
        acc, ll = draft_scores(drafts)
        print(f"W_SYN={w}: 区分度 全部 {sep(rows):.4f}  有队友时 {sep(rows, True):.4f}  "
              f"阵容准确率 {acc * 100:.2f}%  对数损失 {ll:.5f}")
    rng = np.random.default_rng(0)
    idx = np.arange(len(matches))
    diffs = {"区分度 全部": [], "区分度 有队友时": [], "对数损失": []}
    a, b = res[WEIGHTS[0]], res[WEIGHTS[1]]
    for _ in range(BOOT):
        s = rng.choice(idx, len(idx))
        ra, rb = [a[0][i] for i in s], [b[0][i] for i in s]
        diffs["区分度 全部"].append(sep(ra) - sep(rb))
        diffs["区分度 有队友时"].append(sep(ra, True) - sep(rb, True))
        diffs["对数损失"].append(draft_scores([a[1][i] for i in s])[1] - draft_scores([b[1][i] for i in s])[1])
    print(f"\n{WEIGHTS[0]} 减 {WEIGHTS[1]}（bootstrap {BOOT} 次，95% 区间；区分度越大越好，对数损失越小越好）：")
    for k, v in diffs.items():
        v = np.array(v)
        print(f"  {k}: {np.mean(v):+.5f}  [{np.percentile(v, 2.5):+.5f}, {np.percentile(v, 97.5):+.5f}]")


if __name__ == "__main__":
    main()
