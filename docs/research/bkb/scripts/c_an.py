"""第 3 步：黑皇杖是不是什么局面出都有用。

单位：核心（1 / 2 / 3 号位）的一局。只看打到 T 分钟（15 / 20 / 25）的局；处理组 = T 分钟前出了黑皇杖。
线性回归（稳健标准误）：结果 = 黑皇杖 + 黑皇杖×分组 + 分组 + 当时全队经济差（10 档 + 线性）+ 自己经济 + 之前死了几次
                         + 位置 + 阵营 + 英雄（固定效应，每个英雄自己的平均水平）
「同样经济下，这钱花在黑皇杖上 vs 花在别的装备上」。结果：胜率；之后 10 分钟死几次（总数，不分凶手）。

分组（事先定好，每个按三等分 / 固定档位）：
- 对面控制技能个数（官方字段「只能强驱散」或知识库标签 眩晕 / 妖术 / 沉默 / 缠绕定身）
- 对面魔法伤害技能个数（官方字段 damage = 魔法）
- 对面无视技能免疫的技能个数（官方字段 immunity = 对敌方无视），0 / 1 / 2+
- 当时全队经济：落后 / 差不多 / 领先（三等分）
- 自己几号位；自己近战 / 远程
用法：python c_an.py"""
import json
import math
import sys
from collections import Counter
from pathlib import Path
from statistics import NormalDist

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from dota_assistant import kb  # noqa: E402
from dota_assistant.core.config import DATA_DIR  # noqa: E402

D = DATA_DIR / "research" / "bkb"
BKB = 116
TS = (25, 30, 35)       # 7.41f 黑皇杖 4050 金，核心 20 分钟前出的只有约 1%，25 分钟约 12%（试跑实查），所以从 25 分钟起算
MIN_CELL = 40           # 一组里出了 / 没出的局数少于这个就不报
WINDOW = 10
DIS = {"眩晕", "妖术", "沉默", "缠绕 / 定身"}

# ---------- 每个英雄的技能特征（官方字段 + 知识库标签） ----------
raw = json.loads((DATA_DIR / "kb" / "kb.json").read_text(encoding="utf-8"))
base = kb.KnowledgeBase.load()
heroes_raw = raw["heroes"] if isinstance(raw["heroes"], list) else list(raw["heroes"].values())
FEAT, MELEE, NAME = {}, {}, {}
for h in heroes_raw:
    hk = base.heroes.get(h["id"])
    ents = {e.key: e for e in hk.abilities} if hk else {}
    mag = ctl = pier = 0
    for ab in h["abilities"]:
        if ab.get("is_item") or ab.get("ability_is_innate") or ab.get("target_team") not in (0, 2, 3):
            continue
        e = ents.get(ab["name"])
        tags = set(e.tags()) if e else set()
        c = ab.get("dispellable") == 1 or bool(tags & DIS)
        mag += ab.get("damage") == 2
        ctl += c
        pier += ab.get("immunity") == 3 and c      # 只算「无视技能免疫的控制」：带伤害的被动（反击螺旋这类）几乎人人都有，分不开组
    FEAT[h["id"]] = (ctl, mag, pier)
    MELEE[h["id"]] = h.get("attack_capability") == 1
    NAME[h["id"]] = h["name_loc"]

# ---------- 读对局 ----------
rows = []
for line in (D / "fetched.jsonl").read_text(encoding="utf-8").splitlines():
    m = json.loads(line)
    if m.get("missing"):
        continue
    ps = m["players"]
    if any(not p.get("nw") for p in ps):
        continue
    for p in ps:
        if p.get("pos") not in ("POSITION_1", "POSITION_2", "POSITION_3") or not p.get("parsed"):
            continue
        enemies = [q["h"] for q in ps if q["rad"] != p["rad"]]
        buy = {}
        for t, i in p["items"]:
            buy.setdefault(i, t)
        rows.append({"dur": m["dur"], "win": bool(p["win"]), "h": p["h"], "pos": p["pos"], "rad": p["rad"],
                     "bkb": buy.get(BKB, 10 ** 9), "deaths": [d[0] for d in p["deaths"]], "nw": p["nw"],
                     "ours": [q["nw"] for q in ps if q["rad"] == p["rad"]],
                     "theirs": [q["nw"] for q in ps if q["rad"] != p["rad"]],
                     "ctl": sum(FEAT.get(e, (0, 0, 0))[0] for e in enemies),
                     "mag": sum(FEAT.get(e, (0, 0, 0))[1] for e in enemies),
                     "pier": sum(FEAT.get(e, (0, 0, 0))[2] for e in enemies)})
print(f"核心局 {len(rows)}（{len({r['h'] for r in rows})} 个英雄），出过黑皇杖 {sum(r['bkb'] < 10 ** 9 for r in rows) / len(rows):.0%}")
for T in TS:
    print(f"  {T} 分钟前出了：{sum(r['bkb'] <= T * 60 for r in rows if r['dur'] >= T * 60) / sum(r['dur'] >= T * 60 for r in rows):.1%}")


def at(a, t):
    return a[min(t, len(a) - 1)] if a else 0.0


def ols(X, y):
    inv = np.linalg.pinv(X.T @ X)
    b = inv @ X.T @ y
    e = y - X @ b
    meat = (X * e[:, None]).T @ (X * e[:, None])
    n, k = X.shape
    return b, inv @ meat @ inv * n / max(1, n - k)


def tercile(vals):
    q1, q2 = np.quantile(vals, [1 / 3, 2 / 3])
    return np.where(vals <= q1, 0, np.where(vals <= q2, 1, 2)), (q1, q2)


def fit(gs, T, outcome, seg=None, seg_names=None):
    """seg = 每局的分组（0/1/2…），返回各组黑皇杖效果 [(组名, 效果, 标准误, 出了局数)]，以及最高组 − 最低组"""
    n = len(gs)
    lead = np.array([sum(at(a, T) for a in g["ours"]) - sum(at(a, T) for a in g["theirs"]) for g in gs]) / 1000
    lb = np.digitize(lead, np.quantile(lead, np.linspace(0, 1, 11)[1:-1]))
    bk = np.array([g["bkb"] <= T * 60 for g in gs], dtype=float)
    cols, names = [np.ones(n)], ["const"]

    def add(nm, v):
        cols.append(np.asarray(v, dtype=float))
        names.append(nm)

    for b in range(1, 10):
        add(f"lead{b}", lb == b)
    add("lead", lead)
    add("nw", [at(g["nw"], T) / 1000 for g in gs])
    add("deaths_before", [sum(d < T * 60 for d in g["deaths"]) for g in gs])
    add("p2", [g["pos"] == "POSITION_2" for g in gs])
    add("p3", [g["pos"] == "POSITION_3" for g in gs])
    add("rad", [g["rad"] for g in gs])
    hc = Counter(g["h"] for g in gs)
    for h in sorted(hc):
        if hc[h] >= 30 and h != max(hc, key=hc.get):
            add(f"h{h}", [g["h"] == h for g in gs])
    seg = np.zeros(n, dtype=int) if seg is None else np.asarray(seg)
    groups = [k for k in sorted(set(seg.tolist()))
              if (bk * (seg == k)).sum() >= MIN_CELL and ((1 - bk) * (seg == k)).sum() >= MIN_CELL]
    if not groups:
        return [], (0.0, float("inf"))
    keep = np.isin(seg, groups)
    if not keep.all():                      # 样本太少的组整组去掉
        return fit([g for g, k in zip(gs, keep) if k], T, outcome, seg[keep].tolist(), seg_names)
    for k in groups[1:]:
        add(f"seg{k}", seg == k)
    for k in groups:
        add(f"bkb_seg{k}", bk * (seg == k))
    X = np.column_stack(cols)
    if outcome == "win":
        y = np.array([g["win"] for g in gs], dtype=float) * 100
    else:
        y = np.array([sum(T * 60 <= d < (T + WINDOW) * 60 for d in g["deaths"]) for g in gs], dtype=float)
    b, V = ols(X, y)
    out = []
    for k in groups:
        i = names.index(f"bkb_seg{k}")
        out.append(((seg_names or {}).get(k, str(k)), b[i], math.sqrt(V[i, i]), int((bk * (seg == k)).sum()),
                    int(((1 - bk) * (seg == k)).sum())))
    i0, i1 = names.index(f"bkb_seg{groups[0]}"), names.index(f"bkb_seg{groups[-1]}")
    diff = (b[i1] - b[i0], math.sqrt(V[i0, i0] + V[i1, i1] - 2 * V[i0, i1]))
    return out, diff


z_lim = NormalDist().inv_cdf(1 - 0.025 / 60)     # 约 60 次比较一起校正
print(f"多重比较校正后 |z| 要超过 {z_lim:.2f} 才算数（区间是 95%）")
for outcome in ("win", "deaths"):
    unit = "胜率（百分点）" if outcome == "win" else f"之后 {WINDOW} 分钟死亡数（次）"
    fmt = "{:+.1f} ±{:.1f}" if outcome == "win" else "{:+.2f} ±{:.2f}"
    for T in TS:
        need = T * 60 + (WINDOW * 60 if outcome == "deaths" else 0)
        gs = [g for g in rows if g["dur"] >= need]
        print(f"\n==== {T} 分钟前出了黑皇杖 → {unit}，{len(gs)} 局 ====")

        def show(title, seg=None, names=None):
            res, diff = fit(gs, T, outcome, seg, names)
            if not res:
                print(f"  {title}：样本太少")
                return
            parts = [f"{nm} {fmt.format(e, 1.96 * s)}{'*' if abs(e) > 1.96 * s else ''}（{nb}/{nn}）"
                     for nm, e, s, nb, nn in res]
            line = f"  {title}：" + "；".join(parts)
            if len(res) > 1:
                z = diff[0] / diff[1]
                line += f"  ‖ 最后一组 − 第一组 {fmt.format(diff[0], 1.96 * diff[1])}（z = {z:.1f}{'，过校正' if abs(z) > z_lim else ''}）"
            print(line)

        show("全部")
        for key, title in (("ctl", "对面控制技能"), ("mag", "对面魔法伤害技能")):
            seg, (q1, q2) = tercile(np.array([g[key] for g in gs], dtype=float))
            show(f"{title}（≤{q1:.0f} / ≤{q2:.0f} / 更多）", seg, {0: "少", 1: "中", 2: "多"})
        show("对面无视技能免疫的技能", [min(2, g["pier"]) for g in gs], {0: "0 个", 1: "1 个", 2: "2 个+"})
        lead = np.array([sum(at(a, T) for a in g["ours"]) - sum(at(a, T) for a in g["theirs"]) for g in gs])
        seg, _ = tercile(lead)
        show("当时全队经济", seg, {0: "落后", 1: "差不多", 2: "领先"})
        show("自己位置", [int(g["pos"][-1]) - 1 for g in gs], {0: "1 号位", 1: "2 号位", 2: "3 号位"})
        show("自己攻击方式", [0 if MELEE.get(g["h"]) else 1 for g in gs], {0: "近战", 1: "远程"})
