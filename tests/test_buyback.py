"""买活计时：点一下开始 / 再点取消、冷却结束后亮一会儿「可买活」、自己的按 GSI 同步；
截图自动记：顶栏头像变灰 → 认复活倒计时 → 提前变回彩色 = 买活（合成的顶栏图，OCR 换成假的）。"""
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from dota_assistant import buyback
from dota_assistant import buybackscan as bs
from dota_assistant.buyback import COOLDOWN, READY_SHOW, Tracker


def topbar(dead=(), timers=None, size=(1080, 1920)) -> np.ndarray:
    """合成一帧：10 个彩色头像（位置同 buybackscan），dead 里的变灰、下面写倒计时 timers[(阵营, 格)]"""
    h, w = size
    s = h / 1080
    img = np.full((h, w, 3), 40, np.uint8)
    for side in bs.SIDES:
        for i in range(5):
            cx = int(w / 2 + (bs.CENTER0[side] + bs.PITCH * i) * s)
            x0, x1, y0, y1 = cx - int(28 * s), cx + int(28 * s), int(4 * s), int(40 * s)
            hue = (i * 30 + (0 if side == "radiant" else 15)) % 180
            img[y0:y1, x0:x1] = cv2.cvtColor(np.uint8([[[hue, 200, 180]]]), cv2.COLOR_HSV2BGR)[0, 0]
            if (side, i) in dead:
                roi = img[y0:y1, x0:x1]
                img[y0:y1, x0:x1] = cv2.cvtColor(cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
                t = (timers or {}).get((side, i))
                if t is not None:
                    cv2.rectangle(img, (cx - int(18 * s), int(44 * s)), (cx + int(18 * s), int(64 * s)), (20, 20, 20), -1)
                    cv2.putText(img, str(t), (cx - int(14 * s), int(61 * s)), cv2.FONT_HERSHEY_SIMPLEX, 0.6 * s,
                                (255, 255, 255), max(1, int(2 * s)))
    return img


class TrackerTest(unittest.TestCase):
    def test_mark_then_cancel(self):
        t = Tracker()
        self.assertTrue(t.mark("dire", 2, 1500))
        self.assertEqual(t.end("dire", 2, 1600), 1500 + COOLDOWN)
        self.assertFalse(t.mark("dire", 2, 1510))              # 点错了：取消
        self.assertIsNone(t.end("dire", 2, 1510))

    def test_ready_then_idle(self):
        t = Tracker()
        t.mark("radiant", 0, 1000)
        end = 1000 + COOLDOWN
        self.assertEqual(t.end("radiant", 0, end + READY_SHOW - 1), end)   # 冷却好了：还亮着「可买活」
        self.assertIsNone(t.end("radiant", 0, end + READY_SHOW))           # 过一会儿变回空条
        self.assertTrue(t.mark("radiant", 0, end + 5))                     # 冷却好了又买活：重新计时，不是取消

    def test_self_synced_from_gsi(self):
        t = Tracker()
        t.sync_self("dire", 3, 2000, 470)
        self.assertEqual(t.end("dire", 3, 2000), 2470)
        self.assertEqual(t.auto, {("dire", 3)})
        t.sync_self("dire", 3, 2001, 470)                     # 推送晚到 1 秒：差得不多不改
        self.assertEqual(t.end("dire", 3, 2001), 2470)
        t.sync_self("dire", 3, 2010, 300)                     # 差得多：按 GSI 改
        self.assertEqual(t.end("dire", 3, 2010), 2310)
        t.sync_self("dire", 3, 2020, 0)                       # GSI 说没在冷却
        self.assertIsNone(t.end("dire", 3, 2020))
        t.reset()
        self.assertEqual((t.ends, t.auto), ({}, set()))

    def test_cooldown_is_eight_minutes(self):
        self.assertEqual(buyback.COOLDOWN, 480)


class WatcherTest(unittest.TestCase):
    def test_saturation_alive_vs_dead(self):
        img = topbar(dead={("dire", 2)})
        self.assertGreater(bs.saturation(img, "radiant", 0), bs.DEAD_SAT)
        self.assertLess(bs.saturation(img, "dire", 2), bs.DEAD_SAT)

    def run_life(self, revive_at: float, timer: int | None = 40, size=(1080, 1920)):
        """夜魇第 3 个在 100 秒死、倒计时 timer 秒，revive_at 秒变回彩色；OCR 当认到 timer"""
        w = bs.Watcher()
        d, r = w.update(topbar(size=size), 99)
        self.assertEqual((d, r), ([], []))
        dead = {("dire", 2)}
        self.assertEqual(w.update(topbar(dead, size=size), 100), ([], []))       # 灰一帧不算（躲特效）
        deaths, _ = w.update(topbar(dead, {("dire", 2): timer}, size), 101)
        self.assertEqual([(x.side, x.index) for x in deaths], [("dire", 2)])
        w.set_timer(deaths[0], timer)
        deaths, _ = w.update(topbar(dead, {("dire", 2): timer - 1}, size), 102)   # 第二次认到的对得上才算
        w.set_timer(deaths[0], timer - 1)
        for t in range(103, int(revive_at)):
            self.assertEqual(w.update(topbar(dead, size=size), t), ([], []))     # 认出来了就不再送 OCR
        _, revs = w.update(topbar(size=size), revive_at)
        self.assertEqual(len(revs), 1)
        return revs[0]

    def test_buyback_vs_respawn(self):
        rev = self.run_life(115)                                   # 101 + 40 = 141 才该复活，115 就活了
        self.assertTrue(rev.buyback)
        self.assertEqual((rev.expected, rev.seconds, rev.dead_for), (141, 39, 15))   # 记的是第二次认到的
        self.assertFalse(self.run_life(141).buyback)               # 倒计时走完复活
        self.assertFalse(self.run_life(139).buyback)               # 每秒一帧的误差内
        self.assertTrue(self.run_life(115, size=(2160, 3840)).buyback)   # 4K：位置按图高缩放

    def test_unreadable_timer_not_recorded(self):
        w = bs.Watcher()
        dead = {("radiant", 4)}
        w.update(topbar(dead), 10)
        sent = [len(w.update(topbar(dead), t)[0]) for t in range(11, 18)]
        self.assertEqual(sent, [1] * bs.OCR_TRIES + [0] * (7 - bs.OCR_TRIES))   # 认不出最多送 OCR_TRIES 次
        _, revs = w.update(topbar(), 18)
        self.assertIsNone(revs[0].buyback)                         # 不知道是不是买活：不自动记

    def test_misread_needs_agreement(self):
        """10 认成 1：和下一帧认到的 9 对不上，不算；再下一帧认到 8 和 9 对上了才算"""
        w = bs.Watcher()
        dead = {("dire", 0)}
        w.update(topbar(dead), 10)
        for clock, sec in ((11, 1), (12, 9)):
            w.set_timer(w.update(topbar(dead), clock)[0][0], sec)
        self.assertIsNone(w.slots[("dire", 0)].expected)
        w.set_timer(w.update(topbar(dead), 13)[0][0], 8)
        self.assertEqual(w.slots[("dire", 0)].expected, 21)

    def test_stale_ocr_result_ignored(self):
        w = bs.Watcher()
        dead = {("radiant", 0)}
        w.update(topbar(dead), 10)
        d1 = w.update(topbar(dead), 11)[0][0]
        w.update(topbar(), 12)                                     # 复活了，OCR 结果才回来
        w.set_timer(d1, 30)
        self.assertEqual(w.slots[("radiant", 0)].expected, None)

    def test_skip_self_and_tiny_frames(self):
        w = bs.Watcher()
        dead = {("radiant", 1)}
        w.update(topbar(dead), 10)
        self.assertEqual(w.update(topbar(dead), 11, skip={("radiant", 1)}), ([], []))
        self.assertEqual(w.update(np.zeros((9, 16, 3), np.uint8), 12), ([], []))

    def test_parse_seconds(self):
        self.assertEqual(bs.parse_seconds(["37", "37", "3", "37"]), 37)
        self.assertEqual(bs.parse_seconds(["1O0", "100"]), 100)
        self.assertIsNone(bs.parse_seconds(["", "买活"]))

    def test_sample_saved(self):
        rev = self.run_life(115)
        d = Path(tempfile.mkdtemp(prefix="dota-bb-"))
        path = bs.save_sample(rev, "buyback-40", d)
        self.assertTrue(path.name.endswith("-dire3-buyback-40.png") and path.exists())


class TrackerStartTest(unittest.TestCase):
    def test_auto_start_does_not_cancel_manual(self):
        t = Tracker()
        t.mark("dire", 1, 100)                                     # 玩家先点了
        self.assertFalse(t.start("dire", 1, 103))                  # 截图认出来的不覆盖、也不取消
        self.assertEqual(t.end("dire", 1, 200), 100 + COOLDOWN)
        self.assertTrue(t.start("radiant", 0, 50))


if __name__ == "__main__":
    unittest.main()
