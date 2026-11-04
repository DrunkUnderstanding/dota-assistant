import json, statistics as st, math
from collections import Counter, defaultdict
exec(open("n_an.py", encoding="utf-8").read().split("# 1)")[0])
def med(x): return st.median(x) if x else None
N = len(P)
bins = [(0,5),(5,10),(10,15),(15,20),(20,25),(25,30),(30,35),(35,40),(40,50)]
def b(t): return next((f"{lo}-{hi}" for lo,hi in bins if lo*60 <= t < hi*60), None)
# team kills: each enemy death killed by our team; pair involvement
print("=== 我方击杀（每局平均）、拉席克/艾欧参与率、击杀发生地 ===")
K = defaultdict(list)   # bin -> list of (involved, region, smoke)
for mid, m in P.items():
    le, io = hero(m, 52), hero(m, 91); side = le["isRadiant"]
    for p in m["players"]:
        if p["isRadiant"] != side: continue
        for e in p["stats"]["killEvents"] or []:
            inv = p["heroId"] in (52, 91) or any(abs(a["time"] - e["time"]) <= 1 and a["target"] == e["target"] for q in (le, io) for a in q["stats"]["assistEvents"] or [])
            r = region((e["positionX"], e["positionY"]), side) if e["positionX"] else None
            K[b(e["time"])].append((inv, r, e["isSmoke"], p["heroId"] == 52))
for lo, hi in bins:
    ks = K[f"{lo}-{hi}"]
    if not ks: continue
    rc = Counter(r for i, r, s, l in ks)
    print(f"  {lo:2}-{hi:2}分  我方击杀 {len(ks)/N:.1f}/局  拉/艾参与 {sum(i for i,*_ in ks)/len(ks):.0%}  拉席克亲手 {sum(l for *_,l in ks)/len(ks):.0%}  开雾 {sum(s for _,_,s,_ in ks)/len(ks):.0%}  地点: " + " ".join(f"{r}{c/len(ks):.0%}" for r, c in rc.most_common(4)))
print("\n=== 死亡地点 ===")
for who, h in (("拉席克", 52), ("艾欧", 91)):
    for lo, hi in ((0,10),(10,20),(20,30),(30,45)):
        ds = [(d, hero(m, h)["isRadiant"]) for m in P.values() for d in hero(m, h)["stats"]["deathEvents"] or [] if lo*60 <= d["time"] < hi*60]
        rc = Counter(region((d["positionX"], d["positionY"]), s) for d, s in ds)
        print(f"  {who} {lo}-{hi}分 死 {len(ds)/N:.2f}/局: " + " ".join(f"{r}{c/len(ds):.0%}" for r, c in rc.most_common(4)))
# relocate
print("\n=== 降临 ===")
res = []; origin = Counter(); dest = Counter(); kills_after = []
for mid, m in P.items():
    f = F[mid]; io_f = hero(f, 91); le, io = hero(m, 52), hero(m, 91); side = le["isRadiant"]
    casts = [e["time"] for e in (io_f["playbackData"] or {}).get("abilityUsedEvents") or [] if e["abilityId"] == 5488]
    teamk = [(e["time"], e) for p in m["players"] if p["isRadiant"] == side for e in p["stats"]["killEvents"] or []]
    ourd = [(d["time"]) for p in m["players"] if p["isRadiant"] == side for d in p["stats"]["deathEvents"] or []]
    for t in casts:
        o = region(at(io, t - 5), side)
        ks = [e for tt, e in teamk if t <= tt <= t + 25]
        ds = [tt for tt in ourd if t <= tt <= t + 25]
        res.append((t, len(ks), len(ds), o))
        if o: origin[o] += 1
        for e in ks:
            if e["positionX"]: dest[region((e["positionX"], e["positionY"]), side)] += 1
for lo, hi in bins:
    rs = [r for r in res if lo*60 <= r[0] < hi*60]
    if rs: print(f"  {lo:2}-{hi:2}分 降临 {len(rs)/N:.2f}/局  25秒内我方有击杀 {sum(r[1]>0 for r in rs)/len(rs):.0%}  平均击杀 {sum(r[1] for r in rs)/len(rs):.2f}  我方阵亡 {sum(r[2] for r in rs)/len(rs):.2f}")
print("  从哪降临:", " ".join(f"{r}{c/sum(origin.values()):.0%}" for r, c in origin.most_common(6)))
print("  降临后击杀发生在:", " ".join(f"{r}{c/sum(dest.values()):.0%}" for r, c in dest.most_common(6)))
# towers
print("\n=== 推塔顺序（对面的塔 / 自己的塔）===")
def tower_name(npc, our_rad):
    n = NPC.get(npc, "")
    if "tower" not in n and "rax" not in n and "fort" not in n: return None
    good = "goodguys" in n
    mine = good == our_rad
    lane = "top" if "top" in n else "mid" if "mid" in n else "bot" if "bot" in n else ""
    # 我方视角：天辉的 bot / 夜魇的 top 是天辉优势路
    rel = {"mid": "中"}.get(lane) or (("优" if (lane == "bot") == our_rad else "劣") if mine else ("优" if (lane == "bot") == our_rad else "劣"))
    # 注：对面的塔也按「我们的哪条路」命名（我们优势路 = 对面劣势路）
    kind = "一塔" if "tower1" in n else "二塔" if "tower2" in n else "高地塔" if "tower3" in n else "基地塔" if "tower4" in n else "兵营" if "rax" in n else "基地"
    return ("敌" if not mine else "我") + (rel + "路" if lane else "") + kind
NPC = {x["id"]: x["name"] for x in json.load(open("npcs.json", encoding="utf-8"))["constants"]["npcs"]}
first = defaultdict(list); order = Counter()
for mid, m in P.items():
    le = hero(F[mid], 52); side = le["isRadiant"]; seen = []
    for d in sorted(F[mid]["towerDeaths"] or [], key=lambda d: d["time"]):
        nm_ = tower_name(d["npcId"], side)
        if not nm_ or nm_ in seen: continue
        seen.append(nm_); first[(nm_, le["isVictory"])].append(d["time"] / 60)
    e = [x for x in seen if x.startswith("敌") and "塔" in x][:3]
    order[" → ".join(e)] += 1
names = sorted({k[0] for k in first}, key=lambda n: med(first[(n, True)] + first[(n, False)]))
for n in names:
    w, l = first[(n, True)], first[(n, False)]
    if len(w) + len(l) >= N * 0.3:
        print(f"  {n:<10} 倒的局 {(len(w)+len(l))/N:.0%}  中位 {med(w+l):.1f}分  (赢局 {med(w) or 0:.1f} n={len(w)} / 输局 {med(l) or 0:.1f} n={len(l)})")
print("  对面前三座塔最常见顺序:", order.most_common(5))
# last hits
print("\n=== 拉席克补刀（累计）===")
for t in (5, 10, 15, 20, 25):
    xs = [sum(hero(m, 52)["stats"]["lastHitsPerMinute"][:t]) for m in P.values() if len(hero(m, 52)["stats"]["lastHitsPerMinute"] or []) >= t]
    print(f"  {t}分 补刀中位 {med(xs)}")
