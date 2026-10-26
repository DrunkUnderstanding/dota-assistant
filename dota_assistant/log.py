"""日志：每次启动写一个文件 logs/<时间>-<命令>.log，只留最近 KEEP 个。

- 文件里记 INFO 及以上（config.toml 的 log_level 可改成 DEBUG）；控制台（「启动助手」的黑窗口）只显示本程序的 WARNING 及以上；
- 主线程 / 后台线程里没接住的异常带完整堆栈写进去；
- STRATZ token、AI API Key 不会出现在日志里（输出前替换成 ***，包括异常堆栈）；
- Electron 界面的输出、页面脚本报错也转进来（logger 名 electron，见 webui.py）。
出问题时把最新的日志文件发给开发者（控制面板「打开日志」，或 python -m dota_assistant logs）。

各模块照常 logging.getLogger(__name__)；没调 setup() 时（单元测试、被别的代码引用）什么都不输出。
"""
from __future__ import annotations

import dataclasses
import logging
import logging.handlers
import os
import platform
import subprocess
import sys
import threading
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

from .config import ROOT, Config

LOG_DIR = ROOT / "logs"
KEEP = 30                          # 留最近多少次启动的日志
MAX_BYTES = 5 * 1024 * 1024        # 单次启动的日志超过这么大就滚动（开着助手打一晚上也不会无限长）
BACKUPS = 2
PKG = "dota_assistant"

current: Path | None = None        # 这次启动写的日志文件
_handlers: list[logging.Handler] = []
log = logging.getLogger(__name__)


class _Formatter(logging.Formatter):
    """logger 名去掉包名前缀；输出前把密钥换成 ***（异常堆栈里也换）。brief=True 时不带堆栈（控制台用，堆栈看文件）"""

    def __init__(self, fmt: str, datefmt: str, secrets: list[str], brief: bool = False):
        super().__init__(fmt, datefmt)
        self.secrets = [s for s in secrets if len(s) >= 8]
        self.brief = brief

    def format(self, record: logging.LogRecord) -> str:
        if self.brief and (record.exc_info or record.exc_text):
            record = logging.makeLogRecord({**record.__dict__, "exc_info": None, "exc_text": None})
            record.msg, record.args = f"{record.getMessage()}（详情见日志）", None
        record.mod = record.name.removeprefix(PKG + ".")
        if record.threadName.endswith("(process_request_thread)"):
            record.threadName = "http"                         # 本机 HTTP 服务（GSI / 界面）每个请求一个线程，名字没意义
        text = super().format(record)
        for s in self.secrets:
            text = text.replace(s, "***")
        return text


def setup(cfg: Config, command: str, to_file: bool = True) -> Path | None:
    """配置日志；to_file=False 时只在控制台显示警告（查询类命令不值得留文件）。可以重复调用。"""
    global current
    root = logging.getLogger()
    for h in _handlers:
        root.removeHandler(h)
        h.close()
    _handlers.clear()
    secrets = [cfg.stratz_token, cfg.ai_api_key]

    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.addFilter(logging.Filter(PKG))                  # 第三方库的警告（如 urllib3 重试）只进文件
    console.setFormatter(_Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S", secrets, brief=True))
    _handlers.append(console)

    current = None
    if to_file:
        LOG_DIR.mkdir(exist_ok=True)
        _cleanup()
        current = LOG_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{command}.log"
        fh = logging.handlers.RotatingFileHandler(current, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
        fh.setFormatter(_Formatter("%(asctime)s.%(msecs)03d %(levelname)-7s [%(threadName)s] %(mod)s: %(message)s",
                                   "%Y-%m-%d %H:%M:%S", secrets))
        _handlers.append(fh)

    for h in _handlers:
        root.addHandler(h)
    root.setLevel(logging.INFO)                              # 第三方库最多到 INFO，不然 DEBUG 时被 urllib3 刷屏
    level = logging.getLevelName(str(cfg.log_level).upper())
    logging.getLogger(PKG).setLevel(level if isinstance(level, int) else logging.INFO)
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    if current:
        _header(cfg, command)
    return current


def _cleanup() -> None:
    """只留最近 KEEP - 1 次的（这次马上要新建一个）；文件名以时间开头，按名字排就是按时间排"""
    for old in sorted(LOG_DIR.glob("*.log"), reverse=True)[KEEP - 1:]:
        for f in LOG_DIR.glob(old.name + "*"):               # 连同滚动出来的 .log.1 .log.2
            try:
                f.unlink()
            except OSError:
                pass                                          # 别的进程还开着，下次再删


def _header(cfg: Config, command: str) -> None:
    """开头记一下环境：开发机和游戏电脑不是同一台，看日志时先知道是哪台、什么配置"""
    conf = dataclasses.asdict(cfg)
    for k in ("stratz_token", "ai_api_key"):
        conf[k] = "已设置" if conf[k] else "未设置"
    log.info("==== 启动：%s ====", " ".join(sys.argv[1:]) or command)
    log.info("Python %s | %s | 代码版本 %s", platform.python_version(), platform.platform(), _git_rev())
    log.info("配置：%s", conf)


def _git_rev() -> str:
    stamp = ROOT / "VERSION.txt"                              # 打包版没有 .git，打包时写好的版本号
    if stamp.exists():
        return stamp.read_text(encoding="utf-8").strip()
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=5,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.stdout.strip() or "?"
    except (OSError, subprocess.SubprocessError):
        return "?（没装 git）"


def _excepthook(exc_type, exc, tb) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        return sys.__excepthook__(exc_type, exc, tb)
    log.critical("程序出错退出", exc_info=(exc_type, exc, tb))


def _thread_excepthook(args) -> None:
    if args.exc_type is SystemExit:
        return
    name = args.thread.name if args.thread else "?"
    log.error("后台线程 %s 出错", name, exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def latest() -> Path | None:
    files = sorted(LOG_DIR.glob("*.log")) if LOG_DIR.exists() else []
    return files[-1] if files else None


def open_dir() -> Path:
    LOG_DIR.mkdir(exist_ok=True)
    os.startfile(LOG_DIR)                                     # Windows 资源管理器打开
    return LOG_DIR


class HTTPServer(ThreadingHTTPServer):
    """处理请求时抛出的异常写日志（默认是打印到 stderr，没开控制台就看不到）；页面关了导致的断连不算错"""
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            logging.getLogger(PKG + ".http").debug("连接断开：%s %r", client_address, exc)
            return
        logging.getLogger(PKG + ".http").exception("处理 %s 的请求出错", client_address)
