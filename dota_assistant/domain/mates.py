"""队友战绩（独立页 web/mates.html）：玩家手动填好友代码，查对方最近的战绩；点一局看那一整局 10 个人的数据。

数据：OpenDota（资料、最近 100 局、一起打过的人、整局详情、每个英雄的同英雄分位）；填了 STRATZ token 再加最近 100 局天梯的位置。
只用公开接口，和在游戏里看他的比赛记录、去 OpenDota 网站上看到的一样；对方没公开比赛数据就查不到。
「表现分」：这局的经济、经验、击杀、助攻、死亡（越少越好）、正补、英雄伤害、建筑伤害各自在同英雄所有对局里排百分之几（OpenDota benchmarks），
取平均 × 100。和同英雄比，所以辅助英雄不会因为经济低吃亏；但同一个英雄打不同位置（比如四号位小小）会被一起比。
「STRATZ 评分」是附带的（玩家 2026-10-28：可选，没有就算了）：填了 token、STRATZ 也解析了那局，就在旁边多显示它的 imp（0 = 平均）、
MVP / 最佳核心 / 最佳辅助、它判的位置；没 token、没解析、STRATZ 出错都只是不显示，不影响上面的表现分。
查过的人记在 data/mates.json，看过的整局存 data/matches/，英雄分位存 data/benchmarks/（都是个人数据 / 缓存，不打包）。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from ..core.config import DATA_DIR, Config
from ..sources import assets, http, player

log = logging.getLogger(__name__)

MATES_FILE = DATA_DIR / "mates.json"
MATCH_DIR = DATA_DIR / "matches"
BENCH_DIR = DATA_DIR / "benchmarks"
KEEP = 8                         # 最多记几个人（新查的排最前，挤掉最久没查的）
FRESH = 20 * 60                  # 20 分钟内查过的再填一次不重查（点「刷新」才重查）
SAMPLE = 100                     # 常玩英雄 / 位置按最近多少局算
RECENT = 20                      # 列表里显示最近几局
TOP_HEROES = 6
MATCH_KEEP = 200                 # 整局详情最多存几局
BENCH_DAYS = 7                   # 英雄分位多久重新拉
STRATZ_RETRY = 30 * 60           # 一局 STRATZ 说还没解析，多久以后再问
STEAM64_BASE = 76561197960265728  # Steam 64 位 ID = 这个 + 好友代码
ANONYMOUS = 4294967295            # 没公开比赛数据的人在整局详情里的 account_id

# 表现分用哪几项（OpenDota benchmarks 的字段名 → 显示名）；死亡越少越好，算分时反过来
SCORE_PARTS = {"gold_per_min": "经济", "xp_per_min": "经验", "kills_per_min": "击杀", "assists_per_min": "助攻",
               "deaths_per_min": "死亡少", "last_hits_per_min": "正补", "hero_damage_per_min": "英雄伤害",
               "tower_damage": "建筑伤害"}
GAME_MODES = {1: "全英雄选择", 22: "全英雄选择", 2: "队长模式", 3: "随机征召", 4: "单一征召", 5: "全体随机",
              16: "队长征召", 18: "技能征召", 23: "加速模式", 20: "全英雄随机死亡竞赛", 21: "中路单挑"}
MATCH_FIELDS = ["hero_id", "player_slot", "radiant_win", "start_time", "duration", "game_mode", "lobby_type",
                "party_size", "kills", "deaths", "assists", "last_hits", "denies", "gold_per_min", "xp_per_min",
                "hero_damage", "tower_damage", "hero_healing", "level", "leaver_status"]


def parse_code(text) -> int:
    """好友代码：纯数字；也认 Steam 64 位 ID、OpenDota / STRATZ / Dotabuff 的个人页链接（取里面最长的那串数字）"""
    nums = re.findall(r"\d+", str(text or ""))
    if not nums:
        raise ValueError("请填好友代码（一串数字）")
    n = int(max(nums, key=len))
    if n > STEAM64_BASE:
        n -= STEAM64_BASE
    if not 0 < n < 2 ** 32 - 1:
        raise ValueError(f"{text} 不像好友代码")
    return n


def mode_name(game_mode, lobby_type, short: bool = False) -> str:
    """short：列表里那一列窄，天梯只写「天梯」（天梯基本都是全英雄选择），悬停看全名"""
    name = GAME_MODES.get(game_mode, f"模式 {game_mode}")
    if lobby_type == 7:
        return "天梯" if short and game_mode in (1, 22) else f"天梯 · {name}"
    return name


# ---------- 表现分 ----------

def percentile(value: float, curve: list[dict]) -> float:
    """value 在同英雄所有对局里排百分之几（0~1）：curve 是 OpenDota benchmarks 的 [{percentile, value}]，中间按直线插，
    一串分位都是同一个值（比如治疗量 0）时取中间"""
    pts = sorted((c["percentile"], c["value"]) for c in curve)
    if not pts:
        return 0.5
    equal = [p for p, x in pts if x == value]
    if equal:                                        # 正好等于某几个分位值：取它们中间
        return (equal[0] + equal[-1]) / 2
    below = [(p, x) for p, x in pts if x < value]
    above = [(p, x) for p, x in pts if x > value]
    if not above:
        return 1.0
    p0, x0 = below[-1] if below else (0.0, min(0.0, value))
    p1, x1 = above[0]
    return p0 + (p1 - p0) * (value - x0) / (x1 - x0)


def per_min(p: dict) -> dict:
    """一局里一个人的 benchmarks 那几项（OpenDota 的口径：每分钟，建筑伤害是总数）"""
    minutes = max(p.get("duration") or 0, 60) / 60
    out = {"tower_damage": p.get("tower_damage") or 0}
    for k, src in (("gold_per_min", None), ("xp_per_min", None), ("kills_per_min", "kills"),
                   ("assists_per_min", "assists"), ("deaths_per_min", "deaths"),
                   ("last_hits_per_min", "last_hits"), ("hero_damage_per_min", "hero_damage")):
        out[k] = (p.get(k) or 0) if src is None else (p.get(src) or 0) / minutes
    return out


def score(p: dict, bench: dict | None) -> dict | None:
    """{score: 0~100, parts: [[名字, 百分位 0~100]]}；没有这个英雄的分位数据返回 None"""
    if not bench:
        return None
    vals = per_min(p)
    parts = []
    for k, name in SCORE_PARTS.items():
        if not bench.get(k):
            continue
        pct = percentile(vals[k], bench[k])
        parts.append([name, round((1 - pct if k == "deaths_per_min" else pct) * 100)])
    if not parts:
        return None
    return {"score": round(sum(x for _, x in parts) / len(parts)), "parts": parts}


def hero_benchmarks(hero_id: int) -> dict | None:
    """这个英雄的分位曲线（OpenDota /benchmarks），存 data/benchmarks/，BENCH_DAYS 天拉一次；拉不到用旧的，都没有返回 None"""
    path = BENCH_DIR / f"{hero_id}.json"
    old = None
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - old["fetched_at"] < BENCH_DAYS * 86400:
            return old["result"]
    except (OSError, ValueError, KeyError):
        pass
    try:
        res = http._get(f"{http.OPENDOTA}/benchmarks", hero_id=hero_id)["result"]
    except http.NETWORK_ERRORS as e:
        log.warning("英雄 %s 的分位数据没拉到：%s", hero_id, e)
        return old["result"] if old else None
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched_at": int(time.time()), "result": res}), encoding="utf-8")
    return res


def benchmarks_for(hero_ids) -> dict[int, dict | None]:
    ids = sorted(set(hero_ids))
    with ThreadPoolExecutor(4) as pool:              # 第一次查要拉十几个英雄，并排拉
        return dict(zip(ids, pool.map(hero_benchmarks, ids)))


# ---------- 联网查 ----------

def _win(m: dict) -> bool:
    return (m["player_slot"] < 128) == bool(m["radiant_win"])


def fetch(cfg: Config, account_id: int, bench=benchmarks_for) -> dict:
    """联网查一个人。好友代码不存在抛 LookupError；网络问题抛 http.NETWORK_ERRORS 里的异常"""
    try:
        prof = http._get(f"{http.OPENDOTA}/players/{account_id}")
    except requests.HTTPError as e:                  # 没这个号 OpenDota 回 404
        if e.response is None or e.response.status_code != 404:
            raise
        prof = {}
    profile = prof.get("profile") or {}
    if not profile:
        raise LookupError(f"OpenDota 上没有好友代码 {account_id}（填错了，或者这个号没打过 Dota）")
    out = {"account_id": account_id, "name": profile.get("personaname") or str(account_id),
           "rank_tier": prof.get("rank_tier"), "leaderboard_rank": prof.get("leaderboard_rank"),
           "fetched_at": int(time.time()), "matches": [], "heroes": [], "with_me": None,
           "positions": None, "positions_note": ""}
    matches = http._get(f"{http.OPENDOTA}/players/{account_id}/matches", limit=SAMPLE, project=MATCH_FIELDS)
    if not matches:
        return out                                   # 没公开比赛数据（或没打过），后面的也查不到
    recent = matches[:RECENT]
    benches = bench(m["hero_id"] for m in recent)
    out["matches"] = [{"match_id": m["match_id"], "hero_id": m["hero_id"], "win": _win(m),
                       "kda": [m["kills"], m["deaths"], m["assists"]], "t": m["start_time"],
                       "duration": m.get("duration") or 0, "mode": mode_name(m.get("game_mode"), m.get("lobby_type")),
                       "mode_short": mode_name(m.get("game_mode"), m.get("lobby_type"), short=True),
                       "party": m.get("party_size") or 1, "level": m.get("level"), "lh": m.get("last_hits"),
                       "dn": m.get("denies"), "gpm": m.get("gold_per_min"), "xpm": m.get("xp_per_min"),
                       "damage": m.get("hero_damage"), "abandon": bool(m.get("leaver_status")),
                       "perf": score(m, benches.get(m["hero_id"]))} for m in recent]
    games, wins = Counter(), Counter()
    for m in matches:
        games[m["hero_id"]] += 1
        wins[m["hero_id"]] += _win(m)
    out["heroes"] = [[h, g, wins[h]] for h, g in games.most_common(TOP_HEROES)]
    out["sample"] = len(matches)
    if cfg.account_id and cfg.account_id != account_id:
        # 不用 /wl?included_account_id：那个把对面的局也算进去；peers 分开了同队（with_）和对面（against_）
        me = next((p for p in http._get(f"{http.OPENDOTA}/players/{account_id}/peers")
                   if p.get("account_id") == cfg.account_id), {})
        out["with_me"] = [me.get("with_games", 0), me.get("with_win", 0)]
    if cfg.stratz_token:                             # STRATZ 评分是附带的：查不到就不显示
        try:
            ratings = stratz_ratings(cfg, account_id)
        except http.NETWORK_ERRORS as e:
            log.warning("队友战绩 %s：STRATZ 评分没查到：%s", account_id, e)
            ratings = {}
        for x in out["matches"]:
            x["stratz"] = ratings.get(x["match_id"])
    if not cfg.stratz_token:
        out["positions_note"] = "常打位置要填 STRATZ token"
        return out
    try:
        ranked = player._player_matches_stratz(cfg, account_id, SAMPLE)
    except http.NETWORK_ERRORS as e:                  # STRATZ 偶尔整体超时：别的照样显示
        out["positions_note"] = f"STRATZ 没查到位置（{e}）"
        return out
    pos: dict[int, list[int]] = {}
    for _, p, w in ranked:
        if p:
            g = pos.setdefault(p, [0, 0])
            g[0] += 1
            g[1] += w
    out["positions"] = sorted(([p, g, w] for p, (g, w) in pos.items()), key=lambda x: -x[1])
    out["positions_sample"] = len(ranked)
    if not pos:
        out["positions_note"] = "最近的天梯 STRATZ 没分出位置"
    return out


def fetch_match(match_id: int) -> dict:
    """整局详情（OpenDota /matches/{id}），打完的比赛不会再变，存 data/matches/ 以后直接读"""
    path = MATCH_DIR / f"{match_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    try:
        m = http._get(f"{http.OPENDOTA}/matches/{match_id}")
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            raise LookupError(f"OpenDota 上还没有比赛 {match_id}（刚打完的要等几分钟）") from None
        raise
    if not m.get("players"):
        raise LookupError(f"OpenDota 上比赛 {match_id} 没有玩家数据")
    keep = ("match_id", "start_time", "duration", "radiant_win", "radiant_score", "dire_score", "game_mode",
            "lobby_type", "players")
    m = {k: m.get(k) for k in keep}
    save_match(m)
    return m


def save_match(m: dict) -> None:
    MATCH_DIR.mkdir(parents=True, exist_ok=True)
    (MATCH_DIR / f"{m['match_id']}.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    old = sorted(MATCH_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    for p in old[:-MATCH_KEEP]:
        p.unlink(missing_ok=True)


# ---------- STRATZ 表现评分（可选：填了 token 才有，没有不影响别的）----------

AWARDS = {"MVP": "MVP", "TOP_CORE": "最佳核心", "TOP_SUPPORT": "最佳辅助"}


def _stratz_rating(p: dict) -> dict | None:
    """STRATZ 一个人一局的评分：imp（0 = 平均，正数好于平均）、奖项、位置；还没解析（imp 是 null）返回 None"""
    if p.get("imp") is None:
        return None
    pos = (p.get("position") or "").removeprefix("POSITION_")
    return {"imp": p["imp"], "award": AWARDS.get(p.get("award") or ""), "pos": int(pos) if pos.isdigit() else None}


def stratz_ratings(cfg: Config, account_id: int) -> dict[int, dict]:
    """这个人最近几局的 STRATZ 评分：比赛编号 → 评分。STRATZ 收录 / 解析得慢，最新几局常常没有（2026-10-28 实测最近 20 局只有一半有）"""
    q = """query($id: Long!, $take: Int) { player(steamAccountId: $id) { matches(request: {take: $take}) {
             id players(steamAccountId: $id) { imp award position } } } }"""
    ms = (http._stratz(cfg, q, id=account_id, take=RECENT + 10).get("player") or {}).get("matches") or []
    return {m["id"]: r for m in ms for p in (m.get("players") or [])[:1] if (r := _stratz_rating(p))}


def stratz_match(cfg: Config, match_id: int) -> dict[int, dict]:
    """一整局 10 个人的 STRATZ 评分：英雄 id → 评分（一局里英雄不重复）；STRATZ 还没有这局返回 {}"""
    q = """query($id: Long!) { match(id: $id) { players { heroId imp award position } } }"""
    m = http._stratz(cfg, q, id=match_id).get("match") or {}
    return {p["heroId"]: r for p in m.get("players") or [] if (r := _stratz_rating(p))}


# ---------- 记住查过的人 ----------

class Mates:
    """查过的队友（新查的在前），每人：好友代码、最后一次查到的数据、出错提示"""

    def __init__(self, path: Path | None = None):
        self.path = path or MATES_FILE
        self.items: list[dict] = []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = []
        for r in raw if isinstance(raw, list) else []:
            if isinstance(r, dict) and isinstance(r.get("account_id"), int):
                self.items.append({"account_id": r["account_id"], "data": r.get("data"), "error": ""})

    def get(self, account_id: int) -> dict | None:
        return next((e for e in self.items if e["account_id"] == account_id), None)

    def touch(self, account_id: int) -> dict:
        """挪到最前（没有就加），超过 KEEP 个挤掉最后的"""
        e = self.get(account_id) or {"account_id": account_id, "data": None, "error": ""}
        self.items = [e] + [x for x in self.items if x is not e]
        del self.items[KEEP:]
        return e

    @staticmethod
    def fresh(e: dict) -> bool:
        return bool(e["data"]) and time.time() - e["data"]["fetched_at"] < FRESH

    def done(self, account_id: int, res: dict | None, err: str) -> None:
        e = self.get(account_id)
        if not e:                                    # 查的时候被删了
            return
        e["error"] = err
        if res:
            e["data"] = res
        self.save()

    def remove(self, account_id: int) -> None:
        self.items = [e for e in self.items if e["account_id"] != account_id]
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([{"account_id": e["account_id"], "data": e["data"]} for e in self.items],
                                        ensure_ascii=False), encoding="utf-8")


def rank_text(tier, leaderboard=None) -> str:
    if not tier:
        return ""
    return player.rank_name(tier) + (f" · 第 {leaderboard} 名" if leaderboard else "")


class Service:
    """网页（webui 的 /api/mates、/api/match）调的入口。联网都在请求自己的线程里做（本地服务是多线程的），
    同一个人同时只查一次：第二个请求等第一个查完、直接用它的结果"""

    def __init__(self, cfg: Config, path: Path | None = None, fetch_player=fetch, fetch_match_fn=fetch_match,
                 db=None, items=None, stratz_match_fn=stratz_match):
        self.cfg = cfg
        self.store = Mates(path)
        self._stratz_match = stratz_match_fn
        self._stratz_miss: dict[int, float] = {}     # 比赛编号 → 上次问 STRATZ 说还没有的时间
        self._fetch, self._fetch_match = fetch_player, fetch_match_fn
        self._db, self._items = db, items            # 英雄表 / 装备 id → (key, 中文名)：第一次用到才读
        self._lock = threading.Lock()
        self._busy: dict[int, threading.Lock] = {}

    # ---- 名字 ----

    def _hero(self, hero_id) -> str:
        if self._db is None:
            self._db = assets.load_heroes()
        h = self._db.by_id.get(hero_id)
        return h.name_cn if h else f"英雄 {hero_id}"

    def _item(self, item_id) -> dict | None:
        if not item_id:
            return None
        if self._items is None:
            self._items = _item_names()
        key, name = self._items.get(item_id, (None, None))
        return {"key": key, "name": name} if key else None

    # ---- 操作 ----

    def list(self) -> list[dict]:
        with self._lock:
            return [self._view(e) for e in self.store.items]

    def query(self, code, force: bool = False) -> tuple[int, str]:
        """查一个人（阻塞），返回 (好友代码, 提示)"""
        aid = parse_code(code)
        with self._lock:
            e = self.store.touch(aid)
            if not force and Mates.fresh(e):
                self.store.save()
                return aid, ""
            busy = self._busy.setdefault(aid, threading.Lock())
        with busy:
            with self._lock:
                e = self.store.get(aid)
                if e and Mates.fresh(e) and time.time() - e["data"]["fetched_at"] < 5:
                    return aid, ""                   # 等的这会儿别的请求刚查完
            t0 = time.monotonic()
            try:
                res, err = self._fetch(self.cfg, aid), ""
                log.info("队友战绩 %s 查好了（%.1f 秒）", aid, time.monotonic() - t0)
            except LookupError as ex:
                res, err = None, str(ex)
            except http.NETWORK_ERRORS as ex:        # 断网 / 限速：一行就够，不记堆栈
                log.warning("队友战绩 %s 没查到：%s", aid, ex)
                res, err = None, f"联网失败：{ex}"
            with self._lock:
                self.store.done(aid, res, err)
        return aid, err

    def remove(self, account_id: int) -> None:
        with self._lock:
            self.store.remove(int(account_id))

    def match(self, match_id: int, focus: int | None = None) -> dict:
        """整局详情（阻塞）：两队各 5 人的数据 + 表现分。比赛不存在抛 LookupError"""
        m = self._fetch_match(int(match_id))
        players = m["players"]
        benches = benchmarks_for(p["hero_id"] for p in players)
        stratz, stratz_note = self._match_stratz(m)
        parties = Counter(p.get("party_id") for p in players if p.get("party_id") is not None)
        party_no = {pid: i + 1 for i, pid in enumerate(sorted(pid for pid, n in parties.items() if n > 1))}
        teams = {"radiant": [], "dire": []}
        for p in sorted(players, key=lambda p: p["player_slot"]):
            aid = p.get("account_id")
            anon = not aid or aid == ANONYMOUS
            d = {**p, "duration": m["duration"]}
            teams["radiant" if p["player_slot"] < 128 else "dire"].append({
                "account_id": None if anon else aid, "name": "匿名玩家" if anon else (p.get("personaname") or str(aid)),
                "rank": rank_text(p.get("rank_tier")), "hero_id": p["hero_id"], "hero": self._hero(p["hero_id"]),
                "level": p.get("level"), "kda": [p.get("kills"), p.get("deaths"), p.get("assists")],
                "lh": p.get("last_hits"), "dn": p.get("denies"), "gpm": p.get("gold_per_min"), "xpm": p.get("xp_per_min"),
                "damage": p.get("hero_damage"), "tower": p.get("tower_damage"), "heal": p.get("hero_healing"),
                "net_worth": p.get("net_worth"),
                "items": [self._item(p.get(f"item_{i}")) for i in range(6)],
                "backpack": [x for i in range(3) if (x := self._item(p.get(f"backpack_{i}")))],
                "neutral": self._item(p.get("item_neutral")),
                "scepter": bool(p.get("aghanims_scepter")), "shard": bool(p.get("aghanims_shard")),
                "party": party_no.get(p.get("party_id")), "abandon": bool(p.get("leaver_status")),
                "perf": score(d, benches.get(p["hero_id"])), "stratz": stratz.get(p["hero_id"]),
                "focus": not anon and aid == focus, "me": not anon and aid == self.cfg.account_id,
            })
        return {"match_id": m["match_id"], "start_time": m["start_time"], "duration": m["duration"],
                "radiant_win": m["radiant_win"], "score": [m.get("radiant_score"), m.get("dire_score")],
                "mode": mode_name(m.get("game_mode"), m.get("lobby_type")), "teams": teams, "stratz_note": stratz_note}

    def _match_stratz(self, m: dict) -> tuple[dict[int, dict], str]:
        """这局 10 个人的 STRATZ 评分 + 一句说明（没填 token 不说）。10 个人齐了存进整局缓存，没齐下次打开再问"""
        if not self.cfg.stratz_token:
            return {}, ""
        if m.get("stratz"):
            return {int(h): r for h, r in m["stratz"].items()}, ""
        mid = m["match_id"]
        missing = "STRATZ 还没解析这局，没有它的评分（它收录、解析都慢，最新的局常常没有）"
        if time.time() - self._stratz_miss.get(mid, 0) < STRATZ_RETRY:
            return {}, missing                       # 刚问过没有：来回翻页不用每次多等一两秒
        try:
            got = self._stratz_match(self.cfg, mid)
        except http.NETWORK_ERRORS as e:
            log.warning("比赛 %s：STRATZ 评分没查到：%s", mid, e)
            return {}, f"STRATZ 评分没查到（{e}）"
        if got and len(got) >= len(m["players"]):
            save_match({**m, "stratz": {str(h): r for h, r in got.items()}})
        if not got:
            self._stratz_miss[mid] = time.time()
        return got, "" if got else missing

    # ---- 显示 ----

    def _view(self, e: dict) -> dict:
        d = e["data"] or {}
        v = {"account_id": e["account_id"], "error": e["error"], "loaded": bool(d),
             "name": d.get("name") or "", "fetched_at": d.get("fetched_at")}
        if not d:
            return v
        v.update(stale=any("match_id" not in m for m in d["matches"]),   # 旧版存的（没有比赛编号点不进去）：页面自动重查
                 rank=rank_text(d.get("rank_tier"), d.get("leaderboard_rank")) or "没定段 / 不公开",
                 private=not d["matches"], with_me=d.get("with_me"), sample=d.get("sample", 0),
                 positions=d.get("positions"), positions_sample=d.get("positions_sample", 0),
                 positions_note=d.get("positions_note", ""),
                 matches=[{**m, "name": self._hero(m["hero_id"])} for m in d["matches"]],
                 heroes=[{"id": h, "name": self._hero(h), "games": g, "wins": w} for h, g, w in d["heroes"]])
        return v


def _item_names() -> dict[int, tuple[str, str]]:
    """装备 id → (key, 中文名)：id 和 key 来自 OpenDota 装备常量，中文名来自知识库（没有就用英文名）"""
    from .. import kb
    try:
        const = assets.items_const()
    except (OSError, ValueError, requests.RequestException) as e:
        log.warning("装备常量读不到：%s", e)
        return {}
    base = kb.KnowledgeBase.load()
    out = {}
    for key, v in const.items():
        if isinstance(v, dict) and v.get("id") and not key.startswith("recipe_"):
            e = base.items.get(key) if base else None
            out[v["id"]] = (key, e.name if e else v.get("dname") or key)
    return out
