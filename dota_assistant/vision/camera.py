"""游戏画面里的镜头：从截图里自己英雄的血条 + GSI 里英雄的世界坐标，反推镜头正对着地图哪一点，
再把野点的世界坐标换算成屏幕像素（叠加层画在游戏画面里的野点上，web/world.html）。

为什么这样做：GSI 不给镜头位置；读游戏内存越线。血条是玩家自己看得到的画面，英雄坐标是 GSI 官方给的。
英雄不在画面里（死了、镜头拉走了）就不知道镜头在哪，那时什么都不画。

镜头模型（2026-11-03 在 Dota「试玩英雄 · 完整地图」里量的，1920×1080、默认镜头距离、没滚轮拉近）：
针孔相机，朝正北俯视，俯角 PITCH，离它对着的地面点 DIST，焦距 FOCAL 像素，画面中心 = 对着的那一点；
镜头只平移不转。量法：镜头不动，右键 12 个地面点让英雄走过去，对照 GSI 的 hero.xpos / ypos 拟合，
7 个没被挡住的点残差 0.1~0.4 像素。血条：左上角（绿条左上那一点）= 英雄头顶 HEALTHBAR_Z（游戏文件
npc_dota_hero_base 的 HealthBarOffset 200，拟合出 198.7）投到屏幕上再挪 (BAR_DX, BAR_DY) 像素，12 个点残差 < 1 像素。
拿它反推镜头再投野点：和 Dota 自己按住 Alt 显示的野点图标（刷新框里「堆野 / 拉野」那个圈）对上。
别的分辨率按屏幕高度等比缩放（FOCAL、血条像素都乘 高 / 1080）；16:10 之类的宽高比、滚轮拉近过镜头都没试。
不知道地面高低：野点和英雄当成一样高（高低差一层 128 码，画面上约差 45 像素）。
"""
from __future__ import annotations

import math

import cv2
import numpy as np

PITCH = 59.94                # 俯角（度）
DIST = 1191.1                # 镜头到它对着的地面点（世界单位）
FOCAL = 832.4                # 焦距（1080 高时的像素）
HEALTHBAR_Z = 198.7          # 血条在英雄脚下往上多高
BAR_DX, BAR_DY = -54.07, -37.30   # 血条左上角相对那一点挪多少像素（1080 高时）
BAR_ROWS = (8, 14)           # 绿条高几行（1080 高时 10 行）
BAR_MIN_W = 20               # 绿条至少多长（掉血了会变短；1080 高时满血约 106）
MATCH_RANGE = 1500           # 好几个血条时（队友也在画面里）：选算出来的镜头离上一次最近的，超过这么远都不认

_SIN, _COS = math.sin(math.radians(PITCH)), math.cos(math.radians(PITCH))


def _scale(h: int) -> float:
    return h / 1080


def project(points, target, size: tuple[int, int]) -> np.ndarray:
    """世界坐标 (x, y[, z]) → 屏幕像素。target = 镜头对着的地面点 (x, y)，z 相对那一点的高度；size = 画面 (宽, 高)"""
    w, h = size
    p = np.atleast_2d(np.asarray(points, float))
    z = p[:, 2] if p.shape[1] > 2 else np.zeros(len(p))
    dx, dy = p[:, 0] - target[0], p[:, 1] - target[1] + DIST * _COS
    dz = z - DIST * _SIN
    depth = dy * _COS - dz * _SIN
    up = dy * _SIN + dz * _COS
    f = FOCAL * _scale(h)
    return np.c_[w / 2 + f * dx / depth, h / 2 - f * up / depth]


def unproject(px, z: float, size: tuple[int, int]) -> np.ndarray:
    """屏幕像素 → 高度 z 的水平面上的点，相对镜头对着的地面点（镜头在原点上方）"""
    w, h = size
    f = FOCAL * _scale(h)
    a, b = (px[0] - w / 2) / f, -(px[1] - h / 2) / f
    d = np.array([a, _COS + b * _SIN, -_SIN + b * _COS])       # 视线方向：正前 + a·右 + b·上
    c = np.array([0.0, -DIST * _COS, DIST * _SIN])
    t = (z - c[2]) / d[2]
    return (c + t * d)[:2]


def target_from_bar(bar: tuple[int, int], hero: tuple[float, float], size: tuple[int, int]) -> np.ndarray:
    """血条左上角像素 + 英雄世界坐标 → 镜头对着的地面点"""
    s = _scale(size[1])
    q = unproject((bar[0] - BAR_DX * s, bar[1] - BAR_DY * s), HEALTHBAR_Z, size)
    return np.asarray(hero, float) - q


def find_bars(img: np.ndarray) -> list[tuple[int, int, int]]:
    """画面里自己英雄的血条：亮绿条（8~14 行），上面黑边再上面一道白边，下面隔一道黑线是蓝色魔法条。返回 [(左, 上, 长)]。
    小兵 / 野怪的血条没有魔法条；队友英雄的血条绿得暗一点、外框是深绿不是白的（2026-11-03 试玩里加了个电脑队友对比过），都不算。
    先在一半大小的图上找，再回原图量准"""
    h = img.shape[0]
    s = _scale(h)
    lo, hi = round(BAR_ROWS[0] * s), round(BAR_ROWS[1] * s)
    small = img[::2, ::2]
    # BGR：绿条约 (100, 249, 177)，魔法条约 (247, 121, 78)
    mask = cv2.inRange(small, (0, 200, 120), (140, 255, 215))
    n, _, st, _ = cv2.connectedComponentsWithStats(mask)
    out = []
    for i in range(1, n):
        x, y, ww, hh, area = (int(v) for v in st[i])
        if not (lo // 2 <= hh <= hi // 2 + 1 and ww * 2 >= BAR_MIN_W * s and area >= 0.7 * ww * hh):
            continue
        x0, y0, x1, y1 = max(0, 2 * x - 2), max(0, 2 * y - 2), 2 * (x + ww) + 2, 2 * (y + hh) + 2
        roi = img[y0:y1, x0:x1]
        g = (roi[:, :, 1] > 200) & (roi[:, :, 2] > 120) & (roi[:, :, 2] < 215) & (roi[:, :, 0] < 140) \
            & (roi[:, :, 1].astype(int) - roi[:, :, 2] > 40)
        rows, cols = np.nonzero(g.sum(axis=1) >= max(4, g.shape[1] // 2))[0], np.nonzero(g.any(axis=0))[0]
        if not len(rows) or not len(cols):
            continue
        top, bottom, left, right = y0 + rows[0], y0 + rows[-1] + 1, x0 + cols[0], x0 + cols[-1] + 1
        if not lo <= bottom - top <= hi or top < 2:
            continue
        below = img[bottom + 1:bottom + 1 + max(2, round(3 * s)), left:right]
        blue = (below[:, :, 0] > 200) & (below[:, :, 1] > 90) & (below[:, :, 1] < 150) & (below[:, :, 2] < 110)
        if not below.size or blue.mean() < 0.6:
            continue
        if (img[top - 2, left:right].max(axis=1) > 60).mean() > 0.3:      # 上面要是黑边
            continue
        frame = img[max(0, top - round(6 * s)):top - 1, left:right].min(axis=2)
        if not len(frame) or (frame > 200).mean(axis=1).max() < 0.8:      # 黑边再上面一道白边：自己的英雄
            continue
        out.append((int(left), int(top), int(right - left)))
    return out


class Tracker:
    """每帧：画面 + GSI 英雄坐标 → 镜头对着的地面点（认不出是 None）。好几个血条时选离上一次最近的；
    第一次（或丢了以后）只有画面里正好一个英雄血条才认"""

    def __init__(self):
        self.target: np.ndarray | None = None

    def update(self, img: np.ndarray, hero: tuple[float, float]) -> np.ndarray | None:
        size = (img.shape[1], img.shape[0])
        cands = [target_from_bar(b[:2], hero, size) for b in find_bars(img)]
        if self.target is not None and cands:
            best = min(cands, key=lambda t: float(np.hypot(*(t - self.target))))
            ok = np.hypot(*(best - self.target)) <= MATCH_RANGE
            self.target = best if ok else (cands[0] if len(cands) == 1 else None)
        else:
            self.target = cands[0] if len(cands) == 1 else None
        return self.target
