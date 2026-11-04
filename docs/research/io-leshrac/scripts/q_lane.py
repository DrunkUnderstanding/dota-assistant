import json, sys
from collections import Counter, defaultdict
sys.path.insert(0, r"D:\xqs\dota-assistant")
M = {m["id"]: m for m in json.load(open("matches_full.json", encoding="utf-8"))}
L = {m["id"]: m for m in json.load(open("lane.json", encoding="utf-8"))}
LANE = {("SAFE_LANE", True): "bottomLaneOutcome", ("SAFE_LANE", False): "topLaneOutcome",
        ("OFF_LANE", True): "topLaneOutcome", ("OFF_LANE", False): "bottomLaneOutcome", ("MID_LANE", True): "midLaneOutcome", ("MID_LANE", False): "midLaneOutcome"}
def res(o, rad):
    if not o: return None
    if o == "TIE": return "均势"
    good = o.startswith("RADIANT") == rad
    return ("优势" if good else "劣势") + ("（碾压）" if "STOMP" in o else "")
groups = defaultdict(list)
for mid, m in M.items():
    io = next(p for p in m["players"] if p["heroId"] == 91); le = next(p for p in m["players"] if p["heroId"] == 52)
    if io["lane"] != le["lane"] or not le["lane"]: continue
    key = f"{le['lane']}  艾{(io['position'] or '?')[-1]}+拉{(le['position'] or '?')[-1]}"
    r = res(L[mid].get(LANE[(le["lane"], le["isRadiant"])]), le["isRadiant"])
    if r:
        other = [res(L[mid].get(f), le["isRadiant"]) for f in ("topLaneOutcome", "midLaneOutcome", "bottomLaneOutcome") if f != LANE[(le["lane"], le["isRadiant"])]]
        groups[key].append((r, le["isVictory"], other))
for k, xs in sorted(groups.items(), key=lambda x: -len(x[1])):
    n = len(xs); c = Counter(r.split("（")[0] for r, *_ in xs); cs = Counter(r for r, *_ in xs)
    print(f"{k}: n={n}  " + "  ".join(f"{t} {c[t]/n:.0%}" for t in ("优势", "均势", "劣势")) + "   细分: " + " ".join(f"{t}{v}" for t, v in cs.most_common()))
    for t in ("优势（碾压）", "优势", "均势", "劣势", "劣势（碾压）"):
        ys = [w for r, w, _ in xs if r == t]
        if ys: print(f"     {t}: 胜率 {sum(ys)/len(ys):.0%} (n={len(ys)})")
    # 其他两路
    oc = Counter(o.split("（")[0] for *_, os in xs for o in os if o)
    tot = sum(oc.values()); print("     同局另外两路: " + " ".join(f"{t}{oc[t]/tot:.0%}" for t in ("优势", "均势", "劣势")))
