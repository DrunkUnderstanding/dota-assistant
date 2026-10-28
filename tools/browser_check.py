"""G6：起一个本机界面服务（同 run --browser 的 WebServer + Live），用无头 Edge / Chrome 打开控制面板、叠加层、顶栏，收控制台报错。

和 run --browser 的区别（为了不碰玩家的东西）：
- 运行目录是临时目录（DOTA_ASSISTANT_HOME），从本仓库只读复制 data/ knowledge/ web/ config.example.toml calibration.json，
  不带 config.toml（设置只读进内存，token / AI Key 清空）；
- Live 不开 GSI 端口、不注册全局热键（玩家可能正开着助手打游戏），不截屏、不出声、不调 AI。
过程：页面打开后先看空状态，几秒后在 Live 里填一组选人阵容（推送新状态，页面重画），再收一轮。
用法：python tools/browser_check.py [--seconds 每轮秒数]。退出码：有报错 / 页面空白 = 1，找不到浏览器 = 2。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BROWSERS = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
]
# 页面 → 要不要有文字（顶栏不在比赛里本来就是空的）
PAGES = {"panel.html": True, "overlay.html?preview=1": True, "topbar.html?preview=1": True, "overlay.html": True,
         "topbar.html?scale=1": False}
# 不算报错的：浏览器自己要的 favicon；顶栏预览故意放了一个下不到的大招图标（测「图标下不到退回写字」）
IGNORED = ["/favicon.ico", "/icons/abilities/no_such_ability.png"]
# 设置从这里读（行为快照的冻结目录里那份已经去掉了 token；没有再用本仓库的）
CONFIG_SOURCES = [Path(r"D:\dota-golden\home\config.toml"), REPO / "config.toml"]
DRAFT = {"radiant": ["卓尔游侠", "水晶室女", "斧王", "", ""], "dire": ["幻影刺客", "拉比克", "", "风行者", ""]}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_home() -> Path:
    home = Path(tempfile.mkdtemp(prefix="dota-browser-"))
    for name in ("data", "knowledge", "web"):                # web/ 也要：页面按 ROOT / "web" 找
        if (REPO / name).is_dir():
            shutil.copytree(REPO / name, home / name)
    for name in ("config.example.toml", "calibration.json"):
        if (REPO / name).is_file():
            shutil.copy2(REPO / name, home / name)
    return home


def wait_debugger(port: int, timeout: float = 20) -> None:
    import urllib.request
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1).read()
            return
        except OSError:
            time.sleep(0.3)
    raise TimeoutError("浏览器调试端口没起来")


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--verbose", action="store_true", help="顺便打印每个页面开头一段文字")
    ap.add_argument("--code", type=Path, default=REPO, help="从哪个目录 import dota_assistant（和别的版本对比用；数据仍从本仓库复制）")
    args = ap.parse_args(argv)
    browser = next((b for b in BROWSERS if b.exists()), None)
    if browser is None:
        print("没找到 Edge / Chrome")
        return 2

    home = make_home()
    os.environ["DOTA_ASSISTANT_HOME"] = str(home)          # 必须在 import dota_assistant 之前
    sys.path.insert(0, str(args.code.resolve()))
    import importlib

    def first(*names):                                      # 新路径优先，旧路径兜底（重构前后都能跑）
        for n in names:
            try:
                return importlib.import_module(n)
            except ImportError:
                continue
        raise ImportError(names)
    config = first("dota_assistant.core.config", "dota_assistant.config")
    WebServer = first("dota_assistant.app.webui", "dota_assistant.webui").WebServer
    Live = first("dota_assistant.app.live", "dota_assistant.live").Live
    print(f"代码：{Path(sys.modules['dota_assistant'].__file__).resolve().parent}")

    # 统计数据文件名跟着段位 / 天数走，要用本机的设置；只取这几项，token / key 一律清空（不联网、不外传）
    cfg = config.load(next((c for c in CONFIG_SOURCES if c.is_file()), None))
    cfg.stratz_token = ""
    cfg.ai_api_key = ""
    cfg.ai_plan = False
    cfg.voice = False
    cfg.buyback_auto = False
    live = Live(cfg, screen_factory=lambda: None, use_gsi=False, use_hotkeys=False,
                overlay_file=home / "data" / "overlay.json")
    live.config_file = home / "config.toml"
    srv = WebServer(live).start()
    live.start()
    live.submit(live.refresh)

    dbg = free_port()
    profile = Path(tempfile.mkdtemp(prefix="dota-browser-profile-"))
    proc = subprocess.Popen([str(browser), "--headless=new", f"--remote-debugging-port={dbg}",
                             f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
                             "--disable-extensions", "--window-size=1920,1080", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_debugger(dbg)

        def fill():                                         # 第二轮：填一组选人阵容，推新状态
            time.sleep(args.seconds)

            def go():
                for side, names in DRAFT.items():
                    live.slots[side] = list(names)
                live.refresh()
            live.submit(go)
        threading.Thread(target=fill, daemon=True).start()

        urls = [f"{srv.url}/{p}" for p in PAGES]
        out = subprocess.run(["node", str(REPO / "tools" / "browser_console.mjs"), str(dbg), str(args.seconds * 2 + 2),
                              *urls], capture_output=True, text=True, encoding="utf-8", timeout=120)
        if out.returncode:
            print(out.stdout, out.stderr)
            return 1
        results = json.loads(out.stdout.strip().splitlines()[-1])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        srv.stop()
        live.close()
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)

    bad = False
    for r in results:
        page = r["url"].removeprefix(srv.url + "/")
        r["errors"] = [e for e in r["errors"] if not any(f"{srv.url}{s}" in e for s in IGNORED)]
        ok = not r["errors"] and (r["text"] > 0 or not PAGES[page])
        bad |= not ok
        print(f"[{'OK' if ok else '失败'}] {page}：报错 {len(r['errors'])}，警告 {len(r['warnings'])}，页面文字 {r['text']} 字")
        if args.verbose:
            print(f"    文字：{r['sample']}")
        for line in r["errors"] + r["warnings"]:
            print("    " + line)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
