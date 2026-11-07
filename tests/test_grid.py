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


def screen(bans=(), taken=(), shift=0, dy=0, order=None) -> tuple[np.ndarray, dict]:
    """1920×1080 画面，网格 5 行 × 6 列，格子 41×71、间距 5（和真实 1080p 小格子一样大）；order = 英雄排列（默认按 id）"""
    H, W = 1080, 1920
    yy = np.linspace(0, 1, H)[:, None]
    img = np.zeros((H, W, 3), np.uint8)
    img[..., 0], img[..., 1], img[..., 2] = 75 + 20 * yy, 66 + 18 * yy, 54 + 15 * yy   # 蓝灰渐变背景
    where = {}
    for k, hid in enumerate(order or ARTS):
        r, c = divmod(k, 6)
        x, y = 200 + shift + c * 46, 220 + dy + r * 76
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

    def test_rearranged_grid_is_repaired(self):
        """格子位置不变、英雄换了位置（2026-11-06 实战：力丸 / 斯温 / 复仇之魂 轮换，禁了力丸报成斯温）：
        没变暗的两格按长相改过来；变暗的那格清空后图里只缺一个英雄，就是它。改过的图存盘、截图存下"""
        w = grid.BanWatcher(TMP / f"maps-{id(self)}-re.json", bank_factory=lambda: grid.GridBank(BANK_DIR), min_cells=20,
                            shot_dir=TMP / f"shots-{id(self)}")
        clean, _ = screen()
        w.add_map(w.build(clean, grid.find_cells(clean)))
        w.reset()
        order = list(ARTS)
        order[2], order[10], order[25] = order[10], order[25], order[2]
        img, _ = screen(bans=(order[2],), order=order)
        self.assertTrue(w.update(img))
        self.assertEqual(w.bans, {order[2]})
        self.assertEqual(grid.load_maps(w.path)[0].heroes[:30], order)
        self.assertTrue(list(w.shot_dir.glob("*-改图.png")))

    def test_other_layout_is_dropped(self):
        """格子位置碰巧对得上、英雄全不对（多半是别的布局）：扔掉这张图，这一帧不出结果"""
        w = grid.BanWatcher(TMP / f"maps-{id(self)}-x.json", bank_factory=lambda: grid.GridBank(BANK_DIR), min_cells=20)
        clean, _ = screen()
        w.add_map(w.build(clean, grid.find_cells(clean)))
        img, _ = screen(order=list(ARTS)[::-1])
        boxes = grid.find_cells(img)
        self.assertIsNone(w.update(img, boxes))
        self.assertTrue(w.needs_map(img, boxes))

    def test_hover_card_cells_filled_later(self):
        """建图那一帧鼠标停在英雄上，放大的卡片盖住 3×3 格（2026-11-07 殁境神蚀者）：盖住的格子先空着、
        盖着时不算禁用；卡片挪开后补认，之后禁了它报得出来"""
        w = grid.BanWatcher(TMP / f"maps-{id(self)}-hv.json", bank_factory=lambda: grid.GridBank(BANK_DIR), min_cells=20)
        img, where = screen(bans=(3,))
        card = cv2.resize(ARTS[14], (150, 230))
        x, y = where[14][0] - 50, where[14][1] - 76
        hover = img.copy()
        hover[y:y + 230, x:x + 150] = card
        hover[y + 150:y + 230, x + 10:x + 140, 2] = 160          # 卡片下半截偏红（名字底板），别当成红斜线
        boxes = grid.find_cells(img)
        gmap = w.build(hover, boxes)
        w.add_map(gmap)
        under = [i for i, b in enumerate(boxes) if x <= b[0] and b[0] + b[2] <= x + 150 and y <= b[1] and b[1] + b[3] <= y + 230]
        self.assertGreaterEqual(len(under), 4)
        self.assertTrue(all(gmap.heroes[i] is None for i in under))
        self.assertTrue(w.update(hover, boxes))
        self.assertEqual(w.bans, {3})                            # 卡片盖着的格子不算禁用

        w.update(img, boxes)                                     # 卡片挪开：补认
        self.assertNotIn(None, grid.load_maps(w.path)[0].heroes)
        self.assertTrue(w.update(screen(bans=(3, 14))[0], boxes))
        self.assertEqual(w.bans, {3, 14})

    def test_same_order_passes_check(self):
        w = grid.BanWatcher(TMP / f"maps-{id(self)}-ok.json", bank_factory=lambda: grid.GridBank(BANK_DIR), min_cells=20)
        clean, _ = screen()
        w.add_map(w.build(clean, grid.find_cells(clean)))
        w.reset()
        self.assertTrue(w.update(screen(bans=(4,), taken=(9,))[0]))
        self.assertEqual(w.bans, {4})

    def test_moved_grid_reuses_clean_map(self):
        """同一套网格整体挪了位置、画面里已有禁用：直接搬干净图的英雄过来，变暗的格子也认对"""
        w = grid.BanWatcher(TMP / f"maps-{id(self)}-mv.json", bank_factory=lambda: grid.GridBank(BANK_DIR), min_cells=20)
        clean, _ = screen()
        w.add_map(w.build(clean, grid.find_cells(clean)))
        img, where = screen(bans=(3, 17, 22, 29), taken=(9, 11), dy=-40)
        boxes = grid.find_cells(img)
        self.assertTrue(w.needs_map(img, boxes))
        gmap = w.build(img, boxes)
        self.assertTrue(gmap.clean)
        for hid, (x, y) in where.items():
            i = min(range(len(boxes)), key=lambda i: abs(boxes[i][0] - x) + abs(boxes[i][1] - y))
            self.assertEqual(gmap.heroes[i], hid)

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
