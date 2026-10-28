"""识别回归：拿官方头像拼一张假的选人截图（缩放 + 模糊 + 变暗 + JPEG），确认能认回来。
需要先 update 下载过头像；没有就跳过。真实截图上的准确率要在游戏机上用 recognize / label 验证。"""
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from dota_assistant.core.config import DATA_DIR
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

    def test_noise_on_portrait(self):
        # 顶栏头像上盖的东西（vision.VIEWS）：右下角英雄徽章、天梯段位条（半透明红底白字）、底下颜色条；
        # 空位上也盖着段位旗 + 段位条，不能因为挖掉噪点就被认成谁
        rng = np.random.default_rng(1)
        hid = self.ids[55]
        def noisy(tile, badge=True):
            t = tile.copy()
            band = slice(int(SLOT_H * 0.60), int(SLOT_H * 0.88))
            t[band] = (t[band] * 0.45 + np.array([40, 40, 120]) * 0.55).astype(np.uint8)
            cv2.putText(t, "IIII", (int(SLOT_W * 0.3), int(SLOT_H * 0.84)), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (230, 230, 230), 1)
            t[int(SLOT_H * 0.91):] = (200, 120, 40)
            if badge:
                pts = np.array([[0.72, 0.40], [0.97, 0.40], [0.845, 0.85]]) * [SLOT_W, SLOT_H]
                cv2.fillPoly(t, [pts.astype(np.int32)], (60, 110, 200))
                cv2.putText(t, "5", (int(SLOT_W * 0.81), int(SLOT_H * 0.68)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
            return t
        picked = noisy(degrade(read_image(PORTRAITS / f"{hid}.png")))
        empty = np.full((SLOT_H, SLOT_W, 3), 52, np.uint8)
        cv2.rectangle(empty, (int(SLOT_W * 0.4), int(SLOT_H * 0.25)), (int(SLOT_W * 0.6), int(SLOT_H * 0.5)), (120, 150, 190), -1)
        empty = noisy(np.clip(empty + rng.integers(0, 6, empty.shape), 0, 255).astype(np.uint8), badge=False)
        for r, tile in zip(self.cal.dire[3:], (picked, empty)):
            x, y = int(round(r[0] * W)), int(round(r[1] * H))
            self.canvas[y:y + SLOT_H, x:x + SLOT_W] = tile
        view = recognize(self.canvas, self.cal, self.bank, threshold=0.70)
        self.assertEqual(view.dire[3].hero_id, hid)
        self.assertGreater(view.dire[3].score, 0.9)                     # 噪点挖掉了：和官方头像几乎一样（整张比只有 0.6 上下）
        self.assertGreaterEqual(view.dire[3].gap, 0.15)
        self.assertIsNone(view.dire[4].hero_id)


SHOTS = Path(__file__).resolve().parent.parent / "shots"
REAL = {"20261023-014347": "加速选人第 26 秒：拉比克盖着徽章「5」，天辉第 1、2 格是队友预选的灰头像",
        "20261022-231827": "天梯选人：斯拉克上盖着「超凡入圣III」段位条，其余是空位（也盖着段位旗 + 段位条）",
        "20261023-004917": "天梯选人：10 格全空，都盖着段位旗 + 段位条"}


@unittest.skipUnless(PORTRAITS.exists() and all((SHOTS / f"{k}.png").exists() for k in REAL), "没有真实截图（shots/ 不进 git）")
class RealShotTest(unittest.TestCase):
    """真实选人截图（只在有 shots/ 的电脑上跑）：盖着噪点的认得出，空位 / 预选不乱认"""

    def test_real_draft_noise(self):
        from dota_assistant import data
        cal = Calibration.load()
        if not (cal and cal.ready):
            self.skipTest("没校准")
        key = {h.key: h.id for h in data.load_heroes()}
        bank = TemplateBank(template_dir=Path(tempfile.mkdtemp()))    # 只用官方头像，不受本机学过的模板影响
        expect = {"20261023-014347": [None] * 8 + [key["rubick"], None],
                  "20261022-231827": [None, None, None, key["slark"]] + [None] * 6,
                  "20261023-004917": [None] * 10}
        for f, want in expect.items():
            view = recognize(read_image(SHOTS / f"{f}.png"), cal, bank, threshold=0.70)
            got = [s.hero_id for s in view.radiant + view.dire]
            # 2026-10-22 那张天辉前两格被我们自己的叠加层挡着，不算
            if f == "20261022-231827":
                got[:2] = [None, None]
            self.assertEqual(got, want, f"{f}：{REAL[f]}")


if __name__ == "__main__":
    unittest.main()
