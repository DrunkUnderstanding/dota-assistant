"""认自己这局打几号位：天梯选人时，顶栏每个队友名字下面写着他排的位置（优势路 / 中路 / 劣势路 / 辅助 / 纯辅助），
只显示自己这一边。用 Windows 自带的 OCR（Windows.Media.Ocr，PowerShell 调，不用装东西）认这条字。

- 哪一格是我：先在名字那一行找 GSI 给的玩家名（OCR 认名字会错一两个字，按共同字数算），找不到再用 GSI 的 team_slot。
- 加速模式、普通全英雄选择没有这行字，认不出就沿用原来的位置。
- 只读截图。OCR 一次约 0.3~0.5 秒，在后台线程里做，一局认出来就不再认。
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .recognize import Calibration

# 「纯辅助」要排在「辅助」前面先匹配
ROLE_WORDS = (("纯辅助", 5), ("优势路", 1), ("劣势路", 3), ("中路", 2), ("辅助", 4),
              ("Hard Support", 5), ("Safe Lane", 1), ("Off Lane", 3), ("Offlane", 3), ("Mid Lane", 2), ("Soft Support", 4))
BAND = (0.072, 0.118)      # 名字 + 位置那两行在屏幕上的高度范围（占屏幕高的比例，1080p 实测名字 y≈90、位置 y≈112）
UPSCALE = 2                # 字小，放大了 OCR 才认得准
log = logging.getLogger(__name__)

OCR_SCRIPT = r"""
param([string]$path, [string]$lang)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | ? { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$t) { $task = $asTask.MakeGenericMethod($t).Invoke($null, @($op)); $task.Wait() | Out-Null; $task.Result }
$eng = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new($lang))
if ($eng -eq $null) { $eng = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
if ($eng -eq $null) { Write-Error "no ocr engine"; exit 2 }
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$dec = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bmp = Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$res = Await ($eng.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
$i = 0
foreach ($l in $res.Lines) { foreach ($w in $l.Words) { $r = $w.BoundingRect; "{0}`t{1}`t{2}`t{3}`t{4}`t{5}" -f $i, $w.Text, $r.X, $r.Y, $r.Width, $r.Height }; $i++ }
"""


@dataclass
class Word:
    line: int
    text: str
    x: float
    y: float
    w: float
    h: float


def ocr(img: np.ndarray, lang: str = "zh-Hans-CN") -> list[Word]:
    """Windows OCR 认一张图里的字（坐标是这张图的像素）。没有 OCR（非 Windows / 没装语言）抛 OSError。"""
    d = Path(tempfile.mkdtemp(prefix="dota_ocr_"))
    try:
        png, ps1 = d / "img.png", d / "ocr.ps1"
        ok, buf = cv2.imencode(".png", img)
        buf.tofile(str(png))
        ps1.write_text(OCR_SCRIPT, encoding="utf-8-sig")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1), str(png), lang],
                           capture_output=True, timeout=20, creationflags=flags)
        if r.returncode != 0:
            raise OSError(f"OCR 失败：{r.stderr.decode('utf-8', 'replace')[:200]}")
        words = []
        for row in r.stdout.decode("utf-8", "replace").splitlines():
            parts = row.split("\t")
            if len(parts) == 6:
                words.append(Word(int(parts[0]), parts[1], *map(float, parts[2:])))
        return words
    finally:
        for p in d.iterdir():
            p.unlink()
        d.rmdir()


def _chars(words: list[Word]) -> list[tuple[str, float, float]]:
    """拆成单个字符 (字, x 中心, y 中心)：中文 OCR 一个字一个词；英文一个词按宽度平均分给每个字母"""
    out = []
    for w in sorted(words, key=lambda w: (w.line, w.x)):
        n = max(len(w.text), 1)
        for k, ch in enumerate(w.text):
            out.append((ch, w.x + w.w * (k + 0.5) / n, w.y + w.h / 2))
        out.append((" ", w.x + w.w, w.y + w.h / 2))       # 词和词之间
    return out


def find_roles(words: list[Word]) -> list[tuple[int, float]]:
    """[(位置, x 中心)]：在 OCR 的每一行里找位置关键词"""
    found = []
    for line in sorted({w.line for w in words}):
        cs = _chars([w for w in words if w.line == line])
        text = "".join(c for c, _, _ in cs)
        flat = text.replace(" ", "")
        idx = [i for i, (c, _, _) in enumerate(cs) if c != " "]     # 去掉空格后的第 k 个字 → cs 里的下标
        used = [False] * len(flat)
        for word, pos in ROLE_WORDS:
            key = word.replace(" ", "")
            start = 0
            while (k := flat.find(key, start)) >= 0:
                start = k + 1
                if any(used[k:k + len(key)]):
                    continue
                used[k:k + len(key)] = [True] * len(key)
                xs = [cs[idx[j]][1] for j in range(k, k + len(key))]
                found.append((pos, sum(xs) / len(xs)))
    return found


def find_name(words: list[Word], name: str) -> float | None:
    """名字那一行里和 GSI 玩家名最像的一段的 x 中心（共同字数 ≥ 名字的一半才算）"""
    name = name.strip()
    if not name:
        return None
    best, best_x = 0, None
    for line in sorted({w.line for w in words}):
        cs = [c for c in _chars([w for w in words if w.line == line]) if c[0] != " "]
        for i in range(len(cs)):
            seg = cs[i:i + len(name)]
            hit = sum(a[0] == b for a, b in zip(seg, name))
            if hit > best:
                best, best_x = hit, sum(c[1] for c in seg) / len(seg)
    return best_x if best * 2 >= len(name) else None


def read_roles(img: np.ndarray, cal: Calibration, side: str, my_name: str = "",
               team_slot: int | None = None, words: list[Word] | None = None) -> tuple[list[int | None], int | None]:
    """返回 (我方 5 格各自排的位置, 我在第几格)。认不出的是 None。words 给了就不再 OCR（测试用）。"""
    slots = cal.radiant if side == "radiant" else cal.dire
    H, W = img.shape[:2]
    x0 = int(min(r[0] for r in slots) * W)
    x1 = int(max(r[0] + r[2] for r in slots) * W)
    y0, y1 = int(BAND[0] * H), int(BAND[1] * H)
    if words is None:
        band = cv2.resize(img[y0:y1, x0:x1], None, fx=UPSCALE, fy=UPSCALE, interpolation=cv2.INTER_CUBIC)
        words = ocr(band)
    centers = [((r[0] + r[2] / 2) * W - x0) * UPSCALE for r in slots]
    nearest = lambda x: int(np.argmin([abs(x - c) for c in centers]))
    roles: list[int | None] = [None] * len(slots)
    for pos, x in find_roles(words):
        roles[nearest(x)] = pos
    me = None
    x = find_name(words, my_name)
    if x is not None:
        me = nearest(x)
    elif team_slot is not None and 0 <= team_slot < len(slots):
        me = team_slot
    return roles, me
