"""选人界面识别：按校准好的格子位置裁头像，和模板逐个比相似度。

- 校准（calibration.json）：每个格子是屏幕上的矩形，用占屏幕宽高的比例存，换分辨率（同宽高比）不用重标。
- 模板：data/portraits/ 下的官方头像是初始模板；templates/<英雄 id>/ 下是从真实截图里
  「学」来的模板（控制面板里点「记住这次识别」或 label 命令存下），越用越准。
  templates/0/ 存空位（还没选英雄时格子的样子）。
- 相似度：缩成小图后逐通道标准化，算余弦相似度（≈ 归一化互相关）。
- 噪点：顶栏头像上常盖着别的东西（颜色条、天梯段位条、英雄徽章），比的时候把这些地方挖掉（VIEWS）。
"""
from __future__ import annotations

import json
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from ..core.config import DATA_DIR, ROOT

log = logging.getLogger(__name__)

CALIBRATION_PATH = ROOT / "calibration.json"
TEMPLATE_DIR = ROOT / "templates"
FEAT_W, FEAT_H = 32, 18          # 特征图尺寸（16:9，和官方横版头像一致）
EMPTY = 0                        # 空位的「英雄 id」


def _mask(bar: float = 1.0, cut: float = 1.0, badge: tuple[float, float] | None = None) -> np.ndarray:
    """特征图上哪些格子参与比较（True = 用）：y ≥ bar 去掉、y ≥ cut 去掉、x ≥ badge[0] 且 y ≥ badge[1] 去掉。坐标都是占格子的比例"""
    y, x = np.meshgrid((np.arange(FEAT_H) + 0.5) / FEAT_H, (np.arange(FEAT_W) + 0.5) / FEAT_W, indexing="ij")
    m = (y < bar) & (y < cut)
    if badge:
        m &= ~((x >= badge[0]) & (y >= badge[1]))
    return m


# 顶栏格子里盖在头像上的东西（2026-10-28 拿真实选人截图量的，按格子比例）：
# - 下面 y ≥ 0.90：玩家颜色条，一直有
# - 天梯：y 0.59~0.89 一条半透明红底「超凡入圣III」段位字，选了英雄也还盖着（10-22 天梯截图，斯拉克只有 0.62）
# - 右下角 x ≥ 0.68、y ≥ 0.28：英雄徽章（10-23 拉比克盖着「5」0.63、痛苦女王盖着「7」0.73）
# 不知道这格盖了哪几样，三种比法都比一次，取第一名和第二名拉得最开的那种：
# 整张 / 去掉颜色条和徽章 / 只看上面 58%（段位条以上）去掉徽章
VIEWS = (None, _mask(bar=0.90, badge=(0.68, 0.28)), _mask(cut=0.58, badge=(0.68, 0.28)))
# 认出来的判据：第一名比第二名（别的英雄）高出 SURE_GAP 以上，而且分数过 SURE_SCORE（match_threshold 设得更低就用它）。
# 只看分数靠不住：盖了噪点的对的那个只有 0.6 上下，挖掉噪点以后空位 / 预选的灰头像反而能到 0.76（像艾欧）；拉不拉得开才靠得住。
# 同一批截图（20 个已选格子、29 个空位 / 预选）：已选的第一名全对、高出第二名 0.23 以上，分数 0.68 以上；空位 / 预选最多高出 0.09
SURE_SCORE = 0.5
SURE_GAP = 0.15


def confident(score: float, gap: float, threshold: float) -> bool:
    return gap >= SURE_GAP and score >= min(threshold, SURE_SCORE)

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
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return cls(**{k: [tuple(r) for r in v] if isinstance(v, list) else v for k, v in raw.items()})
        except (ValueError, TypeError, AttributeError) as e:   # 文件坏了：当没校准，别让启动直接失败
            log.warning("校准文件 %s 读不了（%s），当作没校准；重新 calibrate 一次", path, e)
            return None

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


def feature(img: np.ndarray, mask: np.ndarray | None = None) -> np.ndarray:
    """BGR 图 → 单位长度特征向量；mask 给了就只用这些位置（VIEWS）"""
    small = cv2.resize(img, (FEAT_W, FEAT_H), interpolation=cv2.INTER_AREA).astype(np.float32)
    if mask is not None:
        small = small[mask]                                  # (n, 3)
    small -= small.mean(axis=tuple(range(small.ndim - 1)), keepdims=True)
    small /= small.std(axis=tuple(range(small.ndim - 1)), keepdims=True) + 1e-6
    v = small.reshape(-1)
    return v / (np.linalg.norm(v) + 1e-6)


def _feats(img: np.ndarray) -> list[np.ndarray]:
    return [feature(img, m) for m in VIEWS]


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    """两张格子裁图像不像：几种挖噪点的比法（VIEWS）里最高的那个，和 TemplateBank.scores 一个算法"""
    return max(float(x @ y) for x, y in zip(_feats(a), _feats(b)))


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
            feats.append(_feats(read_image(p)))
        self.portraits = len(ids)
        self.learned = 0
        if template_dir.exists():
            for d in template_dir.iterdir():
                if d.is_dir() and d.name.isdigit():
                    for p in d.glob("*.png"):
                        ids.append(int(d.name))
                        feats.append(_feats(read_image(p)))
                        self.learned += 1
        if not feats:
            raise FileNotFoundError("没有任何头像模板，先运行：python -m dota_assistant update")
        self.ids = np.array(ids)
        self.feats = [np.stack([f[v] for f in feats]) for v in range(len(VIEWS))]   # 每种比法一个矩阵

    def best(self, img: np.ndarray) -> tuple[int, float, float]:
        """返回 (最像的英雄 id，相似度，比第二像的别的 id 高出多少)；id=0 表示最像空位。
        几种挖噪点的比法（VIEWS）各比一次，用拉得最开的那种"""
        if img.size == 0:
            return EMPTY, 0.0, 0.0
        out = (EMPTY, 0.0, -1.0)
        for F, f in zip(self.feats, _feats(img)):
            sims = F @ f
            i = int(np.argmax(sims))
            other = sims[self.ids != self.ids[i]]
            gap = float(sims[i] - other.max()) if other.size else float(sims[i])
            if gap > out[2]:
                out = (int(self.ids[i]), float(sims[i]), gap)
        return out

    def match(self, img: np.ndarray) -> tuple[int, float]:
        """返回 (最像的英雄 id，相似度)；id=0 表示最像空位"""
        return self.best(img)[:2]

    def scores(self, img: np.ndarray) -> dict[int, float]:
        """每个英雄（不含空位）和这张图最像的那张模板的相似度（几种比法里最高的）"""
        if img.size == 0:
            return {}
        sims = np.max([F @ f for F, f in zip(self.feats, _feats(img))], axis=0)
        out: dict[int, float] = {}
        for hid, s in zip(self.ids.tolist(), sims.tolist()):
            if hid != EMPTY and s > out.get(hid, -2.0):
                out[hid] = s
        return out

    def learn(self, hero_id: int, img: np.ndarray) -> Path:
        """把一张裁好的格子存成模板（hero_id=0 表示空位）。下次构造 TemplateBank 时生效，也立即加入内存。"""
        path = self.template_dir / str(hero_id) / f"{int(time.time() * 1000)}.png"
        write_image(path, img)
        self.ids = np.append(self.ids, hero_id)
        self.feats = [np.vstack([F, f]) for F, f in zip(self.feats, _feats(img))]
        self.learned += 1
        return path


@dataclass
class Slot:
    hero_id: int | None        # None = 空位或认不出
    score: float
    guess: int                 # 最像的那个 id（哪怕低于阈值），方便调试
    image: np.ndarray | None = None
    gap: float = 0.0           # 比第二像的别的英雄高出多少（confident）
    clash: bool = False        # 和别的格子认成了同一个英雄，都不算（见 recognize）


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
        hid, score, gap = bank.best(c)
        ok = hid != EMPTY and confident(score, gap, threshold)
        return Slot(hid if ok else None, score, hid, c.copy() if keep_images else None, gap)

    radiant, dire = [one(r) for r in cal.radiant], [one(r) for r in cal.dire]
    # 一个英雄只能在一格：认进好几格说明模板学坏了，这几格都不算。2026-10-29 实战：帮认时一张空位样子的裁图被记成了风行者，
    # 下一局选人 8 个空格全认成风行者（0.79~0.98），推荐按「对面 4 个风行者」算
    seen = Counter(s.hero_id for s in radiant + dire if s.hero_id)
    for s in radiant + dire:
        if seen[s.hero_id] > 1:
            s.hero_id, s.clash = None, True
    return DraftView(radiant, dire, [one(r) for r in cal.bans])
