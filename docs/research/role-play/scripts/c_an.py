"""第 3 步：分析 1/2/3 号位打法和输赢（7.42 vs 7.41f 对照），结果写 data/research/role-play/result.json + report.txt。

    python -X utf8 docs/research/role-play/scripts/c_an.py

核心模型（每个位置、每个时间段、每个版本各跑一次）：一局一行，
  天辉赢没赢(0/1) ~ Σ 特征差(天辉该位置 − 夜魇该位置) + 时间段开始时两队经济差分档 + 两边该位置英雄（+1 / −1）
线性概率模型，系数直接是「胜率百分点」，标准误用 HC1 稳健标准误。只用打到时间段结束的对局。
特征之间互相控制（同样参团数下多补 10 刀值多少），时间段里打出来的经济不控制（那是行为的结果）。
"""
import gzip
import importlib.util
import json
import math
from collections import Counter, defaultdict

import numpy as np
from common import D, ROOT

from dota_assistant.core.config import DATA_DIR

POS = ["POSITION_1", "POSITION_2", "POSITION_3", "POSITION_4", "POSITION_5"]
VERS = ["741", "742"]
WINDOWS = [(0, 10), (10, 20), (20, 30)]
LEAD_BINS = [-6000, -3000, -1500, -500, 500, 1500, 3000, 6000]
FEAT_NAME = {"kp": "参与击杀（击杀 + 助攻）", "dth": "死亡", "lh": "正补（每 10 个）", "push": "站位靠前（每 10%）",
             "tw": "亲手推掉的塔", "rune": "拿符（赏金 / 神符 / 智慧）", "stack": "屯野"}
FEAT_UNIT = {"lh": 10.0, "push": 0.1}
FOUNTAIN = {True: (76, 78), False: (178, 174)}      # STRATZ 坐标（格子），从开局位置取的
FDIST = math.dist(FOUNTAIN[True], FOUNTAIN[False])
HEROES = {h["id"]: h for h in json.loads((DATA_DIR / "heroes.json").read_text(encoding="utf-8"))}

_spec = importlib.util.spec_from_file_location("hb_build", ROOT / "docs" / "research" / "hero-builds" / "scripts" / "c_build.py")
hb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hb)
ITEMS = hb.Items()
KB = ITEMS.base
BIG = 1800


# ---------------------------------------------------------------- 读数据

def load(v: str) -> list[dict]:
    out = []
    try:
        with gzip.open(D / f"f{v}.jsonl.gz", "rt", encoding="utf-8") as f:
            for line in f:
                m = json.loads(line)
                if m.get("missing") or m.get("dur", 0) < 900 or len(m["players"]) != 10:
                    continue
                ps = m["players"]
                if not all(p["parsed"] and p["nw"] and p["loc"] for p in ps):
                    continue
                if any(sorted(p["pos"] or "" for p in ps if p["rad"] == rad) != POS for rad in (True, False)):
                    continue
                # 有人掉线 / 放弃：放弃后钱分给队友，身价掉到最高时的一半以下；或者开局就没连上（15 分钟身价不到 1500）
                if any(p["nw"][-1] < 0.5 * max(p["nw"]) or nw_at(p, 15) < 1500 for p in ps):
                    continue
                m["by"] = {(p["rad"], p["pos"]): p for p in ps}
                out.append(m)
    except (EOFError, OSError):          # 还在抓，最后一段没写完
        pass
    return out


def nw_at(p: dict, minute: int) -> float:
    a = p["nw"]
    return a[min(minute, len(a) - 1)]


def team_lead(m: dict, minute: int) -> float:
    return sum(nw_at(p, minute) * (1 if p["rad"] else -1) for p in m["players"])


def team_nw(m: dict, rad: bool, minute: int) -> float:
    return sum(nw_at(p, minute) for p in m["players"] if p["rad"] == rad)


def rad_win(m: dict) -> int:
    return int(next(p["win"] for p in m["players"] if p["rad"]))


def feats(m: dict, p: dict, a: int, b: int) -> dict:
    ta, tb = a * 60, b * 60
    kp = sum(1 for e in p["kills"] if ta <= e[0] < tb) + sum(1 for e in p["assists"] if ta <= e[0] < tb)
    dth = sum(1 for e in p["deaths"] if ta <= e[0] < tb)
    lh = sum((p["lhm"] or [])[a:b])
    # locationReport 不管打多久都是约 68 个点，均匀铺在整局上（第 i 个点 ≈ i × 时长 / 点数）
    L = len(p["loc"])
    pts = [xy for i, xy in enumerate(p["loc"]) if ta <= i * m["dur"] / L < tb and xy[0] is not None]
    push = (sum(math.dist(xy, FOUNTAIN[p["rad"]]) for xy in pts) / len(pts) / FDIST) if pts else float("nan")
    tw = sum(1 for t in m["towers"] if ta <= t[0] < tb and t[3] == p["h"] and t[2] != p["rad"])
    rune = sum(1 for r in p["runes"] if ta <= r[0] < tb and r[2] in ("PICKUP", "BOTTLE") and r[1] != "WATER")
    st = p["stack"] or [0]
    stack = st[min(b, len(st) - 1)] - (st[min(a, len(st) - 1)] if a > 0 else 0)
    return {"kp": kp, "dth": dth, "lh": lh, "push": push, "tw": tw, "rune": rune, "stack": stack}


# ---------------------------------------------------------------- 回归

def ols(X: np.ndarray, y: np.ndarray):
    XtX_inv = np.linalg.pinv(X.T @ X)
    beta = XtX_inv @ X.T @ y
    e = y - X @ beta
    n, k = X.shape
    meat = (X * (e ** 2)[:, None]).T @ X
    cov = XtX_inv @ meat @ XtX_inv * n / max(1, n - k)
    return beta, np.sqrt(np.clip(np.diag(cov), 0, None))


def lead_dummies(lead: float) -> list[float]:
    k = sum(1 for c in LEAD_BINS if lead > c)
    return [1.0 if j == k else 0.0 for j in range(1, len(LEAD_BINS) + 1)]      # 第 0 档当基准


def hero_block(hrows: list[tuple[int, int]]) -> np.ndarray:
    idx: dict[int, int] = {}
    for a, b in hrows:
        idx.setdefault(a, len(idx))
        idx.setdefault(b, len(idx))
    H = np.zeros((len(hrows), len(idx)))
    for i, (a, b) in enumerate(hrows):
        H[i, idx[a]] += 1
        H[i, idx[b]] -= 1
    return H[:, 1:]


def pack(beta, se, names, raw, offset=1) -> dict:
    out = {}
    for j, f in enumerate(names):
        vals = [x / FEAT_UNIT.get(f, 1.0) for x in raw[f]]
        out[f] = {"b": float(beta[offset + j]) * 100, "se": float(se[offset + j]) * 100,
                  "mean": float(np.mean(raw[f])), "sd_unit": float(np.std(vals))}
    return out


def window_model(ms: list[dict], pos: str, a: int, b: int, fl: list[str]) -> dict:
    rows, ys, hrows, raw = [], [], [], defaultdict(list)
    for m in ms:
        if m["dur"] < b * 60:
            continue
        pr, pd = m["by"][(True, pos)], m["by"][(False, pos)]
        fr, fd = feats(m, pr, a, b), feats(m, pd, a, b)
        if any(math.isnan(fr[f]) or math.isnan(fd[f]) for f in fl):
            continue
        for f in fl:
            raw[f] += [fr[f], fd[f]]
        ctrl = lead_dummies(team_lead(m, a)) if a > 0 else []
        rows.append([1.0] + [(fr[f] - fd[f]) / FEAT_UNIT.get(f, 1.0) for f in fl] + ctrl)
        ys.append(rad_win(m))
        hrows.append((pr["h"], pd["h"]))
    if len(rows) < 500:
        return {"n": len(rows)}
    X = np.hstack([np.array(rows), hero_block(hrows)])
    beta, se = ols(X, np.array(ys, dtype=float))
    return {"n": len(rows), "coef": pack(beta, se, fl, raw)}


def comeback_model(ms: list[dict], pos: str, at: int = 15, until: int = 25, gap: int = 2000) -> dict:
    """第 at 分钟落后 ≥ gap 的一方：它这个位置在 at~until 分钟做了什么（减对面同位置）→ 最后翻没翻盘。"""
    fl = ["kp", "dth", "lh", "push", "tw"]
    rows, ys, hrows, raw = [], [], [], defaultdict(list)
    for m in ms:
        if m["dur"] < until * 60:
            continue
        lead = team_lead(m, at)
        if abs(lead) < gap:
            continue
        behind = lead < 0                         # True = 天辉落后
        pb, pa = m["by"][(behind, pos)], m["by"][(not behind, pos)]
        fb, fa = feats(m, pb, at, until), feats(m, pa, at, until)
        for f in fl:
            raw[f] += [fb[f], fa[f]]
        rows.append([1.0] + [(fb[f] - fa[f]) / FEAT_UNIT.get(f, 1.0) for f in fl] + [abs(lead) / 1000])
        ys.append(int(rad_win(m) == int(behind)))
        hrows.append((pb["h"], pa["h"]))
    if len(rows) < 400:
        return {"n": len(rows)}
    X = np.hstack([np.array(rows), hero_block(hrows)])
    beta, se = ols(X, np.array(ys, dtype=float))
    return {"n": len(rows), "base": float(np.mean(ys)), "coef": pack(beta, se, fl, raw)}


def gap_model(ms: list[dict], minute: int) -> dict:
    """每个位置和对面同位置的经济差（千）→ 天辉胜率，五个位置一起放进去。"""
    rows, ys = [], []
    for m in ms:
        if m["dur"] < minute * 60 + 60:
            continue
        rows.append([1.0] + [(nw_at(m["by"][(True, q)], minute) - nw_at(m["by"][(False, q)], minute)) / 1000 for q in POS])
        ys.append(rad_win(m))
    beta, se = ols(np.array(rows), np.array(ys, dtype=float))
    sds = np.std(np.array(rows)[:, 1:], axis=0)
    return {"n": len(rows), "coef": [{"b": float(beta[1 + j]) * 100, "se": float(se[1 + j]) * 100,
                                       "sd": float(sds[j])} for j in range(5)]}


def share_model(ms: list[dict], minute: int = 20) -> dict:
    """全队经济一样多时，钱多分给 1 / 2 / 3 号位（占全队比例，每 5 个百分点）→ 胜率；4、5 号位当基准。"""
    rows, ys = [], []
    for m in ms:
        if m["dur"] < minute * 60 + 60:
            continue
        tr, td = team_nw(m, True, minute), team_nw(m, False, minute)
        sh = [(nw_at(m["by"][(True, q)], minute) / tr - nw_at(m["by"][(False, q)], minute) / td) / 0.05 for q in POS[:3]]
        rows.append([1.0] + sh + lead_dummies(tr - td))
        ys.append(rad_win(m))
    beta, se = ols(np.array(rows), np.array(ys, dtype=float))
    return {"n": len(rows), "coef": [{"b": float(beta[1 + j]) * 100, "se": float(se[1 + j]) * 100} for j in range(3)]}


# ---------------------------------------------------------------- 描述统计

LANE_IDX = {"POSITION_1": ("bot", "top"), "POSITION_5": ("bot", "top"), "POSITION_3": ("top", "bot"),
            "POSITION_4": ("top", "bot"), "POSITION_2": ("mid", "mid")}
LI = {"top": 0, "mid": 1, "bot": 2}


def lane_result(m: dict, p: dict) -> str | None:
    o = m["lanes"][LI[LANE_IDX[p["pos"]][0 if p["rad"] else 1]]]
    if not o:
        return None
    if o == "TIE":
        return "平"
    mine = o.startswith("RADIANT") == p["rad"]
    stomp = o.endswith("STOMP")
    return ("大优" if stomp else "优") if mine else ("大劣" if stomp else "劣")


def first_bigs(p: dict) -> list[tuple[int, str]]:
    seen, out = set(), []
    for t, iid in p["items"]:
        k = ITEMS.key_of.get(iid)
        if not k or k in seen:
            continue
        seen.add(k)
        if ITEMS.built(k) and not ITEMS.is_boots(k) and ITEMS.info(k)["cost"] >= BIG:
            out.append((t, k))
    return out


def ci(w: int, n: int) -> tuple[float, float]:
    p = w / n
    return p, 1.96 * math.sqrt(p * (1 - p) / n)


def describe(ms: list[dict]) -> dict:
    out: dict = {"n": len(ms)}
    durs = np.array([m["dur"] / 60 for m in ms])
    out["dur"] = {"mean": float(durs.mean()), "median": float(np.median(durs)),
                  "lt30": float((durs < 30).mean()), "30_40": float(((durs >= 30) & (durs < 40)).mean()),
                  "ge40": float((durs >= 40).mean()), "ge50": float((durs >= 50).mean())}
    out["kpm"] = float(sum(sum(p["k"] or 0 for p in m["players"]) for m in ms) / durs.sum())
    out["kills10"] = float(np.mean([sum(1 for p in m["players"] for e in p["kills"] if e[0] < 600) for m in ms]))
    out["deaths_by_window"] = {f"{a}-{b}": float(np.mean([sum(1 for p in m["players"] for e in p["deaths"] if a * 60 <= e[0] < b * 60)
                                                        for m in ms if m["dur"] >= b * 60]))
                               for a, b in [(0, 10), (10, 20), (20, 30), (30, 40)]}
    fw, ft = [], []
    for m in ms:
        ts = sorted(m["towers"])
        if ts:
            ft.append(ts[0][0] / 60)
            fw.append(int((not ts[0][2]) == bool(rad_win(m))))
    out["first_tower"] = float(np.median(ft))
    out["first_tower_win"] = float(np.mean(fw))
    # 领先方最后赢的比例
    cb = {}
    for minute in (10, 15, 20, 25, 30):
        for L in (2000, 5000, 10000):
            w = [int((team_lead(m, minute) > 0) == bool(rad_win(m))) for m in ms
                 if m["dur"] >= minute * 60 + 60 and abs(team_lead(m, minute)) >= L]
            cb[f"{minute}_{L}"] = {"n": len(w), "p": float(np.mean(w)) if len(w) >= 30 else None}
    out["comeback"] = cb
    roles = {}
    for pos in POS:
        r: dict = {}
        ps = [(m, m["by"][(rad, pos)]) for m in ms for rad in (True, False)]
        for minute in (10, 15, 20, 25, 30):
            r[f"nw{minute}"] = float(np.mean([nw_at(p, minute) for m, p in ps if m["dur"] >= minute * 60 + 60]))
            r[f"share{minute}"] = float(np.mean([nw_at(p, minute) / team_nw(m, p["rad"], minute)
                                                 for m, p in ps if m["dur"] >= minute * 60 + 60]))
        for a, b in WINDOWS:
            sub = [feats(m, p, a, b) for m, p in ps if m["dur"] >= b * 60]
            r[f"w{a}-{b}"] = {f: float(np.nanmean([x[f] for x in sub])) for f in FEAT_NAME}
        r["lvl"] = float(np.mean([p["lvl"] or 0 for m, p in ps]))
        src = defaultdict(float)
        for m, p in ps:
            fd = p.get("farm") or {}
            for k in ("creepLocation", "neutralLocation", "ancientLocation", "buildings"):
                src[k] += sum(x[2] or 0 for x in (fd.get(k) or []))
        tot = sum(src.values()) or 1
        r["farm_share"] = {k: v / tot for k, v in src.items()}
        lr = defaultdict(lambda: [0, 0])
        for m, p in ps:
            x = lane_result(m, p)
            if x:
                lr[x][0] += 1
                lr[x][1] += int(p["win"])
        r["lane"] = {k: {"n": v[0], "win": v[1] / v[0]} for k, v in lr.items()}
        # 出装节奏
        fb = [first_bigs(p) for m, p in ps]
        r["big1_min"] = float(np.median([x[0][0] / 60 for x in fb if x]))
        r["big2_min"] = float(np.median([x[1][0] / 60 for x in fb if len(x) > 1]))
        bkb = [next((t for t, k in x if k == "black_king_bar"), None) for x in fb]
        r["bkb_share"] = float(np.mean([t is not None for t in bkb]))
        r["bkb_min"] = float(np.median([t / 60 for t in bkb if t is not None])) if any(t is not None for t in bkb) else None
        c1 = Counter(x[0][1] for x in fb if x)
        r["first_big"] = [{"k": k, "name": ITEMS.info(k)["name"], "share": n / len(ps)} for k, n in c1.most_common(8)]
        anyb = Counter(k for x in fb for _, k in x)
        r["any_big"] = {k: n / len(ps) for k, n in anyb.items() if n / len(ps) >= 0.03}
        # 英雄
        hs = defaultdict(lambda: [0, 0])
        for m, p in ps:
            hs[p["h"]][0] += 1
            hs[p["h"]][1] += int(p["win"])
        r["heroes"] = {str(h): v for h, v in hs.items()}
        # 英雄类型：近战 / 远程、带不带 Nuker（法术爆发）标签
        ty = defaultdict(lambda: [0, 0])
        for h, (n, w) in hs.items():
            kh = KB.heroes.get(h)
            tags = [("近战" if kh and kh.attack == "近战" else "远程")]
            tags.append("法系爆发" if "Nuker" in HEROES.get(h, {}).get("roles", []) else "非法系")
            for t in tags:
                ty[t][0] += n
                ty[t][1] += w
        r["types"] = {k: {"n": n, "win": w / n} for k, (n, w) in ty.items()}
        sh = [next((t for t, iid in p["items"] if ITEMS.key_of.get(iid) == "aghanims_shard"), None) for m, p in ps]
        r["shard_share"] = float(np.mean([t is not None for t in sh]))
        roles[pos] = r
    out["roles"] = roles
    return out


def examples(ms: list[dict]) -> list[dict]:
    """挑几局实际对局当例子：第 15 分钟落后 ≥5000 还翻盘的，记一号位 / 中单 / 三号位 15~30 分钟干了什么。"""
    out = []
    for m in ms:
        if m["dur"] < 30 * 60:
            continue
        lead = team_lead(m, 15)
        if abs(lead) < 5000:
            continue
        behind = lead < 0
        if rad_win(m) != int(behind):
            continue
        row = {"id": m["id"], "dur": m["dur"] / 60, "lead15": -abs(lead), "lead_curve": [team_lead(m, x) * (1 if behind else -1)
                                                                                        for x in range(0, int(m["dur"] // 60) + 1, 5)],
               "roles": {}}
        for pos in POS[:3]:
            p, e = m["by"][(behind, pos)], m["by"][(not behind, pos)]
            f, g = feats(m, p, 15, 30), feats(m, e, 15, 30)
            row["roles"][pos] = {"hero": HEROES.get(p["h"], {}).get("name_cn", str(p["h"])),
                                 "vs": HEROES.get(e["h"], {}).get("name_cn", str(e["h"])),
                                 "me": f, "enemy": g, "nw15": nw_at(p, 15), "nw30": nw_at(p, 30),
                                 "enw15": nw_at(e, 15), "enw30": nw_at(e, 30),
                                 "items": [ITEMS.info(k)["name"] for t, k in first_bigs(p) if t < 30 * 60]}
        out.append(row)
    out.sort(key=lambda r: r["lead15"])
    return out[:80]


def main() -> None:
    result: dict = {"vers": {}}
    for v in VERS:
        ms = load(v)
        print(v, "可用对局", len(ms), flush=True)
        R: dict = {"desc": describe(ms), "gap": {}, "win": {}, "comeback": {}}
        for minute in (10, 15, 20):
            R["gap"][str(minute)] = gap_model(ms, minute)
        R["share"] = share_model(ms, 20)
        for pos in POS[:3]:
            for a, b in WINDOWS:
                fl = ["kp", "dth", "lh", "push", "tw"] + (["rune"] if pos == "POSITION_2" else []) + (["stack"] if pos == "POSITION_1" else [])
                R["win"][f"{pos}|{a}-{b}"] = window_model(ms, pos, a, b, fl)
            R["comeback"][pos] = comeback_model(ms, pos)
        if v == "742":
            R["examples"] = examples(ms)
        result["vers"][v] = R
    (D / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    txt = report(result)
    (D / "report.txt").write_text(txt, encoding="utf-8")
    print(txt)


def report(result: dict) -> str:
    L = []
    for v in VERS:
        R = result["vers"][v]
        d = R["desc"]
        L.append(f"===== {v}  {d['n']} 局 =====")
        L.append(f"时长 均 {d['dur']['mean']:.1f} 中位 {d['dur']['median']:.1f}  <30 {d['dur']['lt30']:.0%} 30-40 {d['dur']['30_40']:.0%} ≥40 {d['dur']['ge40']:.0%} ≥50 {d['dur']['ge50']:.0%}")
        L.append(f"每分钟击杀 {d['kpm']:.2f}  前 10 分钟击杀 {d['kills10']:.1f}  各段死亡 {d['deaths_by_window']}  第一座塔 {d['first_tower']:.1f} 分  先推塔方胜率 {d['first_tower_win']:.1%}")
        L.append("领先方胜率：" + "  ".join(f"{k} {x['p']:.1%}({x['n']})" for k, x in d["comeback"].items() if x["p"] is not None))
        for pos, r in d["roles"].items():
            L.append(f"{pos}: nw10/15/20/25/30 {r['nw10']:.0f}/{r['nw15']:.0f}/{r['nw20']:.0f}/{r['nw25']:.0f}/{r['nw30']:.0f}  占全队 {r['share10']:.1%}/{r['share20']:.1%}/{r['share30']:.1%}  "
                     f"第一件大件 {r['big1_min']:.1f} 第二件 {r['big2_min']:.1f}  BKB {r['bkb_share']:.0%} @{r['bkb_min'] or 0:.1f}")
            for a, b in WINDOWS:
                w = r[f"w{a}-{b}"]
                L.append(f"    {a}-{b}: " + " ".join(f"{k} {x:.2f}" for k, x in w.items()))
            L.append("    钱来源 " + " ".join(f"{k[:6]} {x:.0%}" for k, x in r["farm_share"].items())
                     + "  对线 " + " ".join(f"{k} {x['n']}/{x['win']:.0%}" for k, x in sorted(r["lane"].items())))
            L.append("    第一件大件 " + " ".join(f"{x['name']} {x['share']:.0%}" for x in r["first_big"]))
            L.append("    英雄类型 " + " ".join(f"{k} {x['n']}/{x['win']:.1%}" for k, x in r["types"].items())
                     + f"  买魔晶 {r['shard_share']:.0%}")
        for minute, g in R["gap"].items():
            L.append(f"第 {minute} 分钟各位置经济差（每千）→胜率：" + "  ".join(
                f"{j + 1}号 {c['b']:+.1f}±{1.96 * c['se']:.1f}(sd {c['sd']:.1f}k)" for j, c in enumerate(g["coef"])))
        L.append("第 20 分钟同样全队经济，钱多分给（每 5 个百分点）→胜率：" + "  ".join(
            f"{j + 1}号 {c['b']:+.2f}±{1.96 * c['se']:.2f}" for j, c in enumerate(R["share"]["coef"])))
    L.append("\n===== 时间段模型（系数 = 胜率百分点 / 每单位；括号里 7.41f；* = 两版本差超出误差） =====")
    for key, r42 in result["vers"]["742"]["win"].items():
        r41 = result["vers"]["741"]["win"][key]
        if "coef" not in r42 or "coef" not in r41:
            continue
        L.append(f"-- {key}  n {r42['n']} / {r41['n']}")
        L += coef_lines(r42["coef"], r41["coef"])
    L.append("\n===== 15 分钟落后 ≥2000 的一方，15~25 分钟这个位置做了什么 → 翻盘（括号 7.41f） =====")
    for pos in POS[:3]:
        a, b = result["vers"]["742"]["comeback"][pos], result["vers"]["741"]["comeback"][pos]
        if "coef" not in a or "coef" not in b:
            continue
        L.append(f"-- {pos}  n {a['n']} / {b['n']}  翻盘率 {a['base']:.1%} / {b['base']:.1%}")
        L += coef_lines(a["coef"], b["coef"])
    return "\n".join(L)


def coef_lines(c42: dict, c41: dict) -> list[str]:
    out = []
    for f, c in c42.items():
        o = c41[f]
        dd, dse = c["b"] - o["b"], math.hypot(c["se"], o["se"])
        flag = " *" if abs(dd) > 1.96 * dse else ""
        out.append(f"   {FEAT_NAME[f]:<16} {c['b']:+6.2f}±{1.96 * c['se']:.2f}  ({o['b']:+6.2f}±{1.96 * o['se']:.2f})  "
                   f"差 {dd:+.2f}±{1.96 * dse:.2f}{flag}   均值 {c['mean']:.2f}({o['mean']:.2f})  1sd≈{c['sd_unit']:.2f}")
    return out


if __name__ == "__main__":
    main()
