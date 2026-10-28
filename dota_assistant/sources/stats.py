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


# ---------- 克制 / 配合 ----------

def _bracket_sql(cfg: Config) -> str:
    lo = cfg.rank_min * 10 if cfg.rank_min else 0
    hi = cfg.rank_max * 10 + 9 if cfg.rank_max else 99
    return f"AND avg_rank_tier BETWEEN {lo} AND {hi}" if (lo or hi != 99) else ""


def _chunk_queries(lo_id: int, hi_id: int, bracket: str) -> tuple[str, str]:
    base = (f"SELECT radiant_team r, dire_team d, radiant_win w FROM public_matches "
            f"WHERE match_id > {lo_id} AND match_id <= {hi_id} AND game_mode = 22 AND lobby_type = 7 "
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


def fetch_stats(cfg: Config, log=http._say) -> dict:
    latest = max(m["match_id"] for m in http._get(f"{http.OPENDOTA}/publicMatches"))
    start = latest - cfg.days * IDS_PER_DAY
    bracket = _bracket_sql(cfg)

    vs = defaultdict(lambda: [0, 0])    # (a,b) -> [a 打 b 的局数, a 赢的局数]
    syn = defaultdict(lambda: [0, 0])   # (a,b) a<b -> [同队局数, 赢的局数]
    total = 0

    def run(lo: int, hi: int) -> None:
        nonlocal total
        q_vs, q_syn = _chunk_queries(lo, hi, bracket)
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


def fetch_positions(cfg: Config) -> dict:
    """{英雄 id: {位置 1~5: [局数, 胜场]}}，STRATZ 最近一周、对应段位，只算打到 POSITION_MIN_MINUTES 分钟的局。
    STRATZ 不分时间的 stats 给的是「打到 maxTime 分钟还没结束」的局数 / 胜场，maxTime 不写默认 35（2026-10-26 实测），
    写 0 是全部对局"""
    q = """query($b: [RankBracketBasicEnum], $mx: Int) { heroStats { stats(bracketBasicIds: $b, groupByPosition: true, maxTime: $mx) {
             heroId position matchCount winCount } } }"""
    out: dict[str, dict[str, list[int]]] = defaultdict(dict)
    for r in http._stratz(cfg, q, b=http._stratz_brackets(cfg), mx=POSITION_MIN_MINUTES)["heroStats"]["stats"]:
        pos = (r["position"] or "").removeprefix("POSITION_")
        if pos in _POSITIONS:
            out[str(r["heroId"])][pos] = [r["matchCount"], r["winCount"]]
    return {"fetched_at": int(time.time()), "brackets": http._stratz_brackets(cfg), "min_minutes": POSITION_MIN_MINUTES,
            "heroes": out}


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


def load_stats(cfg: Config) -> dict:
    p = stats_path(cfg)
    if not p.exists():
        raise FileNotFoundError(f"没有数据文件 {p.name}，先运行：python -m dota_assistant update")
    return json.loads(p.read_text(encoding="utf-8"))
