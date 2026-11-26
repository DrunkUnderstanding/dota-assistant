"""第 88 节（玩家问「7.42 是什么节奏」）：两版本的对局节奏对比。

数据：
- 7.42：i_rhythm_ids.py 从上线到现在（含 7.42a）随机抽的局，j_rhythm_fetch.py 抓到 r742.jsonl.gz（带开始时间）；
- 7.41f：第 85 节的 f741.jsonl.gz；
- 核对用：第 85 节 11-20 抽的 f742.jsonl.gz（只覆盖上线头两天），和新样本的同一时段比，数字应该对得上。
两边用同一套过滤（c_an.load 的条件，去掉「parsed / loc」两项——r742 没抓这两个字段，用每分钟身价代替是否解析）。
窗口内的击杀 / 死亡只统计活到窗口结束的局。

输出 data/research/role-play/rhythm.json，页面在 data/mockups/7.42-对局节奏.html。

    python -X utf8 docs/research/role-play/scripts/h_rhythm.py
"""
import gzip
import json
import math
import statistics

from common import CUT_742, D

POS = ["POSITION_1", "POSITION_2", "POSITION_3", "POSITION_4", "POSITION_5"]
WINDOW = 5
DAY = 86400
T_D2 = CUT_742 + 2 * DAY          # 上线满两天（11-20 21:00 UTC）
T_A = 1795507200                  # Valve 更新日志里 7.42a 的时间（11-24 08:00 UTC，只精确到日）


def nw_at(p: dict, minute: int) -> float:
    a = p["nw"]
    return a[min(minute, len(a) - 1)]


def ok(m: dict) -> bool:
    if m.get("missing") or m.get("dur", 0) < 900 or len(m["players"]) != 10:
        return False
    ps = m["players"]
    if not all(p["nw"] for p in ps):
        return False
    if any(sorted(p["pos"] or "" for p in ps if p["rad"] == rad) != POS for rad in (True, False)):
        return False
    if any(p["nw"][-1] < 0.5 * max(p["nw"]) or nw_at(p, 15) < 1500 for p in ps):
        return False
    return bool(m.get("lead"))


def load(name: str) -> list[dict]:
    out = []
    try:
        with gzip.open(D / name, "rt", encoding="utf-8") as f:
            for line in f:
                m = json.loads(line)
                if ok(m):                    # 只留节奏要用的字段（f741 / f742 带出装、站位等，全留内存不够）
                    out.append({"id": m["id"], "dur": m["dur"], "st": m.get("st"), "rw": m["rw"], "lead": m["lead"],
                                "towers": [[x[0]] for x in m["towers"]],
                                "players": [{k: p[k] for k in ("pos", "k", "lhm", "kills", "deaths", "assists")}
                                            for p in m["players"]]})
    except (EOFError, OSError):          # 还在抓，最后一段没写完
        pass
    return out


def per_game(ms: list[dict], key: str, a: int, b: int) -> float | None:
    """窗口内的事件数 / 活到窗口结束的局。key = kills（英雄击杀，和每分钟击杀同一口径）/ deaths。"""
    g = [m for m in ms if m["dur"] >= b * 60]
    if not g:
        return None
    lo = -1e9 if a == 0 else a * 60            # 第一个窗口带上开号角前的击杀（和 c_an 的「前 10 分钟击杀」一致）
    return sum(sum(1 for p in m["players"] for e in p[key] if lo <= e[0] < b * 60) for m in g) / len(g)


def tower_min(ms: list[dict], i: int) -> float | None:
    v = sorted(sorted(x[0] for x in m["towers"])[i] / 60 for m in ms if len(m["towers"]) > i)
    return v[len(v) // 2] if v else None


def rate(xs: list[int]) -> dict:
    n = len(xs)
    if not n:
        return {"n": 0, "p": None, "se": None}
    p = sum(xs) / n
    return {"n": n, "p": p, "se": math.sqrt(p * (1 - p) / n)}


def lead(ms: list[dict], minute: int) -> dict:
    g = [m for m in ms if m["dur"] >= minute * 60 + 60 and len(m["lead"]) > minute]
    vals = [abs(m["lead"][minute]) for m in g]
    win = {str(L): rate([int((m["lead"][minute] > 0) == bool(m["rw"])) for m in g if abs(m["lead"][minute]) >= L])
           for L in (2000, 5000, 10000)}
    return {"median": statistics.median(vals),
            "ge2000": sum(1 for x in vals if x >= 2000) / len(vals),
            "ge5000": sum(1 for x in vals if x >= 5000) / len(vals),
            "win": win}


def comeback(ms: list[dict], at: int = 15, gap: int = 2000) -> dict:
    g = [m for m in ms if m["dur"] >= at * 60 + 60 and len(m["lead"]) > at and abs(m["lead"][at]) >= gap]
    return rate([int((m["lead"][at] > 0) != bool(m["rw"])) for m in g])


def pos_rows(ms: list[dict]) -> dict:
    """各位置 0-10 / 10-20 / 20-30 的参团、死亡、正补（均值）。"""
    out = {}
    for p in range(1, 6):
        rows = []
        for a, b in ((0, 10), (10, 20), (20, 30)):
            acc = []
            for m in ms:
                if m["dur"] < b * 60:
                    continue
                for pl in m["players"]:
                    if pl["pos"] != f"POSITION_{p}":
                        continue
                    kp = sum(1 for e in pl["kills"] if a * 60 <= e[0] < b * 60) + \
                        sum(1 for e in pl["assists"] if a * 60 <= e[0] < b * 60)
                    dth = sum(1 for e in pl["deaths"] if a * 60 <= e[0] < b * 60)
                    lh = sum(pl["lhm"][a:b]) if pl.get("lhm") and len(pl["lhm"]) >= b else None
                    acc.append((kp, dth, lh))
            lhs = [x[2] for x in acc if x[2] is not None]
            rows.append({"win": f"{a}-{b}", "n": len(acc),
                         "kp": statistics.mean(x[0] for x in acc) if acc else None,
                         "dth": statistics.mean(x[1] for x in acc) if acc else None,
                         "lh": statistics.mean(lhs) if lhs else None})
        out[str(p)] = rows
    return out


def summary(ms: list[dict]) -> dict:
    n = len(ms)
    durs = [m["dur"] / 60 for m in ms]
    bins = [(0, 20), (20, 25), (25, 30), (30, 35), (35, 40), (40, 45), (45, 50), (50, 60), (60, 999)]
    secs = sum(m["dur"] for m in ms)
    return {
        "n": n,
        "dur_mean": statistics.mean(durs),
        "dur_median": statistics.median(durs),
        "dur": {f"{a}-{b if b < 999 else '+'}": sum(1 for d in durs if a <= d < b) / n for a, b in bins},
        "kills5": {f"{a}-{a + WINDOW}": per_game(ms, "kills", a, a + WINDOW) for a in range(0, 50, WINDOW)},
        "deaths5": {f"{a}-{a + WINDOW}": per_game(ms, "deaths", a, a + WINDOW) for a in range(0, 50, WINDOW)},
        "kills10": per_game(ms, "kills", 0, 10),
        "kpm": sum(sum(p["k"] or 0 for p in m["players"]) for m in ms) / secs * 60,
        "tower": {str(i + 1): tower_min(ms, i) for i in (0, 2, 5, 8)},
        "lead": {str(t): lead(ms, t) for t in (10, 15, 20, 25, 30)},
        "comeback15": comeback(ms),
        "pos": pos_rows(ms),
    }


def daily(ms: list[dict]) -> list[dict]:
    """按上线后第几天（每 24 小时一段）看关键数字有没有稳定下来。"""
    out = []
    for d in range(0, 8):
        g = [m for m in ms if CUT_742 + d * DAY <= m["st"] < CUT_742 + (d + 1) * DAY]
        if len(g) < 300:
            continue
        secs = sum(m["dur"] for m in g)
        l10 = lead(g, 10)
        out.append({"day": d + 1, "n": len(g), "dur_median": statistics.median(m["dur"] / 60 for m in g),
                    "kpm": sum(sum(p["k"] or 0 for p in m["players"]) for m in g) / secs * 60,
                    "win2k10": l10["win"]["2000"], "lead20": lead(g, 20)["median"], "comeback15": comeback(g)})
    return out


def main() -> None:
    new = [m for m in load("r742.jsonl.gz") if m["st"] >= CUT_742]
    ids = json.loads((D / "rhythm_ids.json").read_text(encoding="utf-8"))
    last_pre = max(i for i, s in ids if s < CUT_742)            # 上线前最后一局的编号，旧样本没有开始时间，按编号去
    old = [m for m in load("f742.jsonl.gz") if m["id"] > last_pre]
    groups = {
        "742": new,
        "741": load("f741.jsonl.gz"),
        "742_d12": [m for m in new if m["st"] < T_D2],
        "742_d36": [m for m in new if T_D2 <= m["st"] < T_A],
        "742_a": [m for m in new if m["st"] >= T_A],
        "742_old": old,
    }
    groups = {k: v for k, v in groups.items() if v}       # 还没抓到的时段先跳过
    res = {k: summary(v) for k, v in groups.items()}
    res["daily"] = daily(new)
    res["meta"] = {"cut": CUT_742, "last_pre_id": last_pre, "t_d2": T_D2, "t_a": T_A,
                   "st_min": min(m["st"] for m in new), "st_max": max(m["st"] for m in new)}
    out = D / "rhythm.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print("写出", out)
    for k in groups:
        r = res[k]
        l10 = r["lead"]["10"]["win"]
        print(f"{k:8s} {r['n']:6d} 局  中位 {r['dur_median']:.1f} 分  每分钟击杀 {r['kpm']:.2f}  "
              f"前10分击杀 {r['kills10']:.1f}  10分领先2000胜 {l10['2000']['p']:.1%}±{1.96 * l10['2000']['se']:.1%}  "
              f"领先5000胜 {l10['5000']['p']:.1%}(n{l10['5000']['n']})  "
              f"15分翻盘 {r['comeback15']['p']:.1%}±{1.96 * r['comeback15']['se']:.1%}  "
              f"20分中位领先 {r['lead']['20']['median']:.0f}  1塔 {r['tower']['1']:.1f}")
    for d in res["daily"]:
        print(f"  第{d['day']}天 n{d['n']}  中位 {d['dur_median']:.1f}  kpm {d['kpm']:.2f}  "
              f"10分领先2000胜 {d['win2k10']['p']:.1%}  20分领先 {d['lead20']:.0f}  翻盘 {d['comeback15']['p']:.1%}")


if __name__ == "__main__":
    main()
