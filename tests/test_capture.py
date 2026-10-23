import unittest

import numpy as np

from dota_assistant.capture import Screen, WindowGrabber


class FakeWindow:
    def __init__(self, img):
        self.img = img

    def grab(self):
        return self.img


class CaptureTest(unittest.TestCase):
    def test_prefers_dota_window(self):
        img = np.full((90, 160, 3), 7, np.uint8)
        s = Screen(window=FakeWindow(img))
        self.assertIs(s.grab(), img)
        self.assertEqual(s.source, "window")

    def test_falls_back_to_screen(self):
        s = Screen(window=FakeWindow(None))
        shot = s.grab()
        self.assertEqual(shot.ndim, 3)
        self.assertEqual(shot.shape[2], 3)
        self.assertEqual(s.source, "screen")

    def test_missing_window_returns_none(self):
        self.assertIsNone(WindowGrabber("没有这个窗口-测试用").grab())


if __name__ == "__main__":
    unittest.main()
