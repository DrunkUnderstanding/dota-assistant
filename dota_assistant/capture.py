"""截屏。Dota 要开「无边框窗口」，否则独占全屏下截到的可能是黑屏，叠加层也盖不上去。"""
from __future__ import annotations

import mss
import numpy as np


class Screen:
    def __init__(self, monitor: int = 1):
        self._mss = mss.mss()
        mons = self._mss.monitors
        self.monitor = mons[monitor] if monitor < len(mons) else mons[1]

    def grab(self) -> np.ndarray:
        """整块屏幕，BGR"""
        shot = self._mss.grab(self.monitor)
        return np.asarray(shot)[:, :, :3].copy()
