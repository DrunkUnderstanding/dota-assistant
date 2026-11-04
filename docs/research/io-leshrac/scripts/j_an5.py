import json
from collections import defaultdict
d = json.load(open(r"D:\xqs\dota-assistant\data\stats_r7-8_d14.json", encoding="utf-8"))
H = {x["id"]: x["name_cn"] for x in json.load(open(r"D:\xqs\dota-assistant\data\heroes.json", encoding="utf-8"))}
vs = defaultdict(lambda: [0, 0]); tot = defaultdict(lambda: [0, 0])
for a, b, g, w in d["vs"]:
    vs[(a, b)][0] += g; vs[(a, b)][1] += w
    vs[(b, a)][0] += g; vs[(b, a)][1] += g - w
    tot[a][0] += g; tot[a][1] += w; tot[b][0] += g; tot[b][1] += g - w
wr = {h: w / g for h, (g, w) in tot.items()}
def delta(h, e):
    g, w = vs[(h, e)]
    if g < 100: return None, g
    return w / g - (wr[h] - wr[e] + 0.5), g
rows = []
for e in H:
    if e in (91, 52): continue
    a, ga = delta(91, e); b, gb = delta(52, e)
    if a is None or b is None: continue
    rows.append((a + b, H[e], round(a * 100, 1), round(b * 100, 1), ga, gb))
rows.sort()
print("最克这对（Io 偏差 + Lesh 偏差，百分点）:")
for r in rows[:15]: print(" ", r)
print("这对最好打:")
for r in rows[-10:]: print(" ", r)
# sample-based
M = json.load(open("matches_full.json", encoding="utf-8"))
en = defaultdict(lambda: [0, 0])
for m in M:
    le = next(p for p in m["players"] if p["heroId"] == 52)
    for p in m["players"]:
        if p["isRadiant"] != le["isRadiant"]:
            en[p["heroId"]][0] += 1; en[p["heroId"]][1] += le["isVictory"]
print("sample: enemy with >=20 games, pair wr:")
for h, (g, w) in sorted(en.items(), key=lambda x: x[1][1] / x[1][0]):
    if g >= 20: print(" ", H[h], g, f"{w/g:.0%}")
