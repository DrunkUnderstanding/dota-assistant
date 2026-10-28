"""网页界面的本地服务 + Electron 启动。

  Python（live.Live：识别 / 推荐 / 语音 / GSI / 热键）
    └ 本机 HTTP 服务（只监听 127.0.0.1）
        ├ /overlay.html /panel.html /topbar.html …   web/ 下的静态页面
        ├ /portraits/<id>.png           英雄头像（data/portraits）
        ├ /items/<key>.png              装备图标（data/items）
        ├ /icons/abilities/<key>.png    技能图标 / 金币图标（icons/gold.png）：第一次从 Valve CDN 下，存 data/icons
        ├ /api/events                   SSE：状态一变就推一份完整快照
        └ /api/action                   POST：控制面板的操作
    └ Electron（electron/main.js）：开三个窗口加载上面的页面
        ├ 叠加层：透明、置顶、鼠标穿透、不抢焦点（普通窗口，不注入游戏）；解锁后可以拖动，固定后恢复穿透
        ├ 买活 / 大招计时条：同样透明置顶，盖在游戏顶栏上，每个英雄头像下面一个买活小条、对面的再加一个大招小条（topbar.html）
        └ 控制面板：普通窗口，关掉它整个助手退出

没装 Electron 时可以 --browser 用浏览器打开（叠加层只能当预览看，盖不到游戏上）。
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import queue
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

from .core.config import DATA_DIR, ROOT, Config
from .core.log import HTTPServer

log = logging.getLogger(__name__)
elog = logging.getLogger("dota_assistant.electron")      # Electron 主进程 / 页面的输出

WEB_DIR = ROOT / "web"
ELECTRON_DIR = ROOT / "electron"
# Chromium（Electron）拒绝访问的端口（net/base/port_util.cc 的 kRestrictedPorts，1024 以上的部分）：
# 随机端口分到这些，界面直接 ERR_UNSAFE_PORT 打不开。这台机器的动态端口从 1024 起，2026-10-27 实测分到过 6669
UNSAFE_PORTS = {1719, 1720, 1723, 2049, 3659, 4045, 4190, 5060, 5061, 6000, 6566, 6665, 6666, 6667, 6668, 6669, 6679,
                6697, 10080}
UI_DIR = ROOT / "ui"            # 打包版（packaging/build.py）：Electron 本体 + resources/app 里的 main.js，双击就能跑
PORTRAIT_DIR = DATA_DIR / "portraits"
ITEM_ICON_DIR = DATA_DIR / "items"
JSON_TYPE = "application/json; charset=utf-8"


def overlay_config(cfg: Config, pos: dict | None = None) -> dict:
    """pos：用户拖过的位置（比例）和拉过的宽度，优先于 config.toml 里的 overlay_x / overlay_y / overlay_width"""
    pos = pos or {"x": cfg.overlay_x, "y": cfg.overlay_y}
    return {"monitor": cfg.monitor, "x": pos["x"], "y": pos["y"], "width": pos.get("width", cfg.overlay_width),
            "opacity": cfg.overlay_opacity, "buyback_timer": cfg.buyback_timer, "ult_timer": cfg.ult_timer}


class WebServer:
    """live 需要有 current_snapshot / subscribe / unsubscribe / submit / action / heroes / cfg（测试里可以换成假的）"""

    def __init__(self, live, port: int = 0):
        self.live = live
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/":
                    self.send_response(302)
                    self.send_header("Location", "/panel.html")
                    self.end_headers()
                elif path == "/api/events":
                    server._events(self)
                elif path == "/api/state":
                    self._send(200, server.live.current_snapshot or "{}", JSON_TYPE)
                elif path == "/api/heroes":
                    self._json(200, server.live.heroes())
                elif path == "/api/config":
                    self._json(200, overlay_config(server.live.cfg, server.live.overlay_pos))
                elif path.startswith("/portraits/"):
                    self._file(PORTRAIT_DIR, path.removeprefix("/portraits/"))
                elif path.startswith("/items/"):
                    self._file(ITEM_ICON_DIR, path.removeprefix("/items/"))
                elif path.startswith("/icons/"):
                    from .sources.assets import ICON_DIR, fetch_icon
                    rel = path.removeprefix("/icons/")
                    if fetch_icon(rel):
                        self._file(ICON_DIR, rel)
                    else:
                        self._json(404, {"ok": False, "message": "没有这个图标"})
                else:
                    self._file(WEB_DIR, path.lstrip("/"))

            def do_POST(self):
                if urlparse(self.path).path != "/api/action":
                    return self._json(404, {"ok": False, "message": "没有这个接口"})
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                    msg = server.live.submit(server.live.action, body).result(timeout=20)
                    self._json(200, {"ok": True, "message": msg or ""})
                except TimeoutError:                                # 工作线程卡住了：最值得查的一种
                    log.warning("网页操作 20 秒没做完：%s", body)
                    self._json(400, {"ok": False, "message": "助手忙不过来，操作超时（详情见日志）"})
                except Exception as e:                              # 操作本身的错误 Live 已经记过日志
                    self._json(400, {"ok": False, "message": str(e)})

            def _file(self, base: Path, rel: str):
                target = (base / rel).resolve()
                if base.resolve() not in target.parents or not target.is_file():
                    return self._json(404, {"ok": False, "message": "没有这个文件"})
                ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                if ctype.startswith("text/") or ctype.endswith("javascript"):
                    ctype += "; charset=utf-8"
                self._send(200, target.read_bytes(), ctype)

            def _json(self, code: int, obj):
                self._send(code, json.dumps(obj, ensure_ascii=False), JSON_TYPE)

            def _send(self, code: int, body, ctype: str):
                data = body.encode() if isinstance(body, str) else body
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", port), Handler)
        while not port and self.server.server_address[1] in UNSAFE_PORTS:   # 换一个，旧的关掉前先占着免得又分到它
            old, self.server = self.server, HTTPServer(("127.0.0.1", 0), Handler)
            old.server_close()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="webui")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> WebServer:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _events(self, h: BaseHTTPRequestHandler) -> None:
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream; charset=utf-8")
        h.send_header("Cache-Control", "no-store")
        h.end_headers()
        q = self.live.subscribe()
        try:
            while True:
                try:
                    h.wfile.write(f"data: {q.get(timeout=15)}\n\n".encode())
                except queue.Empty:
                    h.wfile.write(b": ping\n\n")                   # 保活，顺便发现断开的连接
                h.wfile.flush()
        except OSError:                                             # 页面关了 / 刷新了
            pass
        finally:
            self.live.unsubscribe(q)


def find_electron() -> Path | None:
    for exe in (UI_DIR / "electron.exe", ELECTRON_DIR / "node_modules" / "electron" / "dist" / "electron.exe"):
        if exe.exists():
            return exe
    return None


def electron_command(exe: Path, url: str) -> list[str]:
    """打包版的界面代码在 ui/resources/app，Electron 自己会加载；开发版要把 electron/ 目录传给它"""
    app = [] if exe.parent == UI_DIR else [str(ELECTRON_DIR)]
    return [str(exe), *app, f"--server={url}"]


def serve(live, browser: bool = False) -> int:
    """起服务 + 打开界面，直到控制面板关掉（Electron）或 Ctrl+C（浏览器模式）"""
    srv = WebServer(live).start()
    live.start()
    try:
        if browser:
            webbrowser.open(srv.url + "/panel.html")
            webbrowser.open(srv.url + "/overlay.html?preview=1")
            log.info("浏览器模式：%s", srv.url)
            print(f"界面：{srv.url}/panel.html（叠加层预览：{srv.url}/overlay.html?preview=1，"
                  f"买活计时条预览：{srv.url}/topbar.html?preview=1），Ctrl+C 退出")
            threading.Event().wait()
            return 0
        exe = find_electron()
        if exe is None:
            log.warning("没找到 Electron（%s）", ELECTRON_DIR / "node_modules")
            print("没找到 Electron。先装一次：cd electron && npm install（需要 Node.js）\n"
                  "或者先用 run --browser 在浏览器里看界面（叠加层盖不到游戏上）。")
            return 1
        env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"}
        proc = subprocess.Popen(electron_command(exe, srv.url), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        log.info("界面服务 %s，已启动 Electron（pid %d）", srv.url, proc.pid)
        threading.Thread(target=_pipe_electron, args=(proc.stdout,), daemon=True, name="electron-log").start()
        try:
            code = proc.wait()
            log.info("Electron 退出，返回 %s", code)
            return code
        except KeyboardInterrupt:
            proc.terminate()
            return 0
    except KeyboardInterrupt:
        return 0
    finally:
        log.info("助手退出")
        live.close()
        srv.stop()


def _pipe_electron(stream) -> None:
    """Electron 的输出逐行转进日志。main.js 自己写的行以级别开头（INFO / WARN / ERROR），
    其余是 Chromium 自己的输出（GPU 之类），按 INFO 记，不刷控制台。"""
    levels = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARN": logging.WARNING, "ERROR": logging.ERROR}
    for raw in iter(stream.readline, b""):
        line = raw.decode("utf-8", "replace").rstrip()
        if not line:
            continue
        head, _, rest = line.partition(" ")
        if head in levels:
            elog.log(levels[head], rest)
        else:
            elog.info(line)


def run(cfg: Config, browser: bool = False) -> int:
    from .live import Live
    return serve(Live(cfg), browser)
