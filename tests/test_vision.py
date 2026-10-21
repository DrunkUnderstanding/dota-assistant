"""识别回归：拿官方头像拼一张假的选人截图（缩放 + 模糊 + 变暗 + JPEG），确认能认回来。
需要先 update 下载过头像；没有就跳过。真实截图上的准确率要在游戏机上用 recognize / label 验证。"""
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from dota_assistant.config import DATA_DIR
from dota_assistant.vision import Calibration, TemplateBank, crop, interpolate, read_image, recognize

PORTRAITS = DATA_DIR / "portraits"
W, H = 1920, 1080
SLOT_W, SLOT_H = 96, 54


def degrade(img: np.ndarray) -> np.ndarray:
    img = cv2.resize(img, (SLOT_W, SLOT_H), interpolation=cv2.INTER_AREA)
    img = cv2.GaussianBlur(img, (3, 3), 0)
    img = np.clip(img.astype(np.float32) * 0.8 + 10, 0, 255).astype(np.uint8)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


@unittest.skipUnless(PORTRAITS.exists() and any(PORTRAITS.glob("*.png")), "没有头像，先 update")
class VisionTest(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(0)
        self.canvas = rng.integers(15, 35, (H, W, 3), dtype=np.uint8)
        px = lambda x, y: (x / W, y / H, SLOT_W / W, SLOT_H / H)
        self.cal = Calibration(interpolate(px(300, 20), px(740, 20), 5),
                               interpolate(px(1084, 20), px(1524, 20), 5))
        self.ids = sorted(int(p.stem) for p in PORTRAITS.glob("*.png"))
        picks = [self.ids[i] for i in (0, 17, 40, 63, 90, 101, 120)]
        # 天辉选了 4 个（第 5 格空），夜魇选了 3 个（后两格空）
        self.expect = {"radiant": picks[:4] + [None], "dire": picks[4:] + [None, None]}
        # 空位：游戏里是固定的占位图，这里用同一张暗色剪影 + 每格不同的噪声模拟
        placeholder = np.full((SLOT_H, SLOT_W, 3), 30, np.uint8)
        cv2.circle(placeholder, (SLOT_W // 2, SLOT_H // 2), 16, (70, 70, 70), -1)
        for side in ("radiant", "dire"):
            for r, hid in zip(getattr(self.cal, side), self.expect[side]):
                x, y = int(round(r[0] * W)), int(round(r[1] * H))
                if hid:
                    tile = degrade(read_image(PORTRAITS / f"{hid}.png"))
                else:
                    tile = np.clip(placeholder + rng.integers(0, 8, placeholder.shape), 0, 255).astype(np.uint8)
                self.canvas[y:y + SLOT_H, x:x + SLOT_W] = tile
        self.tmp = tempfile.TemporaryDirectory()
        self.bank = TemplateBank(template_dir=Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_recognizes_picks_and_empty_slots(self):
        view = recognize(self.canvas, self.cal, self.bank, threshold=0.70)
        for side in ("radiant", "dire"):
            self.assertEqual([s.hero_id for s in getattr(view, side)], self.expect[side], side)

    def test_learned_empty_template(self):
        empty = crop(self.canvas, self.cal.dire[4])
        self.bank.learn(0, empty)
        hid, score = self.bank.match(crop(self.canvas, self.cal.dire[3]))
        self.assertEqual(hid, 0)
        self.assertGreater(score, 0.9)
        self.assertEqual(TemplateBank(template_dir=Path(self.tmp.name)).learned, 1)   # 落盘了


if __name__ == "__main__":
    unittest.main()
