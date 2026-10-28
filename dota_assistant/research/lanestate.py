"""对线优劣：3:00 / 6:00 这一刻能不能猜出这条路对线是优是劣、能猜多准，以及屯野 / 抢莲花和对线结果的关系。

玩家（2026-10-26）：「屯野和抢莲花都可以亮，是两种不同的选择而已，如果要说具体情况应该按照当时对线的优势劣势情况来分配」；
判断优劣看「杀敌死亡次数、等级差这种」，要用数据验证。三步：
1. 回测（lane-test）：样本外对局，只用 3:00 / 6:00 时的击杀 / 死亡 / 等级（自己的 + 本路两边的）猜这条路的 STRATZ 对线结果
2. 同一批对局：按「当时看着」的优劣分组，看屯了野 / 拿了莲花的辅助对线结果和胜率（只是相关，不是因果）
3. 实战记录：live.py 在 3:00 / 6:00 / 10:00 把自己的 GSI 数据记进 data/lane_log.jsonl，赛后 lane-review 对照 STRATZ

数据（STRATZ match，2026-10-26 实查字段含义）：
- 对线结果：topLaneOutcome / midLaneOutcome / bottomLaneOutcome（RADIANT_VICTORY / RADIANT_STOMP / TIE / DIRE_…）
- stats.level：每升一级的时间（秒，1 级是 -89 左右）；killEvents / deathEvents / assistEvents 的 time
- stats.networthPerMinute：第 i 个 = 第 i 分钟；campStack：到第 i 分钟累计屯了几组（1:53 那组记在第 2 个）
- stats.inventoryReport：第 i 个 = 第 i-2 分钟的背包（3:00 / 6:00 / 9:00 长的莲花第一次出现在第 5 / 8 / 11 个，按这个推的）；
  拿了马上吃掉的莲花背包里看不到，itemUsed 只有整局用了几次、没有时间
"""
from __future__ import annotations

import json
import random
from collections import Counter
from dataclasses import dataclass

import numpy as np

from ..core.config import DATA_DIR, Config
from ..domain import lanelog
from ..sources import http
from ..sources import stats as stats_mod

CACHE = DATA_DIR / "lane_matches.json"
REPORT = DATA_DIR / "lane_report.txt"
BATCH = 10                   # 一次 GraphQL 请求查几场（每场数据不小）
UNTIL = 12                   # 只存前 12 分钟
INV_OFFSET = 2
FAMANGOS = {4204, 4205, 4206}  # 治疗莲花 / 大 / 特大（OpenDota 常量 famango / great_famango / greater_famango）
MINUTES = (3, 6)
CLASSES = (-1, 0, 1)
CLASS_NAMES = {-1: "劣", 0: "平", 1: "优"}
SLOTS = ("item0", "item1", "item2", "item3", "item4", "item5", "backPack0", "backPack1", "backPack2")
_FIELDS = ("id didRadiantWin topLaneOutcome midLaneOutcome bottomLaneOutcome "
           "players { heroId isRadiant position steamAccountId stats { level killEvents { time } deathEvents { time } "
           "assistEvents { time } networthPerMinute campStack itemUsed { itemId count } "
           "inventoryReport { " + " ".join(f"{s} {{ itemId }}" for s in SLOTS) + " } } }")


# ---------- 取数据 ----------

def compact(m: dict | None) -> dict | None:
    """STRATZ 一场 → 只留前 UNTIL 分钟要用的；没解析完（没有对线结果 / 每分钟数据）的返回 None"""
    if not m or m.get("didRadiantWin") is None:
        return None
    lanes = {"top": m.get("topLaneOutcome"), "mid": m.get("midLaneOutcome"), "bot": m.get("bottomLaneOutcome")}
    if not all(lanes.values()):
        return None
    players = []
    end = UNTIL * 60
    for p in m.get("players") or []:
        s, pos = p.get("stats") or {}, p.get("position") or ""
        if not pos.startswith("POSITION_") or not s.get("networthPerMinute") or not s.get("level"):
            return None
        inv = s.get("inventoryReport") or []
        players.append({
            "hero": p["heroId"], "radiant": p["isRadiant"], "pos": int(pos[-1]), "account": p.get("steamAccountId"),
            "lv": [t for t in s["level"] if t <= end],
            **{k: [e["time"] for e in s.get(f) or [] if e["time"] <= end]
               for k, f in (("k", "killEvents"), ("d", "deathEvents"), ("a", "assistEvents"))},
            "nw": s["networthPerMinute"][:UNTIL + 1],
            "stack": (s.get("campStack") or [])[:UNTIL + 1],
            "lotus_used": sum(u["count"] for u in s.get("itemUsed") or [] if u["itemId"] in FAMANGOS),
            "lotus_inv": [i - INV_OFFSET for i, r in enumerate(inv[:UNTIL + INV_OFFSET + 1])
                          if any(v and v["itemId"] in FAMANGOS for v in (r or {}).values())],
        })
    if len(players) != 10:
        return None
    return {"id": m["id"], "radiant_win": m["didRadiantWin"], "lanes": lanes, "players": players}


def fetch(cfg: Config, n: int, log=print) -> list[dict]:
    """对局 id 用选人回测那批（backtest.fetch_matches，超凡～冠绝、统计数据之后的样本外对局），逐批去 STRATZ 取前 12 分钟。
    缓存可以接着取（中途断了再跑一次就行）"""
    from . import backtest
    stats = stats_mod.load_stats(cfg)
    if not (stats.get("hi_id") and cfg.stratz_token):
        raise RuntimeError("需要统计数据（update）和 STRATZ token")
    ids = [m["id"] for m in backtest.fetch_matches(cfg, stats["hi_id"], n, log)]
    cached = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {"tried": [], "matches": []}
    tried = set(cached["tried"])
    todo = [i for i in ids if i not in tried]
    log(f"  对局 {len(ids)} 场，缓存里有 {len(cached['matches'])} 场可用，还要取 {len(todo)} 场 …")
    for k in range(0, len(todo), BATCH):
        chunk = todo[k:k + BATCH]
        q = "{ " + " ".join(f"m{j}: match(id: {mid}) {{ {_FIELDS} }}" for j, mid in enumerate(chunk)) + " }"
        try:
            res = http._stratz(cfg, q)
        except Exception as e:                   # 个别批次失败不影响整体，下次再取
            log(f"  第 {k // BATCH + 1} 批失败：{e}")
            continue
        cached["matches"] += [c for c in map(compact, res.values()) if c]
        cached["tried"] += chunk
        if (k // BATCH) % 20 == 0 or k + BATCH >= len(todo):
            log(f"  已取 {min(k + BATCH, len(todo))}/{len(todo)}，可用 {len(cached['matches'])}")
            CACHE.write_text(json.dumps(cached), encoding="utf-8")
    CACHE.write_text(json.dumps(cached), encoding="utf-8")
    keep = set(ids)
    return [m for m in cached["matches"] if m["id"] in keep]


# ---------- 特征：某个人在第几分钟「看得见的」 ----------

def lane_of(pos: int, radiant: bool) -> str:
    """天辉优势路在下、夜魇优势路在上"""
    if pos == 2:
        return "mid"
    safe = pos in (1, 5)
    return ("bot" if safe else "top") if radiant else ("top" if safe else "bot")


def outcome(lanes: dict, lane: str, radiant: bool) -> int:
    o = lanes[lane]
    return 0 if o == "TIE" else (1 if o.startswith("RADIANT") == radiant else -1)


def _n(ts, sec) -> int:
    return sum(1 for t in ts if t <= sec)


@dataclass
class Row:
    match: int
    pos: int
    minute: int
    f: dict[str, float]          # 特征
    y: int                       # 这条路最终对线结果（-1 劣 / 0 平 / 1 优，自己这边看）
    win: bool
    stacked: bool                # (minute-1):53 那组屯了没有
    lotus: bool                  # minute:00 那朵莲花在背包里见到了（马上吃掉的看不到）
    lotus_used: int
    hero: int = 0


def rows(matches: list[dict], minute: int) -> list[Row]:
    sec = minute * 60
    out = []
    for m in matches:
        ps = m["players"]
        lane = {id(p): lane_of(p["pos"], p["radiant"]) for p in ps}
        for p in ps:
            mates = [q for q in ps if q["radiant"] == p["radiant"] and lane[id(q)] == lane[id(p)]]
            foes = [q for q in ps if q["radiant"] != p["radiant"] and lane[id(q)] == lane[id(p)]]
            if not foes or len(p["nw"]) <= minute:
                continue
            lv = lambda q: _n(q["lv"], sec)
            f = {
                "自己死亡": _n(p["d"], sec),
                "自己击杀+助攻": _n(p["k"], sec) + _n(p["a"], sec),
                "自己等级": lv(p),
                "本路击杀差": sum(_n(q["k"], sec) - _n(q["d"], sec) for q in mates),
                "本路等级差": np.mean([lv(q) for q in mates]) - np.mean([lv(q) for q in foes]),
                "本路经济差": (np.mean([q["nw"][minute] for q in mates]) - np.mean([q["nw"][minute] for q in foes])) / 100,
            }
            st = p["stack"]
            out.append(Row(m["id"], p["pos"], minute, f, outcome(m["lanes"], lane[id(p)], p["radiant"]),
                           m["radiant_win"] == p["radiant"],
                           len(st) > minute and st[minute] > st[minute - 1], minute in p["lotus_inv"], p["lotus_used"], p["hero"]))
    return out


# ---------- 多分类逻辑回归（numpy，不引 sklearn） ----------

OWN_SET = "只看自己（游戏接口拿得到：击杀 / 死亡 / 助攻 / 等级）"
LANE_SET = "+ 本路击杀差（队友的击杀 / 死亡也要看计分板）"
FEATURE_SETS = {
    "只看自己（游戏接口拿得到：击杀 / 死亡 / 助攻 / 等级）": ["自己死亡", "自己击杀+助攻", "自己等级"],
    "+ 本路等级差（对面等级要从顶栏 / 计分板看）": ["自己死亡", "自己击杀+助攻", "自己等级", "本路等级差"],
    "+ 本路击杀差（队友的击杀 / 死亡也要看计分板）": ["自己死亡", "自己击杀+助攻", "自己等级", "本路等级差", "本路击杀差"],
    "参考上限：再加本路经济差": ["自己死亡", "自己击杀+助攻", "自己等级", "本路等级差", "本路击杀差", "本路经济差"],
}


def _matrix(rs: list[Row], feats: list[str]) -> np.ndarray:
    pos = np.array([[r.pos == k for k in (1, 2, 3, 4, 5)] for r in rs], dtype=float)
    return np.hstack([np.array([[r.f[k] for k in feats] for r in rs], dtype=float), pos])


class Model:
    def __init__(self, feats: list[str]):
        self.feats = feats

    def fit(self, rs: list[Row], iters: int = 400, lr: float = 0.5, l2: float = 1e-3) -> Model:
        X = _matrix(rs, self.feats)
        self.mu, self.sd = X.mean(0), X.std(0) + 1e-9
        X = np.hstack([(X - self.mu) / self.sd, np.ones((len(X), 1))])
        Y = np.eye(3)[[CLASSES.index(r.y) for r in rs]]
        self.W = np.zeros((X.shape[1], 3))
        for _ in range(iters):
            P = self._softmax(X @ self.W)
            self.W -= lr * (X.T @ (P - Y) / len(X) + l2 * self.W)
        return self

    @staticmethod
    def _softmax(z):
        z = np.exp(z - z.max(1, keepdims=True))
        return z / z.sum(1, keepdims=True)

    def proba(self, rs: list[Row]) -> np.ndarray:
        X = (_matrix(rs, self.feats) - self.mu) / self.sd
        return self._softmax(np.hstack([X, np.ones((len(X), 1))]) @ self.W)


def cross_proba(rs: list[Row], feats: list[str]) -> np.ndarray:
    """两折：按对局一分为二，各用另一半训练，每行的概率都是样本外的"""
    ids = sorted({r.match for r in rs})
    random.Random(0).shuffle(ids)
    half = set(ids[: len(ids) // 2])
    a = [i for i, r in enumerate(rs) if r.match in half]
    b = [i for i, r in enumerate(rs) if r.match not in half]
    out = np.zeros((len(rs), 3))
    for train, test in ((a, b), (b, a)):
        m = Model(feats).fit([rs[i] for i in train])
        out[test] = m.proba([rs[i] for i in test])
    return out


# ---------- 报告 ----------

def _pct(x, n) -> str:
    return f"{x / n:4.0%}" if n else "  - "


def _dist(rs: list[Row]) -> str:
    n = len(rs)
    c = Counter(r.y for r in rs)
    return f"{n:6d} 人 · 优 {_pct(c[1], n)} 平 {_pct(c[0], n)} 劣 {_pct(c[-1], n)}"


def _bucket_table(rs: list[Row], key: str, edges: list[tuple[str, callable]]) -> list[str]:
    out = []
    for label, pred in edges:
        sub = [r for r in rs if pred(r.f[key])]
        if sub:
            out.append(f"    {label:>8}  {_dist(sub)}")
    return out


BUCKETS = {
    "自己死亡": [("0", lambda v: v == 0), ("1", lambda v: v == 1), ("≥2", lambda v: v >= 2)],
    "本路击杀差": [("≤-2", lambda v: v <= -2), ("-1", lambda v: v == -1), ("0", lambda v: v == 0),
                 ("+1", lambda v: v == 1), ("≥+2", lambda v: v >= 2)],
    "本路等级差": [("≤-1", lambda v: v <= -1), ("-0.5", lambda v: -1 < v < 0), ("0", lambda v: v == 0),
                 ("+0.5", lambda v: 0 < v < 1), ("≥+1", lambda v: v >= 1)],
}


def evaluate(rs: list[Row]) -> list[str]:
    out = []
    y = np.array([r.y for r in rs])
    base = Counter(y).most_common(1)[0]
    out.append(f"  全部 {_dist(rs)}；一直猜「{CLASS_NAMES[base[0]]}」能对 {base[1] / len(y):.0%}")
    for name, feats in FEATURE_SETS.items():
        P = cross_proba(rs, feats)
        pred = np.array(CLASSES)[P.argmax(1)]
        conf = P.max(1) >= 0.6
        lose = P[:, 0]
        cal = []
        for lo, hi in ((0, .2), (.2, .4), (.4, .6), (.6, 1.01)):
            sel = (lose >= lo) & (lose < hi)
            if sel.sum():
                cal.append(f"{lo:.0%}~{min(hi, 1):.0%}：{sel.sum()} 人实际劣 {np.mean(y[sel] == -1):.0%}")
        out.append(f"  {name}")
        out.append(f"    猜对 {np.mean(pred == y):.0%}；把握 ≥60% 的占 {conf.mean():.0%}，其中猜对 "
                   f"{np.mean(pred[conf] == y[conf]) if conf.any() else 0:.0%}")
        out.append(f"    猜「劣」的概率分档 → {'；'.join(cal)}")
    return out


def choices(rs: list[Row], P: np.ndarray) -> list[str]:
    """三 / 四 / 五号位：按当时看着的优劣（模型猜的）分组，屯了野 / 拿了莲花的对线结果和胜率"""
    out = []
    state = np.array(CLASSES)[P.argmax(1)]
    for s in CLASSES:
        sub = [(r, p) for r, p, st in zip(rs, P, state) if st == s and r.pos in (3, 4, 5)]
        if not sub:
            continue
        out.append(f"  当时看着「{CLASS_NAMES[s]}」（{len(sub)} 人）")
        groups = (("屯了野", lambda r: r.stacked and not r.lotus), ("拿了莲花", lambda r: r.lotus and not r.stacked),
                  ("两个都做了", lambda r: r.stacked and r.lotus), ("都没有", lambda r: not r.stacked and not r.lotus))
        for label, pred in groups:
            g = [r for r, _ in sub if pred(r)]
            if g:
                wins = sum(r.win for r in g)
                out.append(f"    {label:<6} {_dist(g)} · 比赛胜率 {_pct(wins, len(g))}")
    return out


def report(matches: list[dict]) -> str:
    lines = [f"对线优劣回测：{len(matches)} 场样本外对局（超凡～冠绝，STRATZ 对线结果；两折交叉，概率都是样本外的）",
             "这是相关性：会玩的人往往两样都做得好，屯野 / 莲花那部分不能直接当成「这么做就更好」。"]
    for minute in MINUTES:
        rs = rows(matches, minute)
        lines += ["", f"===== {minute}:00 ====="]
        for key, edges in BUCKETS.items():
            lines.append(f"  按{key}：")
            lines += _bucket_table(rs, key, edges)
        lines += evaluate(rs)
        P = cross_proba(rs, FEATURE_SETS[LANE_SET])
        sup = [r for r in rs if r.pos in (3, 4, 5)]
        used = [r for r in sup if r.lotus_used]
        lines += ["", f"  屯野 / 莲花（三 / 四 / 五号位；{minute - 1}:53 屯了没有、{minute}:00 那朵莲花在背包里见到没有）",
                  f"  覆盖率：整局用过莲花的 {len(used)} 人里，{minute}:00 背包里见到的 {sum(r.lotus for r in used)} 人"
                  "（拿了马上吃的看不到，所以「没拿莲花」那组里混着拿了的）"]
        lines += choices(rs, P)
    return "\n".join(lines)


def run(cfg: Config, n: int = 3000, log=print) -> str:
    matches = fetch(cfg, n, log)
    log(f"  可用 {len(matches)} 场，开始分析 …")
    text = report(matches)
    REPORT.write_text(text, encoding="utf-8")
    return text


# ---------- 实战记录：live 在 3:00 / 6:00 / 10:00 记一笔（domain/lanelog.py），赛后对照 ----------

def own_features(rec: dict) -> dict | None:
    """实战记录里自己的 GSI 数据 → 「只看自己」那组特征；字段名按 Dota2GSI 的解析代码，真实推送里没有就是 None"""
    p, h = rec.get("player") or {}, rec.get("hero") or {}
    if p.get("kills") is None or h.get("level") is None:
        return None
    return {"自己死亡": p.get("deaths") or 0, "自己击杀+助攻": (p.get("kills") or 0) + (p.get("assists") or 0),
            "自己等级": h["level"]}


def _models(matches: list[dict]) -> dict[tuple[int, str], Model]:
    out = {}
    for minute in MINUTES:
        rs = rows(matches, minute)
        for key in (OWN_SET, LANE_SET):
            out[(minute, key)] = Model(FEATURE_SETS[key]).fit(rs)
    return out


def _guess(model: Model, f: dict, pos: int) -> str:
    p = model.proba([Row(0, pos, 0, f, 0, False, False, False, 0)])[0]
    i = int(p.argmax())
    return f"猜{CLASS_NAMES[CLASSES[i]]} {p[i]:.0%}（劣 {p[0]:.0%} / 平 {p[1]:.0%} / 优 {p[2]:.0%}）"


def review(cfg: Config, log=print) -> str:
    """把 data/lane_log.jsonl 里记下的对局去 STRATZ 取赛后数据，逐个时刻对照：
    当时只看自己能猜成什么、看全本路击杀 / 等级能猜成什么、实际对线结果、屯了野 / 拿了莲花没有"""
    if not lanelog.LOG_FILE.exists():
        return f"还没有实战记录（{lanelog.LOG_FILE}）：开着助手打一局，3:00 / 6:00 / 10:00 会自动记"
    recs: dict[str, list[dict]] = {}
    for line in lanelog.LOG_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            recs.setdefault(str(r.get("match")), []).append(r)
    cached = json.loads(CACHE.read_text(encoding="utf-8"))["matches"] if CACHE.exists() else []
    models = _models(cached) if cached else {}
    out = []
    for mid, rs in recs.items():
        head = rs[0]
        title = f"比赛 {mid} · {head.get('hero_name') or head.get('hero_key')} · {head.get('pos')} 号位"
        if not mid.isdigit() or mid == "0":
            out.append(f"{title}：本地 / 机器人局，STRATZ 查不到")
            continue
        m = None
        if cfg.stratz_token:
            try:
                m = compact(http._stratz(cfg, f"{{ match(id: {mid}) {{ {_FIELDS} }} }}")["match"])
            except Exception as e:
                out.append(f"{title}：STRATZ 没取到（{e}）")
                continue
        if m is None:
            out.append(f"{title}：STRATZ 还没解析完，过一阵再看")
            continue
        me = next((p for p in m["players"] if cfg.account_id and p.get("account") == cfg.account_id), None) \
            or next((p for p in m["players"] if p["hero"] == head.get("hero_id")), None)
        if me is None:
            out.append(f"{title}：对局里找不到你")
            continue
        lane = lane_of(head.get("pos") or me["pos"], me["radiant"])
        y = outcome(m["lanes"], lane, me["radiant"])
        out.append(f"{title} · {'赢' if m['radiant_win'] == me['radiant'] else '输'} · 对线结果：{CLASS_NAMES[y]}"
                   f"（STRATZ {m['lanes'][lane]}）")
        for r in sorted(rs, key=lambda r: r.get("minute", 0)):
            minute = r.get("minute")
            row = next((x for x in rows([m], minute) if x.hero == me["hero"]), None) if minute in MINUTES else None
            p, h = r.get("player") or {}, r.get("hero") or {}
            line = (f"  {minute}:00 你：击杀 {p.get('kills', '?')} 死亡 {p.get('deaths', '?')} 助攻 {p.get('assists', '?')} "
                    f"等级 {h.get('level', '?')}")
            if r.get("hp") is not None:
                line += f" 血 {r['hp']:.0%} 蓝 {r['mana']:.0%}" if r.get("mana") is not None else f" 血 {r['hp']:.0%}"
            if r.get("cue"):
                line += f" · 当时提醒「{r['cue']}」"
            out.append(line)
            if row is None:
                continue
            own = own_features(r)
            if own and (minute, OWN_SET) in models:
                out.append(f"    只看自己：{_guess(models[(minute, OWN_SET)], own, me['pos'])}")
            if (minute, LANE_SET) in models:
                out.append(f"    看全本路（击杀差 {row.f['本路击杀差']:+d}、等级差 {row.f['本路等级差']:+.1f}）："
                           f"{_guess(models[(minute, LANE_SET)], row.f, me['pos'])}")
            out.append(f"    实际：{minute - 1}:53 屯野 {'✓' if row.stacked else '✗'} · {minute}:00 莲花 "
                       f"{'✓' if row.lotus else '背包里没见到'}")
    if not models:
        out.append("（还没跑过 lane-test，没有模型，只列数据）")
    return "\n".join(out)
