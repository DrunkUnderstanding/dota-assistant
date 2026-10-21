"""Dota 2 Game State Integration：游戏把状态 POST 到本地 HTTP 服务。

官方功能、不碰游戏进程。玩家身份下拿不到敌方信息，这里只用它知道：
  我在天辉还是夜魇、现在是不是选人阶段、我选了哪个英雄。
启用方法见 README「GSI」一节（放 cfg 文件 + 启动项 -gamestateintegration）。
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DRAFT_STATES = {"DOTA_GAMERULES_STATE_HERO_SELECTION", "DOTA_GAMERULES_STATE_STRATEGY_TIME"}

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
        "abilities" "0"
        "items"     "0"
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
    def team(self) -> str | None:
        t = (self.raw.get("player") or {}).get("team_name")
        return t if t in ("radiant", "dire") else None

    @property
    def my_hero_key(self) -> str | None:
        name = (self.raw.get("hero") or {}).get("name") or ""
        return name.removeprefix("npc_dota_hero_") or None


class GSIServer:
    def __init__(self, port: int):
        self.state = GameState()
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    state.raw = json.loads(body or b"{}")
                    state.updated = time.time()
                except json.JSONDecodeError:
                    pass
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> GSIServer:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
