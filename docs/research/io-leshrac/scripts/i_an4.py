import json, statistics as st
from collections import Counter, defaultdict
exec(open("f_an2.py", encoding="utf-8").read().split("def items")[0])
def med(xs): return st.median(xs) if xs else None
n = len(sel)
# relocate usage
first_rel, cnt_rel, rel_bins = [], [], Counter()
for m, io, le in sel:
    ev = [e["time"] for e in (io["playbackData"] or {}).get("abilityUsedEvents") or [] if e["abilityId"] == 5488]
    cnt_rel.append(len(ev))
    if ev: first_rel.append(ev[0] / 60)
    for t in ev: rel_bins[int(t // 300) * 5] += 1
print("relocate first median %.1f, q1 %.1f q3 %.1f, per game median %s, games with any %.0f%%" % (
    med(first_rel), st.quantiles(first_rel, n=4)[0], st.quantiles(first_rel, n=4)[2], med(cnt_rel), 100 * len(first_rel) / n))
print("relocate casts by 5-min bin per game:", {k: round(v / n, 2) for k, v in sorted(rel_bins.items()) if k < 60})
# kills/deaths of the pair by 5-min bins, win vs loss
for who in ("le", "io"):
    for kind in ("killEvents", "deathEvents", "assistEvents"):
        b = Counter()
        for m, io, le in sel:
            pl = io if who == "io" else le
            for e in pl["stats"][kind] or []: b[int(max(e["time"], 0) // 300) * 5] += 1
        print(who, kind, {k: round(v / n, 2) for k, v in sorted(b.items()) if k < 50})
# Lesh networth & team networth vs enemy
for t in (5, 10, 15, 20, 25, 30):
    w = [le["stats"]["networthPerMinute"][t] for m, io, le in sel if le["isVictory"] and len(le["stats"]["networthPerMinute"]) > t]
    l = [le["stats"]["networthPerMinute"][t] for m, io, le in sel if not le["isVictory"] and len(le["stats"]["networthPerMinute"]) > t]
    print(f"le networth @{t}: win {med(w):.0f}  loss {med(l):.0f}")
# team nw lead
for t in (5, 10, 15, 20, 25, 30, 35):
    leads = defaultdict(list)
    for m, io, le in sel:
        side = le["isRadiant"]
        mine = [p for p in m["players"] if p["isRadiant"] == side]; their = [p for p in m["players"] if p["isRadiant"] != side]
        try:
            d = sum(p["stats"]["networthPerMinute"][t] for p in mine) - sum(p["stats"]["networthPerMinute"][t] for p in their)
        except Exception: continue
        leads[le["isVictory"]].append(d)
    print(f"team nw lead @{t}: win {med(leads[True])}  loss {med(leads[False])}  n={len(leads[True])}/{len(leads[False])}")
# enemy buildings destroyed by time
for t in (10, 15, 20, 25, 30):
    w, l = [], []
    for m, io, le in sel:
        c = sum(1 for d in m["towerDeaths"] or [] if d["isRadiant"] != le["isRadiant"] and d["time"] <= t * 60)
        (w if le["isVictory"] else l).append(c)
    print(f"enemy buildings down by {t}: win avg {sum(w)/len(w):.1f}  loss avg {sum(l)/len(l):.1f}")
ft = [min((d["time"] for d in m["towerDeaths"] or [] if d["isRadiant"] != le["isRadiant"]), default=None) for m, io, le in sel]
ft = [x / 60 for x in ft if x]
print("first enemy building median %.1f" % med(ft))
ownt = [min((d["time"] for d in m["towerDeaths"] or [] if d["isRadiant"] == le["isRadiant"]), default=None) for m, io, le in sel]
print("first own building lost median %.1f" % med([x/60 for x in ownt if x]))
# Lesh camp stacks / io stacks
print("io stacks", med([sum(io["stats"]["campStack"] or [0][-1:]) if False else (io["stats"]["campStack"] or [0])[-1] for m, io, le in sel]))
