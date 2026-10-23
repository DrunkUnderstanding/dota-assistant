"""Dota 2 Game State Integration：游戏把状态 POST 到本地 HTTP 服务。

官方功能、不碰游戏进程。玩家身份下拿不到敌方信息，这里只用它知道：
  我在天辉还是夜魇、现在是不是选人阶段、我选了哪个英雄；
  对局中：游戏时间、自己的装备 / 正补 / 死亡次数（给局内缺口提醒用）；
  自己英雄的血量 / 状态 / 装备和技能冷却，留最近一分钟的推送（给死亡复盘用，death.py）。
启用方法见 README「GSI」一节（放 cfg 文件 + 启动项 -gamestateintegration）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from .log import HTTPServer

STRATEGY_STATE = "DOTA_GAMERULES_STATE_STRATEGY_TIME"   # 选完人到出门之间
SELECTION_STATE = "DOTA_GAMERULES_STATE_HERO_SELECTION"
DRAFT_STATES = {SELECTION_STATE, STRATEGY_STATE}
MATCH_STATES = {"DOTA_GAMERULES_STATE_PRE_GAME", "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"}

HISTORY_SECONDS = 60         # 对局中留最近多久的推送（死亡复盘往回看 20 秒，多留点余量）
log = logging.getLogger(__name__)

CFG_NAME = "gamestate_integration_dota_assistant.cfg"
CFG_TEMPLATE = """"Dota Assistant"
{{
    "uri"           "http://127.0.0.1:{port}/"
    "timeout"       "5.0"
    "buffer"        "0.1"
    "throttle"      "0.5"
    "heartbeat"     "10.0"
    "data"
    {{
        "provider"  "1"
        "map"       "1"
        "player"    "1"
        "hero"      "1"
        "abilities" "1"
        "items"     "1"
        "draft"     "1"
        "wearables" "0"
    }}
}}
"""


def install_cfg(dota_dir: Path, port: int) -> Path:
    """dota_dir = ...\\steamapps\\common\\dota 2 beta"""
    target = dota_dir / "game" / "dota" / "cfg" / "gamestate_integration"
    if not (dota_dir / "game" / "dota").exists():
        raise FileNotFoundError(f"{dota_dir} 不像 Dota 2 安装目录（找不到 game\\dota）")
    target.mkdir(parents=True, exist_ok=True)
    path = target / CFG_NAME
    path.write_text(CFG_TEMPLATE.format(port=port), encoding="utf-8")
    return path


class GameState:
    def __init__(self):
        self.raw: dict = {}
        self.updated = 0.0
        self._last_logged = ""
        self._history: deque[tuple[float, dict]] = deque()  # 对局中的 (收到时间, 推送)
        self._lock = threading.Lock()                         # GSI 线程写、主控线程读

    def update(self, raw: dict, now: float | None = None) -> None:
        """now：记进历史的时间（测试用），默认就是收到的时间"""
        self.raw, self.updated = raw, time.time()
        now = self.updated if now is None else now
        with self._lock:
            if not self.in_match:
                self._history.clear()
                return
            self._history.append((now, raw))
            while self._history[0][0] < now - HISTORY_SECONDS:
                self._history.popleft()

    def history(self) -> list[tuple[float, dict]]:
        with self._lock:
            return list(self._history)

    def log_draft(self) -> None:
        """选人阶段的推送自动存到 shots/gsi/，用来确认玩家视角下 GSI 给不给双方阵容（不用另开 gsi-dump）。
        内容没变不存；最多留最近 300 份。"""
        if not self.in_draft:
            return
        text = json.dumps(self.raw, ensure_ascii=False, sort_keys=True)
        if text == self._last_logged:
            return
        self._last_logged = text
        from .config import ROOT
        d = ROOT / "shots" / "gsi"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.json").write_text(
            json.dumps(self.raw, ensure_ascii=False, indent=1), encoding="utf-8")
        for old in sorted(d.glob("*.json"))[:-300]:
            old.unlink(missing_ok=True)

    @property
    def alive(self) -> bool:
        """最近 30 秒内收到过游戏的推送（heartbeat 10 秒）"""
        return time.time() - self.updated < 30

    @property
    def game_state(self) -> str | None:
        return (self.raw.get("map") or {}).get("game_state")

    @property
    def in_draft(self) -> bool:
        return self.game_state in DRAFT_STATES

    @property
    def in_match(self) -> bool:
        """出门（号角前）到比赛结束"""
        return self.game_state in MATCH_STATES

    @property
    def match_id(self) -> str:
        return str((self.raw.get("map") or {}).get("matchid") or "")

    @property
    def clock_minutes(self) -> float:
        """游戏时间（分钟），号角前是负数"""
        return float((self.raw.get("map") or {}).get("clock_time") or 0) / 60

    @property
    def deaths(self) -> int:
        """死亡次数：用计数而不是 hero.alive 判断「刚死了」，采样间隔再长也不会漏"""
        return int((self.raw.get("player") or {}).get("deaths") or 0)

    @property
    def last_hits(self) -> int | None:
        v = (self.raw.get("player") or {}).get("last_hits")
        return int(v) if v is not None else None

    @property
    def net_worth(self) -> int | None:
        """GSI 不一定给经济总值（没在真实推送里确认过），没有就是 None"""
        v = (self.raw.get("player") or {}).get("net_worth")
        return int(v) if v is not None else None

    @property
    def items(self) -> set[str]:
        """身上 + 背包 + 储藏处的装备，OpenDota 的 key（去掉 item_ 前缀）"""
        out = set()
        for slot, it in (self.raw.get("items") or {}).items():
            name = (it or {}).get("name") or ""
            if slot.startswith(("slot", "stash")) and name.startswith("item_"):
                out.add(name.removeprefix("item_"))
        return out

    @property
    def team(self) -> str | None:
        t = (self.raw.get("player") or {}).get("team_name")
        return t if t in ("radiant", "dire") else None

    @property
    def player_name(self) -> str:
        return str((self.raw.get("player") or {}).get("name") or "")

    @property
    def team_slot(self) -> int | None:
        """我在自己队里是第几个（0~4，和顶栏从左到右的顺序一致）"""
        v = (self.raw.get("player") or {}).get("team_slot")
        return v if isinstance(v, int) else None

    @property
    def my_hero_key(self) -> str | None:
        name = (self.raw.get("hero") or {}).get("name") or ""
        return name.removeprefix("npc_dota_hero_") or None

    def summary(self) -> tuple:
        """日志用：这几项变了才记一行（推送每 0.5 秒一次，全记会刷屏）"""
        return self.game_state, self.team, self.my_hero_key, self.match_id


class GSIServer:
    def __init__(self, port: int):
        self.state = GameState()
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    raw = json.loads(body or b"{}")
                except json.JSONDecodeError:
                    log.warning("GSI 推送不是 JSON：%r", body[:200])
                else:
                    was_alive, before = state.alive, state.summary()
                    state.update(raw)
                    if not was_alive:
                        log.info("GSI 收到游戏推送（字段 %s）", sorted(raw))
                    if state.summary() != before or not was_alive:
                        log.info("GSI：阶段 %s · 阵营 %s · 英雄 %s · 比赛 %s", *state.summary())
                    log.debug("GSI 推送：时间 %.1f 分 · 死亡 %s · 正补 %s", state.clock_minutes, state.deaths,
                              state.last_hits)
                    state.log_draft()
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="gsi")
        log.info("GSI 服务在 127.0.0.1:%d 等游戏推送", port)

    def start(self) -> GSIServer:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()


def dump(port: int, out_dir) -> None:
    """只收 GSI 推送、不做别的：每次内容变化就存一份 JSON，并打印顶层字段和 draft 部分。
    用来在游戏电脑上确认「玩家身份选人时，GSI 到底给了什么」。Ctrl+C 结束。"""
    from pathlib import Path
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    srv = GSIServer(port).start()
    print(f"在 127.0.0.1:{port} 等 Dota 推送，内容存到 {out}（Ctrl+C 结束）", flush=True)
    last, n = None, 0
    try:
        while True:
            time.sleep(0.5)
            raw = srv.state.raw
            text = json.dumps(raw, ensure_ascii=False, sort_keys=True)
            if not raw or text == last:
                continue
            last, n = text, n + 1
            (out / f"{n:04d}.json").write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
            draft = raw.get("draft")
            print(f"[{n}] 阶段 {srv.state.game_state}  阵营 {srv.state.team}  我的英雄 {srv.state.my_hero_key}  "
                  f"字段 {sorted(raw)}  draft {'有内容' if draft else '空 / 没有'}", flush=True)
            if draft:
                print("    draft:", json.dumps(draft, ensure_ascii=False)[:600], flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()
