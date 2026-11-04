import json, statistics as st
from collections import Counter, defaultdict
exec(open("n_an.py", encoding="utf-8").read().split("# 1)")[0])      # P, F, region, hero, at
L = {m["id"]: m for m in json.load(open("lane.json", encoding="utf-8"))}
TD = {m["id"]: m for m in json.load(open("towerdmg.json", encoding="utf-8"))}
NPC = {x["id"]: x["name"] for x in json.load(open("npcs.json", encoding="utf-8"))["constants"]["npcs"]}
kb = json.load(open(r"D:\xqs\dota-assistant\data\kb\kb.json", encoding="utf-8"))
ID = {i["name_loc"]: i["id"] for i in kb["items"]}
med = lambda x: round(st.median(x), 1) if x else None
# 1) OpenDota 1122 局（所有位置）
R = json.load(open("pair_matches.json", encoding="utf-8"))
for side in (True, False):
    xs = [r for r in R if r["io_rad"] == side]
    w = sum(1 for r in xs if r["radiant_win"] == side)
    print("OpenDota 全部位置", "天辉" if side else "夜魇", len(xs), f"{w/len(xs):.1%}")
# 2) 177 局主流打法
rows = defaultdict(list)
for mid, m in P.items():
    f = F[mid]; le = hero(f, 52); io = hero(f, 91); rad = le["isRadiant"]
    buy = lambda p, n: next((e["time"] / 60 for e in p["stats"]["itemPurchases"] if e["itemId"] == ID[n]), None)
    rel = [e["time"] / 60 for e in (io["playbackData"] or {}).get("abilityUsedEvents") or [] if e["abilityId"] == 5488]
    o = L[mid].get("bottomLaneOutcome" if rad else "topLaneOutcome")
    lane = None if not o else "均" if o == "TIE" else ("优" if o.startswith("RADIANT") == rad else "劣")
    safe = "bot" if rad else "top"
    falls = {}
    for d in sorted(f["towerDeaths"] or [], key=lambda d: d["time"]):
        n = NPC.get(d["npcId"], "")
        if "tower" in n and ("goodguys" in n) != rad: falls.setdefault(n.split("guys_")[1], d["time"] / 60)
        if "tower" in n and ("goodguys" in n) == rad: falls.setdefault("own_" + n.split("guys_")[1], d["time"] / 60)
    lp = hero(m, 52)
    lh10 = sum((lp["stats"]["lastHitsPerMinute"] or [])[:10])
    nw10 = le["stats"]["networthPerMinute"][10] if len(le["stats"]["networthPerMinute"]) > 10 else None
    kills10 = sum(1 for p in f["players"] if p["isRadiant"] == rad for e in p["stats"]["killEvents"] or [] if e["time"] < 600)
    deaths10 = sum(1 for p in f["players"] if p["isRadiant"] == rad for e in p["stats"]["deathEvents"] or [] if e["time"] < 600)
    rows[rad].append(dict(w=le["isVictory"], d=f["durationSeconds"] / 60, lane=lane, arc=buy(le, "奥术鞋"), kaya=buy(le, "慧光"), bkb=buy(le, "黑皇杖"),
                          rel=rel[0] if rel else None, nrel=len(rel), t1=falls.get(f"tower1_{safe}"), t1all=[falls.get(f"tower1_{x}") for x in ("top", "mid", "bot")],
                          ot1=falls.get(f"own_tower1_{safe}"), lh10=lh10, nw10=nw10, k10=kills10, d10=deaths10,
                          t2=min([v for k, v in falls.items() if k.startswith("tower2")], default=None),
                          own_t2=min([v for k, v in falls.items() if k.startswith("own_tower2")], default=None)))
for rad in (True, False):
    xs = rows[rad]; n = len(xs); W = [x for x in xs if x["w"]]
    lc = Counter(x["lane"] for x in xs if x["lane"]); ln = sum(lc.values())
    print(f"\n== {'天辉' if rad else '夜魇'}  n={n}  胜率 {sum(x['w'] for x in xs)/n:.1%}  时长中位 {med([x['d'] for x in xs])}")
    print("  对线 优/均/劣:", f"{lc['优']/ln:.0%} / {lc['均']/ln:.0%} / {lc['劣']/ln:.0%}", " 各档胜率:", {k: f"{sum(x['w'] for x in xs if x['lane']==k)/max(1,lc[k]):.0%}" for k in ("优", "均", "劣")})
    for k in ("arc", "kaya", "bkb", "rel", "nrel", "t1", "ot1", "t2", "own_t2", "lh10", "nw10", "k10", "d10"):
        print(f"  {k:7} 全部 {med([x[k] for x in xs if x[k] is not None])}   赢局 {med([x[k] for x in W if x[k] is not None])}")
    for lo, hi in ((0, 30), (30, 35), (35, 40), (40, 45), (45, 99)):
        g = [x for x in xs if lo <= x["d"] < hi]
        if g: print(f"  {lo}-{hi}分 n={len(g)} 胜率 {sum(x['w'] for x in g)/len(g):.0%}")
json.dump({str(k): v for k, v in rows.items()}, open("side_rows.json", "w", encoding="utf-8"))
