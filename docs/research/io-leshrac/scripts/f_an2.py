import json, statistics as st
from collections import Counter, defaultdict
M = json.load(open("matches_full.json", encoding="utf-8"))
kb = json.load(open(r"D:\xqs\dota-assistant\data\kb\kb.json", encoding="utf-8"))
od = json.load(open(r"D:\xqs\dota-assistant\data\opendota_items.json", encoding="utf-8"))
NAME = {v["id"]: v.get("dname", k) for k, v in od.items() if isinstance(v, dict) and "id" in v}
cost = {v["id"]: v.get("cost") or 0 for k, v in od.items() if isinstance(v, dict) and "id" in v}
for i in kb["items"]: NAME[i["id"]] = i["name_loc"]
def nm(i): return NAME.get(i, str(i))
sel = []
for m in M:
    io = next(p for p in m["players"] if p["heroId"] == 91)
    le = next(p for p in m["players"] if p["heroId"] == 52)
    if io["position"] == "POSITION_5" and le["position"] == "POSITION_1" and le["stats"] and le["stats"]["itemPurchases"]:
        sel.append((m, io, le))
print("main config parsed n=", len(sel), "wr", sum(le["isVictory"] for _, _, le in sel) / len(sel))
def items(p, who, minpct=0.12, maxcost_skip=0):
    first = defaultdict(list); start = Counter(); n = 0
    winby = defaultdict(list)
    for m, io, le in sel:
        pl = io if who == "io" else le
        n += 1; seen = set()
        for e in pl["stats"]["itemPurchases"]:
            if e["time"] <= 0: start[e["itemId"]] += 1
            if e["itemId"] in seen: continue
            seen.add(e["itemId"])
            first[e["itemId"]].append(e["time"])
            winby[e["itemId"]].append(pl["isVictory"])
    print(f"\n=== {who} starting (time<=0) ===")
    for i, c in start.most_common(12): print(f"  {nm(i)} {c/n:.0%}")
    print(f"=== {who} items (first buy, >= {minpct:.0%}, cost>=1000 or boots/key) ===")
    rows = [(st.median(ts) / 60, nm(i), len(ts) / n, sum(winby[i]) / len(winby[i]), cost.get(i, 0)) for i, ts in first.items() if len(ts) / n >= minpct and e is not None]
    for t, name, pct, wr, c in sorted(rows):
        if c >= 900 or c == 0 or name in ("Boots of Speed",):
            print(f"  {t:5.1f}min  {name:<16} {pct:4.0%}  wr={wr:.0%} cost={c}")
items(sel, "le"); items(sel, "io")
json.dump({"n": len(sel)}, open("sel.json", "w"))
