"""截屏。优先只截 Dota 2 窗口本身，找不到窗口再退回截整块屏幕。

只截 Dota 窗口（PrintWindow + PW_RENDERFULLCONTENT，由系统合成器给出窗口画面，不碰游戏进程）的好处：
盖在游戏上面的东西（我们自己的叠加层、直播弹幕窗口等）不会截进去，不会挡住英雄格。
Dota 要开「无边框窗口」：独占全屏下截到的可能是黑屏，叠加层也盖不上去。
"""
from __future__ import annotations

import ctypes
import logging
import sys

import mss
import numpy as np

log = logging.getLogger(__name__)

DOTA_TITLE = "Dota 2"
PW_CLIENTONLY_FULLCONTENT = 3        # PW_CLIENTONLY | PW_RENDERFULLCONTENT


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]


class _Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int32), ("top", ctypes.c_int32), ("right", ctypes.c_int32), ("bottom", ctypes.c_int32)]


class WindowGrabber:
    """按标题找窗口，截它的客户区（不含标题栏）。不是 Windows / 找不到窗口 / 截不到时 grab() 返回 None。"""

    def __init__(self, title: str = DOTA_TITLE):
        self.title = title
        self.ok = sys.platform == "win32"
        if not self.ok:
            return
        v = ctypes.c_void_p
        u, g = ctypes.windll.user32, ctypes.windll.gdi32
        u.FindWindowW.restype = v
        u.IsWindow.argtypes = u.IsIconic.argtypes = [v]
        u.GetClientRect.argtypes = [v, ctypes.POINTER(_Rect)]
        u.GetDC.restype = v
        u.GetDC.argtypes = [v]
        u.ReleaseDC.argtypes = [v, v]
        u.PrintWindow.argtypes = [v, v, ctypes.c_uint]
        g.CreateCompatibleDC.restype = v
        g.CreateCompatibleDC.argtypes = [v]
        g.CreateCompatibleBitmap.restype = v
        g.CreateCompatibleBitmap.argtypes = [v, ctypes.c_int, ctypes.c_int]
        g.SelectObject.restype = v
        g.SelectObject.argtypes = [v, v]
        g.DeleteObject.argtypes = g.DeleteDC.argtypes = [v]
        g.GetDIBits.argtypes = [v, v, ctypes.c_uint, ctypes.c_uint, v, v, ctypes.c_uint]
        self.u, self.g = u, g
        self.hwnd = None

    def _window(self):
        if not (self.hwnd and self.u.IsWindow(self.hwnd)):
            self.hwnd = self.u.FindWindowW(None, self.title)
        return self.hwnd

    def grab(self) -> np.ndarray | None:
        if not self.ok:
            return None
        hwnd = self._window()
        if not hwnd or self.u.IsIconic(hwnd):          # 没开 / 最小化
            return None
        r = _Rect()
        self.u.GetClientRect(hwnd, ctypes.byref(r))
        w, h = r.right, r.bottom
        if w <= 0 or h <= 0:
            return None
        u, g = self.u, self.g
        hdc = u.GetDC(hwnd)
        mdc = g.CreateCompatibleDC(hdc)
        bmp = g.CreateCompatibleBitmap(hdc, w, h)
        old = g.SelectObject(mdc, bmp)
        try:
            if not u.PrintWindow(hwnd, mdc, PW_CLIENTONLY_FULLCONTENT):
                return None
            bi = _BitmapInfoHeader(ctypes.sizeof(_BitmapInfoHeader), w, -h, 1, 32)   # 高度取负 = 从上往下
            buf = np.empty((h, w, 4), np.uint8)
            if not g.GetDIBits(mdc, bmp, 0, h, buf.ctypes.data, ctypes.byref(bi), 0):
                return None
        finally:
            g.SelectObject(mdc, old)
            g.DeleteObject(bmp)
            g.DeleteDC(mdc)
            u.ReleaseDC(hwnd, hdc)
        img = buf[:, :, :3]
        if not img.any():                               # 全黑：多半是独占全屏，交给整屏截图
            return None
        return np.ascontiguousarray(img)


class Screen:
    def __init__(self, monitor: int = 1, window: WindowGrabber | None = None):
        self._mss = mss.mss()
        mons = self._mss.monitors
        self.monitor = mons[monitor] if monitor < len(mons) else mons[1]
        self.window = window if window is not None else WindowGrabber()
        self.source = ""                                # 最近一次截的是什么：window / screen

    def grab(self) -> np.ndarray:
        """优先 Dota 窗口，退回整块屏幕；BGR"""
        img = self.window.grab()
        source = "window" if img is not None else "screen"
        if source != self.source:
            log.info("截图来源：%s", "Dota 窗口" if source == "window" else "整块屏幕（没找到 Dota 窗口）")
            self.source = source
        if img is not None:
            return img
        shot = self._mss.grab(self.monitor)
        return np.asarray(shot)[:, :, :3].copy()
