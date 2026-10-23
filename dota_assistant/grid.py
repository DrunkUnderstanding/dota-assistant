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
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .config import DATA_DIR
from .gamefiles import GRID_PORTRAIT_DIR

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
        for i, (x, y, w, h) in enumerate(boxes):
            y0, y1, x0, x1 = y + int(h * .12), y + int(h * .70), x + 3, x + w - 3
            t = img[y0:y1, x0:x1]
            mask = None
            if dimmed and dimmed[i]:
                f = t.astype(np.int16)
                red = f[..., 2] - f[..., 1]
                mask = cv2.erode((red < np.median(red) + 8).astype(np.uint8), np.ones((5, 5), np.uint8)).astype(np.float32)
                t = cv2.cvtColor(t, cv2.COLOR_BGR2GRAY)
            for f in scales:
                for j, v in enumerate(self.scaled(int(round(w * f)))):
                    if t.shape[0] > v.shape[0] or t.shape[1] > v.shape[1]:
                        continue
                    if mask is not None:
                        r = cv2.matchTemplate(cv2.cvtColor(v, cv2.COLOR_BGR2GRAY), t, cv2.TM_CCOEFF_NORMED, mask=mask)
                        r = np.nan_to_num(r, nan=0, posinf=0, neginf=0)
                    else:
                        r = cv2.matchTemplate(v, t, cv2.TM_CCOEFF_NORMED)
                    out[i, j] = max(out[i, j], float(r.max()))
        return out


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

    @classmethod
    def build(cls, img: np.ndarray, bank: GridBank, boxes: list[Box] | None = None) -> GridMap:
        boxes = find_cells(img) if boxes is None else boxes
        dim = [dimness(img, b) < DIM_STD for b in boxes]
        scale = bank.best_scale(img, boxes, dim)
        res = assign(bank.scores(img, boxes, dim, scales=(scale,)), bank.ids, dim)
        H, W = img.shape[:2]
        return cls([(x / W, y / H, w / W, h / H) for x, y, w, h in boxes],
                   [h for h, _ in res], [round(s, 3) for _, s in res], (W, H), sum(dim) <= CLEAN_MAX_DIM)

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
        return {"cells": self.cells, "heroes": self.heroes, "scores": self.scores, "size": list(self.size), "clean": self.clean}

    @classmethod
    def from_json(cls, raw: dict) -> GridMap:
        return cls([tuple(c) for c in raw["cells"]], raw["heroes"], raw["scores"], tuple(raw["size"]), raw.get("clean", True))


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


@dataclass
class CellState:
    hero_id: int | None
    box: Box
    dim: float
    slash: float

    @property
    def banned(self) -> bool:
        return self.dim < DIM_STD and self.slash >= SLASH_MIN

    @property
    def taken(self) -> bool:
        """变暗：禁用或者已经被选走"""
        return self.dim < DIM_STD


def states(img: np.ndarray, gmap: GridMap) -> list[CellState]:
    out = []
    for hid, box in zip(gmap.heroes, gmap.boxes(img)):
        d = dimness(img, box)
        out.append(CellState(hid, box, d, slash(img, box) if d < DIM_STD else 0.0))
    return out


def banned(img: np.ndarray, gmap: GridMap) -> list[int]:
    return [s.hero_id for s in states(img, gmap) if s.banned and s.hero_id]


# ---------- 选人时一直看 ----------

class BanWatcher:
    """每次扫描调 update(画面)：认得这个布局就直接看哪些格子有红斜线（约 0.05 秒）；
    新布局交给 build_async 在后台建图（几秒），建好之前这一帧不出结果。禁用整局累加，新一局 reset()。

    干净画面（几乎没有变暗的格子）建的图最可靠；只能在已经有禁用的画面上建时先凑合用，等以后遇到干净画面再换掉。"""

    def __init__(self, path: Path = GRID_PATH, bank_factory=GridBank, min_cells: int = MIN_CELLS):
        self.path = path
        self.min_cells = min_cells
        self.maps = load_maps(path)
        self._bank_factory = bank_factory
        self._bank: GridBank | None = None
        self.building = False
        self.bans: set[int] = set()
        self.taken: set[int] = set()            # 变暗没斜线：已经被选走（调试 / 以后核对用）
        self.last_map: GridMap | None = None

    def reset(self) -> None:
        self.bans, self.taken = set(), set()

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
        """慢（几秒），在后台线程里调；结果用 add_map 放回来"""
        if self._bank is None:
            self._bank = self._bank_factory()
        if not len(self._bank):
            raise FileNotFoundError(f"没有网格图（{GRID_PORTRAIT_DIR}），启动助手时要能找到 Dota 安装目录")
        return GridMap.build(img, self._bank, boxes)

    def add_map(self, gmap: GridMap) -> None:
        dummy = np.zeros((gmap.size[1], gmap.size[0], 1), np.uint8)
        self.maps = [m for m in self.maps if not m.matches(dummy, gmap.boxes())][-(MAX_MAPS - 1):] + [gmap]
        save_maps(self.maps, self.path)

    def update(self, img: np.ndarray, boxes: list[Box] | None = None) -> bool | None:
        """返回 True = 禁用有变化，False = 没变，None = 这一帧没法看（不是网格 / 布局还没建图）"""
        boxes = find_cells(img) if boxes is None else boxes
        if len(boxes) < self.min_cells:
            return None
        gmap = self.find_map(img, boxes)
        if gmap is None:
            return None
        self.last_map = gmap
        before = (set(self.bans), set(self.taken))
        for s in states(img, gmap):
            if s.hero_id is None:
                continue
            if s.banned:
                self.bans.add(s.hero_id)
            elif s.taken and s.hero_id not in self.bans:
                self.taken.add(s.hero_id)
        return (self.bans, self.taken) != before
