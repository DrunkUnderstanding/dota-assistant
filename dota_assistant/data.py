"""拉取并缓存英雄表、克制矩阵、配合矩阵。

数据源：
- 中文英雄名：Valve 官方 datafeed
- 英雄定位标签：OpenDota /heroStats
- 克制 / 配合：OpenDota explorer 直接聚合 public_matches（天梯全英雄选择，可按段位过滤）
- 英雄分位置场次 / 胜率、你每局打的位置：STRATZ（需要 token）
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path

import requests

from .core.config import DATA_DIR, Config
from .core.heroes import HeroDB
from .sources import assets, http
from .sources import stats as stats_mod


def fetch_icons(log=http._say) -> None:
    """顶栏 / 买活自动记要的图标一次下全，局内不用临时下：金币、每个英雄的小图标、每个对面会给小条的大招图标。
    已有的跳过；第一张就下不到（断网）就不试剩下的、不写标记，下次再补；个别 CDN 没有的照样写标记（运行时按需再试）"""
    from concurrent.futures import ThreadPoolExecutor
    from . import kb, ultcd
    base = kb.KnowledgeBase.load()
    rels = ["icons/gold.png"] + [f"heroes/icons/{h.key}.png" for h in assets.load_heroes().by_id.values()]
    rels += [f"abilities/{ab.key}.png" for h in (base.heroes.values() if base else ()) if (ab := ultcd.pick(h))]
    todo = [r for r in rels if not (assets.ICON_DIR / r).is_file()]
    if todo and assets.fetch_icon(todo[0]) is None:
        log("  顶栏图标没下到（断网？），局内用到时再下")
        return
    with ThreadPoolExecutor(8) as pool:
        missing = [r for r, p in zip(todo[1:], pool.map(assets.fetch_icon, todo[1:])) if p is None]
    assets.ICONS_DONE.write_text(base.patch if base else "", encoding="utf-8")
    log(f"  顶栏图标：新下载 {len(todo) - len(missing)} 张，共 {len(rels) - len(missing)}/{len(rels)} 张"
        + (f"（CDN 没有：{'、'.join(missing)}）" if missing else ""))


# ---------- 没填 STRATZ token 时的分位置数据：OpenDota 分路 + 定位标签 ----------

# OpenDota /scenarios/laneRoles：每个英雄走优势路 / 中路 / 劣势路 / 打野的局数（已解析的对局，不分段位，每个英雄约 1000~2000 局）。
# 只分路不分位置：优势路按定位标签（Engine.fits_by_roles）拆成一号位 / 五号位，劣势路拆成三号位 / 四号位，
# 只有一边对得上标签的给它 ROLE_SPLIT，两边都对得上或都对不上就对半；打野对半算四 / 五号位。
# 胜场一律记一半：只用来定「这个英雄打不打这个位置」（位置筛选、猜分路），不按位置修正胜率。
# 1523 场样本外对局回测（HANDOFF 第 35 节）：猜对面单人位置 68.9%（STRATZ 77.8%，只看定位标签 35.2%），
# 实际选的英雄过位置筛选 87.7%（STRATZ 94.3%，只看定位标签 73.7%），推荐区分度在噪声内没变
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


def fetch_positions_opendota(db: HeroDB, log=http._say) -> dict:
    lane_roles = {}
    for i, h in enumerate(db, 1):
        lane_roles[h.id] = http._get(f"{http.OPENDOTA}/scenarios/laneRoles", hero_id=h.id)
        if i % 30 == 0:
            log(f"  {i} 个英雄 …")
        time.sleep(0.5)                    # OpenDota 免费接口每分钟 60 次
    return {"fetched_at": int(time.time()), "source": "opendota_lane_roles",
            "heroes": positions_from_lane_roles(db, lane_roles)}


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
        page = http._stratz(cfg, q, id=account_id, skip=skip)["player"]["matches"] or []
        for m in page:
            pl = m["players"][0]
            pos = (pl["position"] or "").removeprefix("POSITION_")
            out.append((pl["heroId"], int(pos) if pos in stats_mod._POSITIONS else 0, pl["isVictory"]))
        if len(page) < 100:
            break
    return out[:limit]


def _player_matches_opendota(cfg: Config) -> list[tuple[int, int, bool]]:
    matches = http._get(f"{http.OPENDOTA}/players/{cfg.account_id}/matches", limit=cfg.history_matches,
                   lobby_type=7, project=["hero_id", "player_slot", "radiant_win"])
    return [(m["hero_id"], 0, (m["player_slot"] < 128) == m["radiant_win"]) for m in matches]


def fetch_player(cfg: Config, log=http._say) -> dict:
    """段位 + 最近 cfg.history_matches 局天梯每个英雄在每个位置的局数 / 胜场。
    有 STRATZ token 时带位置（未解析的局记为位置 0），否则全部记为位置 0。需要游戏里开了「公开比赛数据」。"""
    prof = http._get(f"{http.OPENDOTA}/players/{cfg.account_id}")
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


def update_player(cfg: Config, log=http._say) -> None:
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
    dist = " ".join(f"{p}号位 {by_pos[p]}" for p in stats_mod._POSITIONS) + f" 未解析 {by_pos['0']}"
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

def update(cfg: Config, log=http._say) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    log("拉取英雄表 …")
    heroes = assets.fetch_heroes()
    (DATA_DIR / "heroes.json").write_text(json.dumps(heroes, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"  {len(heroes)} 个英雄")
    assets.fetch_portraits(log)
    assets.fetch_items_const(log)
    assets.fetch_item_icons(log)
    log("知识库（Valve 官方技能 / 物品说明）…")
    from . import kb
    kb.update([h["id"] for h in heroes], log)
    fetch_icons(log)                       # 要知识库挑大招，放在它后面
    if cfg.stratz_token:
        log("拉取英雄分位置数据（STRATZ）…")
        pos = stats_mod.fetch_positions(cfg)
        if pos["heroes"]:
            stats_mod.positions_path(cfg).write_text(json.dumps(pos), encoding="utf-8")
            log(f"  {len(pos['heroes'])} 个英雄")
        else:   # 2026-10-26 STRATZ 偶尔返回空表，覆盖掉以后推荐退回按定位标签筛位置（赏金猎人进中单），所以保留旧的
            log("  STRATZ 返回了空数据，保留原来的分位置数据")
    elif not stats_mod.positions_path(cfg).exists():      # 打包版自带 STRATZ 的分位置数据，有就不用凑
        log("没填 STRATZ token：用 OpenDota 的分路数据凑分位置数据（约 2 分钟）…")
        try:
            pos = fetch_positions_opendota(assets.load_heroes(), log)
        except http.NETWORK_ERRORS as e:
            log(f"  没拉到（{e}），{'沿用上次的' if stats_mod.OPENDOTA_POSITIONS.exists() else '推荐先按定位标签粗筛位置'}")
        else:
            stats_mod.OPENDOTA_POSITIONS.write_text(json.dumps(pos), encoding="utf-8")
            log(f"  {len(pos['heroes'])} 个英雄")
    update_player(cfg, log)
    log(f"拉取最近 {cfg.days} 天天梯对局的克制 / 配合数据 …")
    stats = stats_mod.fetch_stats(cfg, log)
    stats_mod.stats_path(cfg).write_text(json.dumps(stats), encoding="utf-8")
    log(f"完成：{stats['matches']} 局 → {stats_mod.stats_path(cfg).name}")
