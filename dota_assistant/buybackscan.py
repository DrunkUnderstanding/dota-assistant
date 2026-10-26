"""买活自动记：比赛中每秒截一次 Dota 窗口，看顶栏 10 个头像，谁买活了就自动开始 8 分钟倒计时（buyback.py）。

玩家 2026-10-26 问（原话）：「买活这种事件不会弹出提示吗，弹出提示的时候截个图然后AI自动记录或者你做一个程序自动记录，
买活是正常的全图广播信息吧」。是全场公开的：买活时全场有音效，顶栏那个头像下面冒一个金币堆、留几秒（Fandom「Buyback」）。
但金币堆长什么样、在哪一格我们还没截到过，不敢拿它当判据，先用下面这个**能从已知画面推出来**的办法：

1. 死了的英雄顶栏头像变灰（饱和度掉到接近 0），头像下面显示复活倒计时；活着的头像是彩色的
   （1920×1080 真实截图实测：活着的 10 个头像平均饱和度 75~197，转成灰度图是 0）。
2. 某一格连续 DEAD_FRAMES 帧灰 → 记「死了」；之后变回彩色 = 复活。
3. 刚灰的时候拿那一格（头像 + 下面一截）给 Windows OCR 认复活倒计时 T，算出「该在 这一帧 + T 复活」；
   每帧认一次，**两次算出来的复活时刻对得上**（差 ≤ AGREE 秒）才算数，最多认 OCR_TRIES 次（OCR 偶尔认错：
   合成图上 10 认成 1、14 认成 4，两帧一核对就筛掉了）。变回彩色时比该复活的时间早 ≥ BUYBACK_MARGIN 秒 = 买活。
   为什么不在复活前最后一帧认：那时只剩 0~2 秒，Windows OCR 认短的字很差（2026-10-26 合成图：单个数字原样给几乎都认不出；
   切到字、放大、复制三份以后 1080p / 4K 的 5~104 认对 27 / 28 个，3 还是认不出）；刚死时倒计时是两位数，认得出来。
   不能只用「死了多久」判断：复活时间跟等级走，对面等级看不到。
   不弹 AI：OCR 一次 0.3~0.5 秒、只在有人死的时候认，后台线程做；大模型一次要几秒到几十秒还要花钱，没必要。
4. 每次复活都把「刚死时给 OCR 的那一格」和「复活那一帧」拼一张小图存到 shots/buyback/（文件名带判断结果、认到的秒数），
   实战后对着调阈值 / 换判据。

**都是实现假设，没在真实对局里验证**：死了头像变灰、倒计时在头像下面（OCR 区域把头像也框进去了，在头像上也认得到）、
倒计时是白字。不灵的话先看 shots/buyback/ 里的图。Aegis / 骷髅王大招这类原地复活，顶栏可能根本不变灰，变灰也只有几秒，
倒计时是一位数认不出，不会算买活；早期（倒计时还是一位数）买活也认不出，只能手动点。只读截图，不碰游戏进程。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .config import ROOT

SAMPLES_DIR = ROOT / "shots" / "buyback"
SAMPLES_KEEP = 200
# 顶栏几何：和 web/topbar.html 同一套（1920×1080 截图量的，按图高 / 1080 等比缩放；图宽的中线是顶栏中线）
CENTER0 = {"radiant": -391, "dire": 138}
PITCH = 63
PORTRAIT = (6, 38, 24)       # 量饱和度的区域：y 从 6 到 38、中心左右各 24（避开头像上面那条玩家颜色）
TIMER = (0, 76, 31)          # 给 OCR 的区域：头像 + 下面一截
DEAD_SAT = 40                # 头像平均饱和度（0~255）低于这个算灰（死了）
DEAD_FRAMES = 2              # 连续几帧灰才算死（躲开一闪而过的特效）
OCR_TRIES = 5                # 刚死时倒计时最多认几次（每次一帧），要有两次对得上
BUYBACK_MARGIN = 3           # 比该复活的时间早这么多秒以上复活 = 买活（每秒一帧 + OCR 那一帧的误差）
OCR_TEXT_H = 40              # 给 OCR 的字放到多高（像素）
OCR_TILE = 3                 # 同一格横着复制几份：Windows OCR 认短的字容易漏，排成一行「37 37 37」就认得出
MIN_INK = 20                 # 头像下面那截至少这么多个白点才算有字
PORTRAIT_BOTTOM = 40         # 头像底边（1080 高时的 y）
AGREE = 2                    # 两次认到的「该复活的时刻」差在这么多秒内才算数（防认错：10 认成 1、14 认成 4）
MIN_HEIGHT = 480               # 截图再小就不是 Dota 画面了（或者窗口缩得太小，顶栏认不了）
SIDES = ("radiant", "dire")


def _box(img: np.ndarray, side: str, i: int, y0: float, y1: float, half: float) -> np.ndarray:
    h, w = img.shape[:2]
    s = h / 1080
    cx = w / 2 + (CENTER0[side] + PITCH * i) * s
    return img[max(0, int(y0 * s)):int(y1 * s), max(0, int(cx - half * s)):int(cx + half * s)]


def saturation(img: np.ndarray, side: str, i: int) -> float:
    crop = _box(img, side, i, *PORTRAIT)
    return float(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)[..., 1].mean()) if crop.size else 0.0


def timer_crop(img: np.ndarray, side: str, i: int) -> np.ndarray:
    return _box(img, side, i, *TIMER).copy()


def for_ocr(crop: np.ndarray) -> np.ndarray:
    """只留亮的、不怎么有颜色的像素（白字），黑字白底；切到有字的地方（头像下面那截有字就只要那截，
    免得灰头像上亮的地方算进来），字高放到 OCR_TEXT_H，横着复制几份隔开，OCR 才认得出"""
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    ink = (hsv[..., 2] > 170) & (hsv[..., 1] < 70)
    below = int(crop.shape[0] * PORTRAIT_BOTTOM / TIMER[1])
    if ink[below:].sum() >= MIN_INK:
        ink = ink[below:]
    ys, xs = np.nonzero(ink)
    if len(ys):
        ink = ink[max(0, ys.min() - 2):ys.max() + 3, max(0, xs.min() - 2):xs.max() + 3]
    out = np.where(ink, 0, 255).astype(np.uint8)
    k = OCR_TEXT_H / max(out.shape[0], 1)
    out = cv2.resize(out, None, fx=k, fy=k, interpolation=cv2.INTER_CUBIC)
    gap = np.full((out.shape[0], OCR_TEXT_H), 255, np.uint8)
    out = np.hstack([out, gap] * (OCR_TILE - 1) + [out])
    return cv2.cvtColor(cv2.copyMakeBorder(out, 30, 30, 30, 30, cv2.BORDER_CONSTANT, value=255), cv2.COLOR_GRAY2BGR)


def parse_seconds(texts: list[str]) -> int | None:
    """OCR 认到的字里找倒计时：复制了几份，取出现最多的 1~3 位数（一样多取大的；O / o 当 0）；没有数字是 None"""
    nums = [int(n) for t in texts for n in re.findall(r"\d{1,3}", t.replace("O", "0").replace("o", "0"))]
    return max(nums, key=lambda n: (nums.count(n), n)) if nums else None


@dataclass
class Death:
    """刚灰的一格，要认倒计时"""
    side: str
    index: int
    serial: int                  # 第几次死（结果回来时对一下，免得套到下一次死上）
    clock: float                 # 这一帧的游戏时钟
    crop: np.ndarray


@dataclass
class Revival:
    side: str
    index: int
    clock: float                 # 复活时的游戏时钟（秒）
    dead_for: float              # 灰了多久（游戏秒，看得到的那段）
    expected: float | None       # 按倒计时该在什么时候复活；没认出来是 None
    seconds: int | None          # 认到的倒计时
    before: np.ndarray | None    # 给 OCR 的那一格（存样本）
    after: np.ndarray            # 复活那一帧那一格

    @property
    def buyback(self) -> bool | None:
        """是不是买活；倒计时没认出来是 None（不自动记）"""
        return None if self.expected is None else self.clock <= self.expected - BUYBACK_MARGIN


@dataclass
class _Slot:
    gray: int = 0                # 连续几帧灰
    dead_since: float | None = None
    serial: int = 0
    tries: int = 0               # 这次死已经送去认了几次
    reads: list = field(default_factory=list)   # 每次认到的 (该复活的时刻, 秒数, 那一格)
    expected: float | None = None
    seconds: int | None = None
    crop: np.ndarray | None = None


@dataclass
class Watcher:
    slots: dict[tuple[str, int], _Slot] = field(default_factory=dict)
    serial: int = 0

    def update(self, img: np.ndarray, clock: float, skip=()) -> tuple[list[Death], list[Revival]]:
        """看一帧：返回（要认倒计时的格子，刚复活的格子）。skip：不用看的格子（自己，GSI 有）"""
        deaths, revs = [], []
        if img is None or img.shape[0] < MIN_HEIGHT:
            return deaths, revs
        for side in SIDES:
            for i in range(5):
                if (side, i) in skip:
                    continue
                st = self.slots.setdefault((side, i), _Slot())
                if saturation(img, side, i) < DEAD_SAT:
                    st.gray += 1
                    if st.gray < DEAD_FRAMES:
                        continue
                    if st.dead_since is None:
                        self.serial += 1
                        st.dead_since, st.serial = clock - st.gray + 1, self.serial
                    if st.expected is None and st.tries < OCR_TRIES:
                        st.tries += 1
                        crop = timer_crop(img, side, i)
                        st.crop = st.crop if st.crop is not None else crop
                        deaths.append(Death(side, i, st.serial, clock, crop))
                    continue
                if st.dead_since is not None:
                    revs.append(Revival(side, i, clock, clock - st.dead_since, st.expected, st.seconds, st.crop,
                                        timer_crop(img, side, i)))
                self.slots[(side, i)] = _Slot()
        return deaths, revs

    def set_timer(self, d: Death, seconds: int | None) -> None:
        """OCR 结果回来了：和之前认到的某一次对得上（算出来的复活时刻差 ≤ AGREE 秒）才记「该在什么时候复活」"""
        st = self.slots.get((d.side, d.index))
        if st is None or st.serial != d.serial or st.expected is not None or seconds is None:
            return
        at = d.clock + seconds
        if any(abs(at - a) <= AGREE for a, _, _ in st.reads):
            st.expected, st.seconds, st.crop = at, seconds, d.crop
        st.reads.append((at, seconds, d.crop))

    def reset(self) -> None:
        self.slots.clear()


def read_timer(d: Death, ocr) -> int | None:
    """ocr(img) -> [字]；没有 OCR 抛 OSError"""
    return parse_seconds(ocr(for_ocr(d.crop)))


def save_sample(rev: Revival, label: str, out_dir: Path | None = None) -> Path:
    """刚死时那一格 + 复活那一格左右拼一张存下来，文件名带判断结果；只留最近 SAMPLES_KEEP 张"""
    from .vision import write_image
    out_dir = out_dir or SAMPLES_DIR
    parts = [a for a in (rev.before, rev.after) if a is not None and a.size]
    h = max(a.shape[0] for a in parts)
    pad = lambda a: cv2.copyMakeBorder(a, 0, h - a.shape[0], 0, 4, cv2.BORDER_CONSTANT, value=(255, 0, 255))
    path = out_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{rev.side}{rev.index + 1}-{label}.png"
    write_image(path, np.hstack([pad(a) for a in parts]))
    for old in sorted(out_dir.glob("*.png"))[:-SAMPLES_KEEP]:
        old.unlink(missing_ok=True)
    return path
