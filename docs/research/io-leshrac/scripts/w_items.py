"""艾欧 + 拉席克同队时两人的出装（2026-11-09）：出门装、对线补给、中期 / 后期装备的出场率、时间、胜率。
数据复用第 68 节针对装研究抓的 STRATZ 对局（7.41f 超凡～冠绝）：
  data/research/io-items/fetched.jsonl       只有艾欧的出装
  data/research/hero-items-52/fetched.jsonl  只有拉席克的出装
从里面挑两人同队的局。注意那两批是按「对面难不难打」分层抽的，难打的局偏多（约一半）。
用法：python w_items.py [--pos 1 5]（拉席克、艾欧的位置，默认 1 5；--pos any 不限）"""
import json
import math
import statistics as st
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from dota_assistant.core.config import DATA_DIR  # noqa: E402
from dota_assistant.domain import mates  # noqa: E402

IO, LESH = 91, 52
CN = mates._item_names()
COST = {}
try:
    from dota_assistant.sources import assets
    for k, v in assets.items_const().items():
        if isinstance(v, dict) and v.get("id"):
            COST[v["id"]] = v.get("cost") or 0
except Exception:  # noqa: BLE001
    pass

pos_arg = sys.argv[sys.argv.index("--pos") + 1:] if "--pos" in sys.argv else ["1", "5"]
WANT = None if pos_arg[:1] == ["any"] else (f"POSITION_{pos_arg[0]}", f"POSITION_{pos_arg[1]}")


def name(i):
    return CN.get(i, (None, str(i)))[1]


def load(path, me, mate):
    """返回 [(局, 我, 搭档位置)]：只要 me 和 mate 同队、me 有出装记录的局"""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = json.loads(line)
        if m.get("missing"):
            continue
        p = next((x for x in m["players"] if x["h"] == me), None)
        if not p or not p.get("parsed"):
            continue
        q = next((x for x in m["players"] if x["h"] == mate and x["rad"] == p["rad"]), None)
        if not q:
            continue
        out.append((m, p, q.get("pos")))
    return out


def load_all(path, me):
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = json.loads(line)
        if m.get("missing"):
            continue
        p = next((x for x in m["players"] if x["h"] == me), None)
        if p and p.get("parsed"):
            out.append((m, p))
    return out


CONSUM = {"tango", "flask", "clarity", "enchanted_mango", "faerie_fire", "ward_observer", "ward_sentry",
          "smoke_of_deceit", "dust", "tpscroll", "blood_grenade", "bottle", "tome_of_knowledge"}


def key(i):
    return CN.get(i, (str(i), ""))[0]


def team_diff(m, p, minute):
    """minute 分钟时我方减对面的总经济"""
    tot = 0
    for x in m["players"]:
        nw = x.get("nw") or []
        v = nw[min(minute, len(nw) - 1)] if nw else 0
        tot += v if x["rad"] == p["rad"] else -v
    return tot


def adjusted(reached, item, t):
    """按 t 时刻全队经济差分 5 档，档内算出了 - 没出的胜率差，按档内样本加权平均"""
    minute = int(t // 60)
    rows = [(team_diff(m, p, minute), any(ii == item and tt <= t for tt, ii in p["items"]), p["win"])
            for m, p in reached]
    rows.sort(key=lambda r: r[0])
    k = 5
    num = var = wsum = 0.0
    for b in range(k):
        seg = rows[len(rows) * b // k: len(rows) * (b + 1) // k]
        y = [w for _, d, w in seg if d]
        nn = [w for _, d, w in seg if not d]
        if len(y) < 5 or len(nn) < 5:
            continue
        a, c = sum(y) / len(y), sum(nn) / len(nn)
        w = 1 / (1 / len(y) + 1 / len(nn))
        num += w * (a - c)
        var += w * w * (a * (1 - a) / len(y) + c * (1 - c) / len(nn))
        wsum += w
    if not wsum:
        return None
    return num / wsum, math.sqrt(var) / wsum


def report(title, games, base_games):
    n = len(games)
    wins = sum(1 for m, p, _ in games if p["win"])
    print(f"\n==================== {title}：{n} 局，胜率 {wins / n * 100:.1f}% ====================")
    dur = [m["dur"] / 60 for m, _, _ in games]
    print(f"时长中位 {st.median(dur):.1f} 分钟")

    # ---- 出门装 ----
    start = []
    for m, p, _ in games:
        c = Counter(i for t, i in p["items"] if t <= 0)
        start.append(c)
    cnt = Counter()
    qty = defaultdict(list)
    for c in start:
        for i, k in c.items():
            cnt[i] += 1
            qty[i].append(k)
    print("\n-- 出门装（0 秒前买的）：出场率 / 平均个数 --")
    for i, c in cnt.most_common(14):
        if c / n < 0.03:
            break
        print(f"  {name(i):10s} {c / n * 100:5.1f}%  ×{st.mean(qty[i]):.1f}")
    combo = Counter(tuple(sorted((name(i), k) for i, k in c.items())) for c in start)
    print("  最常见整套：")
    for cb, c in combo.most_common(5):
        print(f"    {c / n * 100:4.1f}%  " + " + ".join(f"{a}×{k}" if k > 1 else a for a, k in cb))

    # ---- 对线补给 0~10 分钟 ----
    print("\n-- 对线补给（0~10 分钟内，不含出门）：买过的比例 / 买过的人平均个数 --")
    sup = defaultdict(list)
    for m, p, _ in games:
        c = Counter(i for t, i in p["items"] if 0 < t <= 600 and key(i) in CONSUM)
        for i, k in c.items():
            sup[i].append(k)
    for i, v in sorted(sup.items(), key=lambda kv: -len(kv[1])):
        if len(v) / n < 0.05:
            continue
        print(f"  {name(i):10s} {len(v) / n * 100:5.1f}%  ×{st.mean(v):.1f}")

    # ---- 小件 0~15 分钟（价格 300~999）----
    print("\n-- 前期小件（15 分钟内、价格 300~999，含合成）：出场率 / 中位时间 --")
    small = defaultdict(list)
    for m, p, _ in games:
        seen = {}
        for t, i in p["items"]:
            if 0 < t <= 900 and 300 <= COST.get(i, 0) < 1000 and key(i) not in CONSUM and i not in seen:
                seen[i] = t
        for i, t in seen.items():
            small[i].append(t)
    for i, ts in sorted(small.items(), key=lambda kv: -len(kv[1])):
        if len(ts) / n < 0.08:
            continue
        print(f"  {name(i):10s} {len(ts) / n * 100:5.1f}%  {st.median(ts) / 60:5.1f} 分")

    # ---- 装备（价格 >= 1000，非消耗品）----
    print("\n-- 主要装备（价格 ≥ 1000）：出场率 / 中位时间 / 打到中位时间的局里出了 vs 没出的胜率差"
          "（原始 → 按当时全队经济差分 5 档调整后）--")
    first = defaultdict(list)       # item -> [(time, win)]
    for m, p, _ in games:
        seen = {}
        for t, i in p["items"]:
            if COST.get(i, 0) >= 1000 and key(i) not in CONSUM and i not in seen:
                seen[i] = t
        for i, t in seen.items():
            first[i].append(t)
    base_rate = {}
    if base_games:
        bn = len(base_games)
        bc = Counter()
        for m, p in base_games:
            bc.update({i for t, i in p["items"] if COST.get(i, 0) >= 1000})
        base_rate = {i: c / bn for i, c in bc.items()}
    rows = []
    for i, ts in first.items():
        if len(ts) / n < 0.04:
            continue
        med = st.median(ts)
        # 打到 med 的局里：med 前出了 vs 没出
        reached = [(m, p) for m, p, _ in games if m["dur"] >= med]
        yes = [p["win"] for m, p in reached if any(ii == i and t <= med for t, ii in p["items"])]
        no = [p["win"] for m, p in reached if not any(ii == i and t <= med for t, ii in p["items"])]
        if len(yes) >= 20 and len(no) >= 20:
            a, b = sum(yes) / len(yes), sum(no) / len(no)
            se = math.sqrt(a * (1 - a) / len(yes) + b * (1 - b) / len(no))
            adj = adjusted(reached, i, med)
            adjs = f" → {adj[0] * 100:+.1f} ±{1.96 * adj[1] * 100:.1f}" if adj else ""
            diff = f"{a * 100:5.1f}% vs {b * 100:5.1f}%（{(a - b) * 100:+.1f} ±{1.96 * se * 100:.1f}{adjs}）"
        else:
            diff = "样本太少"
        br = base_rate.get(i)
        brs = f"  同位置全部局 {br * 100:4.1f}%" if br is not None else ""
        rows.append((med, f"  {name(i):10s} {len(ts) / n * 100:5.1f}%  {med / 60:5.1f} 分  {diff}{brs}"))
    for _, s in sorted(rows):
        print(s)


def versus(title, games, a, b, t):
    """二选一：打到 t 秒的局里，t 前出了 a 没出 b vs 出了 b 没出 a，按 t 时全队经济差分 5 档调整"""
    minute = int(t // 60)
    rows = []
    for m, p, _ in games:
        if m["dur"] < t:
            continue
        got = {i for tt, i in p["items"] if tt <= t}
        if (a in got) == (b in got):
            continue
        rows.append((team_diff(m, p, minute), a in got, p["win"]))
    ya = [w for _, d, w in rows if d]
    yb = [w for _, d, w in rows if not d]
    rows.sort(key=lambda r: r[0])
    num = var = wsum = 0.0
    for k in range(5):
        seg = rows[len(rows) * k // 5: len(rows) * (k + 1) // 5]
        y = [w for _, d, w in seg if d]
        nn = [w for _, d, w in seg if not d]
        if len(y) < 5 or len(nn) < 5:
            continue
        pa, pb = sum(y) / len(y), sum(nn) / len(nn)
        w = 1 / (1 / len(y) + 1 / len(nn))
        num += w * (pa - pb)
        var += w * w * (pa * (1 - pa) / len(y) + pb * (1 - pb) / len(nn))
        wsum += w
    s = f"  {title}（{t // 60} 分钟前）：{name(a)} {len(ya)} 局 {sum(ya) / max(1, len(ya)) * 100:.1f}%，" \
        f"{name(b)} {len(yb)} 局 {sum(yb) / max(1, len(yb)) * 100:.1f}%"
    if wsum:
        s += f"；同经济差下 {name(a)} {num / wsum * 100:+.1f} ±{1.96 * math.sqrt(var) / wsum * 100:.1f}"
    print(s)


def main():
    lesh = load(DATA_DIR / "research" / "hero-items-52" / "fetched.jsonl", LESH, IO)
    io = load(DATA_DIR / "research" / "io-items" / "fetched.jsonl", IO, LESH)
    print("拉席克数据里有艾欧同队：", len(lesh), Counter((p["pos"], q) for _, p, q in lesh).most_common(6))
    print("艾欧数据里有拉席克同队：", len(io), Counter((q, p["pos"]) for _, p, q in io).most_common(6))
    if WANT:
        lesh = [g for g in lesh if (g[1]["pos"], g[2]) == WANT]
        io = [g for g in io if (g[2], g[1]["pos"]) == WANT]
    lesh_all = [g for g in load_all(DATA_DIR / "research" / "hero-items-52" / "fetched.jsonl", LESH)
                if not WANT or g[1]["pos"] == WANT[0]]
    io_all = [g for g in load_all(DATA_DIR / "research" / "io-items" / "fetched.jsonl", IO)
              if not WANT or g[1]["pos"] == WANT[1]]
    report(f"拉席克（{WANT and WANT[0]}，队里有艾欧）", lesh, lesh_all)
    report(f"艾欧（{WANT and WANT[1]}，队里有拉席克）", io, io_all)
    print(f"\n拉席克 {WANT and WANT[0]} 全部局（不管队友）{len(lesh_all)} 局，艾欧 {WANT and WANT[1]} 全部局 {len(io_all)} 局")
    print("\n-- 二选一对比 --")
    versus("拉席克 第四件", lesh, 277, 273, 1200)
    versus("拉席克 第四件", lesh, 277, 273, 1500)
    versus("拉席克 团队装", lesh, 231, 1854, 1500)
    versus("拉席克 团队装", lesh, 79, 1854, 1200)
    versus("拉席克 25 分钟", lesh, 116, 119, 1680)
    versus("拉席克 25 分钟", lesh, 116, 121, 1680)
    versus("艾欧 中期", io, 269, 79, 1500)
    versus("艾欧 中期", io, 269, 79, 1200)
    versus("艾欧 后期", io, 254, 269, 1680)


main()
