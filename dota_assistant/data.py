"""拉取并缓存英雄表、克制矩阵、配合矩阵。

数据源：
- 中文英雄名：Valve 官方 datafeed
- 英雄定位标签：OpenDota /heroStats
- 克制 / 配合：OpenDota explorer 直接聚合 public_matches（天梯全英雄选择，可按段位过滤）
- 英雄分位置场次 / 胜率、你每局打的位置：STRATZ（需要 token）
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import defaultdict
from pathlib import Path

import requests
from urllib3.util.retry import Retry

from .core.config import DATA_DIR, Config
from .core.heroes import Hero, HeroDB

OPENDOTA = "https://api.opendota.com/api"
VALVE_HEROLIST = "https://www.dota2.com/datafeed/herolist?language=schinese"
STRATZ = "https://api.stratz.com/graphql"

IDS_PER_DAY = 1_550_000   # match_id 每天大约增长这么多（2026-10 实测）
CHUNK_IDS = 1_000_000     # 单次 explorer 查询的 match_id 跨度（约 30 万局，6~9 秒）；超时会自动对半拆
MIN_DURATION = 15 * 60    # 少于 15 分钟的局多半是掉线 / 秒退，不计
logger = logging.getLogger(__name__)  # 不叫 log：下面不少函数有 log 参数（进度提示）


class APIError(RuntimeError):
    """数据源自己报错（STRATZ errors / explorer err）：和断网、503 一样算网络问题"""


NETWORK_ERRORS = (requests.RequestException, APIError)   # 日志里只记一行，不记堆栈（不是程序 bug）


def _say(msg: str) -> None:
    """进度提示：打印出来，同时记进日志"""
    print(msg, flush=True)
    logger.info(msg.strip())

_session = requests.Session()
# 网络不稳时 STRATZ / OpenDota 偶尔直接断连接（SSL EOF / 10054）：连接层自动重试，HTTP 状态码仍由下面自己处理
_session.mount("https://", requests.adapters.HTTPAdapter(max_retries=Retry(
    total=6, backoff_factor=1, allowed_methods=None, status_forcelist=(), raise_on_status=False)))


def _get(url: str, **params):
    for attempt in range(4):
        r = _session.get(url, params=params, timeout=120)
        if r.status_code == 429:
            logger.warning("OpenDota 限速（429），%d 秒后重试：%s", 5 * (attempt + 1), url)
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 400 and "explorer" in url:
            return r.json()   # explorer 把 SQL 错误 / 超时放在 400 的 err 字段里
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def _stratz(cfg: Config, query: str, **variables) -> dict:
    for attempt in range(5):
        r = _session.post(STRATZ, json={"query": query, "variables": variables}, timeout=60,
                          headers={"Authorization": f"Bearer {cfg.stratz_token}", "User-Agent": "STRATZ_API"})
        if r.status_code != 429:
            break
        logger.warning("STRATZ 限速（429），%d 秒后重试", 3 * (attempt + 1))
        time.sleep(3 * (attempt + 1))      # 免费 token 有每秒 / 每分钟限速
    r.raise_for_status()
    res = r.json()
    if res.get("errors"):
        logger.warning("STRATZ 返回错误：%s", res["errors"])
        raise APIError(f"STRATZ 出错：{res['errors'][0].get('message')}")
    return res["data"]


def _sql(q: str) -> list[dict]:
    res = _get(f"{OPENDOTA}/explorer", sql=q)
    if res.get("err"):
        raise APIError(f"OpenDota explorer 出错：{res['err']}")
    return res["rows"]


# ---------- 英雄表 ----------

def fetch_heroes() -> list[dict]:
    cn = {h["id"]: h["name_loc"] for h in _get(VALVE_HEROLIST)["result"]["data"]["heroes"]}
    out = []
    for h in _get(f"{OPENDOTA}/heroStats"):
        out.append({
            "id": h["id"],
            "key": h["name"].removeprefix("npc_dota_hero_"),
            "name_cn": cn.get(h["id"], h["localized_name"]),
            "name_en": h["localized_name"],
            "roles": h["roles"],
            "attr": h["primary_attr"],
            "img": h["img"].split("?")[0],
        })
    return out


CDN = "https://cdn.cloudflare.steamstatic.com"
PORTRAIT_DIR = DATA_DIR / "portraits"


def fetch_portraits(log=_say) -> None:
    """下载每个英雄的横版头像（256×144），屏幕识别的初始模板。已有的跳过。"""
    PORTRAIT_DIR.mkdir(parents=True, exist_ok=True)
    heroes = json.loads((DATA_DIR / "heroes.json").read_text(encoding="utf-8"))
    n = 0
    for h in heroes:
        out = PORTRAIT_DIR / f"{h['id']}.png"
        if out.exists():
            continue
        r = _session.get(CDN + h["img"], timeout=60)
        r.raise_for_status()
        out.write_bytes(r.content)
        n += 1
    log(f"  头像：新下载 {n} 张，共 {len(list(PORTRAIT_DIR.glob('*.png')))} 张")


ICON_DIR = DATA_DIR / "icons"
# 技能图标 abilities/<技能 key>.png、金币 icons/gold.png、英雄小图标 heroes/icons/<英雄 key>.png（买活自动记比对消息栏用）
ICON_PATH = re.compile(r"(abilities|icons|heroes/icons)/[a-z0-9_]+\.png")
_icon_locks: dict[str, threading.Lock] = {}   # 每张图一把锁：页面一打开 10 个买活小条同时要金币图标，只下一次；别的图不用等
_icon_failed: set[str] = set()                 # 这次运行下不到的不再试（断网时每张都要等超时）


def fetch_icon(rel: str) -> Path | None:
    """顶栏小条用的图标：第一次从 Valve 官方 CDN 下（dota2.com 网页用的同一套），存 data/icons，之后读本地。
    下不到（断网 / 没这张）返回 None，页面退回显示文字。不走 _session：它断连要重试好几轮，页面等不起"""
    if not ICON_PATH.fullmatch(rel):
        return None
    out = ICON_DIR / rel
    if not out.is_file() and rel not in _icon_failed:
        with _icon_locks.setdefault(rel, threading.Lock()):
            if not out.is_file() and rel not in _icon_failed:
                _download_icon(rel, out)
                if not out.is_file():
                    _icon_failed.add(rel)
    return out if out.is_file() else None


def _download_icon(rel: str, out: Path) -> None:
    try:
        r = requests.get(f"{CDN}/apps/dota2/images/dota_react/{rel}", timeout=10)
        r.raise_for_status()
    except requests.RequestException as e:
        logger.warning("图标下载失败 %s：%s", rel, e)
        return
    if not r.headers.get("Content-Type", "").startswith("image/"):
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part")        # 写完再改名：别的请求不会读到半张
    tmp.write_bytes(r.content)
    tmp.replace(out)


ICONS_DONE = ICON_DIR / "prefetched.txt"   # 预下过一遍的标记（写的是知识库版本），start 看它决定要不要补下


def fetch_icons(log=_say) -> None:
    """顶栏 / 买活自动记要的图标一次下全，局内不用临时下：金币、每个英雄的小图标、每个对面会给小条的大招图标。
    已有的跳过；第一张就下不到（断网）就不试剩下的、不写标记，下次再补；个别 CDN 没有的照样写标记（运行时按需再试）"""
    from concurrent.futures import ThreadPoolExecutor
    from . import kb, ultcd
    base = kb.KnowledgeBase.load()
    rels = ["icons/gold.png"] + [f"heroes/icons/{h.key}.png" for h in load_heroes().by_id.values()]
    rels += [f"abilities/{ab.key}.png" for h in (base.heroes.values() if base else ()) if (ab := ultcd.pick(h))]
    todo = [r for r in rels if not (ICON_DIR / r).is_file()]
    if todo and fetch_icon(todo[0]) is None:
        log("  顶栏图标没下到（断网？），局内用到时再下")
        return
    with ThreadPoolExecutor(8) as pool:
        missing = [r for r, p in zip(todo[1:], pool.map(fetch_icon, todo[1:])) if p is None]
    ICONS_DONE.write_text(base.patch if base else "", encoding="utf-8")
    log(f"  顶栏图标：新下载 {len(todo) - len(missing)} 张，共 {len(rels) - len(missing)}/{len(rels)} 张"
        + (f"（CDN 没有：{'、'.join(missing)}）" if missing else ""))


ITEM_ICON_DIR = DATA_DIR / "items"


def fetch_items_const(log=_say) -> None:
    """OpenDota 装备常量（价格 / 合成 / 图标地址，coach.items_const 读）。以前只在没有时下一次、之后不再更新，
    版本改了价格 / 出了新装备跟不上；现在跟全量更新一起刷新"""
    from .coach import ITEMS_CONST
    raw = _get(f"{OPENDOTA}/constants/items")
    ITEMS_CONST.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    log(f"  装备常量：{len(raw)} 件")


def fetch_item_icons(log=_say) -> None:
    """下载装备图标（88×64，出装推荐显示成图标）：OpenDota 装备常量里的图片地址，配方共用一张不下。已有的跳过"""
    from concurrent.futures import ThreadPoolExecutor
    from .coach import items_const
    ITEM_ICON_DIR.mkdir(parents=True, exist_ok=True)
    todo = [(ITEM_ICON_DIR / f"{k}.png", v["img"].split("?")[0]) for k, v in items_const().items()
            if v.get("img") and not k.startswith("recipe_") and not (ITEM_ICON_DIR / f"{k}.png").exists()]

    def get(job: tuple[Path, str]) -> None:
        r = _session.get(CDN + job[1], timeout=60)
        r.raise_for_status()
        job[0].write_bytes(r.content)
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(get, todo))
    log(f"  装备图标：新下载 {len(todo)} 张，共 {len(list(ITEM_ICON_DIR.glob('*.png')))} 张")


def load_heroes() -> HeroDB:
    raw = json.loads((DATA_DIR / "heroes.json").read_text(encoding="utf-8"))
    return HeroDB([Hero(r["id"], r["key"], r["name_cn"], r["name_en"], tuple(r["roles"]), r["attr"])
                   for r in raw])


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


def fetch_stats(cfg: Config, log=_say) -> dict:
    latest = max(m["match_id"] for m in _get(f"{OPENDOTA}/publicMatches"))
    start = latest - cfg.days * IDS_PER_DAY
    bracket = _bracket_sql(cfg)

    vs = defaultdict(lambda: [0, 0])    # (a,b) -> [a 打 b 的局数, a 赢的局数]
    syn = defaultdict(lambda: [0, 0])   # (a,b) a<b -> [同队局数, 赢的局数]
    total = 0

    def run(lo: int, hi: int) -> None:
        nonlocal total
        q_vs, q_syn = _chunk_queries(lo, hi, bracket)
        try:
            rows_vs, rows_syn = _sql(q_vs), _sql(q_syn)
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

# STRATZ 的段位只分到两档一组
_STRATZ_BRACKETS = {1: "HERALD_GUARDIAN", 2: "HERALD_GUARDIAN", 3: "CRUSADER_ARCHON", 4: "CRUSADER_ARCHON",
                    5: "LEGEND_ANCIENT", 6: "LEGEND_ANCIENT", 7: "DIVINE_IMMORTAL", 8: "DIVINE_IMMORTAL"}
_POSITIONS = ("1", "2", "3", "4", "5")
# 分位置胜率只算打到第几分钟还没结束的局。玩家 2026-10-26 定 30：速推局（一号位拉席克 35 分钟内结束的赢 72%）不算，
# 回测区分度 0.0666（全部局 0.0673、≥25 分钟 0.0676、≥35 分钟 0.0651），在噪声内。见 HANDOFF 第 29 节
POSITION_MIN_MINUTES = 30


def _stratz_brackets(cfg: Config) -> list[str]:
    if not (cfg.rank_min and cfg.rank_max):
        return ["ALL"]
    return sorted({_STRATZ_BRACKETS[m] for m in range(cfg.rank_min, cfg.rank_max + 1)})


def fetch_positions(cfg: Config) -> dict:
    """{英雄 id: {位置 1~5: [局数, 胜场]}}，STRATZ 最近一周、对应段位，只算打到 POSITION_MIN_MINUTES 分钟的局。
    STRATZ 不分时间的 stats 给的是「打到 maxTime 分钟还没结束」的局数 / 胜场，maxTime 不写默认 35（2026-10-26 实测），
    写 0 是全部对局"""
    q = """query($b: [RankBracketBasicEnum], $mx: Int) { heroStats { stats(bracketBasicIds: $b, groupByPosition: true, maxTime: $mx) {
             heroId position matchCount winCount } } }"""
    out: dict[str, dict[str, list[int]]] = defaultdict(dict)
    for r in _stratz(cfg, q, b=_stratz_brackets(cfg), mx=POSITION_MIN_MINUTES)["heroStats"]["stats"]:
        pos = (r["position"] or "").removeprefix("POSITION_")
        if pos in _POSITIONS:
            out[str(r["heroId"])][pos] = [r["matchCount"], r["winCount"]]
    return {"fetched_at": int(time.time()), "brackets": _stratz_brackets(cfg), "min_minutes": POSITION_MIN_MINUTES,
            "heroes": out}


def positions_path(cfg: Config) -> Path:
    return DATA_DIR / f"positions_r{cfg.rank_min}-{cfg.rank_max}.json"


# ---------- 没填 STRATZ token 时的分位置数据：OpenDota 分路 + 定位标签 ----------

# OpenDota /scenarios/laneRoles：每个英雄走优势路 / 中路 / 劣势路 / 打野的局数（已解析的对局，不分段位，每个英雄约 1000~2000 局）。
# 只分路不分位置：优势路按定位标签（Engine.fits_by_roles）拆成一号位 / 五号位，劣势路拆成三号位 / 四号位，
# 只有一边对得上标签的给它 ROLE_SPLIT，两边都对得上或都对不上就对半；打野对半算四 / 五号位。
# 胜场一律记一半：只用来定「这个英雄打不打这个位置」（位置筛选、猜分路），不按位置修正胜率。
# 1523 场样本外对局回测（HANDOFF 第 35 节）：猜对面单人位置 68.9%（STRATZ 77.8%，只看定位标签 35.2%），
# 实际选的英雄过位置筛选 87.7%（STRATZ 94.3%，只看定位标签 73.7%），推荐区分度在噪声内没变
OPENDOTA_POSITIONS = DATA_DIR / "positions_opendota.json"
ROLE_SPLIT = 0.9


def positions_from_lane_roles(db: HeroDB, lane_roles: dict[int, list[dict]]) -> dict[str, dict[str, list[int]]]:
    from .engine import Engine
    out = {}
    for h in db:
        lane = defaultdict(int)
        for r in lane_roles.get(h.id) or []:
            lane[int(r["lane_role"])] += int(r["games"])
        if not sum(lane.values()):
            continue
        fit = {p: Engine.fits_by_roles(h, p) for p in range(1, 6)}
        core = lambda a, b: ROLE_SPLIT if fit[a] and not fit[b] else 1 - ROLE_SPLIT if fit[b] and not fit[a] else 0.5
        s1, s3 = core(1, 5), core(3, 4)
        games = {1: lane[1] * s1, 2: lane[2], 3: lane[3] * s3,
                 4: lane[3] * (1 - s3) + lane[4] / 2, 5: lane[1] * (1 - s1) + lane[4] / 2}
        out[str(h.id)] = {str(p): [round(g), round(g) // 2] for p, g in games.items()}
    return out


def fetch_positions_opendota(db: HeroDB, log=_say) -> dict:
    lane_roles = {}
    for i, h in enumerate(db, 1):
        lane_roles[h.id] = _get(f"{OPENDOTA}/scenarios/laneRoles", hero_id=h.id)
        if i % 30 == 0:
            log(f"  {i} 个英雄 …")
        time.sleep(0.5)                    # OpenDota 免费接口每分钟 60 次
    return {"fetched_at": int(time.time()), "source": "opendota_lane_roles",
            "heroes": positions_from_lane_roles(db, lane_roles)}


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


# ---------- 我自己 ----------

MEDALS = {1: "先锋", 2: "卫士", 3: "中军", 4: "统帅", 5: "传奇", 6: "万古流芳", 7: "超凡入圣", 8: "冠绝一世"}
MAIN_POSITION_SHARE = 0.6   # 未解析的局：英雄在这个段位 60% 以上的局打某个位置，才按那个位置算


def rank_name(tier: int | None) -> str:
    if not tier:
        return "未知段位"
    medal, star = divmod(tier, 10)
    return MEDALS.get(medal, "?") + (f" {star} 星" if star and medal < 8 else "")


def _player_matches_stratz(cfg: Config, account_id: int | None = None, limit: int | None = None
                           ) -> list[tuple[int, int, bool]]:
    """[(英雄 id, 位置 1~5 / 0=未解析, 是否赢)]，每页 100 局；不传 account_id 就是我自己（查队友战绩时传队友的）"""
    account_id, limit = account_id or cfg.account_id, limit or cfg.history_matches
    q = """query($id: Long!, $skip: Int) { player(steamAccountId: $id) {
             matches(request: {take: 100, skip: $skip, lobbyTypeIds: [7]}) {
               players(steamAccountId: $id) { heroId position isVictory } } } }"""
    out = []
    for skip in range(0, limit, 100):
        page = _stratz(cfg, q, id=account_id, skip=skip)["player"]["matches"] or []
        for m in page:
            pl = m["players"][0]
            pos = (pl["position"] or "").removeprefix("POSITION_")
            out.append((pl["heroId"], int(pos) if pos in _POSITIONS else 0, pl["isVictory"]))
        if len(page) < 100:
            break
    return out[:limit]


def _player_matches_opendota(cfg: Config) -> list[tuple[int, int, bool]]:
    matches = _get(f"{OPENDOTA}/players/{cfg.account_id}/matches", limit=cfg.history_matches,
                   lobby_type=7, project=["hero_id", "player_slot", "radiant_win"])
    return [(m["hero_id"], 0, (m["player_slot"] < 128) == m["radiant_win"]) for m in matches]


def fetch_player(cfg: Config, log=_say) -> dict:
    """段位 + 最近 cfg.history_matches 局天梯每个英雄在每个位置的局数 / 胜场。
    有 STRATZ token 时带位置（未解析的局记为位置 0），否则全部记为位置 0。需要游戏里开了「公开比赛数据」。"""
    prof = _get(f"{OPENDOTA}/players/{cfg.account_id}")
    matches = None
    if cfg.stratz_token:
        try:
            matches = _player_matches_stratz(cfg)
        except (RuntimeError, requests.RequestException) as e:
            # STRATZ 数据库偶尔整体超时（NPGSQL TIMEOUT）：先用 OpenDota 顶上，这次没有位置信息
            log(f"  STRATZ 拉不到对局（{e}），改用 OpenDota（不带位置）")
    if matches is None:
        matches = _player_matches_opendota(cfg)
    heroes: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for hero, pos, win in matches:
        heroes[str(hero)][str(pos)][0] += 1
        heroes[str(hero)][str(pos)][1] += win
    return {
        "fetched_at": int(time.time()),
        "account_id": cfg.account_id,
        "name": (prof.get("profile") or {}).get("personaname"),
        "rank_tier": prof.get("rank_tier"),
        "matches": len(matches),
        "heroes": heroes,
    }


def update_player(cfg: Config, log=_say) -> None:
    if not cfg.account_id:
        return
    DATA_DIR.mkdir(exist_ok=True)
    log("拉取个人数据 …")
    me = fetch_player(cfg, log)
    (DATA_DIR / "player.json").write_text(json.dumps(me, ensure_ascii=False), encoding="utf-8")
    by_pos: dict[str, int] = defaultdict(int)
    for v in me["heroes"].values():
        for pos, (g, _) in v.items():
            by_pos[pos] += g
    dist = " ".join(f"{p}号位 {by_pos[p]}" for p in _POSITIONS) + f" 未解析 {by_pos['0']}"
    log(f"  {me['name']} | {rank_name(me['rank_tier'])} | 最近 {me['matches']} 局天梯：{dist}")
    if not me["matches"]:
        log("  没拿到对局记录：检查游戏设置里是否开了「公开比赛数据」")


def load_player(cfg: Config) -> dict | None:
    p = DATA_DIR / "player.json"
    if not cfg.account_id or not p.exists():
        return None
    me = json.loads(p.read_text(encoding="utf-8"))
    if me.get("account_id") != cfg.account_id:
        return None
    me["heroes"] = {int(h): {int(pos): tuple(gw) for pos, gw in v.items()} for h, v in me["heroes"].items()}
    return me


def player_history(me: dict, position: int, positions: dict[int, dict[int, tuple[int, int]]] | None
                   ) -> tuple[dict[int, tuple[int, int]], bool]:
    """我在这个位置上各英雄的 (局数, 胜场)；第二个返回值 = 是否真的按位置区分了。
    未解析的局按英雄的主位置归类（没有明显主位置就不计）；完全没有位置信息时退回不分位置的合计。"""
    has_pos = positions is not None or any(p for v in me["heroes"].values() for p in v)
    out: dict[int, tuple[int, int]] = {}
    for hero, per_pos in me["heroes"].items():
        g = w = 0
        for pos, (pg, pw) in per_pos.items():
            if pos == 0 and positions:
                pos = _main_position(positions, hero)
            if not has_pos or pos == position:
                g, w = g + pg, w + pw
        if g:
            out[hero] = (g, w)
    return out, has_pos


def _main_position(positions: dict[int, dict[int, tuple[int, int]]], hero: int) -> int:
    per = positions.get(hero) or {}
    total = sum(g for g, _ in per.values())
    if not total:
        return 0
    pos, (g, _) = max(per.items(), key=lambda t: t[1][0])
    return pos if g / total >= MAIN_POSITION_SHARE else 0


# ---------- 汇总 ----------

def update(cfg: Config, log=_say) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    log("拉取英雄表 …")
    heroes = fetch_heroes()
    (DATA_DIR / "heroes.json").write_text(json.dumps(heroes, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"  {len(heroes)} 个英雄")
    fetch_portraits(log)
    fetch_items_const(log)
    fetch_item_icons(log)
    log("知识库（Valve 官方技能 / 物品说明）…")
    from . import kb
    kb.update([h["id"] for h in heroes], log)
    fetch_icons(log)                       # 要知识库挑大招，放在它后面
    if cfg.stratz_token:
        log("拉取英雄分位置数据（STRATZ）…")
        pos = fetch_positions(cfg)
        if pos["heroes"]:
            positions_path(cfg).write_text(json.dumps(pos), encoding="utf-8")
            log(f"  {len(pos['heroes'])} 个英雄")
        else:   # 2026-10-26 STRATZ 偶尔返回空表，覆盖掉以后推荐退回按定位标签筛位置（赏金猎人进中单），所以保留旧的
            log("  STRATZ 返回了空数据，保留原来的分位置数据")
    elif not positions_path(cfg).exists():      # 打包版自带 STRATZ 的分位置数据，有就不用凑
        log("没填 STRATZ token：用 OpenDota 的分路数据凑分位置数据（约 2 分钟）…")
        try:
            pos = fetch_positions_opendota(load_heroes(), log)
        except NETWORK_ERRORS as e:
            log(f"  没拉到（{e}），{'沿用上次的' if OPENDOTA_POSITIONS.exists() else '推荐先按定位标签粗筛位置'}")
        else:
            OPENDOTA_POSITIONS.write_text(json.dumps(pos), encoding="utf-8")
            log(f"  {len(pos['heroes'])} 个英雄")
    update_player(cfg, log)
    log(f"拉取最近 {cfg.days} 天天梯对局的克制 / 配合数据 …")
    stats = fetch_stats(cfg, log)
    stats_path(cfg).write_text(json.dumps(stats), encoding="utf-8")
    log(f"完成：{stats['matches']} 局 → {stats_path(cfg).name}")


def load_stats(cfg: Config) -> dict:
    p = stats_path(cfg)
    if not p.exists():
        raise FileNotFoundError(f"没有数据文件 {p.name}，先运行：python -m dota_assistant update")
    return json.loads(p.read_text(encoding="utf-8"))
