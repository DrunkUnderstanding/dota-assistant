import json, statistics as st
exec(open("n_an.py", encoding="utf-8").read().split("# 1)")[0])
kb = json.load(open(r"D:\xqs\dota-assistant\data\kb\kb.json", encoding="utf-8"))
ID = {i["name_loc"]: i["id"] for i in kb["items"]}
NPC = {x["id"]: x["name"] for x in json.load(open("npcs.json", encoding="utf-8"))["constants"]["npcs"]}
kaya, arc = ID["慧光"], ID["奥术鞋"]
rows = []
for mid, m in P.items():
    f = F[mid]; le = hero(f, 52); lp = hero(m, 52); rad = le["isRadiant"]
    buy = {e["itemId"]: e["time"] for e in reversed(le["stats"]["itemPurchases"])}
    tk, ta = buy.get(kaya), buy.get(arc)
    enemy_towers = sorted((d["time"], NPC[d["npcId"]]) for d in f["towerDeaths"] or [] if "tower" in NPC.get(d["npcId"], "") and ("goodguys" in NPC[d["npcId"]]) != rad)
    safe_name = "bot" if rad else "top"      # 我们优势路 = 对面在这条路上的塔
    t_safe = next((t for t, n in enemy_towers if "tower1" in n and safe_name in n), None)
    t_first = enemy_towers[0][0] if enemy_towers else None
    # 对面这座塔倒时拉席克在哪
    where = region(at(lp, t_safe), rad) if t_safe else None
    ks = sum(1 for e in lp["stats"]["killEvents"] or [] if tk and e["time"] < tk)
    ds = sum(1 for e in lp["stats"]["deathEvents"] or [] if tk and e["time"] < tk)
    rows.append((tk, ta, t_safe, t_first, where, ks, ds, le["isVictory"]))
n = len(rows)
med = lambda x: st.median(x) if x else None
print("慧光中位", med([r[0]/60 for r in rows if r[0]]))
print("对面第一座塔中位", med([r[3]/60 for r in rows if r[3]]), " 在慧光之前倒的局", sum(1 for r in rows if r[3] and r[0] and r[3] < r[0]) / n)
print("对面我们优势路一塔中位", med([r[2]/60 for r in rows if r[2]]), " 在慧光之前倒", sum(1 for r in rows if r[2] and r[0] and r[2] < r[0]) / n,
      " 在奥术鞋之前倒", sum(1 for r in rows if r[2] and r[1] and r[2] < r[1]) / n)
from collections import Counter
print("这座塔倒时拉席克在:", Counter(r[4] for r in rows if r[4]).most_common(5))
print("慧光前拉席克击杀 中位", med([r[5] for r in rows]), "平均", sum(r[5] for r in rows)/n, " 死亡平均", sum(r[6] for r in rows)/n)
for lo, hi in ((0, 8), (8, 11), (11, 13), (13, 15), (15, 18), (18, 99)):
    xs = [r for r in rows if r[2] and lo*60 <= r[2] < hi*60]
    print(f"  优势路对面一塔 {lo}-{hi}分倒: {len(xs)/n:.0%} 局  胜率 {sum(r[7] for r in xs)/max(len(xs),1):.0%}")
print("  到结束都没倒:", sum(1 for r in rows if not r[2]) / n)
