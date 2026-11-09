"""对线期艾欧的出装会不会随局面变（2026-11-09，第 72 节）。
数据：w_mek_fetch.py 抓的 pair_full.jsonl，拉席克 1 + 艾欧 5。
- 线上局面：5 分钟时 拉席克+艾欧 减 对面 3、4 号位 的经济；看艾欧 5~12 分钟买了什么（局面先、买后，少一点倒因为果）
- 对面线上英雄：对面 3、4 号位是谁时，艾欧出门 / 10 分钟内买的东西有没有变
用法：python w_lane_an.py"""
import json
import math
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from dota_assistant.core.config import DATA_DIR  # noqa: E402
from dota_assistant.domain import mates  # noqa: E402
from dota_assistant.sources import assets  # noqa: E402

IO, LESH = 91, 52
CN = mates._item_names()
ID = {k: i for i, (k, _) in CN.items()}
HN = {h.id: h.name_cn for h in assets.load_heroes()}

CONSUM = ["tango", "flask", "clarity", "enchanted_mango", "faerie_fire", "blood_grenade", "ward_observer",
          "ward_sentry", "dust", "smoke_of_deceit", "tpscroll"]
SMALL = ["magic_stick", "magic_wand", "soul_ring", "ring_of_regen", "headdress", "gauntlets", "bracer",
         "wind_lace", "boots", "tranquil_boots", "arcane_boots", "urn_of_shadows", "orb_of_venom", "blight_stone",
         "infused_raindrop", "fluffy_hat", "cloak", "chainmail", "ring_of_basilius", "sobi_mask", "null_talisman",
         "pavise", "glimmer_cape", "force_staff", "mekansm", "holy_locket", "buckler", "medallion_of_courage"]


def nm(k):
    return CN.get(ID.get(k), (k, k))[1] if k in ID else k


def nw_at(p, minute):
    nw = p.get("nw") or []
    return nw[min(minute, len(nw) - 1)] if nw else 0


games = []
for line in (DATA_DIR / "research" / "io-items" / "pair_full.jsonl").read_text(encoding="utf-8").splitlines():
    m = json.loads(line)
    if m.get("missing") or m["dur"] < 15 * 60:
        continue
    ps = m["players"]
    io = next((p for p in ps if p["h"] == IO), None)
    lesh = next((p for p in ps if p["h"] == LESH), None)
    if not io or not lesh or io["rad"] != lesh["rad"] or not io["parsed"]:
        continue
    if (lesh["pos"], io["pos"]) != ("POSITION_1", "POSITION_5"):
        continue
    opp = [p for p in ps if p["rad"] != io["rad"] and p["pos"] in ("POSITION_3", "POSITION_4")]
    if len(opp) != 2:
        continue
    lane = {mm: nw_at(io, mm) + nw_at(lesh, mm) - sum(nw_at(p, mm) for p in opp) for mm in (5, 10)}
    games.append({"io": io, "win": io["win"], "opp": [p["h"] for p in opp], "lane5": lane[5], "lane10": lane[10],
                  "deaths10": sum(1 for t in io["deaths"] if t <= 600)})

n = len(games)
print(f"拉席克 1 + 艾欧 5、有出装、打满 15 分钟：{n} 局")


def bought(g, key, t0, t1):
    i = ID.get(key)
    return sum(1 for t, x in g["io"]["items"] if x == i and t0 < t <= t1)


def first_t(g, key):
    i = ID.get(key)
    return next((t for t, x in g["io"]["items"] if x == i), None)


def pc(a, b):
    return f"{a / b * 100:5.1f}%" if b else "   - "


# ---- 1. 按 5 分钟线上经济差分三档 ----
vals = sorted(g["lane5"] for g in games)
c1, c2 = vals[n // 3], vals[2 * n // 3]
bands = [("劣势", [g for g in games if g["lane5"] < c1]), ("均势", [g for g in games if c1 <= g["lane5"] < c2]),
         ("优势", [g for g in games if g["lane5"] >= c2])]
print(f"\n== 1. 5 分钟线上经济差（我方两人 - 对面 3、4 号位）三档，分界 {c1:+.0f} / {c2:+.0f} ==")
for lab, sub in bands:
    print(f"  {lab}：{len(sub)} 局，胜率 {pc(sum(g['win'] for g in sub), len(sub))}，"
          f"10 分钟前艾欧死 {st.mean(g['deaths10'] for g in sub):.2f} 次")

print("\n  艾欧 5~12 分钟买的消耗品：买过的比例 / 平均个数（三档：劣 / 均 / 优）")
for k in CONSUM:
    cells = []
    for _, sub in bands:
        c = [bought(g, k, 300, 720) for g in sub]
        cells.append(f"{pc(sum(1 for x in c if x), len(c))} ×{st.mean(c):.2f}")
    print(f"    {nm(k):10s} " + "   ".join(cells))

print("\n  艾欧 5~12 分钟买的小件 / 装备：买过的比例（劣 / 均 / 优）")
for k in SMALL:
    rs = [sum(1 for g in sub if bought(g, k, 300, 720)) / len(sub) for _, sub in bands]
    if max(rs) < 0.03:
        continue
    print(f"    {nm(k):10s} " + "   ".join(f"{r * 100:5.1f}%" for r in rs))

print("\n  关键小件到手时间中位（分钟，劣 / 均 / 优，括号里是 15 分钟前出了的比例）")
for k in ("magic_wand", "soul_ring", "urn_of_shadows", "glimmer_cape", "mekansm", "holy_locket"):
    cells = []
    for _, sub in bands:
        ts = [t for g in sub if (t := first_t(g, k)) is not None and t <= 900]
        cells.append(f"{st.median(ts) / 60:5.1f}（{pc(len(ts), len(sub))}）" if ts else "   -   ")
    print(f"    {nm(k):10s} " + "   ".join(cells))

# ---- 2. 出门装和 5 分钟内 ----
print("\n== 2. 出门装（0 秒前），三档对比（出门时还不知道线上结果，这里看的是出门装和之后线上结果的关系）==")
for k in CONSUM + SMALL:
    rs = [sum(1 for g in sub if bought(g, k, -999, 0)) / len(sub) for _, sub in bands]
    if max(rs) < 0.05:
        continue
    print(f"    {nm(k):10s} " + "   ".join(f"{r * 100:5.1f}%" for r in rs))

# ---- 3. 对面线上英雄 ----
print("\n== 3. 对面 3、4 号位是某个英雄时，艾欧出门 / 10 分钟内的东西（只列过校正的，|z| > 3.5）==")
items = [("出门", k, -999, 0) for k in ("gauntlets", "ring_of_regen", "faerie_fire", "ward_sentry", "ward_observer",
                                        "enchanted_mango", "flask", "magic_stick", "ring_of_basilius", "headdress")]
items += [("10 分内", k, 0, 600) for k in ("flask", "clarity", "dust", "ward_sentry", "blood_grenade", "tango",
                                          "soul_ring", "magic_wand", "wind_lace", "boots", "urn_of_shadows",
                                          "headdress", "infused_raindrop", "smoke_of_deceit")]
by = defaultdict(list)
for gi, g in enumerate(games):
    for h in g["opp"]:
        by[h].append(gi)
heroes = [h for h, v in by.items() if len(v) >= 80]
ncmp = len(heroes) * len(items)
print(f"  （{len(heroes)} 个英雄 × {len(items)} 件 = {ncmp} 次比较）")
hits = []
for when, k, t0, t1 in items:
    flag = [1 if bought(g, k, t0, t1) else 0 for g in games]
    base = sum(flag) / n
    if base in (0, 1):
        continue
    for h in heroes:
        v = [flag[i] for i in by[h]]
        r = sum(v) / len(v)
        z = (r - base) / math.sqrt(base * (1 - base) / len(v))
        if abs(z) > 3.5:
            hits.append((when, nm(k), HN.get(h, h), len(v), r, base, z))
for when, k, h, cnt, r, base, z in sorted(hits, key=lambda x: (x[0], x[1], -x[6])):
    print(f"    {when} {k:8s} 对面有{h}（{cnt} 局）：{r * 100:4.1f}%，平时 {base * 100:4.1f}%（z {z:+.1f}）")

# ---- 4. 对线局面和胜率：5~12 分钟买灵魂之戒 / 补药，按线上档内比 ----
print("\n== 4. 同一档线上局面里，12 分钟前出了灵魂之戒 vs 没出的胜率 ==")
for lab, sub in bands:
    y = [g["win"] for g in sub if (t := first_t(g, "soul_ring")) is not None and t <= 720]
    no = [g["win"] for g in sub if not ((t := first_t(g, "soul_ring")) is not None and t <= 720)]
    print(f"  {lab}：出了 {len(y)} 局 {pc(sum(y), len(y))}，没出 {len(no)} 局 {pc(sum(no), len(no))}")
