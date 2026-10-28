# python mouse.py click X Y [right] / type TEXT / who X Y：真鼠标（SendInput），按下前确认那一点是 Electron 窗口（不穿透）
import ctypes, sys, time
from ctypes import wintypes as w
ctypes.windll.shcore.SetProcessDpiAwareness(2)
u, k = ctypes.windll.user32, ctypes.windll.kernel32
u.WindowFromPoint.argtypes = [w.POINT]; u.WindowFromPoint.restype = w.HWND
def owner(x, y):
    h = u.WindowFromPoint(w.POINT(x, y)); pid = w.DWORD(); u.GetWindowThreadProcessId(h, ctypes.byref(pid))
    hp = k.OpenProcess(0x1000, False, pid.value); buf = ctypes.create_unicode_buffer(260); n = w.DWORD(260)
    k.QueryFullProcessImageNameW(hp, 0, buf, ctypes.byref(n)); k.CloseHandle(hp)
    return buf.value.rsplit("\\", 1)[-1]
class MI(ctypes.Structure):
    _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD), ("dwFlags", w.DWORD), ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]
class KI(ctypes.Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD), ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]
class U(ctypes.Union):
    _fields_ = [("mi", MI), ("ki", KI), ("pad", ctypes.c_byte * 32)]
class INPUT(ctypes.Structure):
    _fields_ = [("type", w.DWORD), ("u", U)]
def send(*ins):
    arr = (INPUT * len(ins))(*ins); u.SendInput(len(ins), arr, ctypes.sizeof(INPUT))
def mouse(flags, x=0, y=0):
    W, H = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    i = INPUT(type=0); i.u.mi = MI(int(x * 65535 / (W - 1)), int(y * 65535 / (H - 1)), 0, flags, 0, 0); send(i)
def move(x, y):
    mouse(0x8001, x, y)            # MOVE | ABSOLUTE
cmd = sys.argv[1]
if cmd == "who":
    print(owner(int(sys.argv[2]), int(sys.argv[3])))
elif cmd == "click":
    x, y = int(float(sys.argv[2])), int(float(sys.argv[3])); right = len(sys.argv) > 4
    move(x - 3, y - 3); time.sleep(0.15); move(x, y)
    for _ in range(20):            # 主进程每 30ms 看一次光标，等它切成接鼠标
        time.sleep(0.05)
        if owner(x, y).lower() == "electron.exe": break
    o = owner(x, y)
    if o.lower() != "electron.exe":
        print("NOT_ELECTRON", o); sys.exit(1)
    mouse(0x0008 if right else 0x0002); time.sleep(0.06); mouse(0x0010 if right else 0x0004)
    print("clicked", x, y, o)
elif cmd == "type":
    for ch in sys.argv[2]:
        a = INPUT(type=1); a.u.ki = KI(0, ord(ch), 4, 0, 0); b = INPUT(type=1); b.u.ki = KI(0, ord(ch), 6, 0, 0); send(a, b); time.sleep(0.03)
    print("typed")
elif cmd == "park":
    move(int(sys.argv[2]), int(sys.argv[3])); print("parked")
