"""快照归一化：只抹掉「搬家 / 换时间 / 换临时目录」会变、但不算行为的东西。

抹掉的（其余原样保留，真实行为差异照样会比出来）：
  1. 路径：临时冻结目录 → <HOME>，仓库根 → <REPO>，D:\\dota-golden → <GOLDEN>，系统临时目录 → <TMP>
  2. 堆栈：Traceback 里的每一帧（文件路径 / 行号 / 函数名会因搬家变）→ 一行「  <堆栈>」，只留最后的异常类型和消息；
     其他地方的「xxx.py:行号」→ <py>:<N>
  3. 模块名：日志行里的 %(mod)s（live → app.live.draft 是计划里唯一允许的可见差异）→ <mod>；
     对象 repr 里的 dota_assistant.xxx.yyy → dota_assistant.<mod>
  4. 时间：日志行开头的时间戳、控制台警告开头的 HH:MM:SS、isoformat 时间、文件名里的 YYYYMMDD-HHMMSS
  5. 内存地址 0x… 、线程编号（ThreadPoolExecutor-0_3 → pool，Thread-12 → Thread-N）
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

from g_common import GOLDEN, REPO

_LOG_LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} (\w+)\s+\[([^\]]*)\] ([\w.\-]+): ", re.M)
_CONSOLE = re.compile(r"^\d\d:\d\d:\d\d (?=\[(?:DEBUG|INFO|WARNING|ERROR|CRITICAL)\])", re.M)
_ISO = re.compile(r"\b\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:[+-]\d\d:\d\d)?")
_STAMP = re.compile(r"\b20\d{6}-\d{6}(?:-\d{3})?\b")
_PY_LINE = re.compile(r"(?:[A-Za-z]:)?(?:[\\/][^\s:\"'<>|*?]+)+\.py:\d+")
_PY_FILE = re.compile(r"(?:[A-Za-z]:)?(?:[\\/][^\s:\"'<>|*?]+)+\.py\b")
_MOD = re.compile(r"\bdota_assistant(?:\.[A-Za-z_]\w*)+")
_ADDR = re.compile(r"\b0x[0-9a-fA-F]{6,}\b")
_POOL = re.compile(r"ThreadPoolExecutor-\d+_\d+")
_THREAD_N = re.compile(r"\bThread-\d+")
_PYVER = re.compile(r"Python \d+\.\d+\.\d+ \| [^|\n]* \| 代码版本 [^\n]*")


def _path_pattern(p: Path) -> re.Pattern | None:
    """一个目录的各种写法（\\ 和 /、JSON 里转义过的 \\\\、大小写）"""
    s = str(p).rstrip("\\/")
    if not s:
        return None
    parts = re.split(r"[\\/]+", s)
    sep = r"(?:\\\\|\\|/)"
    return re.compile(sep.join(re.escape(x) for x in parts), re.I)


class Normalizer:
    def __init__(self, home: Path | None):
        self.paths: list[tuple[re.Pattern, str]] = []
        for p, name in ((home, "<HOME>"), (GOLDEN, "<GOLDEN>"), (REPO, "<REPO>"), (Path(tempfile.gettempdir()), "<TMP>")):
            if p is None:
                continue
            for variant in {p, p.resolve()}:
                pat = _path_pattern(variant)
                if pat:
                    self.paths.append((pat, name))
        # 长的先换（<HOME> 在 <TMP> 里面）
        self.paths.sort(key=lambda t: -len(t[0].pattern))

    def text(self, s: str) -> str:
        if not s:
            return s
        s = s.replace("\r\n", "\n")
        for pat, name in self.paths:
            s = pat.sub(name, s)
        s = collapse_tracebacks(s)
        s = _LOG_LINE.sub(lambda m: f"<时间> {m.group(1)} [{_thread(m.group(2))}] <mod>: ", s)
        s = _CONSOLE.sub("<时:分:秒> ", s)
        s = _PYVER.sub("Python <版本> | <系统> | 代码版本 <版本>", s)
        s = _ISO.sub("<ISO时间>", s)
        s = _STAMP.sub("<YYYYMMDD-HHMMSS>", s)
        s = _PY_LINE.sub("<py>:<N>", s)
        s = _PY_FILE.sub("<py>", s)
        s = _MOD.sub("dota_assistant.<mod>", s)
        s = _ADDR.sub("0x?", s)
        return s

    def obj(self, v):
        """JSON 结构里的每个字符串都过一遍 text()（键也过）"""
        if isinstance(v, str):
            return self.text(v)
        if isinstance(v, dict):
            return {self.text(str(k)): self.obj(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [self.obj(x) for x in v]
        return v


def _thread(name: str) -> str:
    return _THREAD_N.sub("Thread-N", _POOL.sub("pool", name))


def collapse_tracebacks(s: str) -> str:
    """Traceback 的帧（两个空格开头的行）换成一行 <堆栈>；异常那行留着"""
    out, in_tb = [], False
    for line in s.split("\n"):
        if line.startswith("Traceback (most recent call last):"):
            out.append(line)
            out.append("  <堆栈>")
            in_tb = True
            continue
        if in_tb:
            if line.startswith(" ") or line.startswith("\t") or not line.strip():
                if not line.strip():
                    in_tb = False
                    out.append(line)
                continue
            in_tb = False
        out.append(line)
    return "\n".join(out)


def sort_pool_runs(text: str) -> str:
    """日志里线程池（pool）打的行，连着的一段按内容排序：并发下谁先打不固定"""
    lines = text.split("\n")
    out, run = [], []
    for ln in lines:
        if "[pool]" in ln:
            run.append(ln)
            continue
        out += sorted(run)
        run = []
        out.append(ln)
    return "\n".join(out + sorted(run))
