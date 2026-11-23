"""第 6 步：「核心多参团了怎么少死」——把每局的英雄死亡按时间 + 地点聚成「一波团」，看 1 / 2 / 3 号位：
  ① 死在哪种情况（团战里 / 被单抓），死的时候 STRATZ 打了什么标签；
  ② 参了团的那一波里，什么情况下更容易死（第一个死、人数、打架地点、身上有没有黑皇杖等、经济）；
  ③ 哪种死法对输赢伤害大（团战里死 vs 被单抓）。
结果写 data/research/role-play/fight.json、fight.txt。

    python -X utf8 docs/research/role-play/scripts/f_fight.py
"""
import gzip
import json
import math
from collections import Counter, defaultdict

import c_an
import numpy as np
from c_an import FDIST, FOUNTAIN, ITEMS, POS, hero_block, lead_dummies, nw_at, ols, rad_win, team_lead
from common import D

GAP, RADIUS = 20, 25            # 同一波：和上一个死亡隔 ≤20 秒、离这一波中心 ≤25 格（STRATZ 1 格约 125 码）
T0, T1 = 10 * 60, 40 * 60       # 只看 10～40 分钟（对线期以后）
FLAGS = ["走过对面眼", "想 TP 走", "刚复活又死", "被秒（爆发）", "死时在交战", "有治疗没用", "被追踪"]
SAVE_ITEMS = ["black_king_bar", "manta", "satanic", "sphere", "aeon_disk", "butterfly", "blink", "hurricane_pike",
              "force_staff", "ghost", "eternal_shroud", "heavens_halberd", "abyssal_blade", "silver_edge", "invis_sword",
              "skadi", "heart", "assault", "bfury", "radiance", "basher", "desolator", "monkey_king_bar", "sange_and_yasha",
              "yasha_and_kaya", "harpoon", "disperser", "mjollnir", "butterfly", "lotus_orb", "pipe", "crimson_guard",
              "blade_mail", "shivas_guard", "refresher", "ultimate_scepter", "aghanims_shard"]


def load_events(v: str) -> dict[int, dict]:
    out = {}
    try:
        with gzip.open(D / f"e{v}.jsonl.gz", "rt", encoding="utf-8") as f:
            for line in f:
                m = json.loads(line)
                if not m.get("missing"):
                    out[m["id"]] = m
    except (EOFError, OSError):
        pass
    return out


def owned(p: dict, t: float) -> set[str]:
    return {ITEMS.key_of.get(iid) for tt, iid in p["items"] if tt <= t - 5} - {None}


def fights_of(m: dict, e: dict) -> list[dict]:
    """一局的所有英雄死亡 → 一波一波的团。每个死亡带死者、坐标、标签、击杀 / 助攻的人。"""
    side = {p["h"]: p["rad"] for p in e["players"]}
    credit = defaultdict(set)                     # (死者, 秒) → 拿到击杀 / 助攻的英雄
    for p in e["players"]:
        for t, tgt, *_ in p["kills"] + p["assists"]:
            credit[(tgt, t)].add(p["h"])
    deaths = []
    for p in e["players"]:
        for t, att, x, y, fl, *_ in p["deaths"]:
            if x is None:
                continue
            who = credit.get((p["h"], t)) or credit.get((p["h"], t - 1)) or credit.get((p["h"], t + 1)) or set()
            deaths.append({"t": t, "h": p["h"], "rad": p["rad"], "x": x, "y": y, "fl": fl, "by": who})
    deaths.sort(key=lambda d: d["t"])
    fights: list[dict] = []
    for d in deaths:
        f = fights[-1] if fights else None
        if f and d["t"] - f["last"] <= GAP and math.dist((d["x"], d["y"]), f["c"]) <= RADIUS:
            f["d"].append(d)
            f["last"] = d["t"]
            n = len(f["d"])
            f["c"] = ((f["c"][0] * (n - 1) + d["x"]) / n, (f["c"][1] * (n - 1) + d["y"]) / n)
        else:
            fights.append({"d": [d], "last": d["t"], "c": (d["x"], d["y"])})
    for f in fights:
        f["t"] = f["d"][0]["t"]
        inv = set()
        for d in f["d"]:
            inv.add(d["h"])
            inv |= d["by"]
        f["inv"] = inv
        f["dead"] = Counter(d["rad"] for d in f["d"])         # True = 天辉死了几个
        f["side"] = side
    return fights


def analyse(v: str, pos: str, ms: list[dict], ev: dict) -> dict:
    rows_fight, rows_death = [], []
    win_rows, win_y, win_h = [], [], []
    for m in ms:
        e = ev.get(m["id"])
        if not e:
            continue
        fs = [f for f in fights_of(m, e) if T0 <= f["t"] < T1 and f["t"] < m["dur"]]
        per_team = {}
        for rad in (True, False):
            p = m["by"][(rad, pos)]
            q = m["by"][(not rad, pos)]
            cnt = Counter()
            for f in fs:
                mine = [d for d in f["d"] if d["h"] == p["h"]]
                inv = p["h"] in f["inv"]
                allies = sum(1 for h in f["inv"] if f["side"].get(h) == rad)
                enemies = sum(1 for h in f["inv"] if f["side"].get(h) != rad)
                own_dead, en_dead = f["dead"][rad], f["dead"][not rad]
                size = len(f["d"])
                minute = int(f["t"] // 60)
                if mine:
                    d = mine[0]
                    team_d = [x for x in f["d"] if x["rad"] == rad]
                    kind = "单抓" if size == 1 or (own_dead == len(mine) and en_dead == 0 and allies <= 1) else "团战"
                    push = math.dist((d["x"], d["y"]), FOUNTAIN[rad]) / FDIST
                    rows_death.append({"kind": kind, "fl": d["fl"], "push": push, "minute": minute,
                                       "first_team": team_d[0]["h"] == p["h"], "first_all": f["d"][0]["h"] == p["h"],
                                       "won_fight": en_dead > own_dead, "allies": allies, "enemies": enemies})
                    cnt["death_" + kind] += 1
                if inv:
                    lead = team_lead(m, minute) * (1 if rad else -1)
                    have = owned(p, f["t"])
                    rows_fight.append({
                        "died": int(bool(mine)), "won_fight": int(en_dead > own_dead), "first_all": int(f["d"][0]["h"] == p["h"]),
                        "allies": allies, "enemies": enemies, "push": math.dist(f["c"], FOUNTAIN[rad]) / FDIST,
                        "minute": minute, "lead": lead, "nw": nw_at(p, minute), "nw_vs": nw_at(p, minute) - nw_at(q, minute),
                        "items": [k for k in SAVE_ITEMS if k in have], "hero": p["h"], "size": size,
                        "kp": sum(1 for d in f["d"] if d["rad"] != rad and p["h"] in d["by"])})
                    if not mine:
                        cnt["fight_alive"] += 1
                    cnt["kp_fight"] += sum(1 for d in f["d"] if d["rad"] != rad and p["h"] in d["by"])
            per_team[rad] = cnt
        if m["dur"] >= 30 * 60:
            # 10～30 分钟：参团没死几波 / 团战里死几次 / 被单抓几次 / 拿到几个击杀助攻 → 赢没赢（天辉减夜魇）
            def c30(rad, key):
                return per_team[rad][key]
            keys = ["kp_fight", "death_团战", "death_单抓"]
            win_rows.append([1.0] + [c30(True, k) - c30(False, k) for k in keys] + lead_dummies(team_lead(m, 10)))
            win_y.append(rad_win(m))
            win_h.append((m["by"][(True, pos)]["h"], m["by"][(False, pos)]["h"]))
    res: dict = {"n_fight": len(rows_fight), "n_death": len(rows_death)}
    # ① 死法
    kinds = Counter(r["kind"] for r in rows_death)
    res["kinds"] = {k: n / len(rows_death) for k, n in kinds.items()}
    flag = {}
    for kind in ("团战", "单抓", None):
        sub = [r for r in rows_death if kind is None or r["kind"] == kind]
        flag[kind or "全部"] = {FLAGS[i]: float(np.mean([r["fl"][i] == "1" for r in sub])) for i in range(len(FLAGS))}
        flag[kind or "全部"]["对面半场"] = float(np.mean([r["push"] > 0.5 for r in sub]))
    res["flags"] = flag
    fd = [r for r in rows_death if r["kind"] == "团战"]
    res["fight_death"] = {"first_all": float(np.mean([r["first_all"] for r in fd])),
                          "first_team": float(np.mean([r["first_team"] for r in fd])),
                          "won_if_first_all": float(np.mean([r["won_fight"] for r in fd if r["first_all"]])),
                          "won_if_not_first": float(np.mean([r["won_fight"] for r in fd if not r["first_all"]]))}
    # ② 参了团的那一波，死没死
    R = rows_fight
    res["died_rate"] = float(np.mean([r["died"] for r in R]))
    buckets = {}
    for name, fn in [("人数", lambda r: "我方多" if r["allies"] > r["enemies"] else ("一样" if r["allies"] == r["enemies"] else "我方少")),
                     ("地点", lambda r: "自家半场" if r["push"] < 0.45 else ("河道附近" if r["push"] < 0.55 else "对面半场")),
                     ("黑皇杖", lambda r: "有" if "black_king_bar" in r["items"] else "没有"),
                     ("经济比对面同位置", lambda r: "多 2000+" if r["nw_vs"] >= 2000 else ("少 2000+" if r["nw_vs"] <= -2000 else "差不多"))]:
        g = defaultdict(list)
        for r in R:
            g[fn(r)].append(r)
        buckets[name] = {k: {"n": len(x), "died": float(np.mean([r["died"] for r in x])),
                             "won_fight": float(np.mean([r["won_fight"] for r in x])),
                             "kp": float(np.mean([r["kp"] for r in x]))} for k, x in g.items()}
    res["buckets"] = buckets
    # 回归：死没死 ~ 人数差 + 地点 + 经济 + 全队领先 + 分钟 + 身上装备 + 英雄
    item_keys = [k for k in SAVE_ITEMS if np.mean([k in r["items"] for r in R]) >= 0.05]
    item_keys = list(dict.fromkeys(item_keys))
    X, y, hh = [], [], []
    for r in R:
        X.append([1.0, r["allies"] - r["enemies"], (r["push"] - 0.5) / 0.1, r["nw_vs"] / 1000, r["nw"] / 1000]
                 + lead_dummies(r["lead"]) + [1.0 if r["minute"] // 5 == b else 0.0 for b in range(3, 8)]
                 + [1.0 if k in r["items"] else 0.0 for k in item_keys])
        y.append(r["died"])
        hh.append(r["hero"])
    X = np.array(X)
    idx = {h: i for i, h in enumerate(sorted(set(hh)))}
    H = np.zeros((len(hh), len(idx)))
    for i, h in enumerate(hh):
        H[i, idx[h]] = 1
    beta, se = ols(np.hstack([X, H[:, 1:]]), np.array(y, dtype=float))
    names = ["人数差（我方 − 对面，每 1 人）", "打架地点往对面压 10%", "比对面同位置多 1000 经济", "自己经济每多 1000"]
    res["died_model"] = {nm: {"b": float(beta[1 + i]) * 100, "se": float(se[1 + i]) * 100} for i, nm in enumerate(names)}
    off = 1 + len(names) + len(lead_dummies(0)) + 5
    res["died_items"] = {ITEMS.info(k)["name"]: {"b": float(beta[off + i]) * 100, "se": float(se[off + i]) * 100,
                                                 "share": float(np.mean([k in r["items"] for r in R]))}
                         for i, k in enumerate(item_keys)}
    # ③ 哪种死法伤害大
    if win_rows:
        Xw = np.hstack([np.array(win_rows), hero_block(win_h)])
        b, s = ols(Xw, np.array(win_y, dtype=float))
        res["win"] = {nm: {"b": float(b[1 + i]) * 100, "se": float(s[1 + i]) * 100}
                      for i, nm in enumerate(["团战里拿到 1 个击杀 / 助攻", "团战里死 1 次", "被单抓死 1 次"])}
        res["win_n"] = len(win_rows)
    return res


def main() -> None:
    out = {}
    for v in ("742", "741"):
        ms = c_an.load(v)
        ev = load_events(v)
        print(v, "对局", len(ms), "有事件", sum(1 for m in ms if m["id"] in ev), flush=True)
        out[v] = {pos: analyse(v, pos, ms, ev) for pos in POS[:3]}
    (D / "fight.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    L = []
    for pos in POS[:3]:
        a, b = out["742"][pos], out["741"][pos]
        L.append(f"===== {pos}  参团 {a['n_fight']} 波 / 死 {a['n_death']} 次（7.41f {b['n_fight']} / {b['n_death']}） =====")
        L.append(f"死法：{a['kinds']}  （7.41f {b['kinds']}）")
        for k in a["flags"]:
            L.append(f"  标签[{k}] " + "  ".join(f"{f} {a['flags'][k][f]:.0%}({b['flags'][k][f]:.0%})" for f in a["flags"][k]))
        L.append(f"团战里死：全场第一个死 {a['fight_death']['first_all']:.0%}({b['fight_death']['first_all']:.0%})  "
                 f"我方第一个死 {a['fight_death']['first_team']:.0%}  那波赢了：先死 {a['fight_death']['won_if_first_all']:.0%} / 后死 {a['fight_death']['won_if_not_first']:.0%}")
        L.append(f"参了团的那一波死的比例 {a['died_rate']:.1%}（{b['died_rate']:.1%}）")
        for name, g in a["buckets"].items():
            L.append(f"  {name}: " + "  ".join(f"{k} 死{x['died']:.0%} 赢波{x['won_fight']:.0%} 参{x['kp']:.2f} n{x['n']}" +
                                               (f"({b['buckets'][name][k]['died']:.0%})" if k in b["buckets"][name] else "")
                                               for k, x in sorted(g.items())))
        L.append("  死没死模型（百分点）：" + "  ".join(f"{k} {x['b']:+.1f}±{1.96 * x['se']:.1f}({b['died_model'][k]['b']:+.1f})" for k, x in a["died_model"].items()))
        L.append("  身上有这件（同经济）：" + "  ".join(f"{k} {x['b']:+.1f}±{1.96 * x['se']:.1f} [{x['share']:.0%}]" +
                                                (f"({b['died_items'][k]['b']:+.1f})" if k in b["died_items"] else "")
                                                for k, x in sorted(a["died_items"].items(), key=lambda kv: kv[1]["b"])))
        if "win" in a:
            L.append(f"  10～30 分钟 → 胜率（{a['win_n']} 局）：" + "  ".join(
                f"{k} {x['b']:+.2f}±{1.96 * x['se']:.2f}({b['win'][k]['b']:+.2f})" for k, x in a["win"].items()))
    txt = "\n".join(L)
    (D / "fight.txt").write_text(txt, encoding="utf-8")
    print(txt)


if __name__ == "__main__":
    main()
