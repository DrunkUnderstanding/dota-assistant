"""全局热键：Windows RegisterHotKey（系统级注册，不装键盘钩子、不读按键流）。"""
from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Callable

WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000
log = logging.getLogger(__name__)


def vk_code(name: str) -> int:
    name = name.strip().upper()
    if name.startswith("F") and name[1:].isdigit():
        return 0x70 + int(name[1:]) - 1        # F1 = 0x70
    if len(name) == 1 and name.isalnum():
        return ord(name)
    raise ValueError(f"不支持的热键 {name}（用 F1~F24 或单个字母 / 数字）")


class Hotkeys:
    """hotkeys = {"F8": 回调}。回调在热键线程里执行，界面相关的事要自己转回主线程（如 Qt 信号）。"""

    def __init__(self, hotkeys: dict[str, Callable[[], None]]):
        self._map = {i + 1: (vk_code(k), k, cb) for i, (k, cb) in enumerate(hotkeys.items())}
        self._thread_id = 0
        self.failed: list[str] = []
        self._thread = threading.Thread(target=self._run, daemon=True, name="hotkeys")

    def start(self) -> Hotkeys:
        self._thread.start()
        return self

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        for hid, (vk, name, _) in self._map.items():
            if not user32.RegisterHotKey(None, hid, MOD_NOREPEAT, vk):
                self.failed.append(name)          # 一般是被别的程序占了
                log.warning("热键 %s 注册失败（多半被别的程序占了，可以在 config.toml 里换一个）", name)
        log.info("热键已注册：%s", "、".join(n for _, n, _ in self._map.values() if n not in self.failed) or "无")
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam in self._map:
                try:
                    self._map[msg.wParam][2]()
                except Exception:                 # 回调出错不能让热键线程退出
                    log.exception("热键 %s 的回调出错", self._map[msg.wParam][1])
        for hid in self._map:
            user32.UnregisterHotKey(None, hid)

    def stop(self) -> None:
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)   # WM_QUIT
