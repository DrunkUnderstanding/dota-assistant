"""克制 / 配合统计（OpenDota explorer）、英雄分位置场次 / 胜率（STRATZ，或没 token 时 OpenDota 分路凑的）：拉取、缓存路径、读取。"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path

from ..core.config import DATA_DIR, Config
from . import http

IDS_PER_DAY = 1_550_000   # match_id 每天大约增长这么多（2026-10 实测）
CHUNK_IDS = 1_000_000     # 单次 explorer 查询的 match_id 跨度（约 30 万局，6~9 秒）；超时会自动对半拆
MIN_DURATION = 15 * 60    # 少于 15 分钟的局多半是掉线 / 秒退，不计
WEEK = 7 * 86400          # STRATZ 按周分桶：周 = 从 1970-01-01（星期四）起每 7 天，`week` 参数传那周开始的 unix 时间


# ---------- 当前版本上线时间（玩家 2026-11-19 定：版本更新以后的胜率只用更新以后的数据） ----------

VALVE_PATCHES = "https://www.dota2.com/datafeed/patchnoteslist"


def patch_start(log=http._say) -> tuple[str, int] | None:
    """(版本号, 上线时间 unix 秒)，两个来源取晚的那个：
    - OpenDota `constants/patch`：只有大版本（7.42），时间是真正上线的时刻（7.42 是 11-18 21:28 UTC，和 STRATZ 分小时出场率翻倍对得上）；
    - Valve datafeed 更新日志：带字母小版本（7.41f），但时间只到当天（7.42 写的 08:00 UTC，比真上线早 13 小时）。
    小版本取 Valve 那个时间会多算进当天上线前几个小时的局，可以接受。两个都拉不到返回 None（不按版本切，照旧按天数）"""
    found: list[tuple[int, str]] = []
    try:
        od = http._get(f"{http.OPENDOTA}/constants/patch")
        last = max(od, key=lambda p: p["date"])
        found.append((int(_iso_ts(last["date"])), str(last["name"])))
    except (*http.NETWORK_ERRORS, KeyError, ValueError, TypeError) as e:
        log(f"  OpenDota 版本表没拉到（{e}）")
    try:
        vp = http._get(VALVE_PATCHES, language="schinese")["patches"]
        last = max(vp, key=lambda p: p["patch_timestamp"])
        found.append((int(last["patch_timestamp"]), str(last["patch_number"])))
    except (*http.NETWORK_ERRORS, KeyError, ValueError, TypeError) as e:
        log(f"  Valve 更新日志没拉到（{e}）")
    if not found:
        return None
    ts, _ = max(found)
    name = max((n for _, n in found), key=_patch_key)    # 版本号取细的那个（7.41f 比 7.41 细）
    return name, ts


def _iso_ts(s: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _patch_key(name: str) -> tuple:
    """'7.41f' → (7, 41, 'f')，比大小用"""
    head = name.rstrip("abcdefghijklmnopqrstuvwxyz")
    nums = tuple(int(x) for x in head.split(".") if x.isdigit())
    return nums, name[len(head):]


def stratz_week(patch: tuple[str, int] | None, now: float | None = None) -> int | None | bool:
    """STRATZ 分位置 / 对线按周取，挑哪一周（2026-11-20 实测：不写 week 给的是上一个整周，写了给那一周、还没过完的当周也给）：
    - None：上一个整周都在当前版本里（或不知道版本），照旧不写 week；
    - 当周开始的 unix 秒：上一个整周有一部分是旧版本，但当周是在上线以后才开始的，用当周（没过完、局少一点）；
    - False：版本是这周中间上的，没有哪一周全是新版本（分位置改按天 / 小时凑，对线先不显示）"""
    if not patch:
        return None
    cur = int((time.time() if now is None else now) // WEEK * WEEK)
    if patch[1] <= cur - WEEK:
        return None
    return cur if patch[1] <= cur else False


# ---------- 克制 / 配合 ----------

def _bracket_sql(cfg: Config) -> str:
    lo = cfg.rank_min * 10 if cfg.rank_min else 0
    hi = cfg.rank_max * 10 + 9 if cfg.rank_max else 99
    return f"AND avg_rank_tier BETWEEN {lo} AND {hi}" if (lo or hi != 99) else ""


def _chunk_queries(lo_id: int, hi_id: int, bracket: str, since: int = 0) -> tuple[str, str]:
    after = f"AND start_time >= {since} " if since else ""
    base = (f"SELECT radiant_team r, dire_team d, radiant_win w FROM public_matches "
            f"WHERE match_id > {lo_id} AND match_id <= {hi_id} {after}AND game_mode = 22 AND lobby_type = 7 "
            f"AND duration >= {MIN_DURATION} AND NOT (0 = ANY(radiant_team)) AND NOT (0 = ANY(dire_team)) "
            f"{bracket}")
    vs = (f"WITH m AS ({base}) SELECT a.h a, b.h b, count(*) g, "
          f"sum(CASE WHEN w THEN 1 ELSE 0 END) wa FROM m, unnest(m.r) a(h), unnest(m.d) b(h) GROUP BY 1, 2")
    # 同队两两组合：天辉一侧按天辉胜计，夜魇一侧按夜魇胜计
    syn = (f"WITH m AS ({base}) "
           f"SELECT a.h a, b.h b, count(*) g, sum(CASE WHEN w THEN 1 ELSE 0 END) wa "
           f"FROM m, unnest(m.r) a(h), unnest(m.r) b(h) WHERE a.h < b.h GROUP BY 1, 2 "
           f"UNION ALL "
           f"SELECT a.h a, b.h b, count(*) g, sum(CASE WHEN w THEN 0 ELSE 1 END) wa "
           f"FROM m, unnest(m.d) a(h), unnest(m.d) b(h) WHERE a.h < b.h GROUP BY 1, 2")
    return vs, syn


def fetch_stats(cfg: Config, log=http._say, patch: tuple[str, int] | None = None) -> dict:
    """patch = patch_start() 的结果：上线不到 cfg.days 天就只统计上线以后的局（start_time 精确切）"""
    latest = max(m["match_id"] for m in http._get(f"{http.OPENDOTA}/publicMatches"))
    start = latest - cfg.days * IDS_PER_DAY
    since = 0
    if patch and patch[1] > time.time() - cfg.days * 86400:
        since = patch[1]
        # 上线后第一局的 match_id（explorer 一次 min，平时约 2 秒）；IDS_PER_DAY 只是估算，多往前留一天
        try:
            rows = http._sql(f"SELECT min(match_id) m FROM public_matches "
                             f"WHERE match_id > {latest - (cfg.days + 1) * IDS_PER_DAY} AND start_time >= {since}")
            first = int(rows[0]["m"]) if rows and rows[0].get("m") else latest
        except http.NETWORK_ERRORS as e:
            # 2026-11-20 实测 explorer 忙时这条 min 会 Query read timeout，整次更新失败、推荐一直用旧版本的克制数据。
            # 退回按上线时长估算起点、多往前留一天：分块查询本身按 start_time 精确切，起点早一点只是多扫一两块
            ago = (time.time() - since) / 86400
            first = latest - int((ago + 1) * IDS_PER_DAY)
            log(f"  上线后第一局没查到（{e}），按上线时长估算起点")
        start = max(start, first - 1)
        log(f"  {patch[0]} 上线不到 {cfg.days} 天，只统计上线以后的局")
    bracket = _bracket_sql(cfg)

    vs = defaultdict(lambda: [0, 0])    # (a,b) -> [a 打 b 的局数, a 赢的局数]
    syn = defaultdict(lambda: [0, 0])   # (a,b) a<b -> [同队局数, 赢的局数]
    total = 0

    def run(lo: int, hi: int) -> None:
        nonlocal total
        q_vs, q_syn = _chunk_queries(lo, hi, bracket, since)
        try:
            rows_vs, rows_syn = http._sql(q_vs), http._sql(q_syn)
        except RuntimeError:
            if hi - lo <= CHUNK_IDS // 8:
                raise
            mid = (lo + hi) // 2
            run(lo, mid)
            run(mid, hi)
            return
        for r in rows_vs:
            g, wa = int(r["g"]), int(r["wa"])
            # 行是「天辉 a 对夜魇 b」，两个视角都记上
            vs[(r["a"], r["b"])][0] += g
            vs[(r["a"], r["b"])][1] += wa
            vs[(r["b"], r["a"])][0] += g
            vs[(r["b"], r["a"])][1] += g - wa
            total += g
        for r in rows_syn:
            syn[(r["a"], r["b"])][0] += int(r["g"])
            syn[(r["a"], r["b"])][1] += int(r["wa"])
        log(f"  match_id {lo}..{hi} 完成，累计 {total // 25} 局")

    for lo in range(start, latest, CHUNK_IDS):
        run(lo, min(lo + CHUNK_IDS, latest))
    matches = total

    return {
        "fetched_at": int(time.time()),
        "lo_id": start,          # 统计用到的 match_id 范围；回测只用 hi_id 之后的对局，避免拿考题当答案
        "hi_id": latest,
        "days": cfg.days,
        "since": since or None,  # 有值 = 版本上线不到 days 天，只统计这之后开始的局（unix 秒）
        "patch": patch[0] if patch else None,
        "rank_min": cfg.rank_min,
        "rank_max": cfg.rank_max,
        "matches": matches // 25,
        "vs": [[a, b, g, w] for (a, b), (g, w) in vs.items()],
        "syn": [[a, b, g, w] for (a, b), (g, w) in syn.items()],
    }


def stats_path(cfg: Config) -> Path:
    return DATA_DIR / f"stats_r{cfg.rank_min}-{cfg.rank_max}_d{cfg.days}.json"

# ---------- 英雄分位置数据（STRATZ） ----------

_POSITIONS = ("1", "2", "3", "4", "5")
# 分位置胜率只算打到第几分钟还没结束的局。玩家 2026-10-26 定 30：速推局（一号位拉席克 35 分钟内结束的赢 72%）不算，
# 回测区分度 0.0666（全部局 0.0673、≥25 分钟 0.0676、≥35 分钟 0.0651），在噪声内。见 HANDOFF 第 29 节
POSITION_MIN_MINUTES = 30


def fetch_positions(cfg: Config, patch: tuple[str, int] | None = None, log=http._say) -> dict:
    """{英雄 id: {位置 1~5: [局数, 胜场]}}，STRATZ 对应段位、只算打到 POSITION_MIN_MINUTES 分钟的局。
    STRATZ 不分时间的 stats 给的是「打到 maxTime 分钟还没结束」的局数 / 胜场，maxTime 不写默认 35（2026-10-26 实测），
    写 0 是全部对局。默认取上一个整周；新版本上线不满一周时改取上线以后的（见 stratz_week）"""
    week = stratz_week(patch)
    if week is False:
        return _positions_since(cfg, patch, log)
    q = """query($b: [RankBracketBasicEnum], $mx: Int, $w: Long) { heroStats { stats(bracketBasicIds: $b, groupByPosition: true,
             maxTime: $mx, week: $w) { heroId position matchCount winCount } } }"""
    if week:
        log(f"  {patch[0]} 上线不满一周，只取上线以后这周的")
    out: dict[str, dict[str, list[int]]] = defaultdict(dict)
    for r in http._stratz(cfg, q, b=http._stratz_brackets(cfg), mx=POSITION_MIN_MINUTES, w=week)["heroStats"]["stats"]:
        pos = (r["position"] or "").removeprefix("POSITION_")
        if pos in _POSITIONS:
            out[str(r["heroId"])][pos] = [r["matchCount"], r["winCount"]]
    return {"fetched_at": int(time.time()), "brackets": http._stratz_brackets(cfg), "min_minutes": POSITION_MIN_MINUTES,
            "week": week, "patch": patch[0] if patch else None, "heroes": out}


def _positions_since(cfg: Config, patch: tuple[str, int], log=http._say) -> dict:
    """版本是这周中间上的：STRATZ 按天（winDay，晚一天才有）+ 按小时（winHour，只有最近约 30 小时）凑上线以后的分位置场次。
    这两个接口不能按时长筛，所以这份不只算打到 30 分钟的局（min_minutes 记 0）"""
    log(f"  {patch[0]} 是这周中间上的，按天 / 按小时凑上线以后的分位置数据")
    br = [b for basic in http._stratz_brackets(cfg) if basic != "ALL" for b in basic.split("_")]
    q = """query($b: [RankBracket], $p: [MatchPlayerPositionType]) { heroStats {
             d: winDay(take: 8, bracketIds: $b, positionIds: $p, groupBy: HERO_ID) { day heroId matchCount winCount }
             h: winHour(take: 48, bracketIds: $b, positionIds: $p, groupBy: HERO_ID) { hour heroId matchCount winCount } } }"""
    out: dict[str, dict[str, list[int]]] = defaultdict(dict)
    for pos in _POSITIONS:
        res = http._stratz(cfg, q, b=br or None, p=[f"POSITION_{pos}"])["heroStats"]
        days = [r for r in res["d"] or [] if r["day"] >= patch[1]]
        covered = {r["day"] for r in days}
        hours = [r for r in res["h"] or [] if r["hour"] >= patch[1] and r["hour"] // 86400 * 86400 not in covered]
        for r in days + hours:
            gw = out[str(r["heroId"])].setdefault(pos, [0, 0])
            gw[0] += r["matchCount"]
            gw[1] += r["winCount"]
    return {"fetched_at": int(time.time()), "brackets": http._stratz_brackets(cfg), "min_minutes": 0,
            "since": patch[1], "patch": patch[0], "heroes": out}


def positions_path(cfg: Config) -> Path:
    return DATA_DIR / f"positions_r{cfg.rank_min}-{cfg.rank_max}.json"


# 没填 STRATZ token 时用 OpenDota 分路数据凑的分位置数据（怎么凑见 fetch_positions_opendota）
OPENDOTA_POSITIONS = DATA_DIR / "positions_opendota.json"


def positions_source(cfg: Config) -> str | None:
    """分位置数据从哪来：'stratz' / 'opendota'（没 token 时凑的）/ None（都没有，推荐按定位标签粗筛位置）"""
    return "stratz" if positions_path(cfg).exists() else "opendota" if OPENDOTA_POSITIONS.exists() else None


def load_positions(cfg: Config) -> dict[int, dict[int, tuple[int, int]]] | None:
    """有 STRATZ 的（填过 token，或打包版自带的）用它，没有再用 OpenDota 凑的"""
    p = positions_path(cfg)
    if not p.exists():
        p = OPENDOTA_POSITIONS
    if not p.exists():
        return None
    raw = json.loads(p.read_text(encoding="utf-8"))["heroes"]
    return {int(h): {int(pos): tuple(gw) for pos, gw in v.items()} for h, v in raw.items()}


# 对线胜负（STRATZ laneOutcome，2026-11-09 参考同行 gg-go-next 的「对线模式」）：英雄打某个位置时，对上某个英雄那条线的赢 / 平 / 输。
# 一次 GraphQL 用别名查 LANE_BATCH 个英雄（实测 20 个约 1 秒），5 个位置 × 7 次 ≈ 35 次请求
LANE_BATCH = 20


def fetch_lanes(cfg: Config, hero_ids: list[int], log=http._say, patch: tuple[str, int] | None = None) -> dict | None:
    """{位置: {英雄: {对线英雄: [线数, 赢线 − 输线]}}}（赢 / 输都含碾压；对线英雄不分他打几号位），STRATZ 对应段位最近的天梯。
    和分位置一样按 stratz_week 挑周；版本是这周中间上的（laneOutcome 只能按周取）返回 None：先不显示对线，不拿旧版本的凑"""
    week = stratz_week(patch)
    if week is False:
        return None
    br = http._stratz_brackets(cfg)
    fields = "heroId2 matchCount winCount lossCount stompWinCount stompLossCount"
    out: dict[str, dict[str, dict[str, list[int]]]] = {}
    for pos in _POSITIONS:
        rows: dict[str, dict[str, list[int]]] = {}
        for i in range(0, len(hero_ids), LANE_BATCH):
            chunk = hero_ids[i:i + LANE_BATCH]
            q = ("query($p: [MatchPlayerPositionType], $b: [RankBracketBasicEnum], $w: Long) { heroStats { "
                 + " ".join(f"h{h}: laneOutcome(heroId: {h}, isWith: false, week: $w, positionIds: $p, bracketBasicIds: $b) "
                            f"{{ {fields} }}" for h in chunk) + " } }")
            for alias, rs in http._stratz(cfg, q, p=[f"POSITION_{pos}"], b=br, w=week)["heroStats"].items():
                rows[alias[1:]] = {str(r["heroId2"]): [r["matchCount"], r["winCount"] + r["stompWinCount"]
                                                       - r["lossCount"] - r["stompLossCount"]] for r in rs or []}
        out[pos] = rows
        log(f"  {pos} 号位：{len(rows)} 个英雄，{sum(v[0] for r in rows.values() for v in r.values())} 条对位记录")
    return {"fetched_at": int(time.time()), "brackets": br, "week": week, "patch": patch[0] if patch else None,
            "positions": out}


def lanes_path(cfg: Config) -> Path:
    return DATA_DIR / f"lanes_r{cfg.rank_min}-{cfg.rank_max}.json"


def load_lanes(cfg: Config) -> dict[int, dict[int, dict[int, tuple[int, int]]]] | None:
    """{位置: {英雄: {对线英雄: (线数, 赢线 − 输线)}}}；没拉过（没 token / 还没 update）返回 None"""
    p = lanes_path(cfg)
    if not p.exists():
        return None
    raw = json.loads(p.read_text(encoding="utf-8"))["positions"]
    return {int(pos): {int(h): {int(e): tuple(v) for e, v in row.items()} for h, row in heroes.items()}
            for pos, heroes in raw.items()}


def load_stats(cfg: Config) -> dict:
    p = stats_path(cfg)
    if not p.exists():
        raise FileNotFoundError(f"没有数据文件 {p.name}，先运行：python -m dota_assistant update")
    return json.loads(p.read_text(encoding="utf-8"))
