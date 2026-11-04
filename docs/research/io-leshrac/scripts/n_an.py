import json, statistics as st, math
from collections import Counter, defaultdict
P = {m["id"]: m for m in json.load(open("pos.json", encoding="utf-8"))}
F = {m["id"]: m for m in json.load(open("matches_full.json", encoding="utf-8")) if m["id"] in P}
def norm(q, rad):
    u, v = (q[0] - 64) / 128, (q[1] - 64) / 128
    return (u, v) if rad else (1 - u, 1 - v)   # 统一成天辉视角：自家基地左下，优势路在下 / 右
def _seg(p, a, b):
    ax, ay = a; bx, by = b; px, py = p
    dx, dy = bx - ax, by - ay
    t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)
BOT = [((0.25, 0.12), (0.88, 0.12)), ((0.88, 0.12), (0.88, 0.75))]
TOP = [((0.12, 0.25), (0.12, 0.88)), ((0.12, 0.88), (0.75, 0.88))]
MID = [((0.25, 0.25), (0.75, 0.75))]
def region(q, rad):
    if not q: return None
    u, v = norm(q, rad); s, d = u + v, v - u
    if u < 0.27 and v < 0.27: return "己方基地"
    if u > 0.73 and v > 0.73: return "敌方高地"
    lane = min((min(_seg((u, v), a, b) for a, b in L), n) for L, n in ((BOT, "优势路"), (TOP, "劣势路"), (MID, "中路")))
    if lane[0] < 0.06: return lane[1]
    if abs(s - 1) < 0.06: return "河道"
    if d < 0: return "己方主野区" if s < 1 else "敌方劣势野区"
    return "己方劣势野区" if s < 1 else "敌方主野区"
ORDER = ["优势路", "己方主野区", "中路", "河道", "劣势路", "己方劣势野区", "敌方主野区", "敌方劣势野区", "敌方高地", "己方基地"]
def idx(t): return int((t + 90) // 15)
def hero(m, h): return next(p for p in m["players"] if p["heroId"] == h)
def at(p, t):
    i = idx(t); return p["pos15"][i] if i < len(p["pos15"]) else None
# 1) region by minute
print("=== 每段时间在哪（占比，按每 15 秒采样）===")
for who, h in (("拉席克", 52), ("艾欧", 91)):
    print(who)
    for lo, hi in ((0, 3), (3, 6), (6, 9), (9, 12), (12, 15), (15, 20), (20, 25), (25, 30), (30, 35), (35, 40)):
        c = Counter(); tog = [0, 0]
        for m in P.values():
            p = hero(m, h); le = hero(m, 52); io = hero(m, 91)
            for t in range(lo * 60, hi * 60, 15):
                r = region(at(p, t), p["isRadiant"])
                if r: c[r] += 1
                a, b = at(le, t), at(io, t)
                if a and b: tog[0] += math.dist(a, b) <= 7; tog[1] += 1
        tot = sum(c.values()) or 1
        print(f"  {lo:2}-{hi:2} 分钟  " + "  ".join(f"{r}{c[r]/tot:.0%}" for r in ORDER if c[r] / tot >= 0.04) + f"   | 两人在一起 {tog[0]/max(tog[1],1):.0%}")
json.dump({}, open("_x", "w"))
