"""第 7 步：「多参团又少死」的人做对了什么。
① 团战层面：核心参了的那一波，死没死 ~ 进场先后 + 开打时附近两边人数（站位采样，不受死没死影响）+ 打架地点 + 经济 + 身上装备 + 英雄；
② 人层面：10～30 分钟参团多的核心里，少死的和多死的比，各项习惯差在哪，最后胜率差多少。
结果写 data/research/role-play/how.json、how.txt。

    python -X utf8 docs/research/role-play/scripts/g_how.py
"""
import json
import math
from collections import defaultdict

import c_an
import numpy as np
from c_an import FDIST, FOUNTAIN, POS, lead_dummies, nw_at, ols, team_lead
from common import D
from f_fight import T0, T1, fights_of, load_events, owned

NEAR = 30                     # 开打时离这一波中心 ≤30 格（约 4000 码）算在附近
BKB_LIKE = {"black_king_bar", "manta", "satanic", "aeon_disk", "sphere", "butterfly", "abyssal_blade",
            "heavens_halberd", "eternal_shroud", "blade_mail", "ghost", "lotus_orb"}
MOBILITY = {"blink", "overwhelming_blink", "swift_blink", "arcane_blink", "force_staff", "hurricane_pike", "harpoon",
            "invis_sword", "silver_edge", "disperser", "travel_boots", "travel_boots_2", "phase_boots"}


def near_count(m: dict, t: float, c, rad: bool, me: int) -> tuple[int, int]:
    """开打那一刻（取开打前最近的站位采样点）附近有几个队友（不算自己）、几个对面。"""
    a = e = 0
    for p in m["players"]:
        L = len(p["loc"])
        i = max(0, min(L - 1, int(t * L / m["dur"])))
        xy = p["loc"][i]
        if xy[0] is None or math.dist(xy, c) > NEAR:
            continue
        if p["rad"] == rad:
            a += p["h"] != me
        else:
            e += 1
    return a, e


def entry_order(f: dict, h: int) -> str:
    """这一波里自己第一次出现（击杀 / 助攻 / 死）是第几个事件：先 = 这波第一个死的就是自己或第一个击杀有自己。"""
    ev = []
    for d in f["d"]:
        if d["h"] == h:
            ev.append(d["t"])
        if h in d["by"]:
            ev.append(d["t"])
    if not ev:
        return "?"
    first = min(ev)
    k = sum(1 for d in f["d"] if d["t"] < first)
    return "第一个" if k == 0 else ("前半" if k < len(f["d"]) / 2 else "后半")


def collect(v: str, pos: str) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    ms = c_an.load(v)
    ev = load_events(v)
    rows, people, tf, pick = [], [], [], []
    for m in ms:
        e = ev.get(m["id"])
        if not e or m["dur"] < 30 * 60:
            continue
        fs = [f for f in fights_of(m, e) if T0 <= f["t"] < T1 and f["t"] < m["dur"]]
        for rad in (True, False):
            p, q = m["by"][(rad, pos)], m["by"][(not rad, pos)]
            pr = defaultdict(float)
            for f in fs:
                if p["h"] not in f["inv"]:
                    continue
                died = any(d["h"] == p["h"] for d in f["d"])
                a, en = near_count(m, f["t"] - 5, f["c"], rad, p["h"])
                minute = int(f["t"] // 60)
                have = owned(p, f["t"])
                r = {"died": int(died), "order": entry_order(f, p["h"]), "near_a": a, "near_e": en,
                     "push": math.dist(f["c"], FOUNTAIN[rad]) / FDIST, "lead": team_lead(m, minute) * (1 if rad else -1),
                     "nw_vs": nw_at(p, minute) - nw_at(q, minute), "nw": nw_at(p, minute), "minute": minute,
                     "save": int(bool(have & BKB_LIKE)), "mob": int(bool(have & MOBILITY)), "hero": p["h"],
                     "kp": sum(1 for d in f["d"] if d["rad"] != rad and p["h"] in d["by"]),
                     "won": int(f["dead"][not rad] > f["dead"][rad]), "t30": f["t"] < 30 * 60}
                rows.append(r)
                if r["t30"]:
                    pr["fights"] += 1
                    pr["kp"] += r["kp"]
                    pr["died"] += r["died"]
                    pr["late"] += r["order"] == "后半"
                    pr["first"] += r["order"] == "第一个"
                    pr["outnum"] += a + 1 > en
                    pr["enemyhalf"] += r["push"] > 0.5
                    pr["save"] += r["save"]
            # 大团（这一波 ≥3 人死）：自己第一次出现之前发生了什么
            for f in fs:
                if len(f["d"]) < 3 or p["h"] not in f["inv"]:
                    continue
                mine_t = [d["t"] for d in f["d"] if d["h"] == p["h"] or p["h"] in d["by"]]
                first = min(mine_t)
                before = [d for d in f["d"] if d["t"] < first]
                died_t = [d["t"] for d in f["d"] if d["h"] == p["h"]]
                if not before:
                    cls = "自己第一个倒下" if died_t and died_t[0] == first else "自己先拿到击杀 / 助攻"
                elif before[0]["rad"] == rad:
                    cls = "队友先倒，自己后到"
                else:
                    cls = "对面先倒，自己跟上"
                tf.append({"cls": cls, "died": int(bool(died_t)), "won": int(f["dead"][not rad] > f["dead"][rad]),
                           "kp": sum(1 for d in f["d"] if d["rad"] != rad and p["h"] in d["by"])})
            # 被单抓（这一波只死了自己一个）：是不是被诡计之雾 / 多人抓
            for f in fs:
                if len(f["d"]) == 1 and f["d"][0]["h"] == p["h"]:
                    d = f["d"][0]
                    ke = [k for x in m["players"] if x["rad"] != rad for k in x["kills"] if k[1] == p["h"] and abs(k[0] - d["t"]) <= 1]
                    pick.append({"smoke": int(any(k[4] for k in ke)), "gank": int(any(k[3] for k in ke)),
                                 "ward": int(d["fl"][0] == "1"), "enemyhalf": int(math.dist((d["x"], d["y"]), FOUNTAIN[rad]) / FDIST > 0.5),
                                 "minute": int(d["t"] // 60)})
            # 被单抓 / 不在团里死的（10～30 分钟）
            solo = sum(1 for f in fs if f["t"] < 30 * 60 and len(f["d"]) == 1 and f["d"][0]["h"] == p["h"])
            people.append({"win": int(p["win"]), "pr": dict(pr), "solo": solo, "hero": p["h"],
                           "lh": sum((p["lhm"] or [])[10:30]), "lead10": team_lead(m, 10) * (1 if rad else -1)})
    return rows, people, tf, pick


def tf_desc(tf: list[dict], pick: list[dict]) -> dict:
    g = defaultdict(list)
    for r in tf:
        g[r["cls"]].append(r)
    out = {"tf": {k: {"n": len(x), "share": len(x) / len(tf), "died": float(np.mean([r["died"] for r in x])),
                      "won": float(np.mean([r["won"] for r in x])), "kp": float(np.mean([r["kp"] for r in x]))} for k, x in g.items()},
           "tf_alive_won": float(np.mean([r["won"] for r in tf if not r["died"]])),
           "tf_dead_won": float(np.mean([r["won"] for r in tf if r["died"]]))}
    out["pick"] = {"n": len(pick), **{k: float(np.mean([r[k] for r in pick])) for k in ("smoke", "gank", "ward", "enemyhalf")}}
    return out


def fight_model(rows: list[dict]) -> dict:
    orders = ["前半", "后半"]                         # 基准 = 第一个
    names = ["进场：前半（基准 = 这波第一个）", "进场：后半", "开打时附近队友每多 1 个", "开打时附近对面每多 1 个",
             "打架地点往对面压 10%", "比对面同位置多 1000 经济", "身上有保命装（黑皇杖 / 分身 / 撒旦 / 永恒之盘 / 林肯…）",
             "身上有位移 / 隐身（跳刀 / 推推 / 鱼叉 / 隐刀…）"]
    X, y, hh = [], [], []
    for r in rows:
        if r["order"] == "?":
            continue
        X.append([1.0] + [1.0 if r["order"] == o else 0.0 for o in orders]
                 + [r["near_a"], r["near_e"], (r["push"] - 0.5) / 0.1, r["nw_vs"] / 1000, r["save"], r["mob"], r["nw"] / 1000]
                 + lead_dummies(r["lead"]) + [1.0 if r["minute"] // 5 == b else 0.0 for b in range(3, 8)])
        y.append(r["died"])
        hh.append(r["hero"])
    idx = {h: i for i, h in enumerate(sorted(set(hh)))}
    H = np.zeros((len(hh), len(idx)))
    for i, h in enumerate(hh):
        H[i, idx[h]] = 1
    beta, se = ols(np.hstack([np.array(X), H[:, 1:]]), np.array(y, dtype=float))
    out = {nm: {"b": float(beta[1 + i]) * 100, "se": float(se[1 + i]) * 100} for i, nm in enumerate(names)}
    # 描述：按进场先后 / 附近人数
    desc = {}
    for key, fn in [("进场", lambda r: r["order"]),
                    ("开打时附近人数", lambda r: "我方多" if r["near_a"] + 1 > r["near_e"] else ("一样" if r["near_a"] + 1 == r["near_e"] else "我方少")),
                    ("附近队友", lambda r: str(min(r["near_a"], 3)) + ("+" if r["near_a"] >= 3 else ""))]:
        g = defaultdict(list)
        for r in rows:
            if r["order"] != "?":
                g[fn(r)].append(r)
        desc[key] = {k: {"n": len(x), "died": float(np.mean([r["died"] for r in x])), "won": float(np.mean([r["won"] for r in x])),
                         "kp": float(np.mean([r["kp"] for r in x]))} for k, x in g.items()}
    return {"n": len(y), "coef": out, "desc": desc}


def people_compare(people: list[dict]) -> dict:
    """10～30 分钟参团（拿到击杀 / 助攻）多的一半人里，按每波死亡率分成少死 / 多死两半，比习惯和胜率。"""
    ok = [p for p in people if p["pr"].get("fights", 0) >= 3]
    kp = np.median([p["pr"]["kp"] for p in ok])
    hi = [p for p in ok if p["pr"]["kp"] >= kp]
    rate = [p["pr"]["died"] / p["pr"]["fights"] for p in hi]
    cut = np.median(rate)
    groups = {"参团多·少死": [p for p, r in zip(hi, rate) if r < cut], "参团多·多死": [p for p, r in zip(hi, rate) if r >= cut],
              "参团少": [p for p in ok if p["pr"]["kp"] < kp]}
    out = {"kp_median": float(kp)}
    for g, xs in groups.items():
        def share(k, xs=xs):
            return float(np.mean([p["pr"].get(k, 0) / p["pr"]["fights"] for p in xs]))
        out[g] = {"n": len(xs), "win": float(np.mean([p["win"] for p in xs])),
                  "kp": float(np.mean([p["pr"]["kp"] for p in xs])), "fights": float(np.mean([p["pr"]["fights"] for p in xs])),
                  "died": float(np.mean([p["pr"]["died"] for p in xs])), "solo": float(np.mean([p["solo"] for p in xs])),
                  "late": share("late"), "first": share("first"), "outnum": share("outnum"), "enemyhalf": share("enemyhalf"),
                  "save": share("save"), "lh": float(np.mean([p["lh"] for p in xs])),
                  "lead10": float(np.mean([p["lead10"] for p in xs]))}
    return out


def main() -> None:
    out = {}
    for v in ("742", "741"):
        out[v] = {}
        for pos in POS[:3]:
            rows, people, tf, pick = collect(v, pos)
            out[v][pos] = {"fight": fight_model(rows), "people": people_compare(people), "tf": tf_desc(tf, pick)}
            print(v, pos, "参团", len(rows), "人", len(people), flush=True)
    (D / "how.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    L = []
    for pos in POS[:3]:
        a, b = out["742"][pos], out["741"][pos]
        L.append(f"===== {pos}  参团 {a['fight']['n']} 波（7.41f {b['fight']['n']}） =====")
        L.append("死没死模型（百分点，括号 7.41f）：")
        for k, x in a["fight"]["coef"].items():
            y = b["fight"]["coef"][k]
            L.append(f"   {k:<40} {x['b']:+5.1f}±{1.96 * x['se']:.1f}  ({y['b']:+5.1f})")
        for key, g in a["fight"]["desc"].items():
            L.append(f"  {key}: " + "  ".join(f"{k} 死{x['died']:.0%} 赢波{x['won']:.0%} 参{x['kp']:.2f} n{x['n']}"
                                              + (f"({b['fight']['desc'][key][k]['died']:.0%})" if k in b["fight"]["desc"][key] else "")
                                              for k, x in sorted(g.items())))
        ta, tb = a["tf"], b["tf"]
        L.append(f"  大团（≥3 人死）：自己活着那波赢 {ta['tf_alive_won']:.0%}({tb['tf_alive_won']:.0%})  自己死了那波赢 {ta['tf_dead_won']:.0%}({tb['tf_dead_won']:.0%})")
        for k, x in sorted(ta["tf"].items(), key=lambda kv: -kv[1]["share"]):
            y = tb["tf"].get(k, {})
            L.append(f"   {k}: 占 {x['share']:.0%}({y.get('share', 0):.0%})  自己死 {x['died']:.0%}({y.get('died', 0):.0%})  那波赢 {x['won']:.0%}({y.get('won', 0):.0%})  拿击杀助攻 {x['kp']:.2f}")
        pk, pq = ta["pick"], tb["pick"]
        L.append(f"  被单抓 {pk['n']} 次：带雾 {pk['smoke']:.0%}({pq['smoke']:.0%})  多人抓 {pk['gank']:.0%}({pq['gank']:.0%})  "
                 f"死前走过对面眼 {pk['ward']:.0%}({pq['ward']:.0%})  在对面半场 {pk['enemyhalf']:.0%}({pq['enemyhalf']:.0%})")
        pa, pb = a["people"], b["people"]
        L.append(f"  人（10～30 分钟，参团中位 {pa['kp_median']:.0f} 次）：")
        for g in ("参团多·少死", "参团多·多死", "参团少"):
            x, y = pa[g], pb[g]
            L.append(f"   {g}: n{x['n']} 胜率 {x['win']:.1%}({y['win']:.1%})  参团波数 {x['fights']:.1f} 击杀助攻 {x['kp']:.1f} 团里死 {x['died']:.1f} 被单抓 {x['solo']:.2f}  "
                     f"后半进场 {x['late']:.0%}({y['late']:.0%}) 第一个 {x['first']:.0%}({y['first']:.0%}) 开打我方人多 {x['outnum']:.0%}({y['outnum']:.0%}) "
                     f"对面半场 {x['enemyhalf']:.0%} 有保命装 {x['save']:.0%}({y['save']:.0%}) 10~30补刀 {x['lh']:.0f} 10分钟全队领先 {x['lead10']:+.0f}")
    txt = "\n".join(L)
    (D / "how.txt").write_text(txt, encoding="utf-8")
    print(txt)


if __name__ == "__main__":
    main()
