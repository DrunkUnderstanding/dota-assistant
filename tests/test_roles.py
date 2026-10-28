"""自动认位置（roles.py）：OCR 结果 → 每格的位置、哪一格是我。不调 OCR，用假的识别结果。"""
import unittest

import numpy as np

from dota_assistant.vision import roles
from dota_assistant.vision.recognize import Calibration

W = roles.Word


def chars(line, text, x0, step=30, y=50):
    """中文 OCR 的样子：一个字一个词"""
    return [W(line, ch, x0 + i * step, y, 25, 25) for i, ch in enumerate(text)]


# 1920×1080、5 格从 x=214 开始、格宽 108（和 calibration.json 差不多）；OCR 坐标是放大 2 倍后的裁剪图
CAL = Calibration(radiant=[(0.1115 + i * 0.0646, 0.0074, 0.0563, 0.0583) for i in range(5)],
                  dire=[(0.5740 + i * 0.0646, 0.0074, 0.0563, 0.0583) for i in range(5)])
IMG = np.zeros((1080, 1920, 3), np.uint8)
CENTERS = [((r[0] + r[2] / 2) * 1920 - 0.1115 * 1920) * 2 for r in CAL.radiant]


class FindTest(unittest.TestCase):
    def test_roles_and_support_vs_hard_support(self):
        words = chars(1, "劣势路", CENTERS[0] - 40) + chars(1, "纯辅助", CENTERS[1] - 40) + chars(1, "中路", CENTERS[2] - 25) \
            + chars(1, "辅助", CENTERS[3] - 25) + chars(1, "优势路", CENTERS[4] - 40)
        team, me = roles.read_roles(IMG, CAL, "radiant", words=words)
        self.assertEqual(team, [3, 5, 2, 4, 1])
        self.assertIsNone(me)

    def test_me_by_name_even_with_ocr_typo(self):
        words = chars(0, "小猪碧囗", CENTERS[2] - 50, y=5) + chars(0, "醉后才知道", CENTERS[3] - 60, y=5) \
            + chars(1, "辅助", CENTERS[3] - 25)
        team, me = roles.read_roles(IMG, CAL, "radiant", my_name="醉后才知道", team_slot=0, words=words)
        self.assertEqual(me, 3)                  # 名字优先于 team_slot
        self.assertEqual(team[3], 4)

    def test_fallback_team_slot_and_no_labels(self):
        team, me = roles.read_roles(IMG, CAL, "radiant", my_name="别人", team_slot=2, words=chars(0, "Chen", 100))
        self.assertEqual((team, me), ([None] * 5, 2))   # 加速模式：没有位置字样

    def test_english_client(self):
        words = [W(1, "Safe", CENTERS[0] - 40, 50, 35, 20), W(1, "Lane", CENTERS[0], 50, 35, 20),
                 W(1, "Hard", CENTERS[1] - 40, 50, 35, 20), W(1, "Support", CENTERS[1], 50, 60, 20)]
        self.assertEqual(roles.read_roles(IMG, CAL, "radiant", words=words)[0][:2], [1, 5])


if __name__ == "__main__":
    unittest.main()
