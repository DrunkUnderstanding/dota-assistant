"""本局要点：强势期计算、猜位置、卡片结构。不联网。"""
import unittest

from dota_assistant import brief


def curve(total, wins_by_end):
    """wins_by_end = {结束分钟: (局数, 赢)} → 「打到第 t 分钟还没结束」的累计曲线"""
    n, w = [0] * 61, [0] * 61
    for end, (cnt, win) in wins_by_end.items():
        for t in range(min(end, 60) + 1):
            n[t] += cnt
            w[t] += win
    return {"n": n, "w": w}


class PhaseTest(unittest.TestCase):
    def test_late_hero(self):
        # 25 分钟结束的 1000 局赢 400，45 分钟结束的 1000 局赢 600：总胜率 50%，前期 -10，后期 +10
        c = curve(2000, {25: (1000, 400), 45: (1000, 600)})
        self.assertAlmostEqual(brief.phase_delta(c, 0, 30), -10)
        self.assertAlmostEqual(brief.phase_delta(c, 40, 61), 10)
        self.assertIsNone(brief.phase_delta(c, 30, 40))        # 这段没有局

    def test_team_average_skips_missing(self):
        c = {"1": curve(2000, {25: (1000, 400), 45: (1000, 600)})}
        self.assertAlmostEqual(brief.team_phases(c, [1, 999])[0], -10)


class GuessTest(unittest.TestCase):
    def test_unique_positions(self):
        positions = {1: {1: (900, 0), 3: (100, 0)}, 2: {1: (600, 0), 3: (400, 0)}}
        self.assertEqual(brief.guess_positions([1, 2], positions), {1: 1, 2: 3})   # 两个都爱打 1，乘积最大的分法
        self.assertEqual(brief.guess_positions([], positions), {})


class BriefTest(unittest.TestCase):
    def test_view_and_text(self):
        b = brief.Brief("本局要点")
        b.add("对线", [("warn", "难打")])
        b.add("空的", [])
        self.assertEqual([s["title"] for s in b.view()["sections"]], ["对线"])
        self.assertIn("⚠ 难打", b.text())


if __name__ == "__main__":
    unittest.main()
