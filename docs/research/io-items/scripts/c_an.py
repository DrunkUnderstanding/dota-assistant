"""第 3 步：艾欧对上难打的阵容时，T 分钟前出了某件装备，胜率 / 之后 10 分钟死亡数有没有变好。

对每件候选装备、每个时间点 T（15 / 20 / 25 分钟），只看打到 T 的局，线性回归（稳健标准误）：
  结果 = 出了 + 难打 + 出了×难打 + T 分钟经济差分档 + 艾欧自己经济 + T 前死亡数 + 位置 + 阵营
- 「难打组里出了的效果」= 出了 + 出了×难打：控制住当时经济后，难打局里出了和没出的差
- 「针对性」= 出了×难打：同一件装备在难打局里比在一般局里多出来的效果（差中差）
候选装备事先按规则定：价格 ≥ 800、不是消耗品 / 纯合成材料，25 分钟前有 ≥ 4% 的艾欧出过。
用法：python c_an.py [--hero 英雄id ...]（把「难打」换成对面有这些英雄）"""
import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from dota_assistant.sources import assets  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

IO = common.take_target(sys.argv)          # 研究的英雄（变量名沿用艾欧时的 IO）
D = common.data_dir(IO)
TS = (15, 20, 25)
WINDOW = 10                       # 死亡数看 T 之后几分钟
STANDALONE = {"ghost", "blink"}   # 标成合成材料、但本身就是成品的

ap = argparse.ArgumentParser()
ap.add_argument("--hero", type=int, nargs="*", default=None)
ap.add_argument("--min-share", type=float, default=0.04)
ap.add_argument("--define", choices=("raw", "engine"), default="raw",
                help="raw = 按艾欧对各英雄的实际胜率定难打；engine = 按引擎的克制（扣掉双方强度）")
ap.add_argument("--role", choices=("support", "core", "all"), default="support",
                help="support = 4/5 号位，core = 1/2/3 号位（两种打法出装完全不同，分开算）")
args = ap.parse_args()

consts = assets.items_const()
by_id = {v["id"]: (k, v) for k, v in consts.items() if isinstance(v, dict) and "id" in v}
from dota_assistant.domain import mates  # noqa: E402

cn = mates._item_names()


def iname(i: int) -> str:
    return cn.get(i, (None, str(i)))[1]


from dota_assistant.core import config  # noqa: E402
from dota_assistant.domain.engine import Engine, pct  # noqa: E402
from dota_assistant.sources import stats as sm  # noqa: E402

_cfg = config.load()
_eng = Engine(assets.load_heroes(), sm.load_stats(_cfg), sm.load_positions(_cfg))
# 克制艾欧的英雄（克制 ≤ -1.5 个百分点）：死亡里凶手是它们的单独数
BAD = {h.id for h in assets.load_heroes() if h.id != IO and pct(_eng.advantage(IO, h.id)) <= -1.5}

groups = json.loads((D / "groups.json").read_text(encoding="utf-8"))
score = {int(k): v for k, v in groups["score"].items()}
cut = groups["cut"]

# 「难打」按艾欧对每个英雄的实际胜率（不是引擎的「扣掉双方强度后的克制」——那个会把影魔这种本身弱的英雄算成克艾欧，
# 实际艾欧对影魔胜率 57%）。只用没取 STRATZ 的那一半对局来算，和分析用的对局不重叠，避免拿结果定义分组。
_all = json.loads((D / "io_matches.json").read_text(encoding="utf-8"))
_fetched = {json.loads(line)["id"] for line in (D / "fetched.jsonl").read_text(encoding="utf-8").splitlines()}
_vs = defaultdict(lambda: [0, 0])
_tot = [0, 0]
for r in _all:
    if r["match_id"] in _fetched:
        continue
    io_rad = IO in r["radiant_team"]
    win = r["radiant_win"] == io_rad
    _tot[0] += 1
    _tot[1] += win
    for e in (r["dire_team"] if io_rad else r["radiant_team"]):
        _vs[e][0] += 1
        _vs[e][1] += win
K_RAW = 200
_base = _tot[1] / _tot[0]


def _lg(p: float) -> float:
    return math.log(p / (1 - p))


RAW = {e: _lg((w + K_RAW * _base) / (g + K_RAW)) - _lg(_base) for e, (g, w) in _vs.items()}
if args.define == "raw":
    score = {r["match_id"]: sum(RAW.get(e, 0.0) for e in (r["dire_team"] if IO in r["radiant_team"] else r["radiant_team"]))
             for r in _all if r["match_id"] in _fetched}
    cut = sorted(score.values())[len(score) // 3]
    BAD ={e for e, v in RAW.items() if v <= -0.06}          # 实际胜率比艾欧平均低约 1.5 个百分点以上
_names = {h.id: h.name_cn for h in assets.load_heroes()}
NAME = _names.get(IO, str(IO))
print(f"对{NAME}实际胜率最低的英雄（另一半对局，收缩过）：",
      "、".join(f"{_names.get(e, e)} {100 * (1 / (1 + math.exp(-(v + _lg(_base)))) - _base):+.1f}"
               for e, v in sorted(RAW.items(), key=lambda t: t[1])[:12]))

games = []
for line in (D / "fetched.jsonl").read_text(encoding="utf-8").splitlines():
    m = json.loads(line)
    if m.get("missing"):
        continue
    io = next((p for p in m["players"] if p["h"] == IO), None)
    if not io or not io.get("parsed") or not io.get("items"):
        continue
    if any(not p.get("nw") for p in m["players"]):
        continue
    pos = io.get("pos") or ""
    if args.role != "all" and (pos in ("POSITION_4", "POSITION_5")) != (args.role == "support"):
        continue
    side = io["rad"]
    enemies = [p["h"] for p in m["players"] if p["rad"] != side]
    hard = (any(e in args.hero for e in enemies) if args.hero else score.get(m["id"], 0) < cut)
    games.append({"id": m["id"], "dur": m["dur"], "win": bool(io["win"]), "rad": side, "hard": hard,
                  "pos": io.get("pos") or "", "items": io["items"], "deaths": [d[0] for d in io["deaths"]],
                  "deaths_ctr": [d[0] for d in io["deaths"] if d[1] in BAD],
                  "ours": [p["nw"] for p in m["players"] if p["rad"] == side],
                  "theirs": [p["nw"] for p in m["players"] if p["rad"] != side],
                  "ionw": io["nw"], "n_bad": sum(e in BAD for e in enemies)})
print(f"可用对局 {len(games)}（难打 {sum(g['hard'] for g in games)}），难打的定义："
      + (f"对面有 {args.hero}" if args.hero else f"对面 5 人合计 < {cut:.3f}（最难打的 1/3，{args.define}）"))
print(f"{NAME}位置：", Counter(g["pos"] for g in games).most_common())
for h in (True, False):
    gs = [g for g in games if g["hard"] == h]
    print(f"  {'难打' if h else '其余'}：{len(gs)} 局，胜率 {sum(g['win'] for g in gs) / max(1, len(gs)):.1%}")


def first_buy(g: dict) -> dict:
    out = {}
    for t, i in g["items"]:
        out.setdefault(i, t)
    return out


for g in games:
    g["buy"] = first_buy(g)

# 候选装备（事先定的规则）
cnt = Counter(i for g in games for i, t in g["buy"].items() if t <= 25 * 60)
cands = []
for i, c in cnt.most_common():
    k, v = by_id.get(i, (None, {}))
    if not k or (v.get("cost") or 0) < 800 or v.get("qual") == "consumable":
        continue
    if v.get("qual") == "component" and k not in STANDALONE:
        continue
    if k.startswith("recipe"):
        continue
    if c / len(games) >= args.min_share:
        cands.append(i)
print("候选装备（25 分钟前出过的比例）：", "、".join(f"{iname(i)} {cnt[i] / len(games):.0%}" for i in cands))


def at(arr: list, t: int) -> float:
    return arr[min(t, len(arr) - 1)] if arr else 0.0


def design(gs: list[dict], item: int, T: int):
    lead = np.array([sum(at(a, T) for a in g["ours"]) - sum(at(a, T) for a in g["theirs"]) for g in gs]) / 1000
    edges = np.quantile(lead, np.linspace(0, 1, 11)[1:-1])
    lb = np.digitize(lead, edges)
    X, names = [], []

    def col(name, v):
        names.append(name)
        X.append(np.asarray(v, dtype=float))

    bought = np.array([g["buy"].get(item, 10 ** 9) <= T * 60 for g in gs])
    hard = np.array([g["hard"] for g in gs])
    col("const", np.ones(len(gs)))
    col("bought", bought)
    col("hard", hard)
    col("bought_x_hard", bought & hard)
    for b in range(1, 10):
        col(f"lead{b}", lb == b)
    col("lead", lead)
    col("ionw", np.array([at(g["ionw"], T) for g in gs]) / 1000)
    col("deaths_before", [sum(d < T * 60 for d in g["deaths"]) for g in gs])
    for p in ("POSITION_1", "POSITION_2", "POSITION_4"):     # 3 号位 / 5 号位当基准
        if any(g["pos"] == p for g in gs):
            col(p, [g["pos"] == p for g in gs])
    col("radiant", [g["rad"] for g in gs])
    # 对面有几个「克制它的英雄」：出装会跟着对面阵容变（比如对面物理核心多就出夜叉），
    # 不控制的话「被克制英雄杀的次数」少了，可能只是因为这些局对面克制英雄本来就少（2026-11-06 拉席克第一遍几乎每件装备都「显著」）
    for k in (1, 2, 3):
        col(f"n_bad{k}", [g["n_bad"] >= k for g in gs])
    return np.column_stack(X), names, bought, hard


def ols(X, y):
    XtX_inv = np.linalg.pinv(X.T @ X)
    b = XtX_inv @ X.T @ y
    e = y - X @ b
    n, k = X.shape
    meat = (X * e[:, None]).T @ (X * e[:, None])
    V = XtX_inv @ meat @ XtX_inv * n / max(1, n - k)
    return b, V


rows = []
TITLE = {"win": "胜率（百分点）", "deaths": f"T 后 {WINDOW} 分钟内{NAME}死亡数（次）",
         "deaths_ctr": f"T 后 {WINDOW} 分钟内被克制{NAME}的英雄杀死的次数"}
for outcome in ("win", "deaths", "deaths_ctr"):
    print(f"\n==== 结果：{TITLE[outcome]} ====")
    print(f"{'装备':<14}{'T':>4}{'难打 出/没出':>14}{'难打组里出了的效果':>22}{'针对性（差中差）':>22}")
    for item in cands:
        for T in TS:
            need = T * 60 + (WINDOW * 60 if outcome == "deaths" else 0)
            gs = [g for g in games if g["dur"] >= need]
            X, names, bought, hard = design(gs, item, T)
            if (bought & hard).sum() < 30 or (~bought & hard).sum() < 30:
                continue
            if outcome == "win":
                y = np.array([g["win"] for g in gs], dtype=float) * 100
            else:
                y = np.array([sum(T * 60 <= d < (T + WINDOW) * 60 for d in g[outcome]) for g in gs], dtype=float)
            b, V = ols(X, y)
            ib, ix = names.index("bought"), names.index("bought_x_hard")
            eff = b[ib] + b[ix]
            se_eff = math.sqrt(V[ib, ib] + V[ix, ix] + 2 * V[ib, ix])
            did, se_did = b[ix], math.sqrt(V[ix, ix])
            rows.append({"item": iname(item), "item_id": item, "T": T, "outcome": outcome,
                         "n_hard_bought": int((bought & hard).sum()), "n_hard_not": int((~bought & hard).sum()),
                         "eff": eff, "se_eff": se_eff, "did": did, "se_did": se_did})
            star = lambda v, s: "*" if abs(v) > 1.96 * s else " "  # noqa: E731
            fmt = "{:+.1f} ±{:.1f}" if outcome == "win" else "{:+.2f} ±{:.2f}"
            print(f"{iname(item):<14}{T:>4}{int((bought & hard).sum()):>7}/{int((~bought & hard).sum()):<6}"
                  f"{fmt.format(eff, 1.96 * se_eff):>20}{star(eff, se_eff)}"
                  f"{fmt.format(did, 1.96 * se_did):>20}{star(did, se_did)}")

n_tests = len(rows) * 2
# Bonferroni：所有比较一起算，显著需要 |z| > 门槛
from statistics import NormalDist  # noqa: E402
zb = NormalDist().inv_cdf(1 - 0.025 / max(1, n_tests))
print(f"\n共 {n_tests} 次比较；只靠运气，95% 区间不含 0 的约 {n_tests * 0.05:.0f} 个。"
      f"多重比较校正（Bonferroni）后要 |z| > {zb:.2f}：")
for r in rows:
    for key, se in (("eff", "se_eff"), ("did", "se_did")):
        if abs(r[key]) > zb * r[se]:
            print(f"  {r['item']} T={r['T']} {r['outcome']} {('难打组效果' if key == 'eff' else '针对性')} "
                  f"{r[key]:+.2f}（z = {r[key] / r[se]:.1f}）")
tag = ("_".join(map(str, args.hero)) if args.hero else "tercile_" + args.define) + "_" + args.role
(D / f"result_{tag}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
