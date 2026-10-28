"""队友战绩：玩家手动填好友代码，查对方最近的战绩、常玩英雄、常打位置、和我一起打的胜负。

数据：OpenDota（资料、最近 100 局、和我一起的胜负）；填了 STRATZ token 再加最近 100 局天梯的位置。
只用公开接口，和在游戏里点开他资料、去 OpenDota 网站上看到的一样；对方没公开比赛数据就查不到。
查过的人记在 data/mates.json（个人数据，不打包），下次打开面板还在，点「刷新」重查。
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path

import requests

from .core.config import DATA_DIR, Config
from .sources import http, player

MATES_FILE = DATA_DIR / "mates.json"
KEEP = 8                         # 最多记几个人（新查的排最前，挤掉最久没查的）
FRESH = 20 * 60                  # 20 分钟内查过的再填一次不重查（点「刷新」才重查）
SAMPLE = 100                     # 常玩英雄 / 位置按最近多少局算
RECENT = 20                      # 「最近 N 局」胜负
TOP_HEROES = 6
STEAM64_BASE = 76561197960265728  # Steam 64 位 ID = 这个 + 好友代码


def parse_code(text) -> int:
    """好友代码：纯数字；也认 Steam 64 位 ID、OpenDota / STRATZ / Dotabuff 的个人页链接（取里面最长的那串数字）"""
    nums = re.findall(r"\d+", str(text or ""))
    if not nums:
        raise ValueError("请填好友代码（一串数字）")
    n = int(max(nums, key=len))
    if n > STEAM64_BASE:
        n -= STEAM64_BASE
    if not 0 < n < 2 ** 32:
        raise ValueError(f"{text} 不像好友代码")
    return n


def fetch(cfg: Config, account_id: int) -> dict:
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
    matches = http._get(f"{http.OPENDOTA}/players/{account_id}/matches", limit=SAMPLE,
                        project=["hero_id", "player_slot", "radiant_win", "start_time", "lobby_type",
                                 "kills", "deaths", "assists"])
    if not matches:
        return out                                   # 没公开比赛数据（或没打过），后面的也查不到
    win = lambda m: (m["player_slot"] < 128) == bool(m["radiant_win"])
    out["matches"] = [{"hero_id": m["hero_id"], "win": win(m), "kda": [m["kills"], m["deaths"], m["assists"]],
                       "t": m["start_time"], "ranked": m.get("lobby_type") == 7} for m in matches[:RECENT]]
    games, wins = Counter(), Counter()
    for m in matches:
        games[m["hero_id"]] += 1
        wins[m["hero_id"]] += win(m)
    out["heroes"] = [[h, g, wins[h]] for h, g in games.most_common(TOP_HEROES)]
    out["sample"] = len(matches)
    if cfg.account_id and cfg.account_id != account_id:
        # 不用 /wl?included_account_id：那个把对面的局也算进去；peers 分开了同队（with_）和对面（against_）
        me = next((p for p in http._get(f"{http.OPENDOTA}/players/{account_id}/peers")
                   if p.get("account_id") == cfg.account_id), {})
        out["with_me"] = [me.get("with_games", 0), me.get("with_win", 0)]
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


class Mates:
    """查过的队友（新查的在前），每人：好友代码、最后一次查到的数据、在不在查、出错提示"""

    def __init__(self, path: Path | None = None):
        self.path = path or MATES_FILE
        self.items: list[dict] = []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = []
        for r in raw if isinstance(raw, list) else []:
            if isinstance(r, dict) and isinstance(r.get("account_id"), int):
                self.items.append({"account_id": r["account_id"], "data": r.get("data"), "busy": False, "error": ""})

    def get(self, account_id: int) -> dict | None:
        return next((e for e in self.items if e["account_id"] == account_id), None)

    def touch(self, account_id: int) -> dict:
        """挪到最前（没有就加），超过 KEEP 个挤掉最后的"""
        e = self.get(account_id) or {"account_id": account_id, "data": None, "busy": False, "error": ""}
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
        e["busy"], e["error"] = False, err
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

    def view(self, db) -> list[dict]:
        out = []
        for e in self.items:
            d = e["data"] or {}
            v = {"account_id": e["account_id"], "busy": e["busy"], "error": e["error"], "loaded": bool(d),
                 "name": d.get("name") or "", "fetched_at": d.get("fetched_at")}
            if d:
                rank = player.rank_name(d.get("rank_tier")) if d.get("rank_tier") else "没定段 / 不公开"
                if d.get("leaderboard_rank"):
                    rank += f" · 第 {d['leaderboard_rank']} 名"
                name = lambda h: (x.name_cn if (x := db.by_id.get(h)) else f"英雄 {h}")
                v.update(rank=rank, private=not d["matches"], with_me=d.get("with_me"),
                         sample=d.get("sample", 0), positions=d.get("positions"),
                         positions_sample=d.get("positions_sample", 0), positions_note=d.get("positions_note", ""),
                         matches=[{**m, "name": name(m["hero_id"])} for m in d["matches"]],
                         heroes=[{"id": h, "name": name(h), "games": g, "wins": w} for h, g, w in d["heroes"]])
            out.append(v)
        return out
