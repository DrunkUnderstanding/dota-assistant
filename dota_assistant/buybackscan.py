"""买活自动记：比赛中每秒截一次 Dota 窗口，看顶栏 10 个头像，谁买活了就自动开始 8 分钟倒计时（buyback.py）。

玩家 2026-10-26 问（原话）：「买活这种事件不会弹出提示吗，弹出提示的时候截个图然后AI自动记录或者你做一个程序自动记录，
买活是正常的全图广播信息吧」。是全场公开的：买活时全场有音效，顶栏那个头像下面冒一个金币堆、留几秒（Fandom「Buyback」）。

**2026-10-27 起按「买活了」这条消息记**（玩家：「应该还是得按照事件来吧，比如买活了以后屏幕左侧会有一个xxx买活的标记」
「用英雄的头像小图应该也可以确认吧」「可以多次确认，超过两个说明对的概率就大了」）：
买活时屏幕左边消息栏出一行「<英雄小图> 玩家名 [战队] 买活了」，聊天区还有一行「玩家名（英雄名）买活了!」
（2026-10-26 实战截图 shots/deaths/20261026-225257、235009 看到的）。顶栏某一格变回彩色（复活）后，接下来 FEED_TRIES 帧
每帧把消息栏给 Windows OCR，找带「买活」的行，再拿那一行开头的小图和这局 10 个英雄的小图标（Valve CDN
heroes/icons/<key>.png，和消息栏里的是同一套图）比：**「买活」字 + 小图（或聊天行里的英雄名）都对上刚复活的那个英雄，
有 FEED_AGREE 帧都这样才记**。玩家名 OCR 常认错（「大王师傅123」认成「夭王师傅12新」），不用它。
真实截图验证：两次真买活的小图在全部 127 个英雄里都排第一（0.79 / 0.83，第二名 ≤ 0.56），
一整列 11 行消息的小图全认对（第一 0.74~0.90，第二 ≤ 0.69）。只有这两次真买活的样本，都是 1080p。
下面倒计时那套（第 3 条）现在只在小图标下不到（断网、第一次用）时兜底：消息栏有「买活」字 + 倒计时也说早复活了，才记。
2026-10-28 起小图和全部英雄比：选人时没认出来、格子空着的英雄也认得出是谁（还要那一格有早复活的迹象，见 FeedCheck._mine）。

原来的办法（**能从已知画面推出来**的）：

1. 死了的英雄顶栏头像变灰（饱和度掉到接近 0），头像下面显示复活倒计时；活着的头像是彩色的
   （1920×1080 真实截图实测：活着的 10 个头像平均饱和度 75~197，转成灰度图是 0）。
2. 某一格连续 DEAD_FRAMES 帧灰 → 记「死了」；之后连续 REVIVE_FRAMES 帧彩色 = 复活（复活时刻记第一帧彩色的）。
   「灰」看头像饱和度的**中位数**，不看平均：头像左下角的小图标（神杖 / 魔晶 / 橙色三角这类）是彩色的，
   人死了平均饱和度也有 38~41，正好卡在阈值上来回跳，2026-10-26 实战里狙击手两次「复活一帧又死了」被记成买活；
   中位数不受小图标影响（那天 200 张样本：死了全是 0，真复活 ≥ 31）。另外死了以后要到 ALIVE_SAT 才算彩色（回差），免得在阈值附近抖。
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

from .core.config import ROOT

SAMPLES_DIR = ROOT / "shots" / "buyback"
SAMPLES_KEEP = 200
# 顶栏几何：和 web/topbar.html 同一套（1920×1080 截图量的，按图高 / 1080 等比缩放；图宽的中线是顶栏中线）
CENTER0 = {"radiant": -391, "dire": 138}
PITCH = 63
PORTRAIT = (6, 38, 24)       # 量饱和度的区域：y 从 6 到 38、中心左右各 24（避开头像上面那条玩家颜色）
TIMER = (0, 76, 31)          # 给 OCR 的区域：头像 + 下面一截
DEAD_SAT = 12                # 头像饱和度中位数（0~255）低于这个算灰（死了）；死了是 0，活着最低见过 28（shots/ 下的截图）
ALIVE_SAT = 20               # 已经算死了的，中位数要到这个才算彩色（回差）
DEAD_FRAMES = 2              # 连续几帧灰才算死（躲开一闪而过的特效）
REVIVE_FRAMES = 2            # 连续几帧彩色才算复活（同上）
OCR_TRIES = 5                # 刚死时倒计时最多认几次（每次一帧），要有两次对得上
BUYBACK_MARGIN = 3           # 比该复活的时间早这么多秒以上复活 = 买活（每秒一帧 + OCR 那一帧的误差）
OCR_TEXT_H = 40              # 给 OCR 的字放到多高（像素）
OCR_TILE = 3                 # 同一格横着复制几份：Windows OCR 认短的字容易漏，排成一行「37 37 37」就认得出
MIN_INK = 20                 # 头像下面那截至少这么多个白点才算有字
PORTRAIT_BOTTOM = 40         # 头像底边（1080 高时的 y）
AGREE = 2                    # 两次认到的「该复活的时刻」差在这么多秒内才算数（防认错：10 认成 1、14 认成 4）
MIN_HEIGHT = 480               # 截图再小就不是 Dota 画面了（或者窗口缩得太小，顶栏认不了）
SIDES = ("radiant", "dire")
# 消息栏（1080 高时的坐标；2026-10-26 实战截图：「买活了」那行在 y≈626 / 664，聊天行在 y≈746、x 到 870，小地图从 y≈800 起）
FEED = (0, 440, 900, 900)    # x0, y0, x1, y1
FEED_SCALE = 2               # 放大了再给 OCR（实测原样给认不全）
FEED_TRIES = 5               # 复活后看几帧消息栏
FEED_AGREE = 2               # 几帧都认到才算买活（玩家：「超过两个说明对的概率就大了」）
BUYBACK_TEXT = "买活"
ICON_SIZES = range(19, 27)   # 消息栏里英雄小图的边长（1080 高时 ≈22，按图高缩放）
ICON_SIZE = 22               # 全部英雄先只用这一个大小粗排（127 个全试 ICON_SIZES 要 1.4 秒，粗排 0.2 秒）
ICON_REFINE = 3              # 粗排前几名再把 ICON_SIZES 全试一遍（2026-10-27 实战 16 行：粗排第一都是全试的第一）
ICON_X = 90                  # 小图在这一行开头：x < 这个
ICON_DY = 20                 # 行中心上下这么多里找小图
FEED_TEXT_X = 200            # 这一行的字从这个 x 以后才开始 = 聊天行（「玩家名（英雄名）买活了!」，行首没有小图，靠英雄名）
ICON_MIN = 0.7               # 小图相似度（TM_CCOEFF_NORMED）到这个才算认出来；实测对的 0.74~0.90，错的最高 0.69
ICON_MARGIN = 0.1            # 还要比第二名高这么多：和全部英雄比时第二名（常是狙击手）到过 0.70，对的第一名都高出 ≥ 0.15
HERO_ICON = "heroes/icons/{}.png"   # Valve CDN 上的英雄小图标（data.fetch_icon 下、存 data/icons）


def _box(img: np.ndarray, side: str, i: int, y0: float, y1: float, half: float) -> np.ndarray:
    h, w = img.shape[:2]
    s = h / 1080
    cx = w / 2 + (CENTER0[side] + PITCH * i) * s
    return img[max(0, int(y0 * s)):int(y1 * s), max(0, int(cx - half * s)):int(cx + half * s)]


def saturation(img: np.ndarray, side: str, i: int) -> float:
    """头像饱和度的中位数（不用平均：头像角上的彩色小图标会把死了的灰头像平均值抬到阈值附近）"""
    crop = _box(img, side, i, *PORTRAIT)
    return float(np.median(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)[..., 1])) if crop.size else 0.0


def portrait_crop(img: np.ndarray, side: str, i: int) -> np.ndarray:
    """局内顶栏这一格的头像（顶栏帮认给候选排序用；活着才有颜色，死了是灰的，认不了）"""
    return _box(img, side, i, *PORTRAIT).copy()


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
    hint: bool = False           # 倒计时没核对上（只认到一次之类），但认到的每一次都说早复活了

    @property
    def early(self) -> bool:
        """有没有早复活的迹象（格子没填时要靠这个确定消息栏那行是不是这一格的）"""
        return bool(self.buyback) or (self.buyback is None and self.hint)

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
    color: int = 0               # 死了以后连续几帧彩色
    color_at: float = 0.0        # 第一帧彩色的游戏时钟
    color_crop: np.ndarray | None = None
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
                if saturation(img, side, i) < (DEAD_SAT if st.dead_since is None else ALIVE_SAT):
                    st.gray += 1
                    st.color = 0
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
                    st.color += 1
                    if st.color == 1:
                        st.color_at, st.color_crop = clock, timer_crop(img, side, i)
                    if st.color < REVIVE_FRAMES:
                        continue
                    hint = bool(st.reads) and all(st.color_at <= at - BUYBACK_MARGIN for at, _, _ in st.reads)
                    revs.append(Revival(side, i, st.color_at, st.color_at - st.dead_since, st.expected, st.seconds,
                                        st.crop, st.color_crop, hint))
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


def save_sample(rev: Revival, label: str, out_dir: Path | None = None, feed: np.ndarray | None = None) -> Path:
    """刚死时那一格 + 复活那一格左右拼一张存下来，文件名带判断结果；只留最近 SAMPLES_KEEP 张。
    feed：判断时看的那一帧消息栏，同名存到 feed/ 子目录（核对「买活了」那行认得对不对）"""
    from .vision.recognize import write_image
    out_dir = out_dir or SAMPLES_DIR
    parts = [a for a in (rev.before, rev.after) if a is not None and a.size]
    h = max(a.shape[0] for a in parts)
    pad = lambda a: cv2.copyMakeBorder(a, 0, h - a.shape[0], 0, 4, cv2.BORDER_CONSTANT, value=(255, 0, 255))
    path = out_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{rev.side}{rev.index + 1}-{label}.png"
    write_image(path, np.hstack([pad(a) for a in parts]))
    if feed is not None and feed.size:
        write_image(out_dir / "feed" / path.name, feed)
    for d in (out_dir, out_dir / "feed"):
        for old in sorted(d.glob("*.png"))[:-SAMPLES_KEEP]:
            old.unlink(missing_ok=True)
    return path


# ---------- 消息栏：「<英雄小图> 玩家名 买活了」 ----------

@dataclass
class FeedLine:
    """消息栏里带「买活」的一行"""
    text: str                    # OCR 认到的整行（玩家名常认错，只拿来找「买活」和聊天行里的英雄名）
    y: float                     # 行中心在整张截图里的 y
    hero: str | None             # 行首小图认出来是哪个英雄（key）；没认出来 / 没有小图标可比 / 聊天行是 None
    score: float = 0.0


def feed_crop(img: np.ndarray) -> np.ndarray:
    s = img.shape[0] / 1080
    x0, y0, x1, y1 = (int(v * s) for v in FEED)
    return img[y0:y1, x0:x1]


def load_icon(path: Path | None) -> np.ndarray | None:
    """英雄小图标（带透明通道的 PNG）；没有 / 读不出是 None"""
    if path is None:
        return None
    im = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_UNCHANGED)
    return im if im is not None and im.ndim == 3 and im.shape[2] == 4 else None


def icon_score(region: np.ndarray, icon: np.ndarray, s: float, sizes=ICON_SIZES) -> float:
    """小图在 region 里最像的地方有多像：几种大小都试，只比不透明的像素（TM_CCOEFF_NORMED 带遮罩）。
    不用 TM_CCORR_NORMED：带遮罩时它对所有英雄都给 0.9 上下，分不开（2026-10-27 真实截图实测）"""
    best = -1.0
    reg = region.astype(np.float32)
    for z in sizes:
        n = max(8, round(z * s))
        if n > reg.shape[0] or n > reg.shape[1]:
            continue
        t = cv2.resize(icon, (n, n), interpolation=cv2.INTER_AREA).astype(np.float32)
        mask = (t[..., 3] > 200).astype(np.float32)
        if mask.sum() < n:                       # 几乎全透明，比不了
            continue
        r = cv2.matchTemplate(reg, np.ascontiguousarray(t[..., :3]), cv2.TM_CCOEFF_NORMED, mask=cv2.merge([mask] * 3))
        r[~np.isfinite(r)] = -1
        best = max(best, float(r.max()))
    return best


def read_feed(img: np.ndarray, ocr, icons: dict[str, np.ndarray]) -> list[FeedLine]:
    """消息栏里带「买活」的行，每行认一下行首小图是 icons 里的哪个英雄（icons 可以是全部英雄：先粗排再细比）。
    ocr(img) -> [roles.Word]；没有 OCR 抛 OSError"""
    s = img.shape[0] / 1080
    crop = feed_crop(img)
    if not crop.size:
        return []
    words = ocr(cv2.resize(crop, None, fx=FEED_SCALE, fy=FEED_SCALE, interpolation=cv2.INTER_CUBIC))
    lines: dict[int, list] = {}
    for w in words:
        lines.setdefault(w.line, []).append(w)
    out = []
    for ws in lines.values():
        text = "".join(w.text for w in ws)
        if BUYBACK_TEXT not in text:
            continue
        cy = FEED[1] * s + sum(w.y + w.h / 2 for w in ws) / len(ws) / FEED_SCALE
        if FEED[0] * s + min(w.x for w in ws) / FEED_SCALE > FEED_TEXT_X * s:
            out.append(FeedLine(text, cy, None))     # 聊天行：行首那儿的小图是别的消息的（2026-10-27 实测认成了獸）
            continue
        region = img[max(0, int(cy - (ICON_DY + max(ICON_SIZES) / 2) * s)):int(cy + (ICON_DY + max(ICON_SIZES) / 2) * s),
                     :int((ICON_X + max(ICON_SIZES)) * s)]
        coarse = sorted(((icon_score(region, ic, s, (ICON_SIZE,)), k) for k, ic in icons.items()), reverse=True)
        scores = sorted(((icon_score(region, icons[k], s), k) for _, k in coarse[:ICON_REFINE]), reverse=True) + [(-1.0, None)] * 2
        (top, hero), (second, _) = scores[:2]
        out.append(FeedLine(text, cy, hero if top >= ICON_MIN and top - second >= ICON_MARGIN else None, max(top, 0.0)))
    return out


@dataclass
class FeedCheck:
    """复活了的一格：接下来几帧看消息栏有没有「买活了」、行首小图是不是他"""
    rev: Revival
    hero: str | None             # 这一格的英雄 key（格子没填是 None）
    name: str | None             # 中文名（聊天行「玩家名（英雄名）买活了」里找）
    known: frozenset = frozenset()   # 格子没填时用：这局格子里已有的英雄。小图认出是这之外的英雄，就只能是没填的这格
    found: str | None = None     # 格子没填时，小图认出来是谁
    seen: int = 0                # 看了几帧
    hits: int = 0                # 几帧「买活」+ 小图 / 英雄名都对上他
    text_hits: int = 0           # 几帧有「买活」行但没法确认是谁（行首小图没认出来）
    others: int = 0              # 几帧的「买活」行是别的英雄的
    has_icon: bool = True        # 他的小图标有没有（下不到就只能兜底；每次看消息栏时更新）
    busy: bool = False
    feed: np.ndarray | None = None   # 存样本用的那一帧消息栏（第一次对上的那帧，没对上就是最后一帧）

    def add(self, lines: list[FeedLine], feed: np.ndarray | None) -> None:
        self.seen += 1
        self.busy = False
        mine = [ln for ln in lines if self._mine(ln)]
        if self.hero is None and mine:
            mine = [max(mine, key=lambda ln: ln.y)]   # 格子外的英雄有好几个：取最下面（最新）那行，上面的可能是一分钟前的
            self.found = mine[0].hero
        if mine:
            self.hits += 1
            if self.hits == 1:
                self.feed = feed
        elif any(ln.hero is None for ln in lines):
            self.text_hits += 1
        elif lines:
            self.others += 1
        if self.hits == 0:
            self.feed = feed

    def _mine(self, ln: FeedLine) -> bool:
        if self.hero is not None:
            return ln.hero == self.hero or bool(self.name and self.name in ln.text.replace(" ", ""))
        # 格子没填（选人时没认出来、玩家也没改；2026-10-27 军团指挥官买活就这样漏了）：小图认出是格子里没有的英雄，
        # 每帧都是同一个；小图只说明「格子外的某个英雄」，是不是这一格还要这一格自己有早复活的迹象——
        # 同一波常有几个没填的格子先后复活（2026-10-27 风暴之灵买活时，另一个没填的格子刚好按时复活）
        return (ln.hero is not None and ln.hero not in self.known and ln.hero == (self.found or ln.hero)
                and self.rev.early)

    @property
    def verdict(self) -> tuple[bool | None, str]:
        """(是不是买活，怎么判的)；还没看够帧数是 None"""
        if self.hits >= FEED_AGREE:
            return True, f"消息栏 {self.hits} 帧认到他买活"
        if self.seen < FEED_TRIES:
            return None, ""
        if not self.has_icon and self.text_hits + self.hits >= FEED_AGREE and self.rev.buyback:
            return True, "没有他的小图标可比：消息栏有「买活」+ 倒计时也说早复活了"
        if self.hits or self.text_hits:
            return False, f"消息栏只有 {self.hits} 帧对上他（有「买活」没对上的 {self.text_hits} 帧），不够 {FEED_AGREE} 帧"
        if self.others:
            return False, "消息栏的买活是别的英雄的"
        return False, "消息栏没有买活"
