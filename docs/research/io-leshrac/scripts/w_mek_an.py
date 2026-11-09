"""艾欧 / 拉席克谁出梅肯斯姆、什么阵容下拉席克不出（2026-11-09，第 71 节）。
数据：w_mek_fetch.py 抓的 pair_full.jsonl（两人同队的局，全场 10 人出装 + 对英雄伤害分类型）。
只看拉席克 1 号位 + 艾欧 5 号位、两人都有出装记录的局。
用法：python w_mek_an.py"""
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from dota_assistant.core.config import DATA_DIR  # noqa: E402
from dota_assistant.sources import assets  # noqa: E402

IO, LESH = 91, 52
MEK, GREAVES, LOCKET, PIPE, CRIMSON, WRAPS = 79, 231, 269, 90, 242, 1854
BKB, SHIVA, KS, YK, VEIL, BLOOD, GLIMMER, LOTUS = 116, 119, 273, 277, 190, 121, 254, 226
HN = {h.id: h.name_cn for h in assets.load_heroes()}
NAMES = {MEK: "梅肯斯姆", GREAVES: "卫士胫甲", LOCKET: "圣洁吊坠", PIPE: "洞察烟斗", CRIMSON: "赤红甲",
         WRAPS: "圣化护服", BKB: "黑皇杖", SHIVA: "希瓦", KS: "散慧对剑", YK: "慧夜对剑", VEIL: "纷争面纱",
         BLOOD: "血精石", GLIMMER: "微光披风", LOTUS: "清莲宝珠"}

rows = [json.loads(line) for line in (DATA_DIR / "research" / "io-items" / "pair_full.jsonl")
        .read_text(encoding="utf-8").splitlines()]
rows = [m for m in rows if not m.get("missing")]

# ---- 英雄的伤害类型：全部人次里 魔法+纯粹 占比的平均 ----
share = defaultdict(list)
for m in rows:
    for p in m["players"]:
        d = p.get("dmg") or []
        if len(d) == 3 and all(x is not None for x in d) and sum(d) > 3000:
            share[p["h"]].append((d[1] + d[2]) / sum(d))
MAG = {h: sum(v) / len(v) for h, v in share.items() if len(v) >= 10}
MAG_ALL = float(np.median(list(MAG.values())))


def first(p, item):
    for t, i in p["items"]:
        if i == item:
            return t
    return None


def nw_at(p, minute):
    nw = p.get("nw") or []
    return nw[min(minute, len(nw) - 1)] if nw else 0


games = []
for m in rows:
    ps = m["players"]
    lesh = next((p for p in ps if p["h"] == LESH), None)
    io = next((p for p in ps if p["h"] == IO), None)
    if not lesh or not io or lesh["rad"] != io["rad"]:
        continue
    if (lesh["pos"], io["pos"]) != ("POSITION_1", "POSITION_5") or not lesh["parsed"] or not io["parsed"]:
        continue
    allies = [p for p in ps if p["rad"] == io["rad"] and p["h"] not in (IO, LESH)]
    enemies = [p for p in ps if p["rad"] != io["rad"]]
    if m["dur"] < 20 * 60:
        continue
    lm = first(lesh, MEK)
    lg = first(lesh, GREAVES)
    im = first(io, MEK)
    other = [first(a, x) for a in allies for x in (MEK, GREAVES)]
    other = min([t for t in other if t is not None], default=None)
    games.append({
        "id": m["id"], "dur": m["dur"], "win": io["win"],
        "lm": lm, "lg": lg, "im": im, "other_mek": other,
        "other_pipe": min([t for a in allies for t in [first(a, PIPE)] if t is not None], default=None),
        "lesh": lesh, "io": io, "allies": allies, "enemies": enemies,
        "emag": sum(MAG.get(e["h"], MAG_ALL) for e in enemies) / 5,
        "nwdiff10": sum(nw_at(p, 10) * (1 if p["rad"] == io["rad"] else -1) for p in ps),
        "lesh_nw12": nw_at(lesh, 12),
    })

n = len(games)
print(f"拉席克 1 + 艾欧 5，打满 20 分钟、两人都有出装：{n} 局，胜率 {sum(g['win'] for g in games) / n * 100:.1f}%")


def pc(x, k):
    return f"{x / k * 100:5.1f}%" if k else "  -  "


# ---- 1. 谁出梅肯斯姆 ----
print("\n== 1. 谁出梅肯斯姆（整局任何时候）==")
tab = Counter((g["lm"] is not None, g["im"] is not None) for g in games)
for (a, b), c in sorted(tab.items(), reverse=True):
    sub = [g for g in games if (g["lm"] is not None, g["im"] is not None) == (a, b)]
    print(f"  拉席克{'出' if a else '没出'} / 艾欧{'出' if b else '没出'}：{c} 局（{pc(c, n)}），"
          f"胜率 {pc(sum(g['win'] for g in sub), len(sub))}")
both = [g for g in games if g["lm"] is not None and g["im"] is not None]
if both:
    print(f"  两人都出的 {len(both)} 局里，拉席克先出的 {sum(g['lm'] < g['im'] for g in both)} 局")
print(f"  拉席克出梅肯斯姆的中位时间 {np.median([g['lm'] for g in games if g['lm'] is not None]) / 60:.1f} 分；"
      f"艾欧 {np.median([g['im'] for g in games if g['im'] is not None]) / 60:.1f} 分")
oth = [g for g in games if g["other_mek"] is not None]
print(f"  其余三个队友有人出梅肯 / 卫士胫甲：{len(oth)} 局（{pc(len(oth), n)}）")

# ---- 2. 艾欧什么时候出 ----
print("\n== 2. 艾欧出梅肯斯姆：按情况分 ==")


def rate(sub, key="im"):
    return f"{len(sub):5d} 局，艾欧出梅肯 {pc(sum(g[key] is not None for g in sub), len(sub))}"


print("  拉席克整局没出梅肯：", rate([g for g in games if g["lm"] is None]))
print("  拉席克出了梅肯    ：", rate([g for g in games if g["lm"] is not None]))
# 艾欧出梅肯那一刻拉席克有没有梅肯
io_mek = [g for g in games if g["im"] is not None]
before = sum(1 for g in io_mek if g["lm"] is not None and g["lm"] < g["im"])
print(f"  艾欧出梅肯的 {len(io_mek)} 局里，那时拉席克已经有梅肯的 {before} 局（{pc(before, len(io_mek))}）")
print("  其余队友出了梅肯 / 卫士胫甲：", rate([g for g in games if g["other_mek"] is not None]))
print("  其余队友没出              ：", rate([g for g in games if g["other_mek"] is None]))
print("  拉席克和其余队友都没出    ：", rate([g for g in games if g["other_mek"] is None and g["lm"] is None]))

# ---- 3. 拉席克不出梅肯：出了什么 ----
print("\n== 3. 拉席克不出梅肯的局，他 25 分钟前出了什么 / 出梅肯的局对比 ==")
for lab, sub in (("没出", [g for g in games if g["lm"] is None]), ("出了", [g for g in games if g["lm"] is not None])):
    k = len(sub)
    s = "  ".join(f"{NAMES[i]} {pc(sum(1 for g in sub if (t := first(g['lesh'], i)) is not None and t <= 1500), k)}"
                  for i in (WRAPS, KS, YK, BKB, VEIL, BLOOD, SHIVA, PIPE, CRIMSON))
    print(f"  {lab}（{k} 局）：{s}")


# ---- 4. 分组看：经济、对面伤害类型 ----
def tercile(key, label, sub_games=games):
    vals = sorted(g[key] for g in sub_games)
    cuts = [vals[len(vals) // 3], vals[2 * len(vals) // 3]]
    print(f"  {label}（三档分界 {cuts[0]:.2f} / {cuts[1]:.2f}）：")
    for lo, hi, nm in ((-1e18, cuts[0], "低"), (cuts[0], cuts[1], "中"), (cuts[1], 1e18, "高")):
        sub = [g for g in sub_games if lo <= g[key] < hi]
        print(f"    {nm}：{len(sub):5d} 局，拉席克出梅肯 {pc(sum(g['lm'] is not None for g in sub), len(sub))}，"
              f"艾欧出梅肯 {pc(sum(g['im'] is not None for g in sub), len(sub))}，"
              f"两人都没出 {pc(sum(g['lm'] is None and g['im'] is None for g in sub), len(sub))}")


print("\n== 4. 分档 ==")
tercile("emag", "对面五人的魔法+纯粹伤害占比（按英雄平均）")
tercile("nwdiff10", "10 分钟全队经济差")
tercile("lesh_nw12", "拉席克 12 分钟经济")


# ---- 5. 逻辑回归 ----
def logit(X, y, names, title):
    X = np.column_stack([np.ones(len(X)), X])
    b = np.zeros(X.shape[1])
    for _ in range(50):
        p = 1 / (1 + np.exp(-X @ b))
        W = p * (1 - p)
        H = X.T @ (X * W[:, None]) + 1e-6 * np.eye(X.shape[1])
        step = np.linalg.solve(H, X.T @ (y - p))
        b += step
        if np.abs(step).max() < 1e-8:
            break
    se = np.sqrt(np.diag(np.linalg.inv(H)))
    p = 1 / (1 + np.exp(-X @ b))
    print(f"\n  {title}（{len(y)} 局，平均 {y.mean() * 100:.1f}%）——系数换成「出的概率变化（百分点，在平均处）」")
    slope = (p * (1 - p)).mean()
    for nm, bb, s in zip(["常数"] + names, b, se):
        if nm == "常数":
            continue
        print(f"    {nm:28s} {bb * slope * 100:+6.1f}  (z = {bb / s:+.1f})")


print("\n== 5. 逻辑回归（同时放进所有因素）==")
sd_mag = np.std([g["emag"] for g in games])
feats = lambda g: [  # noqa: E731
    (g["emag"] - MAG_ALL) / sd_mag,
    g["nwdiff10"] / 5000,
    g["lesh_nw12"] / 1000 - 6,
    1.0 if g["other_mek"] is not None and g["other_mek"] < 1200 else 0.0,
    1.0 if g["other_pipe"] is not None and g["other_pipe"] < 1500 else 0.0,
]
fn = ["对面魔法占比 +1 标准差", "10 分经济差 +5000", "拉席克 12 分经济 +1000",
      "队友 20 分前出梅肯 / 卫士胫甲", "队友 25 分前出洞察烟斗"]
X = np.array([feats(g) for g in games])
logit(X, np.array([g["lm"] is not None for g in games], float), fn, "拉席克出梅肯")
X2 = np.column_stack([X, [1.0 if g["lm"] is not None and g["lm"] < 1080 else 0.0 for g in games]])
logit(X2, np.array([g["im"] is not None for g in games], float), fn + ["拉席克 18 分前已出梅肯"], "艾欧出梅肯")


# ---- 6. 对面具体英雄 ----
def hero_lift(key, title, side="enemies", min_n=120):
    base = sum(g[key] is not None for g in games) / n
    by = defaultdict(list)
    for g in games:
        for e in g[side]:
            by[e["h"]].append(g[key] is not None)
    out = []
    for h, v in by.items():
        if len(v) < min_n:
            continue
        r = sum(v) / len(v)
        z = (r - base) / math.sqrt(base * (1 - base) / len(v))
        out.append((z, h, r, len(v)))
    out.sort()
    print(f"\n  {title}（基准 {base * 100:.1f}%，至少 {min_n} 局；比了 {len(out)} 个英雄，|z| > 3.3 才过校正）")
    print("    最少出：" + "，".join(f"{HN.get(h, h)} {r * 100:.0f}%（{k} 局，z {z:+.1f}）" for z, h, r, k in out[:8]))
    print("    最多出：" + "，".join(f"{HN.get(h, h)} {r * 100:.0f}%（{k} 局，z {z:+.1f}）" for z, h, r, k in out[::-1][:8]))


print("\n== 6. 对面 / 队友有某个英雄时 ==")
hero_lift("lm", "拉席克出梅肯，按对面英雄")
hero_lift("im", "艾欧出梅肯，按对面英雄")
hero_lift("lm", "拉席克出梅肯，按其余队友", side="allies", min_n=100)
hero_lift("im", "艾欧出梅肯，按其余队友", side="allies", min_n=100)

print("\n  英雄魔法+纯粹伤害占比（本数据算的）：对面里最常见的几个")
cnt = Counter(e["h"] for g in games for e in g["enemies"])
print("    " + "，".join(f"{HN.get(h, h)} {MAG.get(h, 0) * 100:.0f}%" for h, _ in cnt.most_common(20)))

# ---- 7. 胜率：两人都出 / 只一个出（同经济差） ----
print("\n== 7. 25 分钟时谁有梅肯，胜率（按 20 分钟全队经济差分 5 档调整）==")
reach = [g for g in games if g["dur"] >= 1500]
for g in reach:
    a = g["lm"] is not None and g["lm"] <= 1500
    b = g["im"] is not None and g["im"] <= 1500
    g["grp"] = {(True, False): "只有拉席克", (False, True): "只有艾欧", (True, True): "两人都有",
                (False, False): "都没有"}[(a, b)]
    g["d20"] = sum(nw_at(p, 20) * (1 if p["rad"] == g["io"]["rad"] else -1)
                   for p in g["allies"] + [g["lesh"], g["io"]] + g["enemies"])
reach.sort(key=lambda g: g["d20"])
groups = ["只有拉席克", "只有艾欧", "两人都有", "都没有"]
seg = [reach[len(reach) * k // 5: len(reach) * (k + 1) // 5] for k in range(5)]
for grp in groups:
    sub = [g for g in reach if g["grp"] == grp]
    adj = []
    for s in seg:
        y = [g["win"] for g in s if g["grp"] == grp]
        adj.append((sum(y) / len(y), len(s)) if y else (None, len(s)))
    ok = [(r, w) for r, w in adj if r is not None]
    a = sum(r * w for r, w in ok) / sum(w for _, w in ok)
    print(f"  {grp}：{len(sub)} 局，原始胜率 {pc(sum(g['win'] for g in sub), len(sub))}，"
          f"按经济差档加权 {a * 100:.1f}%")
