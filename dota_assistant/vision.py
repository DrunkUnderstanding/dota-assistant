"""选人界面识别：按校准好的格子位置裁头像，和模板逐个比相似度。

- 校准（calibration.json）：每个格子是屏幕上的矩形，用占屏幕宽高的比例存，换分辨率（同宽高比）不用重标。
- 模板：data/portraits/ 下的官方头像是初始模板；templates/<英雄 id>/ 下是从真实截图里
  「学」来的模板（控制面板里点「记住这次识别」或 label 命令存下），越用越准。
  templates/0/ 存空位（还没选英雄时格子的样子）。
- 相似度：缩成小图后逐通道标准化，算余弦相似度（≈ 归一化互相关）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .config import DATA_DIR, ROOT

CALIBRATION_PATH = ROOT / "calibration.json"
TEMPLATE_DIR = ROOT / "templates"
FEAT_W, FEAT_H = 32, 18          # 特征图尺寸（16:9，和官方横版头像一致）
EMPTY = 0                        # 空位的「英雄 id」

Rect = tuple[float, float, float, float]   # x, y, w, h，都是占屏幕的比例


@dataclass
class Calibration:
    radiant: list[Rect] = field(default_factory=list)   # 天辉 5 个格子，从左到右
    dire: list[Rect] = field(default_factory=list)      # 夜魇 5 个格子
    bans: list[Rect] = field(default_factory=list)      # 禁用格子，可以没有
    note: str = ""                                      # 标定时的分辨率等备注

    def save(self, path: Path = CALIBRATION_PATH) -> None:
        path.write_text(json.dumps(self.__dict__, ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path = CALIBRATION_PATH) -> Calibration | None:
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(**{k: [tuple(r) for r in v] if isinstance(v, list) else v for k, v in raw.items()})

    @property
    def ready(self) -> bool:
        return len(self.radiant) == 5 and len(self.dire) == 5


def interpolate(first: Rect, last: Rect, n: int) -> list[Rect]:
    """首尾两个格子 → 等间距的 n 个格子（顶栏英雄格是等距排列的）"""
    if n == 1:
        return [first]
    return [tuple(a + (b - a) * i / (n - 1) for a, b in zip(first, last)) for i in range(n)]


def crop(img: np.ndarray, r: Rect) -> np.ndarray:
    h, w = img.shape[:2]
    x0, y0 = int(round(r[0] * w)), int(round(r[1] * h))
    x1, y1 = int(round((r[0] + r[2]) * w)), int(round((r[1] + r[3]) * h))
    return img[max(y0, 0):max(y1, y0 + 1), max(x0, 0):max(x1, x0 + 1)]


def feature(img: np.ndarray) -> np.ndarray:
    """BGR 图 → 单位长度特征向量"""
    small = cv2.resize(img, (FEAT_W, FEAT_H), interpolation=cv2.INTER_AREA).astype(np.float32)
    small -= small.mean(axis=(0, 1), keepdims=True)
    small /= small.std(axis=(0, 1), keepdims=True) + 1e-6
    v = small.reshape(-1)
    return v / (np.linalg.norm(v) + 1e-6)


def read_image(path: Path) -> np.ndarray:
    """读图（支持中文路径），带透明通道的铺到黑底上，返回 BGR"""
    img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError(f"读不了图片 {path}")
    if img.ndim == 2:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    if img.shape[2] == 4:
        alpha = img[:, :, 3:4].astype(np.float32) / 255
        return (img[:, :, :3].astype(np.float32) * alpha).astype(np.uint8)
    return img


def write_image(path: Path, img: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".png", img)
    if ok:
        buf.tofile(str(path))


class TemplateBank:
    def __init__(self, portrait_dir: Path = DATA_DIR / "portraits", template_dir: Path = TEMPLATE_DIR):
        self.template_dir = template_dir
        ids, feats = [], []
        for p in sorted(portrait_dir.glob("*.png")):
            ids.append(int(p.stem))
            feats.append(feature(read_image(p)))
        self.portraits = len(ids)
        self.learned = 0
        if template_dir.exists():
            for d in template_dir.iterdir():
                if d.is_dir() and d.name.isdigit():
                    for p in d.glob("*.png"):
                        ids.append(int(d.name))
                        feats.append(feature(read_image(p)))
                        self.learned += 1
        if not feats:
            raise FileNotFoundError("没有任何头像模板，先运行：python -m dota_assistant update")
        self.ids = np.array(ids)
        self.feats = np.stack(feats)

    def match(self, img: np.ndarray) -> tuple[int, float]:
        """返回 (最像的英雄 id，相似度)；id=0 表示最像空位"""
        if img.size == 0:
            return EMPTY, 0.0
        sims = self.feats @ feature(img)
        i = int(np.argmax(sims))
        return int(self.ids[i]), float(sims[i])

    def learn(self, hero_id: int, img: np.ndarray) -> Path:
        """把一张裁好的格子存成模板（hero_id=0 表示空位）。下次构造 TemplateBank 时生效，也立即加入内存。"""
        path = self.template_dir / str(hero_id) / f"{int(time.time() * 1000)}.png"
        write_image(path, img)
        self.ids = np.append(self.ids, hero_id)
        self.feats = np.vstack([self.feats, feature(img)])
        self.learned += 1
        return path


@dataclass
class Slot:
    hero_id: int | None        # None = 空位或认不出
    score: float
    guess: int                 # 最像的那个 id（哪怕低于阈值），方便调试
    image: np.ndarray | None = None


@dataclass
class DraftView:
    radiant: list[Slot]
    dire: list[Slot]
    bans: list[Slot]

    def ids(self, side: str) -> list[int]:
        return [s.hero_id for s in getattr(self, side) if s.hero_id]

    def key(self) -> tuple:
        """用于判断两次识别结果是否一致"""
        return tuple(tuple(s.hero_id for s in getattr(self, k)) for k in ("radiant", "dire", "bans"))


def recognize(img: np.ndarray, cal: Calibration, bank: TemplateBank, threshold: float,
              keep_images: bool = False) -> DraftView:
    def one(r: Rect) -> Slot:
        c = crop(img, r)
        hid, score = bank.match(c)
        ok = hid != EMPTY and score >= threshold
        return Slot(hid if ok else None, score, hid, c.copy() if keep_images else None)

    return DraftView([one(r) for r in cal.radiant], [one(r) for r in cal.dire], [one(r) for r in cal.bans])
