"""第 3 步：把抓到的对局整理成「对面有某个英雄时，同英雄同位置的人出装怎么变、出了赢得多不多」的统计表。

输入：data/research/item-vs/fetched.jsonl.gz（本研究抓的）+ data/research/bkb/fetched.jsonl（黑皇杖研究的 2 万局，同字段）。
输出：docs/research/item-vs/item_vs.json（进 git，产品以后要用就读它）和 data/research/item-vs/report.txt（看结论用）。

单位：一个人打一局（英雄 H、位置 P、对面 5 个英雄）。装备只算第一次买（做出来）的那一件，见 ITEM_SET。

两种数：
1. **购买比例**（主要的）：H 打 P 时买 k 的比例；对面有 e 时的比例。
   和「平时」比之前先按对局时长标准化：对面有 e 的局要是普遍打得长，后期装的比例本来就高。
   期望比例 = 对面有 e 的那些局，按各自时长（5 分钟一档）查「平时同时长买 k 的比例」再平均。变化 = 实际 − 期望。
2. **扣掉局势的胜率**（辅助，第 75 节：原始装备胜率 91% 是买的时候领先还是落后）：
   每次购买记「赢没赢 − 按买的那一分钟全队经济差应有的胜率」，叫「胜率超出」。
   对面有 e 时 k 的胜率超出 − 对面没有 e 时 k 的胜率超出 = 「对上 e 出 k 多赢多少」（还混着买的人的水平，只当参考）。

显著性：所有（H, P, e, k）一起做 Bonferroni 校正（门槛 z 由检验次数算出来，report 里写着）；
另外按 match_id 奇偶分两半各算一遍，两半方向都一致才留（RepA / RepB 都写进表里）。

    python docs/research/item-vs/scripts/c_build.py
"""
import datetime
import gzip
import json
import statistics
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from dota_assistant import kb  # noqa: E402
from dota_assistant.core.config import DATA_DIR  # noqa: E402
from dota_assistant.sources import assets  # noqa: E402

D = DATA_DIR / "research" / "item-vs"
SOURCES = [D / "fetched.jsonl.gz", DATA_DIR / "research" / "bkb" / "fetched.jsonl"]
OUT_JSON = Path(__file__).resolve().parents[1] / "item_vs.json"
OUT_TXT = D / "report.txt"
VERSION = 1

MIN_GROUP = 300          # 一个（英雄, 位置）至少多少人次才统计
MIN_VS = 80              # 对面有 e 的局至少多少人次
MIN_BASE_RATE = 0.02     # 「平时」表里只存购买比例 ≥ 2% 的装备
MIN_LIFT = 0.05          # 购买比例至少变 5 个百分点才进「对面有 e」表
MIN_WIN_BUYERS = 60      # 胜率超出：对面有 e 时至少多少人买了 k
ALPHA = 0.05
DUR_BIN = 5              # 对局时长分档（分钟）
MAX_MIN = 90
NW_BIN = 1000            # 经济差分档（金）
NW_CLIP = 30

const = assets.items_const()
by_id: dict[int, str] = {}
for key, v in const.items():
    if v.get("id") is not None and not key.startswith("recipe_"):
        by_id[v["id"]] = key
EXTRA = {"dust", "ward_sentry", "ward_dispenser", "gem", "aghanims_shard", "magic_stick", "infused_raindrop",
         "blight_stone", "orb_of_venom", "orb_of_corrosion", "wind_lace", "cloak", "lifesteal", "blink"}


def _counted(key: str) -> bool:
    """成装（有合成配方、不是消耗品），≥ 2000 金的非消耗品，或者针对用的便宜东西（粉 / 岗哨 / 宝石 / 魔晶…）"""
    v = const[key]
    cost = v.get("cost") or 0
    if key in EXTRA:
        return True
    if cost <= 0 or "consumable" in (v.get("qual") or ""):
        return False
    return bool(v.get("components")) or cost >= 2000


ITEM_SET = sorted(k for k in by_id.values() if _counted(k))
ITEM_IX = {k: i for i, k in enumerate(ITEM_SET)}
NI = len(ITEM_SET)


# ---------- 读数据 ----------

def lines():
    seen = set()
    for src in SOURCES:
        if not src.exists():
            continue
        opener = gzip.open if src.suffix == ".gz" else open
        try:
            with opener(src, "rt", encoding="utf-8") as f:
                for line in f:
                    m = json.loads(line)
                    if m.get("missing") or m["id"] in seen:
                        continue
                    seen.add(m["id"])
                    yield m
        except (EOFError, OSError):                       # 还在抓的 gz 文件最后一段没写完
            pass


pg_grp, pg_enem, pg_win, pg_dur, pg_half, pg_core = [], [], [], [], [], []
b_pg, b_item, b_min, b_diff = [], [], [], []
grp_ix: dict[tuple[int, int], int] = {}
# 经济差 → 胜率（不看买了什么）：[核心/辅助, 分钟, 经济差档] 的局数和胜场
NB = 2 * NW_CLIP + 1
st_n = np.zeros((2, MAX_MIN + 1, NB))
st_w = np.zeros((2, MAX_MIN + 1, NB))
n_match = 0
for m in lines():
    ps = [p for p in m.get("players") or [] if p.get("nw") and p.get("pos")]
    if len(ps) != 10 or not all(p["parsed"] for p in ps) or sum(p["rad"] for p in ps) != 5:
        continue
    n_match += 1
    L = min(len(p["nw"]) for p in ps)
    rad = np.sum([p["nw"][:L] for p in ps if p["rad"]], axis=0)
    dire = np.sum([p["nw"][:L] for p in ps if not p["rad"]], axis=0)
    dur = min(int(m["dur"] // 60), MAX_MIN)
    for p in ps:
        diff = (rad - dire) if p["rad"] else (dire - rad)
        dbin = np.clip(np.round(diff / NW_BIN).astype(int), -NW_CLIP, NW_CLIP) + NW_CLIP
        pos = int(p["pos"][-1])
        core = 0 if pos <= 3 else 1
        mins = np.arange(min(L, MAX_MIN + 1))
        np.add.at(st_n[core], (mins, dbin[:len(mins)]), 1)
        if p["win"]:
            np.add.at(st_w[core], (mins, dbin[:len(mins)]), 1)
        g = grp_ix.setdefault((p["h"], pos), len(grp_ix))
        i = len(pg_grp)
        pg_grp.append(g)
        pg_enem.append([q["h"] for q in ps if q["rad"] != p["rad"]])
        pg_win.append(bool(p["win"]))
        pg_dur.append(dur // DUR_BIN)
        pg_half.append(m["id"] % 2)
        pg_core.append(core)
        seen = set()
        for t, iid in p["items"]:
            k = by_id.get(iid)
            if k is None or k not in ITEM_IX or k in seen:
                continue
            seen.add(k)
            minute = max(0, min(int(t // 60), L - 1, MAX_MIN))
            b_pg.append(i)
            b_item.append(ITEM_IX[k])
            b_min.append(minute)
            b_diff.append(int(dbin[minute]))

print(f"对局 {n_match}，人次 {len(pg_grp)}，购买 {len(b_pg)}，装备种类 {NI}", flush=True)
pg_grp = np.array(pg_grp, np.int32)
pg_enem = np.array(pg_enem, np.int16)
pg_win = np.array(pg_win, bool)
pg_dur = np.array(pg_dur, np.int16)
pg_half = np.array(pg_half, np.int8)
pg_core = np.array(pg_core, np.int8)
b_pg = np.array(b_pg, np.int32)
b_item = np.array(b_item, np.int16)
b_min = np.array(b_min, np.int16)
b_diff = np.array(b_diff, np.int16)

# 局势应有胜率（分钟 × 经济差档；样本少的格子往相邻档借，最后退回整分钟平均）
with np.errstate(invalid="ignore", divide="ignore"):
    sn = st_n + np.roll(st_n, 1, 2) + np.roll(st_n, -1, 2)
    sw = st_w + np.roll(st_w, 1, 2) + np.roll(st_w, -1, 2)
    curve = np.where(sn >= 50, sw / sn, 0.5)
b_exp = curve[pg_core[b_pg], b_min, b_diff]
b_excess = pg_win[b_pg].astype(float) - b_exp
NH = int(pg_enem.max()) + 1
NDB = MAX_MIN // DUR_BIN + 1

order = np.argsort(pg_grp, kind="stable")
bounds = np.searchsorted(pg_grp[order], np.arange(len(grp_ix) + 1))
b_order = np.argsort(pg_grp[b_pg], kind="stable")
b_bounds = np.searchsorted(pg_grp[b_pg][b_order], np.arange(len(grp_ix) + 1))


def group_stats(g: int, half: int | None = None) -> dict | None:
    rows = order[bounds[g]:bounds[g + 1]]
    bs = b_order[b_bounds[g]:b_bounds[g + 1]]
    if half is not None:
        rows = rows[pg_half[rows] == half]
        bs = bs[pg_half[b_pg[bs]] == half]
    n = len(rows)
    if n < (MIN_GROUP if half is None else MIN_GROUP // 2):
        return None
    local = np.full(len(pg_grp), -1, np.int32)
    local[rows] = np.arange(n)
    li = local[b_pg[bs]]
    X = np.zeros((n, NI), np.float32)
    X[li, b_item[bs]] = 1
    W = np.zeros((n, NI), np.float32)
    W[li, b_item[bs]] = b_excess[bs]
    E = np.zeros((n, NH), np.float32)
    E[np.arange(n)[:, None], pg_enem[rows]] = 1
    Dm = np.zeros((n, NDB), np.float32)
    Dm[np.arange(n), pg_dur[rows]] = 1
    n_e = E.sum(0)
    cnt = X.sum(0)
    rate = cnt / n
    nd = Dm.sum(0)
    r_d = (Dm.T @ X) / np.maximum(nd, 1)[:, None]          # 每个时长档的平时购买比例
    with np.errstate(invalid="ignore", divide="ignore"):
        obs = (E.T @ X) / n_e[:, None]
        exp = (E.T @ (Dm @ r_d)) / n_e[:, None]
        lift = obs - exp
        z_lift = lift / np.sqrt(np.clip(exp * (1 - exp), 1e-4, None) / n_e[:, None])
        cnt_e = E.T @ X
        sw_e = E.T @ W
        sw = W.sum(0)
        wx = sw / np.maximum(cnt, 1)
        wx_e = sw_e / cnt_e
        wx_ne = (sw - sw_e) / (cnt - cnt_e)
        # 对位本身的胜率差（幻影长矛手对上斯温，买什么都少赢 10 个点）会让每件装备都显得「对上 e 少赢」：
        # 减掉同一组所有装备合起来的差 M，剩下的才是「对上 e 时，钱花在 k 上比花在别的装备上」多赢多少
        all_e, all_n = cnt_e.sum(1), cnt.sum()
        M = sw_e.sum(1) / all_e - (sw.sum() - sw_e.sum(1)) / (all_n - all_e)
        dwin = wx_e - wx_ne - M[:, None]
        z_win = dwin / np.sqrt(0.25 / cnt_e + 0.25 / (cnt - cnt_e))
    times = {}
    if half is None:
        for k in np.nonzero(rate >= MIN_BASE_RATE)[0]:
            mm = b_min[bs][b_item[bs] == k]
            times[int(k)] = float(statistics.median(mm.tolist()))
    return {"n": n, "win": float(pg_win[rows].mean()), "rate": rate, "cnt": cnt, "wx": wx, "times": times,
            "n_e": n_e, "obs": obs, "exp": exp, "lift": lift, "z_lift": z_lift,
            "cnt_e": cnt_e, "dwin": dwin, "z_win": z_win}


full = {g: group_stats(g) for g in range(len(grp_ix))}
full = {g: s for g, s in full.items() if s}
halves = {g: (group_stats(g, 0), group_stats(g, 1)) for g in full}

# 检验次数：所有（组, 对面英雄, 装备）里样本够的格子
n_tests_lift = n_tests_win = 0
for s in full.values():
    ok = s["n_e"] >= MIN_VS
    n_tests_lift += int(ok.sum() * (s["rate"] >= MIN_BASE_RATE).sum())
    n_tests_win += int(((s["cnt_e"] >= MIN_WIN_BUYERS) & ok[:, None]).sum())
Z_LIFT = statistics.NormalDist().inv_cdf(1 - ALPHA / 2 / max(n_tests_lift, 1))
Z_WIN = statistics.NormalDist().inv_cdf(1 - ALPHA / 2 / max(n_tests_win, 1))
print(f"组 {len(full)}；购买比例检验 {n_tests_lift} 次，门槛 |z| ≥ {Z_LIFT:.2f}；胜率检验 {n_tests_win} 次，门槛 |z| ≥ {Z_WIN:.2f}",
      flush=True)


def _r(x: float, d: int = 3) -> float:
    return round(float(x), d)


inv_grp = {g: hp for hp, g in grp_ix.items()}
base_out, vs_out = {}, {}
n_lift = n_win = n_drop = 0
for g, s in full.items():
    h, pos = inv_grp[g]
    hk = f"{h}_{pos}"
    items = {}
    for k in np.nonzero(s["rate"] >= MIN_BASE_RATE)[0]:
        items[ITEM_SET[k]] = [_r(s["rate"][k]), s["times"].get(int(k)), _r(s["wx"][k])]
    base_out[hk] = {"n": s["n"], "win": _r(s["win"]), "items": items}
    a, b = halves[g]
    vs = {}
    for e in np.nonzero(s["n_e"] >= MIN_VS)[0]:
        rows = []
        for k in range(NI):
            lift_ok = (s["rate"][k] >= MIN_BASE_RATE or s["obs"][e, k] >= MIN_BASE_RATE) \
                and abs(s["lift"][e, k]) >= MIN_LIFT and abs(s["z_lift"][e, k]) >= Z_LIFT
            win_ok = s["cnt_e"][e, k] >= MIN_WIN_BUYERS and abs(s["z_win"][e, k]) >= Z_WIN
            if not (lift_ok or win_ok):
                continue
            # 两半各算一遍，方向一致才留
            sign_l, sign_w = np.sign(s["lift"][e, k]), np.sign(s["dwin"][e, k])
            rep_l = bool(a and b and np.sign(a["lift"][e, k]) == sign_l == np.sign(b["lift"][e, k]))
            rep_w = bool(a and b and np.sign(a["dwin"][e, k]) == sign_w == np.sign(b["dwin"][e, k]))
            lift_ok, win_ok = lift_ok and rep_l, win_ok and rep_w
            if not (lift_ok or win_ok):
                n_drop += 1
                continue
            n_lift += lift_ok
            n_win += win_ok
            dw = s["dwin"][e, k]
            rows.append([ITEM_SET[k], _r(s["obs"][e, k]), _r(s["exp"][e, k]), _r(s["lift"][e, k]),
                         _r(s["z_lift"][e, k], 1), int(lift_ok), int(s["cnt_e"][e, k]),
                         None if not np.isfinite(dw) else _r(dw), None if not np.isfinite(dw) else _r(s["z_win"][e, k], 1),
                         int(win_ok)])
        if rows:
            rows.sort(key=lambda r: -abs(r[3]))
            vs[str(int(e))] = {"n": int(s["n_e"][e]), "rows": rows}
    if vs:
        vs_out[hk] = vs

print(f"进表：购买比例变化 {n_lift} 条，胜率差 {n_win} 条；两半方向不一致去掉 {n_drop} 条", flush=True)

doc = {
    "v": VERSION,
    "built": datetime.date.today().isoformat(),
    "patch": kb.current_patch(),
    "sample": "天梯全英雄选择、时长 ≥ 15 分钟、平均段位超凡～冠绝（avg_rank_tier 70~89），OpenDota 在版本里均匀抽 match_id，STRATZ 抓出装",
    "matches": n_match,
    "thresholds": {"min_group": MIN_GROUP, "min_vs": MIN_VS, "min_base_rate": MIN_BASE_RATE, "min_lift": MIN_LIFT,
                   "min_win_buyers": MIN_WIN_BUYERS, "z_lift": _r(Z_LIFT, 2), "z_win": _r(Z_WIN, 2),
                   "tests_lift": n_tests_lift, "tests_win": n_tests_win},
    "fields": {
        "base": "「英雄id_位置」→ n 人次、win 胜率、items {装备: [购买比例, 中位购买分钟, 胜率超出]}；"
                "胜率超出 = 买的人赢没赢 − 按买的那一分钟全队经济差应有的胜率（平均），还混着买的人的水平",
        "vs": "「英雄id_位置」→ {对面英雄id: {n 人次, rows: [[装备, 对面有它时的购买比例, 按时长标准化的期望比例, 变化, "
              "变化的 z, 变化过校正且两半一致, 对面有它时买的人次, 胜率超出差（有它 − 没它）, 胜率差的 z, 胜率差过校正且两半一致]]}}",
    },
    "base": base_out,
    "vs": vs_out,
}
OUT_JSON.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
print(f"写出 {OUT_JSON}（{OUT_JSON.stat().st_size / 1e6:.1f} MB）", flush=True)

# ---------- 报告：挑几组看合不合常识 ----------
kbase = kb.KnowledgeBase.load()
hname = {hid: e.name for hid, e in kbase.heroes.items()}


def _iname(k: str) -> str:
    try:
        return kbase.item(k).name
    except KeyError:
        return const[k].get("dname") or k


iname = {k: _iname(k) for k in ITEM_SET}
out = [f"对局 {n_match}，（英雄, 位置）组 {len(full)}；购买比例门槛 |z| ≥ {Z_LIFT:.2f}（{n_tests_lift} 次检验），"
       f"胜率门槛 |z| ≥ {Z_WIN:.2f}（{n_tests_win} 次）；进表：购买比例 {n_lift} 条、胜率 {n_win} 条，两半不一致去掉 {n_drop} 条"]
# 1. 每个对面英雄：哪件装备在最多（英雄, 位置）组里显著变多 / 变少
from collections import Counter, defaultdict  # noqa: E402
up, down = defaultdict(Counter), defaultdict(Counter)
for hk, vs in vs_out.items():
    for e, v in vs.items():
        for r in v["rows"]:
            if r[5]:
                (up if r[3] > 0 else down)[int(e)][r[0]] += 1
out.append("\n【对面有这个英雄时，最多组里多买 / 少买的装备（括号里是几个英雄-位置组过了校正）】")
for e in sorted(up, key=lambda x: -sum(up[x].values())):
    u = "、".join(f"{iname[k]}({c})" for k, c in up[e].most_common(5))
    d = "、".join(f"{iname[k]}({c})" for k, c in down[e].most_common(3))
    out.append(f"  {hname.get(e, e)}：多 {u}" + (f"；少 {d}" if d else ""))
# 2. 胜率差过校正的
out.append("\n【胜率差过校正、两半一致的（对上 e 出 k 比对不上 e 时多赢 / 少赢）】")
wins = []
for hk, vs in vs_out.items():
    h, pos = map(int, hk.split("_"))
    for e, v in vs.items():
        for r in v["rows"]:
            if r[9]:
                wins.append((r[8], f"  {hname.get(h, h)} {pos} 号位 对上 {hname.get(int(e), e)}：{iname[r[0]]} "
                                   f"{r[7] * 100:+.1f}（z {r[8]:+.1f}，{r[6]} 人买），购买比例 {r[1] * 100:.0f}%（期望 {r[2] * 100:.0f}%）"))
out += [w for _, w in sorted(wins, key=lambda x: -abs(x[0]))[:80]] or ["  （没有）"]
OUT_TXT.write_text("\n".join(out), encoding="utf-8")
print(f"报告 {OUT_TXT}", flush=True)
