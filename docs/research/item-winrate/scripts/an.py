"""「同一时段做出来的装备，胜率高低」有没有被局势带偏（2026-11-09，玩家看了同行 gg-go-next 想在出装旁边显示胜率）。

gg-go-next 的做法：同英雄同位置，「接下来 5 分钟里大家常做出哪些成装」，按人数排、旁边写胜率。
疑问（domain/itembuild.py 文件头）：领先的人买得起贵的，胜率天然高；同一时段里也一样——出点金手的多半是劣势局，出大件的多半是优势局。

数据：第 69 节黑皇杖研究抓的 2 万局（7.41f 超凡～冠绝，STRATZ：每人出装时间、每分钟经济、位置），data/research/bkb/fetched.jsonl。
做法：
1. 单位 = 一个人在 5 分钟窗口里做出一件成装（有合成配方或 ≥ 2000 金的非消耗品），窗口 10~15、15~20 … 40~45 分钟。
2. 每件装备记：买的人赢没赢（原始胜率），买的那一分钟自己队伍和对面的经济差。
3. 「按经济差应有的胜率」：所有人在每个整分钟的（窗口内）队伍经济差 → 胜率，按窗口分 20 档查表（不看买了什么）。
   某件装备的「期望胜率」= 买它那些人按当时经济差查到的应有胜率的平均。
4. 原始胜率 − 期望胜率 = 扣掉局势以后剩下的（「装备本身」的上限估计，还混着英雄 / 位置 / 买的人的水平）。
5. 每个（窗口 × 位置组）里 ≥ 300 次的装备，看原始胜率的高低有多少是期望胜率（局势）解释的：
   - 两者跨装备的相关系数、回归斜率；原始胜率的标准差 vs 扣掉以后的标准差
   - 按原始胜率排第一的装备，扣掉局势以后还排第几
用法：python docs/research/item-winrate/scripts/an.py"""
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from dota_assistant.core.config import DATA_DIR  # noqa: E402
from dota_assistant.sources import assets  # noqa: E402

SRC = DATA_DIR / "research" / "bkb" / "fetched.jsonl"
WINDOWS = [(m, m + 5) for m in range(10, 45, 5)]
GROUPS = {"核心（1~3 号位）": {1, 2, 3}, "辅助（4~5 号位）": {4, 5}}
MIN_N = 300
BINS = 20

const = assets.items_const()
by_id, cost, name = {}, {}, {}
for k, v in const.items():
    if v.get("id") is None or k.startswith("recipe_"):
        continue
    by_id[v["id"]] = k
    cost[k] = v.get("cost") or 0
    name[k] = v.get("dname") or k
FULL = {k for k in cost if ((const[k].get("components") and cost[k] >= 1000) or cost[k] >= 2000)
        and const[k].get("qual") != "consumable"}

buys = []          # (窗口序号, 位置, 装备 key, 赢, 经济差)
team_state = []    # (窗口序号, 经济差, 赢)：每人每个整分钟
for line in SRC.read_text(encoding="utf-8").splitlines():
    m = json.loads(line)
    ps = [p for p in m.get("players") or [] if p.get("nw") and p.get("pos")]
    if len(ps) != 10 or not all(p["parsed"] for p in ps):
        continue
    L = min(len(p["nw"]) for p in ps)
    rad = np.sum([p["nw"][:L] for p in ps if p["rad"]], axis=0)
    dire = np.sum([p["nw"][:L] for p in ps if not p["rad"]], axis=0)
    for p in ps:
        diff = (rad - dire) if p["rad"] else (dire - rad)
        pos = int(p["pos"][-1])
        for w, (lo, hi) in enumerate(WINDOWS):
            for minute in range(lo, min(hi, L)):
                team_state.append((w, pos, diff[minute], p["win"]))
        seen = set()
        for t, iid in p["items"]:
            k = by_id.get(iid)
            if k not in FULL or k in seen:
                continue
            seen.add(k)                       # 同一件只算第一次
            minute = int(t // 60)
            if minute >= L:
                continue
            for w, (lo, hi) in enumerate(WINDOWS):
                if lo <= minute < hi:
                    buys.append((w, pos, k, p["win"], diff[minute]))

print(f"成装购买 {len(buys)} 次，人分钟 {len(team_state)}")

# 经济差 → 应有胜率：每个窗口 × 位置组，按经济差分位数分 BINS 档
curve = {}
ts = np.array([(w, pos, d, win) for w, pos, d, win in team_state], float)
for w in range(len(WINDOWS)):
    for gname, gpos in GROUPS.items():
        s = ts[(ts[:, 0] == w) & np.isin(ts[:, 1], list(gpos))]
        edges = np.quantile(s[:, 2], np.linspace(0, 1, BINS + 1))
        idx = np.clip(np.searchsorted(edges, s[:, 2], side="right") - 1, 0, BINS - 1)
        rate = np.array([s[idx == b, 3].mean() for b in range(BINS)])
        curve[(w, gname)] = (edges, rate)


def expected(w, gname, d):
    edges, rate = curve[(w, gname)]
    return rate[min(max(np.searchsorted(edges, d, side="right") - 1, 0), BINS - 1)]


rows = defaultdict(list)    # (窗口, 组, 装备) → [(赢, 期望)]
for w, pos, k, win, d in buys:
    for gname, gpos in GROUPS.items():
        if pos in gpos:
            rows[(w, gname, k)].append((win, expected(w, gname, d)))

out = []
all_raw, all_exp = [], []
noise, resid_var = [], []   # 每格：抽样噪声方差的平均、扣掉局势后跨装备的方差
for w, (lo, hi) in enumerate(WINDOWS):
    for gname in GROUPS:
        cells = []
        for (ww, g, k), v in rows.items():
            if ww == w and g == gname and len(v) >= MIN_N:
                a = np.array(v, float)
                cells.append((k, len(v), a[:, 0].mean(), a[:, 1].mean()))
        if len(cells) < 4:
            continue
        cells.sort(key=lambda c: -c[1])
        raw = np.array([c[2] for c in cells])
        exp = np.array([c[3] for c in cells])
        adj = raw - exp
        ns = np.array([c[1] for c in cells])
        noise.append(float(np.mean(raw * (1 - raw) / ns)))
        resid_var.append(float(adj.var()))
        all_raw += list(raw - raw.mean())
        all_exp += list(exp - exp.mean())
        r = np.corrcoef(raw, exp)[0, 1]
        top_raw = max(cells, key=lambda c: c[2])
        rank_adj = sorted(cells, key=lambda c: -(c[2] - c[3])).index(top_raw) + 1
        out.append(f"\n【{lo}~{hi} 分钟 · {gname}】{len(cells)} 件（各 ≥ {MIN_N} 次）  原始胜率 vs 局势应有：相关 {r:+.2f}；"
                   f"标准差 原始 {raw.std() * 100:.1f} → 扣掉局势 {adj.std() * 100:.1f} 个百分点；"
                   f"原始第一 {name[top_raw[0]]} 扣掉以后排第 {rank_adj}")
        out.append(f"  {'装备':12} {'次数':>6} {'原始胜率':>8} {'局势应有':>8} {'扣掉局势':>8}")
        for k, n, rw, ex in cells[:12]:
            out.append(f"  {name[k]:12} {n:6d} {rw * 100:7.1f}% {ex * 100:7.1f}% {(rw - ex) * 100:+7.1f}")

a, b = np.array(all_raw), np.array(all_exp)
slope = float(np.dot(a, b) / np.dot(b, b))
share = 1 - float(np.var(a - b) / np.var(a))
print(f"\n总体（同窗口同组内、跨装备）：原始胜率差对「局势应有」差的回归斜率 {slope:.2f}；"
      f"原始胜率差里被局势（买时经济差）解释掉的比例 {share * 100:.0f}%")
print(f"扣掉局势后剩下的跨装备标准差 {np.sqrt(np.mean(resid_var)) * 100:.1f} 个百分点，其中光抽样噪声就有 {np.sqrt(np.mean(noise)) * 100:.1f}；"
      f"去掉噪声后真实差别约 {np.sqrt(max(0.0, np.mean(resid_var) - np.mean(noise))) * 100:.1f} 个百分点（还混着英雄 / 买的人的水平）")
print("\n".join(out))
