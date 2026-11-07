"""选人网格里认禁用：被禁的英雄格子变暗、压一道红斜线。

顶栏没有禁用头像栏，聊天框又装不下所有「XX已被禁用」（见 HANDOFF 第 7 节第 2 项），所以直接看选人网格：
1. 找格子：网格背景是平滑的蓝灰渐变，格子是有纹理的矩形；同一行的格子底边对齐、大小一样（玩家可以自定义布局，不写死位置）。
2. 认格子是谁：格子里就是游戏资源里的 selection 图（gamefiles.py 导出到 data/portraits_grid），格子是它居中裁的一段；
   缩到格子宽度附近做模板匹配，再按「每个英雄只出现一次」贪心分配。布局不变就一直沿用（data/grid_map.json）。
3. 判断禁用：格子对比度很低（变暗、蒙一层灰）+ 沿右上 → 左下的直线明显偏红。只变暗没有斜线的是已经被选走的英雄。

只读截图，不碰游戏。
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from ..core.config import DATA_DIR
from .gamefiles import GRID_PORTRAIT_DIR
from .recognize import write_image

GRID_PATH = DATA_DIR / "grid_map.json"
ROI = (0.06, 0.16, 0.70, 0.75)       # 网格大概在屏幕的哪一块（x0, y0, x1, y1 比例；1080p 下右边是英雄详情栏）
DIM_STD = 18.0       # 格子中部灰度标准差低于这个算「变暗」（实测：禁用 / 已选 6~14，正常 ≥ 30）
SLASH_MIN = 12.0     # 斜线比两侧红多少（R−G）算有红斜线（实测：禁用 20~36）
MIN_SCORE = 0.6      # 认格子：模板匹配低于这个当作认不出（正常格子实测最低 0.70）
MIN_SCORE_DIM = 0.3  # 变暗的格子（只在剩下没认领的英雄里挑）
SCALES = (1.16, 1.21, 1.26, 1.31)   # 模板宽 / 格子宽
CLEAN_MAX_DIM = 3    # 变暗的格子不超过几个，算「干净画面」（选人刚开始、还没禁用）
MIN_CELLS = 60       # 找到的格子少于这个就不是选人网格（比如选完人在看英雄模型）
MAX_MAPS = 6         # 最多记几种布局（选人前后网格大小会变、换分辨率）
WRONG_GAP = 0.15     # 核对旧图：别的英雄比记的英雄高出这么多才算记错
REVIEW_PER_FRAME = 10   # 核对时一帧最多拿几格和所有英雄比（一格约 40 毫秒）
COVERED_BG = 0.3     # 格子左右的缝里网格背景占比都低于这个算被盖住（正常 ≥ 0.5，卡片盖着的 0~0.25）
REVIEW_RETRY = 3.0   # 比过没认出的格子（鼠标停在上面、卡片盖着之类），隔几秒再试

Box = tuple[int, int, int, int]       # 像素 x, y, w, h
Rect = tuple[float, float, float, float]   # 占屏幕比例
log = logging.getLogger(__name__)


# ---------- 找格子 ----------

def background(img: np.ndarray) -> np.ndarray:
    """网格背景：平滑、偏蓝（B ≥ G ≥ R）、中等亮度"""
    f = img.astype(np.int16)
    b, g, r = f[..., 0], f[..., 1], f[..., 2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    mag = np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0)) + np.abs(cv2.Sobel(gray, cv2.CV_32F, 0, 1))
    return (mag < 25) & (b - r >= 8) & (b - r <= 45) & (g >= r) & (b >= g) & (gray > 30) & (gray < 120)


def find_cells(img: np.ndarray) -> list[Box]:
    """网格里所有英雄格子（像素矩形，按行再按列排）。变暗的格子（禁用 / 已选）和背景粘在一起，用同一行的间距补出来。"""
    H, W = img.shape[:2]
    bg = background(img)
    roi = np.zeros_like(bg)
    roi[int(H * ROI[1]):int(H * ROI[3]), int(W * ROI[0]):int(W * ROI[2])] = True
    fg = cv2.morphologyEx((~bg & roi).astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, _, st, _ = cv2.connectedComponentsWithStats(fg, connectivity=4)
    lo, hi = H * 0.028, H * 0.13
    boxes = [tuple(int(v) for v in st[i][:4]) for i in range(1, n)
             if lo < st[i][2] < hi and lo * 1.4 < st[i][3] < hi * 1.2 and 0.5 < st[i][2] / st[i][3] < 0.95]
    boxes = _snap(boxes)
    boxes += _fill(boxes, img, bg, roi)
    return sorted(boxes, key=lambda b: (b[1] + b[3], b[0]))


def _rowmates(b: Box, boxes: list[Box], tol: int = 6) -> list[Box]:
    """同一行、差不多大的格子（底边对齐：右上角的涨跌小三角会让格子顶部高出一点，底边不受影响）"""
    return [o for o in boxes if abs(o[1] + o[3] - b[1] - b[3]) <= tol and abs(o[2] - b[2]) <= 8]


def _snap(boxes: list[Box]) -> list[Box]:
    """同一行统一大小、底边对齐到这一行的中位数（变暗的格子常常只找到一部分，底边会偏几个像素）"""
    out = []
    for b in boxes:
        mates = _rowmates(b, boxes, tol=max(6, b[3] // 4))
        w = int(np.median([o[2] for o in mates]))
        h = int(np.median([o[3] for o in mates]))
        bottom = int(np.median([o[1] + o[3] for o in mates]))
        out.append((b[0], bottom - h, w, h))
    return list(dict.fromkeys(out))


def _overlap(a: Box, b: Box) -> bool:
    return not (a[0] + a[2] <= b[0] or b[0] + b[2] <= a[0] or a[1] + a[3] <= b[1] or b[1] + b[3] <= a[1])


def _fill(boxes: list[Box], img: np.ndarray, bg: np.ndarray, roi: np.ndarray) -> list[Box]:
    """补格子：已有格子左右、上下一个间距的位置，有纹理（不是平滑背景）就算一个格子"""
    gaps = [o[0] - b[0] - b[2] for b in boxes for o in _rowmates(b, boxes) if 0 < o[0] - b[0] - b[2] < 20]
    gap = int(np.median(gaps)) if gaps else 5
    # 上下间距单独算（同一列、大小一样的格子）
    vgaps = [o[1] - b[1] - b[3] for b in boxes for o in boxes
             if abs(o[0] - b[0]) <= 3 and abs(o[2] - b[2]) <= 3 and 0 < o[1] - b[1] - b[3] < 25]
    vgap = int(np.median(vgaps)) if vgaps else gap
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    H, W = gray.shape
    added: list[Box] = []
    todo = list(boxes)
    while todo:
        x, y, w, h = todo.pop()
        for dx, dy in ((w + gap, 0), (-(w + gap), 0), (0, h + vgap), (0, -(h + vgap))):
            c = (x + dx, y + dy, w, h)
            if c[0] < 0 or c[1] < 0 or c[0] + w >= W or c[1] + h >= H:
                continue
            if not (roi[c[1], c[0]] and roi[c[1] + h, c[0] + w]):
                continue
            if any(_overlap(c, o) for o in boxes + added):
                continue
            inner = gray[c[1] + h // 8:c[1] + h * 3 // 4, c[0] + 3:c[0] + w - 3]
            if np.abs(cv2.Laplacian(inner, cv2.CV_32F)).mean() > 4 and bg[c[1]:c[1] + h, c[0]:c[0] + w].mean() < 0.8:
                added.append(c)
                todo.append(c)
    return added


# ---------- 认格子是谁 ----------

class GridBank:
    """网格图模板（彩色）"""

    def __init__(self, folder: Path = GRID_PORTRAIT_DIR):
        self.ids, self.imgs = [], []
        for p in sorted(folder.glob("*.png")):
            img = cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                self.ids.append(int(p.stem))
                self.imgs.append(img)
        self._scaled: dict[int, list[np.ndarray]] = {}

    def __len__(self) -> int:
        return len(self.ids)

    def scaled(self, width: int) -> list[np.ndarray]:
        if width not in self._scaled:
            self._scaled[width] = [cv2.resize(v, (width, int(round(width * v.shape[0] / v.shape[1]))),
                                              interpolation=cv2.INTER_AREA) for v in self.imgs]
        return self._scaled[width]

    def best_scale(self, img: np.ndarray, boxes: list[Box], dimmed: list[bool]) -> float:
        """先拿几个清楚的格子试所有缩放，后面整张网格都用最好的那个（省 3/4 时间）"""
        clear = [b for b, d in zip(boxes, dimmed) if not d][:8]
        if not clear:
            return SCALES[len(SCALES) // 2]
        votes = []
        for b in clear:
            per = [self.scores(img, [b], scales=(f,)).max() for f in SCALES]
            votes.append(SCALES[int(np.argmax(per))])
        return float(np.median(votes))

    def scores(self, img: np.ndarray, boxes: list[Box], dimmed: list[bool] | None = None,
               scales: tuple[float, ...] = SCALES) -> np.ndarray:
        """[格子, 英雄] 相似度。格子是网格图居中裁的一段（实测网格图宽 ≈ 格子宽 ×1.2~1.26），
        模板缩到这几个宽度再滑动找最好的位置。只取格子中段：顶上有涨跌三角，底下有等级数字和图标。
        变暗的格子（禁用 / 已选）蒙了一层灰、压了红斜线：改用灰度比，并把斜线附近的像素遮掉。"""
        out = np.zeros((len(boxes), len(self.ids)), np.float32)
        for i, box in enumerate(boxes):
            t, mask = _patch(img, box, bool(dimmed and dimmed[i]))
            for f in scales:
                for j, v in enumerate(self.scaled(int(round(box[2] * f)))):
                    out[i, j] = max(out[i, j], _match(t, mask, v))
        return out

    def score(self, img: np.ndarray, box: Box, hero_id: int, scales: tuple[float, ...] = SCALES) -> float:
        """一个没变暗的格子和指定英雄有多像（核对旧图用，只比一张模板）"""
        if hero_id not in self.ids:
            return 0.0
        j = self.ids.index(hero_id)
        t, _ = _patch(img, box, False)
        return max(0.0, *(_match(t, None, self.scaled(int(round(box[2] * f)))[j]) for f in scales))


def _patch(img: np.ndarray, box: Box, dimmed: bool) -> tuple[np.ndarray, np.ndarray | None]:
    """格子中段（拿去和模板比）；变暗的格子转灰度，并给出遮掉红斜线的 mask"""
    x, y, w, h = box
    t = img[y + int(h * .12):y + int(h * .70), x + 3:x + w - 3]
    if not dimmed:
        return t, None
    f = t.astype(np.int16)
    red = f[..., 2] - f[..., 1]
    mask = cv2.erode((red < np.median(red) + 8).astype(np.uint8), np.ones((5, 5), np.uint8)).astype(np.float32)
    return cv2.cvtColor(t, cv2.COLOR_BGR2GRAY), mask


def _match(t: np.ndarray, mask: np.ndarray | None, v: np.ndarray) -> float:
    if t.shape[0] > v.shape[0] or t.shape[1] > v.shape[1]:
        return 0.0
    if mask is not None:
        r = cv2.matchTemplate(cv2.cvtColor(v, cv2.COLOR_BGR2GRAY), t, cv2.TM_CCOEFF_NORMED, mask=mask)
        r = np.nan_to_num(r, nan=0, posinf=0, neginf=0)
    else:
        r = cv2.matchTemplate(v, t, cv2.TM_CCOEFF_NORMED)
    return float(r.max())


def assign(scores: np.ndarray, ids: list[int], later: list[bool] | None = None) -> list[tuple[int | None, float]]:
    """每个英雄只出现一次：反复取全局最高分的（格子, 英雄）。低于 MIN_SCORE 的格子不认。
    later = 哪些格子最后再分（变暗的格子分数普遍低，先让清楚的格子把英雄认领走，剩下的再给它们挑）。"""
    out: list[tuple[int | None, float]] = [(None, 0.0)] * len(scores)
    taken: set[int] = set()
    later = later or [False] * len(scores)
    for phase in (False, True):
        m = scores.copy()
        m[[i for i in range(len(scores)) if later[i] != phase]] = -1
        m[:, sorted(taken)] = -1
        for _ in range(min(m.shape)):
            i, j = np.unravel_index(int(np.argmax(m)), m.shape)
            if m[i, j] < (MIN_SCORE_DIM if phase else MIN_SCORE):
                break
            out[i] = (ids[j], float(m[i, j]))
            taken.add(j)
            m[i, :] = -1
            m[:, j] = -1
    return out


@dataclass
class GridMap:
    cells: list[Rect] = field(default_factory=list)
    heroes: list[int | None] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)
    size: tuple[int, int] = (0, 0)          # 建图时的画面宽高
    clean: bool = True                      # 建图时没有（或几乎没有）变暗的格子：认得准；否则变暗格子的英雄可能认错
    scale: float | None = None              # 建图时用的模板缩放（核对旧图用；2026-11-06 以前存的图没有）

    @classmethod
    def build(cls, img: np.ndarray, bank: GridBank, boxes: list[Box] | None = None) -> GridMap:
        boxes = find_cells(img) if boxes is None else boxes
        dim = [dimness(img, b) < DIM_STD for b in boxes]
        scale = bank.best_scale(img, boxes, dim)
        scores = bank.scores(img, boxes, dim, scales=(scale,))
        bg = background(img)
        scores[[covered(bg, b) for b in boxes]] = -1       # 被卡片盖住的格子先空着，以后 review 补认
        res = assign(scores, bank.ids, dim)
        H, W = img.shape[:2]
        return cls([(x / W, y / H, w / W, h / H) for x, y, w, h in boxes],
                   [h for h, _ in res], [round(s, 3) for _, s in res], (W, H), sum(dim) <= CLEAN_MAX_DIM, scale)

    def moved(self, img: np.ndarray, boxes: list[Box]) -> GridMap | None:
        """同一套网格整体挪了位置（2026-11-06 看到同一个网格有时整体高 81 像素、最上一排出了找格子的范围）：
        找平移量，把这张图的英雄搬到新格子上。找到的格子九成以上能对上才算；对不上的格子不认。"""
        H, W = img.shape[:2]
        if tuple(self.size) != (W, H) or not self.cells or not boxes:
            return None
        old, new = np.array(self.boxes(img), float), np.array(boxes, float)

        def dist(d):
            return np.abs(old[None, :, :2] - (new[:, None, :2] + d)).max(axis=2)
        best = (0, np.zeros(2))
        for b in new[::max(1, len(new) // 10)][:10]:      # 拿几个格子，各试一遍「它是旧图的哪一格」
            for o in old:
                if abs(o[2] - b[2]) <= 3:
                    n = int((dist(o[:2] - b[:2]).min(axis=1) <= 3).sum())
                    if n > best[0]:
                        best = (n, o[:2] - b[:2])
        if best[0] < len(boxes) * 0.9:
            return None
        d = dist(best[1])
        k, ok = d.argmin(axis=1), d.min(axis=1) <= 3
        return GridMap([(x / W, y / H, w / W, h / H) for x, y, w, h in boxes],
                       [self.heroes[k[i]] if ok[i] else None for i in range(len(boxes))],
                       [self.scores[k[i]] if ok[i] else 0.0 for i in range(len(boxes))], (W, H), self.clean, self.scale)

    def matches(self, img: np.ndarray, boxes: list[Box]) -> bool:
        """同一个布局：画面一样大，找到的格子几乎都落在已有格子上，而且找到了大半（变暗的格子有时找不到）"""
        H, W = img.shape[:2]
        if tuple(self.size) != (W, H) or not self.cells or len(boxes) < len(self.cells) * 0.7:
            return False
        old = np.array(self.boxes(img))
        hits = 0
        for x, y, w, h in boxes:
            d = np.abs(old[:, :2] - (x, y)).max(axis=1)
            k = int(np.argmin(d))
            hits += d[k] <= max(4, old[k, 2] * 0.25) and abs(old[k, 2] - w) <= max(4, w * 0.15)
        return hits >= len(boxes) * 0.9

    def boxes(self, img: np.ndarray | None = None) -> list[Box]:
        W, H = self.size if img is None else (img.shape[1], img.shape[0])
        return [(int(round(x * W)), int(round(y * H)), int(round(w * W)), int(round(h * H))) for x, y, w, h in self.cells]

    def to_json(self) -> dict:
        return {"cells": self.cells, "heroes": self.heroes, "scores": self.scores, "size": list(self.size), "clean": self.clean,
                "scale": self.scale}

    @classmethod
    def from_json(cls, raw: dict) -> GridMap:
        return cls([tuple(c) for c in raw["cells"]], raw["heroes"], raw["scores"], tuple(raw["size"]), raw.get("clean", True),
                   raw.get("scale"))


def wrong_cells(img: np.ndarray, gmap: GridMap, bank: GridBank) -> list[tuple[int, int, float, float]]:
    """核对图：没变暗的格子里，记的英雄对不上的。格子位置一样、英雄排列却可能不一样（2026-11-06 实战：
    力丸 / 斯温 / 复仇之魂 三格轮换了，禁了力丸报成禁了斯温），在已有禁用的画面上建的图也可能把变暗的格子认错。
    只算证据硬的：记的英雄 < MIN_SCORE，另一个英雄 ≥ MIN_SCORE 且高出 WRONG_GAP（鼠标停在格子上之类的小变化不算）。
    返回 [(格子序号, 其实像谁, 记的英雄分数, 那个英雄分数)]，找到 3 个就够了（和所有英雄比一格要几十毫秒）。"""
    bg = background(img)
    cells = [(i, hid, box) for i, (hid, box) in enumerate(zip(gmap.heroes, gmap.boxes(img)))
             if hid is not None and dimness(img, box) >= DIM_STD and not covered(bg, box)]
    if not cells:
        return []
    scales = (gmap.scale,) if gmap.scale else SCALES
    own = np.array([[bank.score(img, box, hid, (f,)) for f in scales] for _, hid, box in cells])
    scale = scales[int(np.argmax(np.median(own, axis=0)))]     # 以前存的图没记缩放：取大多数格子最像的那个
    out = []
    for (i, hid, box), mine in zip(cells, own.max(axis=1)):
        if mine >= MIN_SCORE:
            continue
        s = bank.scores(img, [box], scales=(scale,))[0]
        j = int(np.argmax(s))
        if bank.ids[j] != hid and s[j] >= MIN_SCORE and s[j] - mine >= WRONG_GAP:
            out.append((i, bank.ids[j], round(float(mine), 3), round(float(s[j]), 3)))
            if len(out) >= 3:
                break
    return out


def load_maps(path: Path = GRID_PATH) -> list[GridMap]:
    if not path.exists():
        return []
    try:
        return [GridMap.from_json(m) for m in json.loads(path.read_text(encoding="utf-8"))]
    except (ValueError, KeyError, TypeError):
        log.warning("网格布局文件坏了，重建：%s", path)
        return []


def save_maps(maps: list[GridMap], path: Path = GRID_PATH) -> None:
    path.write_text(json.dumps([m.to_json() for m in maps], ensure_ascii=False), encoding="utf-8")


# ---------- 禁用 ----------

def dimness(img: np.ndarray, box: Box) -> float:
    """格子中部的灰度标准差：变暗的格子很平"""
    x, y, w, h = box
    inner = cv2.cvtColor(img[y + h // 8:y + h * 3 // 4, x + 3:x + w - 3], cv2.COLOR_BGR2GRAY)
    return float(inner.std()) if inner.size else 99.0


def slash(img: np.ndarray, box: Box) -> float:
    """右上 → 左下的直线上，红色程度（R−G）比两侧高多少；只看中段，避开顶上的三角和底下的数字"""
    x, y, w, h = box
    c = img[y:y + h, x:x + w].astype(np.float32)
    if c.shape[0] < h or c.shape[1] < w:
        return 0.0
    red = c[..., 2] - c[..., 1]
    ys = np.arange(int(h * .12), int(h * .72))
    best = 0.0
    for top in np.linspace(0.6, 1.0, 9):
        for bot in np.linspace(-0.1, 0.3, 9):
            xs = (top + (bot - top) * ys / h) * w
            ok = (xs >= 5) & (xs < w - 5)
            if ok.sum() < len(ys) * 0.7:
                continue
            xi, yi = xs[ok].astype(int), ys[ok]
            on = np.median(red[yi, xi])
            off = (np.median(red[yi, xi - 5]) + np.median(red[yi, xi + 5])) / 2
            best = max(best, float(on - off))
    return best


def covered(bg: np.ndarray, box: Box) -> bool:
    """格子被别的东西盖住了：左右两边的缝里都看不到网格背景。鼠标停在英雄上时放大的卡片会盖住周围 3×3 格
    （2026-11-07 实战：卡片盖着的格子认不出、被认成卡片上的英雄，卡片的暗色 + 红边还被当成过禁用）。bg = background(画面)"""
    x, y, w, h = box
    ys = slice(y + h // 5, y + h * 4 // 5)
    left = bg[ys, max(0, x - 3):max(0, x - 1)]
    right = bg[ys, x + w + 1:x + w + 3]
    return max(left.mean() if left.size else 1.0, right.mean() if right.size else 1.0) < COVERED_BG


@dataclass
class CellState:
    hero_id: int | None
    box: Box
    dim: float
    slash: float
    hidden: bool = False                    # 被放大的卡片之类盖住了：这一帧不算

    @property
    def banned(self) -> bool:
        return not self.hidden and self.dim < DIM_STD and self.slash >= SLASH_MIN

    @property
    def taken(self) -> bool:
        """变暗：禁用或者已经被选走"""
        return not self.hidden and self.dim < DIM_STD


def states(img: np.ndarray, gmap: GridMap) -> list[CellState]:
    out, bg = [], background(img)
    for hid, box in zip(gmap.heroes, gmap.boxes(img)):
        d = dimness(img, box)
        out.append(CellState(hid, box, d, slash(img, box) if d < DIM_STD else 0.0, covered(bg, box)))
    return out


def banned(img: np.ndarray, gmap: GridMap) -> list[int]:
    return [s.hero_id for s in states(img, gmap) if s.banned and s.hero_id]


# ---------- 选人时一直看 ----------

class BanWatcher:
    """每次扫描调 update(画面)：认得这个布局就直接看哪些格子有红斜线（约 0.05 秒）；
    新布局交给 build_async 在后台建图（几秒），建好之前这一帧不出结果。禁用整局累加，新一局 reset()。

    干净画面（几乎没有变暗的格子）建的图最可靠；只能在已经有禁用的画面上建时先凑合用，等以后遇到干净画面再换掉。
    存下来的图边用边核对 / 补认（review）：没变暗的格子拿来对一遍，记错的改掉、没认出的补上，变暗的格子留着以前记的。
    给了 shot_dir 的话，禁用有变化、改了图时把这一帧和每格认成谁存下来（shots/grid/），事后好查。"""

    def __init__(self, path: Path = GRID_PATH, bank_factory=GridBank, min_cells: int = MIN_CELLS,
                 shot_dir: Path | None = None):
        self.path = path
        self.min_cells = min_cells
        self.shot_dir = shot_dir
        self.name: Callable[[int], str] = str     # 英雄 id → 名字（只给日志用，Live 换成中文名）
        self.maps = load_maps(path)
        self._bank_factory = bank_factory
        self._bank: GridBank | None = None
        self.building = False
        self.bans: set[int] = set()
        self.taken: set[int] = set()            # 变暗没斜线：已经被选走（调试 / 以后核对用）
        self.last_map: GridMap | None = None
        self._reviewing: int | None = None      # 正在核对的图（id）；换了图下面三项清空
        self._seen: set[int] = set()            # 这局核对过的格子（没变暗时对上了 / 改过了）
        self._retry: dict[int, float] = {}      # 和所有英雄比过、没认出的格子：到这个时间再试
        self._unknown: set[int] = set()         # 这局报过「有红斜线但不知道是谁」的格子

    def reset(self) -> None:
        self.bans, self.taken = set(), set()
        self._reviewing, self._seen, self._retry, self._unknown = None, set(), {}, set()

    def bank(self) -> GridBank:
        if self._bank is None:
            self._bank = self._bank_factory()
        if not len(self._bank):
            raise FileNotFoundError(f"没有网格图（{GRID_PORTRAIT_DIR}），启动助手时要能找到 Dota 安装目录")
        return self._bank

    def find_map(self, img: np.ndarray, boxes: list[Box]) -> GridMap | None:
        return next((m for m in self.maps if m.matches(img, boxes)), None)

    def needs_map(self, img: np.ndarray, boxes: list[Box]) -> bool:
        """要不要（重）建图：没见过这个布局，或者只有凑合的图而现在是干净画面"""
        if len(boxes) < self.min_cells:
            return False
        m = self.find_map(img, boxes)
        if m is None:
            return True
        return not m.clean and sum(dimness(img, b) < DIM_STD for b in boxes) <= CLEAN_MAX_DIM

    def build(self, img: np.ndarray, boxes: list[Box]) -> GridMap:
        """慢（几秒），在后台线程里调；结果用 add_map 放回来。
        先看是不是哪张干净图整体挪了位置：搬过来、核对没错就用它（比在已有禁用的画面上重认准）。"""
        bank = self.bank()
        for m in self.maps:
            if m.clean and (g := m.moved(img, boxes)) is not None and not wrong_cells(img, g, bank):
                log.info("选人网格：和以前一张干净画面建的图是同一套格子、整体挪了位置，直接搬过来")
                return g
        return GridMap.build(img, bank, boxes)

    def add_map(self, gmap: GridMap) -> None:
        dummy = np.zeros((gmap.size[1], gmap.size[0], 1), np.uint8)
        self.maps = [m for m in self.maps if not m.matches(dummy, gmap.boxes())][-(MAX_MAPS - 1):] + [gmap]
        save_maps(self.maps, self.path)

    def review(self, img: np.ndarray, gmap: GridMap) -> bool:
        """核对 / 补认这张图（每帧调；只看这局还没核对过的、没变暗的格子）：
        - 记的英雄对得上（≥ MIN_SCORE）：这格这局不用再看；
        - 对不上 / 没认出：和所有英雄比，另一个英雄 ≥ MIN_SCORE 且高出 WRONG_GAP 就改成它；
          那个英雄原来记在别的格子上的话那格清空（那格这局已经核对过、对得上的话两边都不动）。
          格子位置一样、英雄排列会变（2026-11-06 力丸 / 斯温 / 复仇之魂轮换）；
          建图那一帧鼠标停在某个英雄上，放大的卡片盖住周围 3×3 格，这几格都认不出（2026-11-07 殁境神蚀者）。
          认不出的隔 REVIEW_RETRY 秒再试（等卡片挪开）。
        - 被放大的卡片盖住的格子（covered）这一帧不看；
        - 变暗的格子没法认（灰度比很不准），留着以前记的；清空以后只剩一格变暗的空着、图里也只缺一个英雄，就是它。
        返回 False = 核对的格子一大半对不上：多半是别的布局碰巧格子位置像，扔掉这张图（draft 那边会重建）。"""
        if self._reviewing != id(gmap):
            self._reviewing, self._seen, self._retry, self._unknown = id(gmap), set(), {}, set()
        bank, boxes, now, bg = self.bank(), gmap.boxes(img), time.monotonic(), background(img)
        clear = [i for i in range(len(boxes))
                 if i not in self._seen and dimness(img, boxes[i]) >= DIM_STD and not covered(bg, boxes[i])]
        if not clear:
            return True
        if gmap.scale is None:                 # 2026-11-06 以前存的图没记缩放：取大多数格子最像的那个
            own = np.array([[bank.score(img, boxes[i], gmap.heroes[i], (f,)) for f in SCALES]
                            for i in clear[:20] if gmap.heroes[i] is not None])
            gmap.scale = SCALES[int(np.argmax(np.median(own, axis=0)))] if len(own) else SCALES[1]
        todo = []
        for i in clear:
            hid = gmap.heroes[i]
            own = bank.score(img, boxes[i], hid, (gmap.scale,)) if hid is not None else 0.0
            if own >= MIN_SCORE:
                self._seen.add(i)
                gmap.scores[i] = max(gmap.scores[i], round(own, 3))
            elif now >= self._retry.get(i, 0.0):
                todo.append((i, own))
        labeled = sum(1 for i in clear if gmap.heroes[i] is not None)
        bad = sum(1 for i, _ in todo if gmap.heroes[i] is not None)
        if labeled >= 20 and bad > labeled / 2:
            log.warning("选人网格：存的布局图 %d 格里 %d 格对不上，不是这个布局，扔掉重建", labeled, bad)
            self.save_shot(img, gmap, "对不上")
            self.maps = [m for m in self.maps if m is not gmap]
            save_maps(self.maps, self.path)
            return False
        fixed = []
        for i, own in todo[:REVIEW_PER_FRAME]:
            s = bank.scores(img, [boxes[i]], scales=(gmap.scale,))[0]
            j = int(np.argmax(s))
            h, best, old = bank.ids[j], float(s[j]), gmap.heroes[i]
            other = gmap.heroes.index(h) if h in gmap.heroes else None
            if h == old or best < MIN_SCORE or best - own < WRONG_GAP or (other is not None and other in self._seen):
                self._retry[i] = now + REVIEW_RETRY
                continue
            if other is not None:
                gmap.heroes[other], gmap.scores[other] = None, 0.0
            gmap.heroes[i], gmap.scores[i] = h, round(best, 3)
            self._seen.add(i)
            fixed.append(f"第 {i} 格 {self.name(old) if old else '没认出'}（{own:.2f}）→ {self.name(h)}（{best:.2f}）"
                         + (f"，第 {other} 格清空" if other is not None else ""))
        holes = [i for i, h in enumerate(gmap.heroes)
                 if h is None and dimness(img, boxes[i]) < DIM_STD and not covered(bg, boxes[i])]
        missing = set(bank.ids) - set(gmap.heroes)
        if fixed and len(holes) == 1 and len(missing) == 1:
            gmap.heroes[holes[0]] = missing.pop()
            fixed.append(f"第 {holes[0]} 格变暗了认不了，图里只缺{self.name(gmap.heroes[holes[0]])}，就是它")
        if fixed:
            log.info("选人网格：核对改了 %d 格：%s", len(fixed), "；".join(fixed))
            save_maps(self.maps, self.path)
            self.save_shot(img, gmap, "改图")
        return True

    def save_shot(self, img: np.ndarray, gmap: GridMap, why: str) -> None:
        """存这一帧 + 每格认成谁（英雄 id、变暗程度、红斜线），文件名带时间和原因"""
        if self.shot_dir is None:
            return
        try:
            path = self.shot_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{why}.png"
            write_image(path, img)
            cells = [{"hero": s.hero_id, "box": list(s.box), "dim": round(s.dim, 1), "slash": round(s.slash, 1),
                      "banned": s.banned, "taken": s.taken} for s in states(img, gmap)]
            path.with_suffix(".json").write_text(json.dumps(
                {"bans": sorted(self.bans), "taken": sorted(self.taken), "clean": gmap.clean, "cells": cells},
                ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            log.warning("选人网格截图没存上：%s", e)

    def update(self, img: np.ndarray, boxes: list[Box] | None = None) -> bool | None:
        """返回 True = 禁用有变化，False = 没变，None = 这一帧没法看（不是网格 / 布局还没建图 / 存的图不是这个布局）"""
        boxes = find_cells(img) if boxes is None else boxes
        if len(boxes) < self.min_cells:
            return None
        gmap = self.find_map(img, boxes)
        if gmap is None or not self.review(img, gmap):
            return None
        self.last_map = gmap
        before = (set(self.bans), set(self.taken))
        for i, s in enumerate(states(img, gmap)):
            if s.hero_id is None:
                if s.banned and i not in self._unknown:
                    self._unknown.add(i)
                    log.warning("选人网格：第 %d 格（%d, %d）有红斜线，但不知道是哪个英雄", i, s.box[0], s.box[1])
                    self.save_shot(img, gmap, "禁用没认出")
                continue
            if s.banned:
                self.bans.add(s.hero_id)
            elif s.taken and s.hero_id not in self.bans:
                self.taken.add(s.hero_id)
        if self.bans != before[0]:
            self.save_shot(img, gmap, "禁用")
        return (self.bans, self.taken) != before
