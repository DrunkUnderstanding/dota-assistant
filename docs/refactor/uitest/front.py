# 把 Electron 控制面板窗口提到最前，打印它的客户区屏幕坐标
import ctypes, sys
from ctypes import wintypes as w
ctypes.windll.shcore.SetProcessDpiAwareness(2)
u, k = ctypes.windll.user32, ctypes.windll.kernel32
found = []
@ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
def cb(h, _):
    if not u.IsWindowVisible(h): return True
    n = u.GetWindowTextLengthW(h); b = ctypes.create_unicode_buffer(n + 1); u.GetWindowTextW(h, b, n + 1)
    pid = w.DWORD(); u.GetWindowThreadProcessId(h, ctypes.byref(pid))
    hp = k.OpenProcess(0x1000, False, pid.value); buf = ctypes.create_unicode_buffer(260); m = w.DWORD(260)
    k.QueryFullProcessImageNameW(hp, 0, buf, ctypes.byref(m)); k.CloseHandle(hp)
    if buf.value.lower().endswith("electron.exe"): found.append((h, b.value))
    return True
u.EnumWindows(cb, 0)
for h, t in found: print(h, repr(t))
target = [h for h, t in found if sys.argv[1] in t]
if target:
    h = target[0]
    u.ShowWindow(h, 9); import time; time.sleep(0.4)
    u.SetWindowPos(h, -1, 0, 0, 0, 0, 0x0003); u.SetWindowPos(h, -2, 0, 0, 0, 0, 0x0003)
    u.keybd_event(0x12, 0, 0, 0); u.SetForegroundWindow(h); u.keybd_event(0x12, 0, 2, 0)
    pt = w.POINT(0, 0); u.ClientToScreen(h, ctypes.byref(pt)); print("client", pt.x, pt.y)
