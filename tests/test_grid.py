"""选人网格认禁用（grid.py）+ 游戏资源解码（gamefiles.py）。用合成的网格画面，不需要真实截图和游戏文件。"""
import struct
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from dota_assistant.vision import gamefiles, grid

TMP = Path(tempfile.mkdtemp())
RNG = np.random.default_rng(7)
N_HEROES = 30


def hero_art(i: int) -> np.ndarray:
    """假的英雄图（142×188）：平滑的随机色块，每个英雄不一样"""
    small = RNG.integers(0, 255, (6, 5, 3), dtype=np.uint8)
    art = cv2.resize(small, (142, 188), interpolation=cv2.INTER_CUBIC).astype(np.int16)
    art += RNG.integers(-40, 40, (47, 36, 3)).repeat(4, 0).repeat(4, 1)[:188, :142]   # 真图有细节纹理
    return np.clip(art, 0, 255).astype(np.uint8)


ARTS = {i + 1: hero_art(i) for i in range(N_HEROES)}
BANK_DIR = TMP / "bank"
BANK_DIR.mkdir()
for hid, art in ARTS.items():
    cv2.imwrite(str(BANK_DIR / f"{hid}.png"), art)


def dim(cell: np.ndarray, slash: bool) -> np.ndarray:
    """游戏里的样子：蒙一层灰蓝；禁用的再压一道右上 → 左下的红斜线"""
    out = (cell.astype(np.float32) * 0.25 + np.array([70, 62, 58]) * 0.75).astype(np.uint8)
    if slash:
        h, w = out.shape[:2]
        cv2.line(out, (int(w * 0.9), 2), (int(w * 0.1), h - 3), (95, 90, 150), 3)
    return out


def screen(bans=(), taken=(), shift=0) -> tuple[np.ndarray, dict]:
    """1920×1080 画面，网格 5 行 × 6 列，格子 41×71、间距 5（和真实 1080p 小格子一样大）"""
    H, W = 1080, 1920
    yy = np.linspace(0, 1, H)[:, None]
    img = np.zeros((H, W, 3), np.uint8)
    img[..., 0], img[..., 1], img[..., 2] = 75 + 20 * yy, 66 + 18 * yy, 54 + 15 * yy   # 蓝灰渐变背景
    where = {}
    for k, hid in enumerate(ARTS):
        r, c = divmod(k, 6)
        x, y = 200 + shift + c * 46, 220 + r * 76
        art = cv2.resize(ARTS[hid], (int(41 * 1.23), int(41 * 1.23 * 188 / 142)), interpolation=cv2.INTER_AREA)
        ox = (art.shape[1] - 41) // 2
        cell = np.vstack([art[:, ox:ox + 41], np.full((71 - art.shape[0], 41, 3), 25, np.uint8)])   # 底下一条放等级图标
        if hid in bans or hid in taken:
            cell = dim(cell, hid in bans)
        img[y:y + 71, x:x + 41] = cell
        where[hid] = (x, y)
    return img, where


class FindCellsTest(unittest.TestCase):
    def test_finds_every_cell_including_dimmed(self):
        img, where = screen(bans=(3, 17), taken=(9,))
        boxes = grid.find_cells(img)
        self.assertEqual(len(boxes), N_HEROES)
        found = {(x, y) for x, y, _, _ in boxes}
        for x, y in where.values():
            self.assertTrue(any(abs(x - a) <= 2 and abs(y - b) <= 2 for a, b in found), (x, y))


class GridMapTest(unittest.TestCase):
    def setUp(self):
        self.bank = grid.GridBank(BANK_DIR)

    def test_clean_frame_identifies_all(self):
        img, where = screen()
        gmap = grid.GridMap.build(img, self.bank)
        self.assertTrue(gmap.clean)
        for hid, (x, y) in where.items():
            i = min(range(len(gmap.cells)), key=lambda i: abs(gmap.boxes()[i][0] - x) + abs(gmap.boxes()[i][1] - y))
            self.assertEqual(gmap.heroes[i], hid)

    def test_states_separate_ban_and_taken(self):
        gmap = grid.GridMap.build(screen()[0], self.bank)
        img, _ = screen(bans=(3, 17), taken=(9,))
        st = {s.hero_id: s for s in grid.states(img, gmap)}
        self.assertEqual({h for h, s in st.items() if s.banned}, {3, 17})
        self.assertEqual({h for h, s in st.items() if s.taken and not s.banned}, {9})

    def test_layout_match(self):
        gmap = grid.GridMap.build(screen()[0], self.bank)
        img, _ = screen(bans=(3,))
        self.assertTrue(gmap.matches(img, grid.find_cells(img)))
        moved, _ = screen(shift=300)
        self.assertFalse(gmap.matches(moved, grid.find_cells(moved)))


class BanWatcherTest(unittest.TestCase):
    def test_learn_layout_then_accumulate_bans(self):
        w = grid.BanWatcher(TMP / f"maps-{id(self)}.json", bank_factory=lambda: grid.GridBank(BANK_DIR), min_cells=20)
        clean, _ = screen()
        boxes = grid.find_cells(clean)
        self.assertIsNone(w.update(clean, boxes))           # 没见过的布局：先不出结果
        self.assertTrue(w.needs_map(clean, boxes))
        w.add_map(w.build(clean, boxes))
        self.assertFalse(w.needs_map(clean, boxes))

        self.assertTrue(w.update(screen(bans=(5,))[0]))
        self.assertTrue(w.update(screen(bans=(5, 21), taken=(2,))[0]))
        self.assertFalse(w.update(screen(bans=(5, 21), taken=(2,))[0]))
        self.assertEqual(w.bans, {5, 21})
        self.assertEqual(w.taken, {2})
        w.reset()
        self.assertEqual(w.bans, set())

        again = grid.BanWatcher(w.path, bank_factory=lambda: grid.GridBank(BANK_DIR), min_cells=20)   # 布局存盘，重开助手还认得
        self.assertTrue(again.update(screen(bans=(7,))[0]))
        self.assertEqual(again.bans, {7})

    def test_not_a_grid(self):
        w = grid.BanWatcher(TMP / "none.json", bank_factory=lambda: grid.GridBank(BANK_DIR), min_cells=20)
        blank = np.full((1080, 1920, 3), 30, np.uint8)
        self.assertIsNone(w.update(blank))
        self.assertFalse(w.needs_map(blank, grid.find_cells(blank)))


class DecodeTest(unittest.TestCase):
    def test_dxt5_single_block(self):
        # alpha 端点 255/0，全用索引 0；颜色端点纯红 / 纯蓝，全用索引 1（= 蓝）
        block = bytes([255, 0]) + bytes(6) + struct.pack("<HH", 0xF800, 0x001F) + bytes([0x55] * 4)
        px = gamefiles.dxt5(block, 4, 4)
        self.assertEqual(px.shape, (4, 4, 4))
        self.assertEqual(tuple(px[0, 0]), (0, 0, 255, 255))     # RGBA：蓝色、不透明

    def test_ycocg_gray(self):
        # Co = Cg = 128（无色），缩放 0 → 1，Y = 200 → 灰 200
        rgba = np.array([[[128, 128, 0, 200]]], np.uint8)
        self.assertEqual(tuple(gamefiles.ycocg_to_bgr(rgba)[0, 0]), (200, 200, 200))

    def test_embedded_png(self):
        img = np.full((8, 6, 3), (10, 20, 30), np.uint8)
        blob = b"RED2-header-junk" + cv2.imencode(".png", img)[1].tobytes()
        self.assertTrue(np.array_equal(gamefiles.decode_vtex(blob), img))


if __name__ == "__main__":
    unittest.main()
