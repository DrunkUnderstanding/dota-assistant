import json, statistics as st
from collections import defaultdict, Counter
F = {m["id"]: m for m in json.load(open("matches_full.json", encoding="utf-8"))}
T = json.load(open("towerdmg.json", encoding="utf-8"))
NPC = {x["id"]: x["name"] for x in json.load(open("npcs.json", encoding="utf-8"))["constants"]["npcs"]}
def rel(n, our_rad):
    lane = "top" if "_top" in n else "mid" if "_mid" in n else "bot" if "_bot" in n else ""
    kind = "一塔" if "tower1" in n else "二塔" if "tower2" in n else "高地塔" if "tower3" in n else "基地塔" if "tower4" in n else "近战兵营" if "melee_rax" in n else "远程兵营" if "range_rax" in n else "基地" if "fort" in n else None
    if not kind: return None
    if lane == "mid": l = "中路"
    elif lane: l = "我方优势路上的" if (lane == "bot") == our_rad else "我方劣势路上的"
    else: l = ""
    return l + kind
stat = defaultdict(lambda: {"n": 0, "le": 0, "io": 0, "team": 0, "le_top": 0, "le_abil": 0, "fell": 0})
le_tot = Counter(); le_all = 0; le_abil = 0
per_min = defaultdict(list)
N = len(T)
for m in T:
    f = F[m["id"]]; le = next(p for p in m["players"] if p["heroId"] == 52); rad = le["isRadiant"]
    fell = {d["npcId"] for d in f["towerDeaths"] or []}
    team = [p for p in m["players"] if p["isRadiant"] == rad]
    dmg = defaultdict(dict)
    for p in team:
        for r in p["stats"]["towerDamageReport"] or []:
            n = NPC.get(r["npcId"], "")
            if ("goodguys" in n) == rad: continue  # 只看对面的建筑
            dmg[r["npcId"]][p["heroId"]] = r
    for npc, by in dmg.items():
        name = rel(NPC.get(npc, ""), rad)
        if not name: continue
        s = stat[name]; tot = sum(r["damage"] for r in by.values())
        if tot <= 0: continue
        s["n"] += 1; s["team"] += tot
        s["le"] += by.get(52, {}).get("damage", 0); s["io"] += by.get(91, {}).get("damage", 0)
        s["le_abil"] += by.get(52, {}).get("damageFromAbility", 0)
        s["le_top"] += max(by, key=lambda h: by[h]["damage"]) == 52
        s["fell"] += npc in fell
        le_tot[name] += by.get(52, {}).get("damage", 0)
    pm = le["stats"]["towerDamagePerMinute"] or []
    for i, x in enumerate(pm): per_min[i].append(x)
le_sum = sum(le_tot.values())
order = sorted(stat, key=lambda k: -le_tot[k])
print(f"{'建筑':<16}{'拉席克打的伤害占全部':>6} {'拉席克占我方英雄伤害':>6} {'艾欧占':>4} {'拉席克伤害最高':>6} {'技能伤害占拉席克':>6}")
for k in order:
    s = stat[k]
    if s["n"] < N * 0.2: continue
    print(f"{k:<16}{le_tot[k]/le_sum:8.0%}{s['le']/s['team']:12.0%}{s['io']/s['team']:10.0%}{s['le_top']/s['n']:12.0%}{(s['le_abil']/s['le'] if s['le'] else 0):12.0%}")
print("\n拉席克每分钟推塔伤害（中位 / 平均，含所有建筑）:")
for i in range(0, 36, 1):
    xs = per_min.get(i, [])
    if len(xs) >= 30 and i in (4,6,8,9,10,11,12,13,14,15,16,18,20,22,25,28,30,33,35): print(f"  {i:2}分 平均 {sum(xs)/len(xs):5.0f}  (n={len(xs)})")
