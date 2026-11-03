"""游戏画面里的镜头：认自己英雄的血条、从血条 + 英雄坐标反推镜头、世界坐标换算成屏幕像素。"""
import unittest
from pathlib import Path

import cv2
import numpy as np

from dota_assistant.vision import camera

DATA = Path(__file__).parent / "data"
SIZE = (1920, 1080)
# 2026-11-03 试玩里量的：镜头不动时英雄站在 GSI 坐标 (5270, -4709)，血条左上角在 (1562, 444)；那一刻镜头对着 (4479.6, -4676.7)
TARGET = (4479.55, -4676.7)
SAMPLES = [((1562, 444), (5270, -4709)), ((712, 764), (4290, -5087)), ((1292, 143), (5073, -4152)),
           ((1784, 555), (5457, -4860))]


def canvas(crop, at):
    """把血条截图贴回 1920×1080 的黑底上（认血条按画面高度换算大小）"""
    img = np.zeros((SIZE[1], SIZE[0], 3), np.uint8)
    x, y = at
    img[y:y + crop.shape[0], x:x + crop.shape[1]] = crop
    return img


class ProjectTest(unittest.TestCase):
    def test_center_and_round_trip(self):
        np.testing.assert_allclose(camera.project([TARGET], TARGET, SIZE)[0], (960, 540))
        px = camera.project([(5000, -4500)], TARGET, SIZE)[0]
        back = camera.unproject(px, 0, SIZE) + TARGET
        np.testing.assert_allclose(back, (5000, -4500), atol=1e-6)

    def test_far_is_smaller_and_scales_with_height(self):
        near = camera.project([(4200, -4900), (4800, -4900)], TARGET, SIZE)
        far = camera.project([(4200, -4300), (4800, -4300)], TARGET, SIZE)
        self.assertGreater(near[1][0] - near[0][0], far[1][0] - far[0][0])   # 远处（往北）同样宽在画面上更窄
        self.assertLess(far[0][1], near[0][1])                               # 往北 = 画面上面
        small = camera.project([(4800, -4900)], TARGET, (1280, 720))[0]
        np.testing.assert_allclose(small, camera.project([(4800, -4900)], TARGET, SIZE)[0] * 2 / 3, rtol=1e-9)

    def test_target_from_bar_matches_measurements(self):
        for bar, hero in SAMPLES:
            np.testing.assert_allclose(camera.target_from_bar(bar, hero, SIZE), TARGET, atol=3)


class BarsTest(unittest.TestCase):
    def setUp(self):
        self.crop = cv2.imread(str(DATA / "healthbars_own_and_ally.png"))   # 左边电脑队友、右边自己（截图 (1100, 400) 起）

    def test_only_own_hero(self):
        self.assertEqual(camera.find_bars(canvas(self.crop, (1100, 400))), [(1479, 445, 107)])

    def test_nothing_on_empty_frame(self):
        self.assertEqual(camera.find_bars(np.zeros((SIZE[1], SIZE[0], 3), np.uint8)), [])

    def test_tracker(self):
        t = camera.Tracker()
        img = canvas(self.crop, (1100, 400))
        first = t.update(img, (5000, -4500))
        self.assertIsNotNone(first)
        np.testing.assert_allclose(camera.project([(5000, -4500, camera.HEALTHBAR_Z)], first, SIZE)[0]
                                   + (camera.BAR_DX, camera.BAR_DY), (1479, 445), atol=0.5)
        self.assertIsNone(t.update(np.zeros_like(img), (5000, -4500)))      # 血条不在画面里：不知道镜头在哪


if __name__ == "__main__":
    unittest.main()
