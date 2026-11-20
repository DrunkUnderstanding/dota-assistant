# 7.42 上线后每 3 小时看一次英雄胜率：拉 STRATZ 分小时数据攒进历史，按 3 小时一个点重算排名，写一个单文件网页
# 用法：python -X utf8 docs/research/patch-watch/watch.py [--seed 旧的分小时 json ...] [--no-fetch]
# --loop 常驻每 3 小时跑一次（见同目录 README.md）；历史 / 日志在 data/patch_watch/，网页写到 data/mockups/
import argparse
import base64
import calendar
import io
import json
import math
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

DIR = ROOT / "data" / "patch_watch"
HIST = DIR / "hours.json"
LOG = DIR / "watch.log"
OUT = ROOT / "data" / "mockups" / "7.42-英雄胜率走势.html"
CUT = calendar.timegm((2026, 11, 18, 21, 0, 0))   # 7.42 实际上线（重做英雄出场翻倍的那个小时，UTC）
STEP = 3 * 3600
STOP = CUT + 14 * 86400                           # 两周后不再拉数据（网页照样能看）
BRACKETS = {"all": "", "mid": ", bracketIds: [LEGEND, ANCIENT]", "hi": ", bracketIds: [DIVINE, IMMORTAL]"}


def log(msg):
    DIR.mkdir(parents=True, exist_ok=True)
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_hist():
    if HIST.exists():
        return json.loads(HIST.read_text(encoding="utf-8"))
    return {"hours": {b: {} for b in BRACKETS}, "fetches": []}


def merge(hist, fetched):
    """fetched: {档: [{hour, heroId, matchCount, winCount}]}。同一个小时只在新数据场次更多时替换：
    窗口最前面那个小时只拉到一部分、最后那个小时还没入库完，场次都偏少"""
    changed = 0
    for b, rows in fetched.items():
        new = {}
        for x in rows:
            new.setdefault(str(x["hour"]), {})[str(x["heroId"])] = [x["matchCount"], x["winCount"]]
        old = hist["hours"].setdefault(b, {})
        for h, heroes in new.items():
            if sum(v[0] for v in heroes.values()) >= sum(v[0] for v in old.get(h, {}).values()):
                old[h] = heroes
                changed += 1
    return changed


def fetch():
    from dota_assistant.core import config
    from dota_assistant.sources import http
    cfg = config.load()
    if not cfg.stratz_token:
        raise SystemExit("config.toml 里没有 STRATZ token")
    out = {}
    for b, arg in BRACKETS.items():
        r = http._stratz(cfg, "{ heroStats { winHour(take: 48%s) { hour heroId winCount matchCount } } }" % arg)
        out[b] = r["heroStats"]["winHour"]
    return out


def poslist(pos, hid):
    p = pos.get(str(hid)) or {}
    tot = sum(v[0] for v in p.values()) or 1
    ps = sorted(p.items(), key=lambda kv: -kv[1][0])
    return [int(k) for k, v in ps if v[0] / tot >= 0.2] or ([int(ps[0][0])] if ps else [])


def add(acc, heroes):
    for hid, (m, w) in heroes.items():
        a = acc.setdefault(hid, [0, 0])
        a[0] += m
        a[1] += w


GROUPS = {"lg": ("mid", "hi"), "hi": ("hi",)}   # 网页上的段位筛选：传奇以上 / 超凡以上


def pair(acc, group, hid):
    m = sum(acc[b].get(hid, [0, 0])[0] for b in group)
    return m, sum(acc[b].get(hid, [0, 0])[1] for b in group)


def estimate(pre, post, hid, group):
    """和版本解读页同一个算法：先验 = 上线前本段位胜率 + 全分段涨跌（按样本量收缩），再用上线后本段位实测更新"""
    a0, a1 = pre["all"].get(hid), post["all"].get(hid)
    (n0, v0), (n1, v1) = pair(pre, group, hid), pair(post, group, hid)
    if not (a0 and a1 and a0[0] and a1[0] and n0 and n1):
        return None
    w0, w1 = v0 / n0, v1 / n1
    d = a1[1] / a1[0] - a0[1] / a0[0]
    k = 0.25 / 0.03 ** 2
    prior, tau2 = w0 + d * a1[0] / (a1[0] + k), 0.02 ** 2
    se2 = max(w1 * (1 - w1), 0.01) / n1
    est = (prior / tau2 + w1 / se2) / (1 / tau2 + 1 / se2)
    return est, math.sqrt(1 / (1 / tau2 + 1 / se2)), n1


def compute(hist, now):
    H = {b: {int(h): v for h, v in hist["hours"][b].items()} for b in BRACKETS}
    done = [h for h in H["all"] if h + 3600 <= now - 600 and all(h in H[b] for b in BRACKETS)]
    last_end = max(done) + 3600
    times = list(range(CUT + STEP, last_end + 1, STEP))
    if not times or times[-1] != last_end:
        times.append(last_end)
    pre = {b: {} for b in BRACKETS}
    for b in BRACKETS:
        for h, heroes in H[b].items():
            if CUT - 48 * 3600 <= h < CUT:
                add(pre[b], heroes)
    pre_slots = sum(v[0] for v in pre["all"].values())

    names = {h["id"]: h["name_loc"] for h in json.loads((DIR / "herolist_schinese.json").read_text(encoding="utf-8"))["result"]["data"]["heroes"]}
    pos = json.loads((ROOT / "data" / "positions_r7-8.json").read_text(encoding="utf-8"))["heroes"]
    ids = sorted(int(h) for h in pre["all"] if int(h) in names)
    heroes = {}
    for hid in ids:
        r = heroes[hid] = {"id": hid, "name": names[hid], "pos": poslist(pos, hid), "pk": [],
                           "prepk": round(pre["all"].get(str(hid), [0])[0] * 10 / pre_slots, 4), "b": {}}
        for g, group in GROUPS.items():
            n, v = pair(pre, group, str(hid))
            r["b"][g] = {"est": [], "sd": [], "n": [], "act": [], "ww": [], "wn": [], "pre": round(v / n, 4) if n else None}

    post = {b: {} for b in BRACKETS}
    totals, start = [], CUT
    for t in times:
        win = {b: {} for b in BRACKETS}
        for b in BRACKETS:
            for hh in range(start, t, 3600):
                if hh in H[b]:
                    add(post[b], H[b][hh])
                    add(win[b], H[b][hh])
        wslots = sum(v[0] for v in win["all"].values()) or 1
        totals.append({"all": sum(v[0] for v in post["all"].values()) // 10,
                       **{g: sum(v[0] for b in group for v in post[b].values()) // 10 for g, group in GROUPS.items()},
                       "win": wslots // 10, "hours": (t - start) // 3600})
        for hid in ids:
            s, r = str(hid), heroes[hid]
            r["pk"].append(round(win["all"].get(s, [0])[0] * 10 / wslots, 4))
            for g, group in GROUPS.items():
                e, x = estimate(pre, post, s, group), r["b"][g]
                x["est"].append(round(e[0], 4) if e else None)
                x["sd"].append(round(e[1], 4) if e else None)
                n, v = pair(post, group, s)        # 上线后累计实测（没平滑）
                x["n"].append(n)
                x["act"].append(round(v / n, 4) if n else None)
                n, v = pair(win, group, s)
                x["wn"].append(n)
                x["ww"].append(round(v / n, 4) if n else None)
        start = t
    return {"cut": CUT, "times": times, "totals": totals, "heroes": list(heroes.values()),
            "pre_games": pre_slots // 10, "updated": now, "error": hist.get("last_error")}


def portraits(ids):
    import site
    sys.path.append(site.getusersitepackages())
    from PIL import Image
    css = []
    for hid in ids:
        p = ROOT / "data" / "portraits" / f"{hid}.png"
        if not p.exists():
            continue
        im = Image.open(p).convert("RGB").resize((64, 36), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82)
        css.append(f".p{hid}{{background-image:url(data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()})}}")
    return "\n".join(css)


def render(data):
    page = (Path(__file__).with_name("page.html")).read_text(encoding="utf-8")
    js = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = page.replace("/*PORTRAITS*/", portraits([h["id"] for h in data["heroes"]])).replace("/*DATA*/null", js)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(page, encoding="utf-8", newline="\n")
    tmp.replace(OUT)
    return len(page.encode("utf-8"))


def once(a):
    DIR.mkdir(parents=True, exist_ok=True)
    hist = load_hist()
    for f in a.seed:
        n = merge(hist, json.loads(Path(f).read_text(encoding="utf-8")))
        log(f"并入 {f}：{n} 个小时")
    if not (DIR / "herolist_schinese.json").exists():
        raise SystemExit(f"缺 {DIR / 'herolist_schinese.json'}（Valve datafeed 的英雄中文名表）")
    now = int(time.time())
    if not a.no_fetch and now < STOP:
        try:
            n = merge(hist, fetch())
            hist["fetches"].append(now)
            hist["last_error"] = None
            log(f"拉到数据，更新 {n} 个小时")
        except Exception as e:        # 断网 / STRATZ 出错：记下来，网页照样用旧数据重画
            hist["last_error"] = f"{time.strftime('%m-%d %H:%M')} 拉数据失败：{str(e)[:200]}"
            log(hist["last_error"] + "\n" + traceback.format_exc())
    HIST.write_text(json.dumps(hist, separators=(",", ":")), encoding="utf-8")
    data = compute(hist, now)
    size = render(data)
    t = data["totals"][-1]
    log(f"网页 {OUT.name} {size // 1024} KB；{len(data['times'])} 个点，上线后 {t['all']} 局（传奇以上 {t['lg']}，超凡以上 {t['hi']}）")


def next_run(now):
    """每个 3 小时整点（北京时间 8/11/14/17/20/23/2/5 点）过 1 小时 40 分跑：STRATZ 的分小时数据要晚 1 个多小时才入库"""
    lag = 100 * 60
    return CUT + (math.floor((now - CUT - lag) / STEP) + 1) * STEP + lag


def loop(a):
    """常驻：跑一次，睡到下一个时间点再跑，两周后自己退出。同时只留一个（锁文件）"""
    import msvcrt
    DIR.mkdir(parents=True, exist_ok=True)
    lock = open(DIR / "loop.lock", "w", encoding="utf-8")
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        log("已经有一个监控在跑，这个退出")
        return
    log("监控启动")
    while True:
        try:
            once(a)
        except Exception:
            log("这一轮出错：" + traceback.format_exc())
        a.seed = []
        t = next_run(time.time())
        if t > STOP + STEP:
            log("上线满两周，监控退出")
            return
        log("下次 " + time.strftime("%m-%d %H:%M", time.localtime(t)))
        while time.time() < t:          # 按墙上时间小段睡，电脑睡眠醒来后不会错过
            time.sleep(min(60, max(1, t - time.time())))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", nargs="*", default=[], help="之前拉过的分小时 json（{档: [行]}），先并进历史")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--loop", action="store_true", help="常驻，每 3 小时跑一次")
    a = ap.parse_args()
    (loop if a.loop else once)(a)


if __name__ == "__main__":
    main()
