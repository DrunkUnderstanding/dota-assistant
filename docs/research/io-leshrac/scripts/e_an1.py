import json, statistics as st
from collections import Counter
M = json.load(open("matches_full.json", encoding="utf-8"))
def pair(m):
    io = next(p for p in m["players"] if p["heroId"] == 91)
    le = next(p for p in m["players"] if p["heroId"] == 52)
    return io, le
pos = Counter(); lanes = Counter(); win = Counter(); parsed = 0
dur = []
for m in M:
    io, le = pair(m)
    pos[(io["position"], le["position"])] += 1
    lanes[(io["lane"], le["lane"])] += 1
    win[io["isVictory"]] += 1
    if io["stats"] and io["stats"]["itemPurchases"]: parsed += 1
    dur.append((m["durationSeconds"]/60, io["isVictory"]))
print(len(M), "parsed", parsed, "win", win)
print(pos.most_common(10)); print(lanes.most_common(10))
for lo, hi in ((0,25),(25,30),(30,35),(35,40),(40,45),(45,99)):
    xs = [w for d, w in dur if lo <= d < hi]
    if xs: print(f"{lo}-{hi}min n={len(xs)} wr={sum(xs)/len(xs):.2f}")
print("median dur", st.median(d for d,_ in dur), "win median", st.median(d for d,w in dur if w), "loss median", st.median(d for d,w in dur if not w))
# tether targets
t = Counter()
for m in M:
    io, le = pair(m)
    for e in (io["playbackData"] or {}).get("abilityUsedEvents") or []:
        if e["abilityId"] == 5485: t[e["target"]] += 1
print("tether targets", t.most_common(8))
