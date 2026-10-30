"""个人数据：段位、最近天梯每个英雄在每个位置的局数 / 胜场（OpenDota，有 STRATZ token 时带位置），
每个英雄的生涯局数（OpenDota，推荐分「会玩的 / 不熟的」用，也拉双排队友的）。"""
from __future__ import annotations

import json
import time
from collections import defaultdict

import requests

from ..core.config import DATA_DIR, Config
from . import http, stats

MEDALS = {1: "先锋", 2: "卫士", 3: "中军", 4: "统帅", 5: "传奇", 6: "万古流芳", 7: "超凡入圣", 8: "冠绝一世"}
MAIN_POSITION_SHARE = 0.6   # 未解析的局：英雄在这个段位 60% 以上的局打某个位置，才按那个位置算
PLAYER_FILE = DATA_DIR / "player.json"
DUO_FILE = DATA_DIR / "duo.json"     # 双排队友（cfg.duo_account_id）的名字 + 生涯每个英雄局数


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
            out.append((pl["heroId"], int(pos) if pos in stats._POSITIONS else 0, pl["isVictory"]))
        if len(page) < 100:
            break
    return out[:limit]


def _player_matches_opendota(cfg: Config) -> list[tuple[int, int, bool]]:
    matches = http._get(f"{http.OPENDOTA}/players/{cfg.account_id}/matches", limit=cfg.history_matches,
                   lobby_type=7, project=["hero_id", "player_slot", "radiant_win"])
    return [(m["hero_id"], 0, (m["player_slot"] < 128) == m["radiant_win"]) for m in matches]


def fetch_career(account_id: int, log=http._say) -> dict[str, list[int]] | None:
    """{英雄 id: [生涯局数, 胜场]}（OpenDota 默认口径：不含快速模式等非常规模式）。
    资料没公开时 OpenDota 照样回全部英雄、局数全是 0，这时返回 None（= 不知道，推荐不按熟练度分）；拉不到也是 None"""
    try:
        rows = http._get(f"{http.OPENDOTA}/players/{account_id}/heroes")
    except (RuntimeError, requests.RequestException) as e:
        log(f"  拉不到 {account_id} 的生涯英雄局数（{e}），这次推荐不按熟练度分")
        return None
    career = {str(r["hero_id"]): [r["games"], r["win"]] for r in rows if r.get("games")}
    return career or None


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
        "career": fetch_career(cfg.account_id, log),
    }


def update_player(cfg: Config, log=http._say) -> None:
    if cfg.duo_account_id:
        update_duo(cfg, log)
    if not cfg.account_id:
        return
    PLAYER_FILE.parent.mkdir(exist_ok=True)
    log("拉取个人数据 …")
    me = fetch_player(cfg, log)
    PLAYER_FILE.write_text(json.dumps(me, ensure_ascii=False), encoding="utf-8")
    by_pos: dict[str, int] = defaultdict(int)
    for v in me["heroes"].values():
        for pos, (g, _) in v.items():
            by_pos[pos] += g
    dist = " ".join(f"{p}号位 {by_pos[p]}" for p in stats._POSITIONS) + f" 未解析 {by_pos['0']}"
    log(f"  {me['name']} | {rank_name(me['rank_tier'])} | 最近 {me['matches']} 局天梯：{dist}")
    if not me["matches"]:
        log("  没拿到对局记录：检查游戏设置里是否开了「公开比赛数据」")
    if me["career"]:
        log(f"  生涯 {sum(g for g, _ in me['career'].values())} 局，打过 {cfg.familiar_games} 局以上的英雄 "
            f"{sum(g >= cfg.familiar_games for g, _ in me['career'].values())} 个")


def update_duo(cfg: Config, log=http._say) -> None:
    """双排队友：名字 + 生涯每个英雄局数，写 DUO_FILE（推荐给队友的位置按他会玩的分组）"""
    log("拉取双排队友的生涯英雄局数 …")
    try:
        prof = http._get(f"{http.OPENDOTA}/players/{cfg.duo_account_id}")
    except (RuntimeError, requests.RequestException):
        prof = {}
    duo = {"fetched_at": int(time.time()), "account_id": cfg.duo_account_id,
           "name": (prof.get("profile") or {}).get("personaname"),
           "career": fetch_career(cfg.duo_account_id, log)}
    DUO_FILE.parent.mkdir(exist_ok=True)
    DUO_FILE.write_text(json.dumps(duo, ensure_ascii=False), encoding="utf-8")
    log(f"  {duo['name'] or cfg.duo_account_id}："
        + (f"生涯 {sum(g for g, _ in duo['career'].values())} 局" if duo["career"] else "没拿到（资料没公开？），给队友的推荐不按熟练度分"))


def load_player(cfg: Config) -> dict | None:
    if not cfg.account_id or not PLAYER_FILE.exists():
        return None
    me = json.loads(PLAYER_FILE.read_text(encoding="utf-8"))
    if me.get("account_id") != cfg.account_id:
        return None
    me["heroes"] = {int(h): {int(pos): tuple(gw) for pos, gw in v.items()} for h, v in me["heroes"].items()}
    me["career"] = _career(me.get("career"))
    return me


def load_duo(cfg: Config) -> dict | None:
    """{name, career}；没填双排队友、还没拉过、换了人都是 None"""
    if not cfg.duo_account_id or not DUO_FILE.exists():
        return None
    duo = json.loads(DUO_FILE.read_text(encoding="utf-8"))
    if duo.get("account_id") != cfg.duo_account_id:
        return None
    duo["career"] = _career(duo.get("career"))
    return duo


def _career(raw: dict | None) -> dict[int, tuple[int, int]] | None:
    return {int(h): tuple(gw) for h, gw in raw.items()} if raw else None


def needs_update(cfg: Config) -> bool:
    """个人 / 双排队友数据缺了：换了好友代码、新填了双排队友、老 player.json 还没有生涯局数（2026-10-30 加的）"""
    if cfg.account_id:
        if not PLAYER_FILE.exists():
            return True
        me = json.loads(PLAYER_FILE.read_text(encoding="utf-8"))
        if me.get("account_id") != cfg.account_id or "career" not in me:
            return True
    if cfg.duo_account_id:
        if not DUO_FILE.exists():
            return True
        if json.loads(DUO_FILE.read_text(encoding="utf-8")).get("account_id") != cfg.duo_account_id:
            return True
    return False


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
