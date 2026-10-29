"""助手自己的更新（数据更新是 update.py，不是这个）：GitHub 上有新版本就提示，点一下下载、替换、重启。

两种装法各走各的：
  - 打包版（PyInstaller，config.FROZEN）：看 GitHub Release 最新一版，下 zip、核对 sha256、解压到 _update/new/，
    再用**新版本自己的 exe** 跑 `upgrade --finish`：等旧进程退出 → 替换文件 → 启动新版本。
    替换由新包自己做（它最清楚自己的目录里有什么），旧版本只管下载和交接。
    交接的命令行（upgrade --finish --pid --target --src [--no-restart]）是新旧版本之间的约定，**改了旧版本就升不上来**。
  - 源码版（git clone、用「启动助手.bat」跑的）：git fetch 看上游分支有没有新提交，git pull --ff-only，
    requirements.txt / electron/package.json 变了顺手 pip / npm install，再用同样的 `upgrade --finish`（不带 --src）重启。
  - 都不是（下载的源码 zip）：只提示去 GitHub 下载。

替换时保留玩家自己的东西：config.toml、logs/、shots/、templates/ 不在包里，不碰；data/ 只补本地没有的文件
（本地的数据缓存一般比包里的新，还有个人数据）；calibration.json 本地有就留着（自己校准过）。
其余（exe、_internal、ui、web、knowledge、README …）整个换掉：旧的先挪进 _update/old/，中途失败就挪回去。
_update/ 下次启动时删掉（cleanup）。
"""
from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from ..core import config
from ..core.config import ROOT

REPO = "DrunkUnderstanding/dota-assistant"
API = f"https://api.github.com/repos/{REPO}"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
STAGE = "_update"                    # 运行目录下放下载 / 解压 / 换下来的旧文件，下次启动删掉
KEEP_IF_EXISTS = {"calibration.json"}
MERGE_MISSING = {"data"}
FREE_MB = 1500                       # 下载 + 解压要的空间（zip 约 250 MB、解压约 600 MB，留余量）
LOCK_WAIT = 60                       # 等旧进程（含 Electron）退出、放开文件最多多少秒
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

log = logging.getLogger(__name__)
_session = requests.Session()
_cleaner: threading.Thread | None = None


@dataclass
class Check:
    mode: str                  # package 打包版 / git 源码版 / none 都不是
    current: str = ""          # 本地版本（给人看）
    latest: str = ""           # GitHub 上的
    available: bool = False
    reason: str = ""           # 不用 / 不能更新时说一句为什么
    notes: str = ""            # 更新内容：Release 说明 / 新提交的标题
    url: str = ""              # 打包版：zip 下载地址
    size: int = 0
    sha256: str = ""


# ---------- 检查 ----------

def detect(root: Path = ROOT, frozen: bool | None = None) -> str:
    if config.FROZEN if frozen is None else frozen:
        return "package"
    return "git" if (root / ".git").exists() else "none"


def check(root: Path = ROOT, frozen: bool | None = None) -> Check:
    """联网看有没有新版本；网络 / git 出错直接抛（调用方说一句「检查失败」）"""
    mode = detect(root, frozen)
    if mode == "package":
        return check_package(root)
    if mode == "git":
        return check_git(root)
    return Check("none", reason=f"不是 git 克隆、也不是打包版，没法自动更新：到 {RELEASES_PAGE} 下载")


def local(root: Path = ROOT, frozen: bool | None = None) -> Check:
    """不联网：只看本地是哪种装法、哪个版本"""
    mode = detect(root, frozen)
    if mode == "package":
        return Check(mode, current=local_version(root) or "未知")
    if mode == "git":
        try:
            return Check(mode, current=_git(root, "log", "-1", "--format=%h（%cs）"))
        except RuntimeError:
            return Check(mode)
    return Check(mode)


def _github(path: str) -> dict:
    r = _session.get(API + path, timeout=15,
                     headers={"Accept": "application/vnd.github+json", "User-Agent": "dota-assistant"})
    if r.status_code == 404:
        raise LookupError(path)
    if r.status_code == 403 and r.headers.get("X-RateLimit-Remaining") == "0":
        raise RuntimeError("GitHub 接口限流了（不登录每小时 60 次），过一会儿再查")
    r.raise_for_status()
    return r.json()


def local_version(root: Path) -> str:
    """打包时写的 VERSION.txt：<打包日期>-<提交>[-改动未提交]"""
    f = root / "VERSION.txt"
    return f.read_text(encoding="utf-8").strip() if f.exists() else ""


def check_package(root: Path) -> Check:
    ver = local_version(root)
    c = Check("package", current=ver or "未知")
    try:
        rel = _github("/releases/latest")
    except LookupError:
        c.reason = "GitHub 上还没有发布过打包版"
        return c
    tag = rel["tag_name"]
    c.latest = rel.get("name") or tag
    c.notes = (rel.get("body") or "").strip()
    asset = next((a for a in rel.get("assets", []) if a["name"].lower().endswith(".zip")), None)
    if asset is None:
        c.reason = "GitHub 上最新的发布里没有 zip 包"
        return c
    c.url, c.size = asset["browser_download_url"], int(asset.get("size") or 0)
    c.sha256 = str(asset.get("digest") or "").removeprefix("sha256:")
    m = re.match(r"(\d{8})-([0-9a-f]{7,40})", ver)
    status = ""
    if m:
        try:                                   # base...head：ahead = 发布比本地新
            status = _github(f"/compare/{m[2]}...{tag}")["status"]
        except LookupError:                    # 本地是用没推上 GitHub 的提交打的包，只能按日期比
            pass
    if status in ("ahead", "diverged"):
        c.available = True
    elif status == "identical":
        c.reason = "已经是最新版"
    elif status == "behind":
        c.reason = "你的版本比 GitHub 上最新的发布还新"
    elif not m:
        c.available = True                     # 读不出本地版本号：当旧的
    else:
        day = str(rel.get("published_at") or "")[:10].replace("-", "")
        c.available = day > m[1]
        c.reason = "" if c.available else "看不出本地和 GitHub 上哪个新（本地是没推送的提交打的包）"
    return c


def _git(root: Path, *args: str, timeout: float = 30) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, creationflags=NO_WINDOW)
    except FileNotFoundError:
        raise RuntimeError("没装 git，没法自动更新源码版") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git {args[0]} 超时（网络不好？）") from None
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"git {args[0]} 失败")
    return r.stdout.strip()


def check_git(root: Path) -> Check:
    c = Check("git", current=_git(root, "log", "-1", "--format=%h（%cs）"))
    try:
        _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    except RuntimeError:
        c.reason = "当前分支没有跟踪 GitHub 上的分支，自己 git pull"
        return c
    _git(root, "fetch", "--quiet", timeout=90)
    c.latest = _git(root, "log", "-1", "--format=%h（%cs）", "@{u}")
    behind, ahead = (int(x) for x in _git(root, "rev-list", "--left-right", "--count", "@{u}...HEAD").split())
    if not behind:
        c.reason = "已经是最新版" if not ahead else "本地有还没推送的提交，比 GitHub 上的新"
    elif ahead:
        c.reason = "本地有没推送的提交，和 GitHub 上的分叉了，自己 git pull 合并"
    else:
        c.available = True
        c.notes = _git(root, "log", "--format=- %s", "-n", "30", "HEAD..@{u}")
    return c


def describe(c: Check) -> str:
    """命令行里给人看的一段"""
    lines = [f"当前版本：{c.current or '?'}" + (f"\nGitHub 最新：{c.latest}" if c.latest else "")]
    if c.available:
        size = f"（下载 {c.size / 2**20:.0f} MB）" if c.size else ""
        lines.append(f"有新版本{size}" + (f"，更新内容：\n{c.notes}" if c.notes else ""))
    else:
        lines.append(c.reason)
    return "\n".join(lines)


# ---------- 下载 / 拉代码 ----------

def download(c: Check, root: Path, progress: Callable[[float], None] = lambda f: None) -> Path:
    stage = root / STAGE
    stage.mkdir(exist_ok=True)
    free = shutil.disk_usage(stage).free
    if free < FREE_MB * 2**20:
        raise RuntimeError(f"磁盘空间不够：更新要 {FREE_MB} MB，{stage.drive or stage} 只剩 {free >> 20} MB")
    dest = stage / "package.zip"
    part = stage / "package.zip.part"
    h, done = hashlib.sha256(), 0
    with _session.get(c.url, stream=True, timeout=30, headers={"User-Agent": "dota-assistant"}) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or c.size or 0)
        with open(part, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if total:
                    progress(min(done / total, 1.0))
    if c.sha256 and h.hexdigest() != c.sha256.lower():
        part.unlink()
        raise RuntimeError("下载的文件和 GitHub 上登记的校验值对不上，再试一次")
    part.replace(dest)
    log.info("下载好了 %s（%.0f MB）", c.url, done / 2**20)
    return dest


def extract(zip_path: Path, root: Path) -> Path:
    """解压到 _update/new/，返回包根（有 exe 和 _internal 的那层）"""
    out = root / STAGE / "new"
    if out.exists():
        shutil.rmtree(out)
    base = out.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if base not in (base / name).resolve().parents:
                raise RuntimeError(f"压缩包里有不对劲的路径 {name!r}，不解压")
        z.extractall(out)
    return package_dir(out)


def package_dir(out: Path) -> Path:
    for d in [out, *sorted(p for p in out.iterdir() if p.is_dir())]:
        if (d / "_internal").is_dir() and any(d.glob("*.exe")):
            return d
    raise RuntimeError("压缩包里没找到助手的 exe，不像是助手的打包版")


def _exe_in(pkg: Path) -> Path:
    same = pkg / Path(sys.executable).name                 # 一般和现在跑的同名
    if config.FROZEN and same.exists():
        return same
    exes = sorted(pkg.glob("*.exe"))
    if not exes:
        raise RuntimeError(f"{pkg} 里没有 exe")
    return exes[0]


def pull(root: Path, say: Callable[[str], None] = print) -> str:
    """源码版：git pull --ff-only，依赖变了顺手装上；返回新的提交"""
    old = _git(root, "rev-parse", "HEAD")
    say("git pull …")
    _git(root, "pull", "--ff-only", timeout=180)
    new = _git(root, "rev-parse", "HEAD")
    changed = set(_git(root, "diff", "--name-only", old, new).splitlines())
    log.info("git pull：%s → %s，改了 %d 个文件", old[:7], new[:7], len(changed))
    if "requirements.txt" in changed:
        say("依赖有变化，pip install -r requirements.txt …")
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], cwd=root)
        if r.returncode:
            raise RuntimeError("代码已经更新，但 pip install 失败了：看命令行窗口里的报错，手动装一次")
    npm = shutil.which("npm")
    if "electron/package.json" in changed and npm:
        say("界面组件有变化，npm install …")
        subprocess.run([npm, "install", "--no-fund", "--no-audit"], cwd=root / "electron")
    return new


def prepare(c: Check, root: Path, say: Callable[[str], None] = print,
            progress: Callable[[float], None] = lambda f: None, restart: bool = True) -> list[str]:
    """下载解压（打包版）/ git pull（源码版），返回收尾命令（spawn 它，然后本进程退出）"""
    if c.mode == "package":
        say(f"下载 {c.latest} …")
        z = download(c, root, progress)
        say("解压 …")
        return finish_command(root, os.getpid(), extract(z, root), restart)
    if c.mode == "git":
        pull(root, say)
        return finish_command(root, os.getpid(), None, restart)
    raise RuntimeError(c.reason or "这种装法没法自动更新")


def finish_command(root: Path, pid: int, pkg: Path | None, restart: bool = True) -> list[str]:
    head = [str(_exe_in(pkg))] if pkg else [sys.executable, "-X", "utf8", "-m", "dota_assistant"]
    cmd = [*head, "upgrade", "--finish", "--pid", str(pid), "--target", str(root)]
    if pkg:
        cmd += ["--src", str(pkg)]
    return cmd if restart else [*cmd, "--no-restart"]


def _child_env(home: Path | None) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "DOTA_ASSISTANT_HOME"}
    if home is not None:
        env["DOTA_ASSISTANT_HOME"] = str(home)          # 收尾进程的日志 / 配置用要更新的那个目录，不是 _update/new
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"           # 打包版起另一个打包版：让它按全新进程初始化
    return env


def spawn(cmd: list[str], root: Path) -> None:
    """单开一个控制台窗口跑收尾命令（本进程马上要退出）"""
    log.info("交给收尾进程：%s", cmd)
    subprocess.Popen(cmd, cwd=root, env=_child_env(root), creationflags=NEW_CONSOLE, close_fds=True)


# ---------- 收尾（在新开的窗口里跑，旧进程已经在退出了） ----------

def finish(target: Path, pid: int, src: Path | None, restart: bool = True, say: Callable[[str], None] = print) -> int:
    log.info("收尾：等进程 %d 退出，%s，目标 %s", pid, f"用 {src} 替换" if src else "不换文件", target)
    say("等原来的助手退出 …")
    if not _wait_pid(pid, LOCK_WAIT):
        say(f"原来的助手 {LOCK_WAIT} 秒还没退出，先试着替换")
    ok = True
    if src is not None:
        say(f"替换文件 → {target}")
        try:
            replace(src, target, say)
            say("更新好了")
        except Exception as e:
            log.exception("替换失败")
            say(f"更新失败：{e}\n原来的版本已经恢复。可以到 {RELEASES_PAGE} 下载 zip，解压覆盖到 {target}")
            ok = False
    if not ok or not restart:
        _pause("按回车" + ("启动助手 …" if restart else "关闭 …"))
    if restart:
        say("启动助手 …")
        launch(target)
    return 0 if ok else 1


def _pause(prompt: str) -> None:
    try:
        input(prompt)
    except EOFError:
        pass


def _wait_pid(pid: int, timeout: float) -> bool:
    """等进程退出；True = 退了（或本来就没有）"""
    synchronize = 0x00100000
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(synchronize, False, pid)
    if not h:
        return True
    try:
        return k32.WaitForSingleObject(h, int(timeout * 1000)) == 0
    finally:
        k32.CloseHandle(h)


def launch(target: Path) -> None:
    """启动更新后的助手（一键启动），用当前这个窗口"""
    if config.FROZEN:
        cmd = [str(target / Path(sys.executable).name)]
    else:
        cmd = [sys.executable, "-X", "utf8", "-m", "dota_assistant", "start"]
    subprocess.Popen(cmd, cwd=target, env=_child_env(None))


def replace(src: Path, target: Path, say: Callable[[str], None] = print) -> list[str]:
    """用新包 src 替换 target 里的程序文件，返回换了哪些；出错时已经换的挪回去再抛"""
    backup = target / STAGE / "old"
    if backup.exists():
        shutil.rmtree(backup)
    backup.mkdir(parents=True)
    moved: list[str] = []
    placed: list[str] = []
    merge: list[Path] = []
    try:
        for entry in sorted(src.iterdir()):
            name, dst = entry.name, target / entry.name
            if name == STAGE or (name in KEEP_IF_EXISTS and dst.exists()):
                continue
            if name in MERGE_MISSING and dst.is_dir():
                merge.append(entry)                  # 程序文件都换好了再补，换失败就什么都不动
                continue
            if dst.exists():
                _retry(lambda: dst.rename(backup / name), name)
                moved.append(name)
            placed.append(name)
            _place(entry, dst)
    except BaseException:
        for name in placed:
            _remove(target / name)
        for name in moved:
            (backup / name).rename(target / name)
        raise
    log.info("替换了 %s", placed)
    for entry in merge:
        try:
            n = _copy_missing(entry, target / entry.name)
        except OSError as e:                         # 缺的数据启动时还会自己补，不算更新失败
            log.warning("%s/ 补文件出错：%s", entry.name, e)
            continue
        if n:
            say(f"  {entry.name}/ 补了 {n} 个本地没有的文件")
    return placed


def _retry(fn: Callable[[], object], name: str) -> None:
    end = time.monotonic() + LOCK_WAIT
    while True:
        try:
            fn()
            return
        except OSError as e:
            if time.monotonic() > end:
                raise RuntimeError(f"{name} 被占用，挪不开（助手 / 界面还没完全退出？）：{e}") from e
            time.sleep(1)


def _place(entry: Path, dst: Path) -> None:
    try:
        entry.rename(dst)                       # 同一个盘，挪过去最快
    except OSError:                             # 收尾进程自己正在用的（_internal）挪不动，复制
        if entry.is_dir():
            shutil.copytree(entry, dst)
        else:
            shutil.copy2(entry, dst)


def _remove(p: Path) -> None:
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
    elif p.exists():
        p.unlink()


def _copy_missing(src: Path, dst: Path) -> int:
    n = 0
    for f in src.rglob("*"):
        to = dst / f.relative_to(src)
        if f.is_file() and not to.exists():
            to.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, to)
            n += 1
    return n


def cleanup(root: Path = ROOT) -> None:
    """删掉上次更新留下的 _update/（后台删：收尾进程可能还没完全退出，删不掉就隔一会儿再试）"""
    global _cleaner
    stage = root / STAGE
    if not stage.exists():
        return

    def work():
        for _ in range(30):
            shutil.rmtree(stage, ignore_errors=True)
            if not stage.exists():
                log.info("删掉了上次更新留下的 %s", stage)
                return
            time.sleep(2)
        log.warning("上次更新留下的 %s 删不掉，下次启动再删", stage)
    _cleaner = threading.Thread(target=work, daemon=True, name="upgrade-cleanup")
    _cleaner.start()


# ---------- 给控制面板用 ----------

class Updater:
    """后台检查 / 下载，状态给快照（view）；准备好以后起收尾进程，再调 on_quit 让助手退出"""

    def __init__(self, root: Path = ROOT, frozen: bool | None = None):
        self.root = root
        self.frozen = frozen
        self.state = "idle"        # idle / checking / latest / available / downloading / installing / restarting / error
        self.info: Check | None = None
        self.progress = 0.0
        self.error = ""
        self._busy = threading.Lock()

    def view(self) -> dict:
        v: dict = {"state": self.state}
        if c := self.info:
            v.update(mode=c.mode, current=c.current, latest=c.latest, available=c.available, reason=c.reason,
                     notes=c.notes, size_mb=round(c.size / 2**20), page=RELEASES_PAGE)
        if self.state == "downloading":
            v["progress"] = round(self.progress, 2)
        if self.error:
            v["error"] = self.error
        return v

    def _start(self, fn: Callable, *args) -> bool:
        if not self._busy.acquire(blocking=False):
            return False

        def work():
            try:
                fn(*args)
            finally:
                self._busy.release()
        threading.Thread(target=work, daemon=True, name="upgrade").start()
        return True

    def check_async(self, delay: float = 0.0) -> bool:
        """False = 正在检查 / 下载，这次不查"""
        if self.state in ("restarting",):
            return False
        return self._start(self._check, delay)

    def _check(self, delay: float) -> None:
        time.sleep(delay)
        self.state, self.error = "checking", ""
        try:
            self.info = check(self.root, self.frozen)
        except Exception as e:
            log.warning("检查更新失败：%s", e, exc_info=not isinstance(e, (requests.RequestException, RuntimeError)))
            if self.info is None:                  # 联网失败也让设置里看得到自己是哪个版本、哪种装法
                self.info = local(self.root, self.frozen)
            self.state, self.error = "error", f"检查更新失败：{e}"
            return
        self.state = "available" if self.info.available else "latest"
        log.info("检查更新：%s → %s，%s", self.info.current, self.info.latest or "-",
                 "有新版本" if self.info.available else self.info.reason)

    def install_async(self, on_quit: Callable[[], None]) -> bool:
        if not (self.info and self.info.available) or self.state == "restarting":
            return False
        return self._start(self._install, on_quit)

    def _install(self, on_quit: Callable[[], None]) -> None:
        c = self.info
        self.state, self.error, self.progress = ("downloading" if c.mode == "package" else "installing"), "", 0.0
        try:
            if _cleaner is not None:
                _cleaner.join()                    # 上次更新的残留还在删，别删到这次下的
            cmd = prepare(c, self.root, say=_say, progress=self._set_progress)
            self.state = "installing"
            spawn(cmd, self.root)
        except Exception as e:
            log.warning("更新失败：%s", e, exc_info=not isinstance(e, (requests.RequestException, RuntimeError)))
            self.state, self.error = "error", f"更新失败：{e}"
            return
        self.state = "restarting"
        _say("新版本准备好了，助手马上重启 …")
        on_quit()

    def _set_progress(self, f: float) -> None:
        self.progress = f


def _say(msg: str) -> None:
    print(msg, flush=True)
    log.info(msg)
