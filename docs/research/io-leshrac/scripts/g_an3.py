import json, statistics as st
from collections import Counter, defaultdict
exec(open("f_an2.py", encoding="utf-8").read().split("def items")[0])
def med(xs): return st.median(xs) if xs else None
n = len(sel)
# extra items
for who in ("io", "le"):
    first = defaultdict(list)
    for m, io, le in sel:
        pl = io if who == "io" else le; seen = set()
        for e in pl["stats"]["itemPurchases"]:
            if e["itemId"] not in seen and e["time"] > 0:
                seen.add(e["itemId"]); first[e["itemId"]].append(e["time"])
    print(f"\n{who} all items >=15% (time>0):")
    for t, nmx, p in sorted((med(ts)/60, nm(i), len(ts)/n) for i, ts in first.items() if len(ts)/n >= .15):
        print(f"  {t:5.1f} {nmx} {p:.0%}")
# key item timings win vs loss
def tm(who, iid):
    w, l = [], []
    for m, io, le in sel:
        pl = io if who == "io" else le
        t = next((e["time"] for e in pl["stats"]["itemPurchases"] if e["itemId"] == iid), None)
        if t: (w if pl["isVictory"] else l).append(t/60)
    return med(w), len(w), med(l), len(l)
inv = {v: k for k, v in NAME.items()}
for who, names in (("le", ["奥术鞋","慧光","慧夜对剑","散慧对剑","卫士胫甲","纷争面纱","黑皇杖","血精石","希瓦的守护","阿哈利姆神杖"]), ("io", ["圣洁吊坠","微光披风","梅肯斯姆"])):
    for x in names:
        if x in inv:
            a = tm(who, inv[x]); print(who, x, "win med %.1f (n=%d)  loss med %.1f (n=%d)" % (a[0] or 0, a[1], a[2] or 0, a[3]))
# levels
for who in ("le", "io"):
    for L in (2, 6, 12, 18):
        ts = [ (io if who=="io" else le)["stats"]["level"][L-1]/60 for m, io, le in sel if len((io if who=="io" else le)["stats"]["level"]) >= L]
        print(who, "level", L, "median min %.1f" % med(ts), "reached", f"{len(ts)/n:.0%}")
# skill build
NAMES = {5485:"羁绊",5486:"幽魂",5487:"过载",5488:"降临",5241:"撕裂",5242:"敕令",5243:"风暴",5244:"新星",539:"虚无"}
for who in ("le", "io"):
    seqs = Counter()
    for m, io, le in sel:
        pl = io if who == "io" else le
        ab = [NAMES.get(a["abilityId"], "天赋/其他") for a in sorted(pl["abilities"], key=lambda a: a["time"])][:12]
        seqs[" ".join(ab[:10])] += 1
    print(who, "top skill orders:")
    for s, c in seqs.most_common(4): print("  ", c, s)
    # which skill maxed first: level at which each reaches max
    mx = defaultdict(list)
    for m, io, le in sel:
        pl = io if who == "io" else le; cnt = Counter()
        for k, a in enumerate(sorted(pl["abilities"], key=lambda a: a["time"])):
            cnt[a["abilityId"]] += 1
            if cnt[a["abilityId"]] == 4 and a["abilityId"] in NAMES: mx[NAMES[a["abilityId"]]].append(k+1)
    print("  maxed at hero level (median):", {k: med(v) for k, v in mx.items() if len(v) > n*0.3})
