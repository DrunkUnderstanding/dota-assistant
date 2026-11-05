"""反和谐（可开关，config 的 uncensor）：把 Dota 的低暴力资源文件夹 game\\dota_lv 改名挪开，游戏就用正常资源。

原理（2026-11-05 核对 SteamDatabase 同步的 game/dota/gameinfo.gi 原文）：
    // These are optional low-violence paths. They will only get mounted if you're in a low-violence mode.
    Game_LowViolence	dota_lv
国服 / 低暴力模式下游戏启动时额外挂上 dota_lv（血、骷髅等换成和谐版）；文件夹不在就挂不上，用 game\\dota 里的正常资源。

- 只改名不删除：关掉开关就改回原名，和没动过一样。改成的名字 OFF_NAME 是助手专用的，不会碰别人的文件夹。
- Steam 更新游戏 / 验证文件完整性会把 dota_lv 补回来（这时两个文件夹都在）：开着就删掉旧的 OFF_NAME 再改名一次，
  所以要每次启动助手都检查（app/live 的 _uncensor_tick）。
- 只在 Dota 没开时改名：开着的游戏可能已经挂上了 dota_lv，中途挪走怕读资源出错；开着就等它关了再改（下次开游戏生效）。
  恢复（改回原名）不怕游戏开着：没挂上的文件夹放回去不影响正在跑的游戏。
- 不碰游戏进程：看 Dota 开没开只是列一下进程名（CreateToolhelp32Snapshot），不打开、不读写游戏进程。
"""
from __future__ import annotations

import ctypes
import logging
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

LV_NAME = "dota_lv"
OFF_NAME = "dota_lv_assistant_off"
DOTA_EXE = "dota2.exe"


@dataclass
class Result:
    status: str      # done 改好了 / noop 本来就是这样 / busy Dota 开着、等关了再改 / missing 没有 dota_lv / error 改名失败
    message: str     # 给状态栏 / 设置窗口看的


def paths(dota: Path) -> tuple[Path, Path]:
    return dota / "game" / LV_NAME, dota / "game" / OFF_NAME


def state(dota: Path | None) -> str:
    """on = 已反和谐（dota_lv 被挪开了）/ off = 和谐资源在（游戏低暴力模式会用）/ none = 两个都没有 / unknown = 没找到 Dota"""
    if dota is None:
        return "unknown"
    lv, off = paths(dota)
    if lv.exists():
        return "off"
    return "on" if off.exists() else "none"


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32), ("th32ProcessID", ctypes.c_uint32),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", ctypes.c_uint32), ("cntThreads", ctypes.c_uint32),
                ("th32ParentProcessID", ctypes.c_uint32), ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_uint32),
                ("szExeFile", ctypes.c_wchar * 260)]


def process_names() -> set[str] | None:
    """本机正在跑的进程名（小写）；不是 Windows / 取不到时 None。只列名字，不打开任何进程"""
    if sys.platform != "win32":
        return None
    k = ctypes.windll.kernel32
    k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    k.Process32FirstW.argtypes = k.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessEntry)]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    snap = k.CreateToolhelp32Snapshot(0x2, 0)              # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        return None
    try:
        e = _ProcessEntry()
        e.dwSize = ctypes.sizeof(_ProcessEntry)
        names = set()
        ok = k.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            names.add(e.szExeFile.lower())
            ok = k.Process32NextW(snap, ctypes.byref(e))
        return names
    finally:
        k.CloseHandle(snap)


def dota_running() -> bool:
    """Dota 开着没；看不出来时当开着（宁可晚一次也不在游戏开着时挪资源）"""
    names = process_names()
    return True if names is None else DOTA_EXE in names


def apply(dota: Path | None, on: bool, running: bool | None = None) -> Result:
    """按开关把文件夹改成该有的样子。running 给测试用，None = 真去看 Dota 开没开"""
    if dota is None:
        return Result("missing", "没找到 Dota 目录，反和谐没法弄：先在设置里填 Dota 目录")
    lv, off = paths(dota)
    try:
        if on:
            if not lv.exists():
                if off.exists():
                    return Result("noop", "已反和谐")
                return Result("missing", f"没找到 {lv}，这个版本可能没有低暴力资源，不用反和谐")
            if running if running is not None else dota_running():
                return Result("busy", "Dota 开着，反和谐等关掉游戏后自动弄好，下次开游戏生效")
            if off.exists():                               # Steam 更新把 dota_lv 补回来了：旧的挪开的那份不要了
                shutil.rmtree(off)
                log.info("删掉旧的 %s（Steam 更新补回了新的 %s）", off, LV_NAME)
            lv.rename(off)
            log.info("反和谐：%s 改名成 %s", lv, off.name)
            return Result("done", "已反和谐，下次开 Dota 生效")
        if not off.exists():
            return Result("noop", "")
        if lv.exists():                                    # 两个都在：dota_lv 是 Steam 新补的，挪开的旧份删掉
            shutil.rmtree(off)
            log.info("关反和谐：dota_lv 已经在了，删掉旧的 %s", off)
            return Result("done", "已关闭反和谐")
        off.rename(lv)
        log.info("关反和谐：%s 改回 %s", off, LV_NAME)
        return Result("done", "已关闭反和谐，下次开 Dota 恢复原样")
    except OSError as e:                                   # 文件被占用（游戏 / 杀毒 / 资源管理器开着）、没权限
        log.warning("反和谐改名失败：%s", e)
        return Result("error", f"反和谐改名失败（{e.strerror or e}），关掉 Dota 后再试；没权限就用管理员身份启动助手")
