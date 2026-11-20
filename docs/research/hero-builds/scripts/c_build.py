"""第 3 步：把抓到的对局整理成每个英雄（按位置）的出装路线，写成单文件网页。

    python -X utf8 docs/research/hero-builds/scripts/c_build.py

输出 data/research/hero-builds/builds.json、data/mockups/7.42-胜率前N英雄出装.html（N = top.json 里的英雄数）。
列「同分段的人怎么出」（购买比例 + 中位购买时间），排序不看胜率；胜率单独标出来（原始胜率 + 扣掉经济差和英雄强弱的「同局势」差，见 compare）。
"""
import base64
import gzip
import io
import math
import json
import site
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.append(site.getusersitepackages())
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from dota_assistant import kb  # noqa: E402
from dota_assistant.core.config import DATA_DIR  # noqa: E402

D = DATA_DIR / "research" / "hero-builds"
PAGE = Path(__file__).resolve().parents[1] / "page.html"

POS_MIN_N = 80            # 某个位置至少几个人次才出这一路
POS_MIN_SHARE = 0.15      # 且占这个英雄样本的比例
CORE = 0.30               # 买的人 ≥30% 算核心路线
SITU = 0.07               # 7%~30% 算看情况
BIG = 1800                # 大件门槛（金）
EARLY_MIN = 12            # 中位购买时间早于几分钟、又不到大件门槛的，算对线期小件
EARLY_SHARE = 0.15
AUTO_SEC = 90             # 中间件买了 90 秒内就合成成品（攒零件 / 买齐自动合成），不算单独一步
ROUTE_MIN = 40            # 一条路线至少几个人走过才列
ROUTE_MAX = 6
NTH_MIN = 30              # 前三件大件 / 鞋：某件至少几个人才列
DEEP_MIN = 300            # 做出三件大件的人够这么多
DEEP_SHARE = 0.5          # 且最常见的前两件占一半以上，路线就按前三件分
NW_BIN, NW_CLIP, MAX_MIN = 1000, 15, 60   # 局势应有胜率：分钟 × 全队经济差（每 1000 金一档，±15 档封顶），和 docs/research/item-vs 一样
EXTRA = {"blink", "aghanims_shard"}       # 本身不是合成、但算成件的
NAMES = {"ward_observer": "侦察守卫", "ward_sentry": "岗哨守卫", "ward_dispenser": "守卫组"}


def load_matches():
    ms = []
    try:
        with gzip.open(D / "fetched.jsonl.gz", "rt", encoding="utf-8") as f:
            for line in f:
                m = json.loads(line)
                if not m.get("missing") and all(p["parsed"] and p["pos"] for p in m["players"]):
                    ms.append(m)
    except (EOFError, OSError, json.JSONDecodeError):     # 抓取还在写 / 上次被杀：读到的都算
        pass
    return ms


class Items:
    def __init__(self):
        self.base = kb.KnowledgeBase.load()
        self.od = json.loads((DATA_DIR / "opendota_items.json").read_text(encoding="utf-8"))
        self.key_of = {v["id"]: k for k, v in self.od.items() if isinstance(v, dict) and v.get("id")}
        self.key_of.update({v: k.removeprefix("item_") for k, v in self.base.item_ids.items()})

    def info(self, k):
        e, o = self.base.items.get(k), self.od.get(k) or {}
        return {"name": e.name if e else NAMES.get(k, o.get("dname", k)),
                "cost": e.cost if e else o.get("cost") or 0,
                "neutral": bool(e and e.neutral_tier is not None),
                "consumable": o.get("qual") == "consumable" or k.startswith("recipe") or k in NAMES,
                "comps": o.get("components") or []}

    def built(self, k):
        i = self.info(k)
        return not (i["neutral"] or i["consumable"]) and (bool(i["comps"]) or k in EXTRA)

    def has_comp(self, k, c, depth=0):
        cs = self.info(k)["comps"]
        return depth < 5 and (c in cs or any(self.has_comp(x, c, depth + 1) for x in cs))

    def is_boots(self, k):
        return k != "boots" and self.has_comp(k, "boots")


def build_one(ps, it, expect):
    """一个英雄一个位置的全部人次 → 出装路线"""
    first, starts = [], Counter()
    for _, p in ps:
        fb, start = {}, Counter()
        buys = [(t, it.key_of[iid]) for t, iid in p["items"] if iid in it.key_of]
        for t, k in buys:
            if t <= 0 and not k.startswith("recipe"):
                start[k] += 1
            fb.setdefault(k, t)
        # 很快就合进成品的中间件（例如 7.42 鱼叉 = 回音战刃 + 魔龙枪，魔龙枪常在鱼叉前几秒到一分钟出现）不算单独一步
        for k, t in list(fb.items()):
            if any(0 <= t2 - t <= AUTO_SEC and k in it.info(k2)["comps"] for t2, k2 in buys if k2 != k):
                del fb[k]
        first.append(fb)
        starts[tuple(sorted(start.items()))] += 1
    nn = len(ps)
    times = {}
    for fb in first:
        for k, t in fb.items():
            times.setdefault(k, []).append(t)
    items = {k: {"k": k, "share": round(len(ts) / nn, 3), "min": round(statistics.median(ts) / 60, 1)}
             for k, ts in times.items() if it.built(k) and len(ts) / nn >= SITU}

    def big(k):
        return it.is_boots(k) or it.info(k)["cost"] >= BIG

    wins = [bool(p["win"]) for _, p in ps]
    boot_rows = []                 # (第一双升级鞋, 买的秒数, 赢没赢)
    def ex(t):
        return expect(m, pl, t)
    for (m, pl), fb, w in zip(ps, first, wins):
        bs = sorted((t, k) for k, t in fb.items() if it.is_boots(k) and it.built(k))
        if bs:
            boot_rows.append((bs[0][1], bs[0][0], w, ex(bs[0][0])))
    bigs_all = [[(k, t) for k, t in sorted(fb.items(), key=lambda kv: kv[1])
                 if it.built(k) and not it.is_boots(k) and it.info(k)["cost"] >= BIG] for fb in first]
    nth = []
    for i in range(3):             # 第 i+1 件大件是什么：比例按全部人次算，胜率只在做出了第 i+1 件的人里比
        rows = [(bg[i][0], bg[i][1], w, expect(m, pl, bg[i][1])) for (m, pl), bg, w in zip(ps, bigs_all, wins) if len(bg) > i]
        base, st = compare(rows, NTH_MIN)
        nth.append({"base": base, "items": [{**x, "share": round(x["n"] / nn, 3)} for x in st[:3]]})
    _, bst = compare(boot_rows, NTH_MIN)
    early = sorted((v for k, v in items.items() if not big(k) and v["min"] <= EARLY_MIN and v["share"] >= EARLY_SHARE),
                   key=lambda v: v["min"])
    route = sorted((v for k, v in items.items() if big(k) and v["share"] >= CORE), key=lambda v: v["min"])
    for v in route:      # 被后面某件核心装合进去的过渡件（例如 夜叉 → 幻影斧）
        v["into"] = next((w["k"] for w in route if w is not v and w["min"] >= v["min"] and it.has_comp(w["k"], v["k"])), None)
    situ = sorted((v for k, v in items.items() if big(k) and v["share"] < CORE), key=lambda v: -v["share"])[:10]
    st = [{"items": [[k, c] for k, c in s], "share": round(c / nn, 3)} for s, c in starts.most_common(2) if s]
    return {"n": nn, "win": round(sum(wins) / nn, 3), "start": st,
            "early": early, "route": route, "situ": situ,
            "boots": [{**x, "share": round(x["n"] / nn, 3)} for x in bst[:4]],
            "nth": nth, **paths(ps, bigs_all, wins, expect)}


def compare(rows, min_n):
    """rows = [(key, 做到这一步的秒数, 赢没赢, 局势应有胜率)] → (基准, [每个 key 的统计，按人数从多到少])。
    win = 这些人赢的比例（± ci 是 95% 区间）；adj =「同局势」：赢没赢 − 做完那一分钟两队经济差应有的胜率，取平均，
    再减掉同英雄同位置全部人的这个平均（英雄本身强不强）。做完大件的人本来多半领先、领先的人本来就赢得多，
    扣掉这部分才接近装备 / 路线本身比这个英雄别的出法好多少（还混着玩家水平）"""
    if len(rows) < min_n:
        return None, []
    mean_ex = sum(w - e for _, _, w, e in rows) / len(rows)
    groups = {}
    for key, t, w, e in rows:
        groups.setdefault(key, []).append((t, w, e))
    out = []
    for key, xs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        n = len(xs)
        if n < min_n:
            break
        pw = sum(w for _, w, _ in xs) / n
        out.append({"k": key, "n": n, "of": round(n / len(rows), 3), "win": round(pw, 4),
                    "ci": round(1.96 * math.sqrt(max(pw * (1 - pw), 0.01) / n), 4),
                    "adj": round(sum(w - e for _, w, e in xs) / n - mean_ex, 4),
                    "min": round(statistics.median(t for t, _, _ in xs) / 60, 1)})
    return {"n": len(rows), "win": round(sum(r[2] for r in rows) / len(rows), 4)}, out


def paths(ps, bigs_all, wins, expect):
    """出装路线 = 前两件大件（按顺序）；最常见的那条占了一半以上、做出三件的人又够多时（例如幻影刺客几乎都是
    狂战斧 → 黯灭），改成按前三件分，不然看不出差别。只在做完这几件的人里比。
    过渡件（夜叉 → 幻影斧）照买的顺序算一步，不按「之后有没有合成」去掉：那要看未来——没等到合成就输了的人
    会被分到另一条路线，试过一版，力丸「净魂之刃 → 夜叉」胜率 23%，全是这个假象"""
    for depth in (3, 2):
        rows = [(tuple(k for k, _ in bg[:depth]), bg[depth - 1][1], w, expect(m, pl, bg[depth - 1][1]))
                for (m, pl), bg, w in zip(ps, bigs_all, wins) if len(bg) >= depth]
        if depth == 3:
            two = Counter(tuple(k for k, _ in bg[:2]) for bg in bigs_all if len(bg) >= 2)
            if not (len(rows) >= DEEP_MIN and two and two.most_common(1)[0][1] / sum(two.values()) >= DEEP_SHARE):
                continue
        base, st = compare(rows, ROUTE_MIN)
        return {"paths": st[:ROUTE_MAX], "path_base": base, "depth": depth}
    return {"paths": [], "path_base": None, "depth": 2}


def situation(ms):
    """局势应有胜率：全部抓到的对局里，[核心 / 辅助, 分钟, 全队经济差档] → 胜率（样本少的格子往相邻档借，
    还少就当 50%）。返回 expect(match, player, 秒数)"""
    nb = 2 * NW_CLIP + 1
    st_n, st_w = np.zeros((2, MAX_MIN + 1, nb)), np.zeros((2, MAX_MIN + 1, nb))
    diffs = {}
    for m in ms:
        ps = m["players"]
        if not all(p.get("nw") for p in ps) or sum(p["rad"] for p in ps) != 5:
            continue
        L = min(len(p["nw"]) for p in ps)
        rad = np.sum([p["nw"][:L] for p in ps if p["rad"]], axis=0)
        dire = np.sum([p["nw"][:L] for p in ps if not p["rad"]], axis=0)
        diffs[m["id"]] = rad - dire
        for p in ps:
            d = (rad - dire) if p["rad"] else (dire - rad)
            dbin = np.clip(np.round(d / NW_BIN).astype(int), -NW_CLIP, NW_CLIP) + NW_CLIP
            mins = np.arange(min(L, MAX_MIN + 1))
            core = 0 if int(p["pos"][-1]) <= 3 else 1
            np.add.at(st_n[core], (mins, dbin[:len(mins)]), 1)
            if p["win"]:
                np.add.at(st_w[core], (mins, dbin[:len(mins)]), 1)
    sn = st_n + np.roll(st_n, 1, 2) + np.roll(st_n, -1, 2)
    sw = st_w + np.roll(st_w, 1, 2) + np.roll(st_w, -1, 2)
    with np.errstate(invalid="ignore", divide="ignore"):
        curve = np.where(sn >= 50, sw / sn, 0.5)

    def expect(m, p, t):
        d = diffs.get(m["id"])
        if d is None:
            return float(bool(p["win"]))           # 没经济数据：当作没偏差
        minute = max(0, min(int(t // 60), len(d) - 1, MAX_MIN))
        x = d[minute] if p["rad"] else -d[minute]
        b = int(np.clip(round(x / NW_BIN), -NW_CLIP, NW_CLIP)) + NW_CLIP
        return float(curve[0 if int(p["pos"][-1]) <= 3 else 1, minute, b])
    return expect


def keys_in(b):
    ks = {k for x in b["start"] for k, _ in x["items"]}
    ks |= {v["k"] for v in b["early"] + b["route"] + b["situ"]}
    ks |= {x["k"] for x in b["boots"]} | {x["k"] for col in b["nth"] for x in col["items"]}
    ks |= {k for pth in b["paths"] for k in pth["k"]}
    return ks


def main():
    top = json.loads((D / "top.json").read_text(encoding="utf-8"))
    it = Items()
    ms = load_matches()
    expect = situation(ms)
    ids = {h["id"] for h in top["heroes"]}
    players = {}
    for m in ms:
        for p in m["players"]:
            if p["h"] in ids:
                players.setdefault(p["h"], []).append((m, p))

    used, out = set(), []
    for h in top["heroes"]:
        rows = players.get(h["id"], [])
        bypos = Counter(int(p["pos"][-1]) for _, p in rows)
        hero = {**h, "samples": len(rows), "posn": {str(k): v for k, v in bypos.items()}, "builds": []}
        for pos, n in bypos.most_common():
            if n < POS_MIN_N or n / len(rows) < POS_MIN_SHARE:
                continue
            b = {"pos": pos, **build_one([(m, p) for m, p in rows if p["pos"] == f"POSITION_{pos}"], it, expect)}
            used |= keys_in(b)
            hero["builds"].append(b)
        out.append(hero)

    data = {"asof": top["asof"], "cut": top["cut"], "matches": len(ms), "made": int(time.time()),
            "heroes": out, "names": {k: it.info(k)["name"] for k in used},
            "costs": {k: it.info(k)["cost"] for k in used}}
    (D / "builds.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    render(data, used)
    for hh in out:
        for b in hh["builds"]:
            pb = b["path_base"] or {}
            print(f"{hh['rank']:>2} {hh['name']} {b['pos']}号位 {b['n']}人  做完前 {b['depth']} 件 {pb.get('n')}人 平均 {pb.get('win')}")
            for x in b["paths"]:
                print(f"     {' → '.join(data['names'][k] for k in x['k'])}  {x['n']}人 {x['of']:.0%}  "
                      f"胜率 {x['win']:.1%} ±{x['ci']:.1%}  同局势 {x['adj']:+.1%}  做完 {x['min']:.0f}分")


def icon_css(keys, folder, cls, size):
    css = []
    for k in sorted(keys, key=str):
        p = DATA_DIR / folder / f"{k}.png"
        if not p.exists():
            print("缺图标", p)
            continue
        im = Image.open(p).convert("RGB").resize(size, Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        css.append(f".{cls}-{k}{{background-image:url(data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()})}}")
    return "\n".join(css)


def render(data, used):
    page = PAGE.read_text(encoding="utf-8")
    css = icon_css(used, "items", "i", (66, 48)) + "\n" + icon_css([h["id"] for h in data["heroes"]], "portraits", "p", (128, 72))
    js = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = page.replace("/*ICONS*/", css).replace("/*DATA*/null", js)
    out = DATA_DIR / "mockups" / f"7.42-胜率前{len(data['heroes'])}英雄出装.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8", newline="\n")
    print(out, len(page.encode("utf-8")) // 1024, "KB")


if __name__ == "__main__":
    main()
