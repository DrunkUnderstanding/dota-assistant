import json, statistics as st, math, sys
from collections import defaultdict, Counter
sys.path.insert(0, r"D:\xqs\dota-assistant")
from dota_assistant.domain import camps
exec(open("n_an.py", encoding="utf-8").read().split("# 1)")[0])   # P, F, region, hero, at, idx
TD = {m["id"]: m for m in json.load(open("towerdmg.json", encoding="utf-8"))}
L = {m["id"]: m for m in json.load(open("lane.json", encoding="utf-8"))}
NPC = {x["id"]: x["name"] for x in json.load(open("npcs.json", encoding="utf-8"))["constants"]["npcs"]}
B = 9472
def uv(c, rad=None):     # STRATZ 格子坐标 → 小地图 0~1（真实朝向：天辉左下，不镜像；页面按需要再翻）
    x, y = c
    wx, wy = (x - 128) * 128, (y - 128) * 128
    return [round((wx + B) / (2 * B), 4), round((wy + B) / (2 * B), 4)]
def bkey(npc, pair_rad):
    n = NPC.get(npc, "")
    if not any(k in n for k in ("tower", "rax", "fort")) or "watch" in n: return None
    good = "goodguys" in n
    own = good == pair_rad
    rest = n.split("guys_", 1)[1]
    if not pair_rad: rest = rest.replace("_top", "_TMP").replace("_bot", "_top").replace("_TMP", "_bot")
    return ("own_" if own else "enemy_") + rest
PH = [(0, 6), (6, 11), (11, 15), (15, 20), (20, 25), (25, 30), (30, 35), (35, 45)]
G = 40
SIDES = ("rad", "dire")
heat = {s: {sd: {h: [[0] * (G * G) for _ in PH] for h in ("le", "io")} for sd in SIDES} for s in ("all", "win", "loss")}
regions = {s: {sd: [Counter() for _ in PH] for sd in SIDES} for s in ("all", "win", "loss")}
together = {sd: [[0, 0] for _ in PH] for sd in SIDES}
kills, deaths, matches, bpos = [], [], [], defaultdict(list)
for mid, m in P.items():
    f = F[mid]; le, io = hero(m, 52), hero(m, 91); rad = le["isRadiant"]; win = hero(f, 52)["isVictory"]
    subs = ("all", "win" if win else "loss")
    sd = "rad" if rad else "dire"
    for k, (lo, hi) in enumerate(PH):
        for t in range(lo * 60, hi * 60, 15):
            for name, p in (("le", le), ("io", io)):
                q = at(p, t)
                if not q: continue
                u, v = uv(q, rad); gx, gy = min(G - 1, max(0, int(u * G))), min(G - 1, max(0, int(v * G)))
                for s in subs: heat[s][sd][name][k][gy * G + gx] += 1
                if name == "le":
                    r = region(q, rad)
                    for s in subs: regions[s][sd][k][r] += 1
            a, b = at(le, t), at(io, t)
            if a and b: together[sd][k][0] += math.dist(a, b) <= 7; together[sd][k][1] += 1
    io_f = hero(f, 91)
    rel = [e["time"] for e in (io_f["playbackData"] or {}).get("abilityUsedEvents") or [] if e["abilityId"] == 5488]
    for p in m["players"]:
        if p["isRadiant"] != rad: continue
        for e in p["stats"]["killEvents"] or []:
            if not e["positionX"]: continue
            inv = p["heroId"] in (52, 91) or any(abs(a["time"] - e["time"]) <= 1 and a["target"] == e["target"] for q in (le, io) for a in q["stats"]["assistEvents"] or [])
            ar = any(r <= e["time"] <= r + 25 for r in rel)
            kills.append([e["time"], *uv((e["positionX"], e["positionY"]), rad), int(inv), int(bool(e["isSmoke"])), int(win), int(ar), int(rad)])
    for h, p in ((0, le), (1, io)):
        for d in p["stats"]["deathEvents"] or []:
            if d["positionX"]: deaths.append([d["time"], *uv((d["positionX"], d["positionY"]), rad), h, int(win), int(rad)])
    # 建筑：倒塌时间 + 拉席克 / 艾欧伤害占比
    falls = {}
    for d in sorted(f["towerDeaths"] or [], key=lambda d: d["time"]):
        k = bkey(d["npcId"], rad)
        if k and k not in falls: falls[k] = d["time"]
    share = {}
    td = TD[mid]
    for npc in {r["npcId"] for p in td["players"] for r in p["stats"]["towerDamageReport"] or []}:
        k = bkey(npc, rad)
        if not k or not k.startswith("enemy"): continue
        by = {p["heroId"]: next((r["damage"] for r in p["stats"]["towerDamageReport"] or [] if r["npcId"] == npc), 0) for p in td["players"] if p["isRadiant"] == rad}
        tot = sum(by.values())
        if tot > 0: share[k] = [by.get(52, 0), by.get(91, 0), tot, int(max(by, key=by.get) == 52)]
        # 位置估计：建筑倒塌时打过它的人的位置
    for npc in {r["npcId"] for p in td["players"] for r in p["stats"]["towerDamageReport"] or []}:
        n = NPC.get(npc, ""); k = bkey(npc, rad) and ("good_" if "goodguys" in n else "bad_") + n.split("guys_", 1)[1]
        t = next((d["time"] for d in f["towerDeaths"] or [] if d["npcId"] == npc), None)
        if not k or t is None: continue
        attackers_rad = not ("goodguys" in n)
        for p in td["players"]:
            if p["isRadiant"] != attackers_rad: continue
            if not any(r["npcId"] == npc and r["damage"] > 0 for r in p["stats"]["towerDamageReport"] or []): continue
            pp = next(x for x in m["players"] if x["heroId"] == p["heroId"])
            q = at(pp, t)
            if q: bpos[k].append(uv(q, rad))
    lane_key = {True: "bottomLaneOutcome", False: "topLaneOutcome"}[rad]
    lo_ = L[mid].get(lane_key)
    lane = None if not lo_ else 0 if lo_ == "TIE" else (1 if lo_.startswith("RADIANT") == rad else -1)
    matches.append({"r": [round(r / 60, 2) for r in rel], "rad": int(rad), "w": int(win), "d": round(f["durationSeconds"] / 60, 1), "f": falls, "s": share, "lane": lane})
bld = {}
for k, pts in bpos.items():
    if len(pts) < 15: continue
    bld[k] = [round(st.median(p[0] for p in pts), 4), round(st.median(p[1] for p in pts), 4), len(pts)]
camp = [[c[0].split("_")[0] == "good", c[1], round((c[2] + B) / (2 * B), 4), round((c[3] + B) / (2 * B), 4)] for c in camps.CAMPS]
reg = {s: {sd: [dict(cnt) for cnt in regions[s][sd]] for sd in SIDES} for s in regions}
out = {"n": len(P), "phases": PH, "grid": G, "heat": heat, "regions": reg, "together": together,
       "kills": kills, "deaths": deaths, "matches": matches, "bld": bld, "camps": camp}
json.dump(out, open("viz_data.json", "w", encoding="utf-8"), separators=(",", ":"), ensure_ascii=False)
import os; print(os.path.getsize("viz_data.json"), len(bld), sorted(bld)[:50])
for k in sorted(bld): print(k, bld[k])
