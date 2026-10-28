"""冻结目录里的文件变动：命令 / 回放写进去的东西（data/ 的统计、config.toml、fixes.jsonl、截图、模板…）也算行为，要进快照。

- scan(home)：{相对路径: (大小, mtime_ns)}。解压时所有文件 mtime 都设成冻结时刻，之后谁写过一下 mtime 就变了。
  logs/ 不算（日志内容另外按条记进快照）。
- changes(before, after)：[(新增 / 改动 / 删除, 相对路径)]。
- describe(...)：一个文件 → 「路径  大小  sha1」，可选附上归一化后的全文。
  · 二进制（png 等）：原始字节的大小和 sha1。
  · 文本：先归一化（路径 / 时间戳同 g_normalize），JSON 按键排序重排（键的先后不算行为），再算大小和 sha1。
  · scrub_epoch：cli 模式的假时钟是「冻结时刻 + 真实流逝」，写进文件的 time.time()（fetched_at 之类）每次差零点几秒，
    把 [冻结时刻, 冻结时刻 + 6 小时] 里的数换成 <运行时刻>（毫秒的也换）。回放用的是只在剧本里走的假时钟，不用换。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

EXCLUDE = ("logs/",)
TEXT_SUFFIX = {".json", ".jsonl", ".toml", ".txt", ".csv", ".md", ".ini", ".cfg", ".yaml", ".yml", ".html"}
WINDOW = 6 * 3600
_NUM = re.compile(r"(?<![\w.])\d{10,13}(?:\.\d+)?(?![\w.])")


def scan(home: Path) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    root = str(home)
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        if any((rel_dir + "x").startswith(e) for e in EXCLUDE):
            dirnames[:] = []
            continue
        dirnames.sort()
        for fn in filenames:
            rel = rel_dir + fn
            if any(rel.startswith(e) for e in EXCLUDE):
                continue
            st = os.stat(os.path.join(dirpath, fn))
            out[rel] = (st.st_size, st.st_mtime_ns)
    return out


def changes(before: dict, after: dict) -> list[tuple[str, str]]:
    out = []
    for rel in sorted(set(before) | set(after)):
        if rel not in before:
            out.append(("新增", rel))
        elif rel not in after:
            out.append(("删除", rel))
        elif before[rel] != after[rel]:
            out.append(("改动", rel))
    return out


class Scrubber:
    def __init__(self, epoch: float | None):
        self.epoch = epoch

    def _hit(self, v: float) -> bool:
        e = self.epoch
        return e is not None and (e <= v <= e + WINDOW or e * 1000 <= v <= (e + WINDOW) * 1000)

    def obj(self, v):
        if isinstance(v, bool) or v is None:
            return v
        if isinstance(v, (int, float)):
            return "<运行时刻>" if self._hit(float(v)) else v
        if isinstance(v, dict):
            return {k: self.obj(x) for k, x in v.items()}
        if isinstance(v, list):
            return [self.obj(x) for x in v]
        return v

    def text(self, s: str) -> str:
        if self.epoch is None:
            return s
        return _NUM.sub(lambda m: "<运行时刻>" if self._hit(float(m.group(0))) else m.group(0), s)


def render_text(path: Path, norm, scrub: Scrubber) -> str:
    """文本文件 → 归一化后的内容（JSON 按键排序、一层一行；jsonl 一行一条）"""
    raw = path.read_bytes().decode("utf-8", "replace")
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            return _dump(norm.obj(scrub.obj(json.loads(raw))))
        except ValueError:
            pass
    if suffix == ".jsonl":
        rows = []
        for line in raw.splitlines():
            try:
                rows.append(json.dumps(norm.obj(scrub.obj(json.loads(line))), ensure_ascii=False, sort_keys=True))
            except ValueError:
                rows.append(norm.text(scrub.text(line)))
        return "\n".join(rows)
    return norm.text(scrub.text(raw)).rstrip("\n")


def _dump(v) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True, indent=1)


def describe(home: Path, kind: str, rel: str, norm, scrub: Scrubber, content: bool = False,
             limit: int = 64_000) -> list[str]:
    """一行「kind 路径  大小  sha1」；content=True 时文本文件再附上归一化后的全文（超过 limit 字节只记 sha1）"""
    name = norm.text(rel)
    if kind == "删除":
        return [f"{kind} {name}"]
    p = home / rel
    if p.suffix.lower() in TEXT_SUFFIX:
        text = render_text(p, norm, scrub)
        data = text.encode("utf-8")
        head = f"{kind} {name}  归一化后 {len(data):,} 字节  sha1 {hashlib.sha1(data).hexdigest()[:16]}"
        if not content:
            return [head]
        if limit and len(data) > limit:
            return [head + "（太长，只记 sha1）"]
        return [head, *("  | " + ln for ln in text.split("\n"))]
    data = p.read_bytes()
    return [f"{kind} {name}  {len(data):,} 字节  sha1 {hashlib.sha1(data).hexdigest()[:16]}"]


def report(home: Path, before: dict, after: dict, norm, scrub: Scrubber, content=lambda rel: False,
           limit=lambda rel: 64_000) -> list[str]:
    """变动列表，按归一化后的路径排（文件名里的时间戳会被归一化，同名的再按内容排，顺序固定）。
    content(rel)：要不要附全文；limit(rel)：全文超过多少字节就只记 sha1（0 = 不限）"""
    items = []
    for kind, rel in changes(before, after):
        lines = describe(home, kind, rel, norm, scrub, content(rel), limit(rel))
        items.append((norm.text(rel), lines))
    items.sort(key=lambda t: (t[0], "\n".join(t[1])))
    return [ln for _, lines in items for ln in lines]
