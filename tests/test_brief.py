"""本局要点：强势期计算、猜位置、卡片结构。不联网。"""
import unittest

from dota_assistant import brief, gameplan


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
        self.assertEqual([s["title"] for s in b.view()["sections"]], ["对线", "怎么打 · AI"])   # 空的小节不显示
        self.assertIn("⚠ 难打", b.text())

    def test_stage_pages(self):
        # 数据按小节分到各阶段，AI 打法按段落分；没开 AI / 还在写时每页都有一句说明
        b = brief.Brief("本局要点", plan_note="AI 正在写…")
        b.add("对线", [("row", "对面同路：斧王")])
        b.add("出装", [("row", "相位鞋")])
        b.add("强势期", [("row", "我方越拖越强")])
        titles = lambda page: [s["title"] for s in b.view(page)["sections"]]
        self.assertEqual(titles("lane"), ["对线", "怎么打 · AI"])
        self.assertEqual(titles("mid"), ["出装", "强势期", "怎么打 · AI"])
        self.assertEqual(titles("late"), ["怎么打 · AI"])
        self.assertTrue(b.has("mid") and not b.has("late"))     # 没有后期出装、AI 也还没写好：不算有内容

        Tip = gameplan.Tip
        b.plan = gameplan.Plan([("对线期（0~10 分钟）", [Tip("先补刀", "依据", "原文")]),
                                ("后期（30 分钟后）", [Tip("分推", "依据", "原文")])])
        self.assertEqual(titles("lane"), ["对线", "怎么打 · AI"])
        self.assertEqual(titles("mid"), ["出装", "强势期"])      # AI 中期没给：不再提「正在写」
        self.assertEqual(b.view("late")["sections"][0]["lines"][0]["text"], "分推")
        self.assertTrue(b.has("late"))
        self.assertIn("未验证", b.view("late")["hint"])
        self.assertEqual([t["on"] for t in b.view("mid")["tabs"]], [False, True, False])
        b.add("后期出装", [("row", "蝴蝶 32′")])
        self.assertEqual(titles("late"), ["后期出装", "怎么打 · AI"])

    def test_fold_on_card(self):
        # 卡片上每小节只显示前几条，其余折叠（more，界面上点「展开」）；文字版（给 AI 的资料）还是全文
        b = brief.Brief("本局要点")
        b.add("对线", [("dim", "分路：…"), ("row", "对上斧王对线 …"), ("warn", "提示1"), ("row", "提示2"), ("row", "提示3")],
              short=([("warn", "你这路对面：斧王（3号位·难打）"), ("warn", "提示1"), ("row", "提示2")],
                     [("dim", "分路：…"), ("row", "对上斧王对线 …"), ("row", "提示3")]))
        b.add("对面要防", [("row", f"防{i}") for i in range(4)])
        Tip = gameplan.Tip
        b.plan = gameplan.Plan([("对线劣势时", [Tip(f"劣势{i}", "依据", "原文") for i in range(3)])])
        secs = {s["title"]: s for s in b.view("lane")["sections"]}
        texts = lambda t, k="lines": [x["text"] for x in secs[t].get(k, [])]
        self.assertEqual(texts("对线"), ["你这路对面：斧王（3号位·难打）", "提示1", "提示2"])
        self.assertEqual(texts("对线", "more"), ["分路：…", "对上斧王对线 …", "提示3"])
        self.assertEqual((texts("对面要防"), texts("对面要防", "more")), (["防0", "防1"], ["防2", "防3"]))
        self.assertEqual((texts("对线劣势时 · AI"), texts("对线劣势时 · AI", "more")), (["劣势0", "劣势1"], ["劣势2"]))
        self.assertEqual(secs["对线劣势时 · AI"]["more"][0]["note"], "依据")     # 折叠的也带依据
        b.add("起手", [("row", "树枝")])
        self.assertNotIn("more", {s["title"]: s for s in b.view("lane")["sections"]}["起手"])   # 没有多的就不出「展开」
        self.assertIn("提示3", b.text())
        self.assertNotIn("你这路对面：斧王（3号位·难打）", b.text())

    def test_stage_at(self):
        self.assertEqual([brief.stage_at(m) for m in (-1, 0, 9.9, 10, 29.9, 30)],
                         ["lane", "lane", "lane", "mid", "mid", "late"])

    def test_line_quote(self):
        # 对线提示带官方原句：界面悬停显示，文字版不带
        b = brief.Brief("本局要点")
        b.add("对线", [("warn", "小心莱恩的裂地尖刺", "莱恩·裂地尖刺：……被眩晕"), ("row", "对面同路：斧王")])
        lines = b.view()["sections"][0]["lines"]
        self.assertEqual(lines[0]["quote"], "莱恩·裂地尖刺：……被眩晕")
        self.assertNotIn("quote", lines[1])
        self.assertNotIn("被眩晕", b.text())


if __name__ == "__main__":
    unittest.main()
