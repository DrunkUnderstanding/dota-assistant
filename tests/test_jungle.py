"""野区计时提醒：谁提醒什么、哪几秒亮。"""
import unittest

from dota_assistant import jungle


def bench(stacks10):
    return {"per_min": {"10": {"cs": 40, "networth": 4000, "stacks": stacks10}}}


def at(m, s):
    return m * 60 + s


class MakeTest(unittest.TestCase):
    def test_kinds_by_position(self):
        self.assertEqual(jungle.make(5, None).kinds, {"block", "pull", "stack", "lotus"})
        self.assertEqual(jungle.make(4, None).kinds, {"block", "stack", "lotus"})   # 没同分段数据，辅助照样提醒
        self.assertEqual(jungle.make(3, None).kinds, {"lotus"})
        self.assertEqual(jungle.make(1, None).kinds, set())

    def test_core_stackers_from_data(self):
        self.assertEqual(jungle.make(2, bench(1.5)).kinds, {"stack"})        # 炼金 / 米波这类中单（中单不抢莲花）
        self.assertEqual(jungle.make(2, bench(0.2)).kinds, set())
        self.assertEqual(jungle.make(1, {"per_min": {}}).kinds, set())


class CueTest(unittest.TestCase):
    def test_block_before_first_spawn(self):
        p = jungle.make(4, None)
        self.assertIsNone(p.cue(-61))
        self.assertEqual(p.cue(-60).kind, "block")
        self.assertIn("对面优势路拉野点", p.cue(30).lines[0][1])
        self.assertIsNone(p.cue(60))
        self.assertIn("自家拉野点", jungle.make(5, None).cue(0).lines[0][1])

    def test_stack_window(self):
        p = jungle.make(4, None)
        self.assertIsNone(p.cue(at(1, 44)))
        c = p.cue(at(1, 45))
        self.assertEqual((c.key, c.title), ("stack:1", "屯野 · 1:53"))
        self.assertEqual(p.cue(at(1, 56)).key, "stack:1")
        self.assertIsNone(p.cue(at(1, 57)))
        self.assertEqual(p.cue(at(4, 50)).key, "stack:4")
        self.assertEqual(len(p.cue(at(4, 50)).lines), 1)                     # 第一张之后只留一行
        self.assertIsNone(p.cue(at(jungle.STACK_UNTIL, 50)))                # 过了对线期不提醒
        self.assertIsNone(p.cue(at(0, 50), muted={"block"}))                 # 0:53 还没刷过野，屯不了

    def test_pull_and_double_pull_for_five(self):
        p = jungle.make(5, None)
        c = p.cue(at(1, 10))
        self.assertEqual((c.kind, c.title), ("pull", "拉野 · 1:15"))
        self.assertTrue(any("双拉" in t for _, t in c.lines))
        self.assertEqual(p.cue(at(3, 15)).key, "pull:3")
        self.assertIsNone(jungle.make(4, None).cue(at(3, 15)))              # 四号位不拉
        self.assertIsNone(p.cue(at(jungle.PULL_UNTIL, 15)))

    def test_lotus_at_3_and_6(self):
        p = jungle.make(4, None)
        self.assertIsNone(p.cue(at(2, 44)))                                  # 莲花提前 15 秒，屯野提前 8 秒
        c = p.cue(at(2, 45))                                                 # 和 2:53 的屯野同时亮：两个都写，按对线选
        self.assertEqual((c.kind, c.title), ("lotus", "屯野 2:53 / 抢莲花 3:00"))
        self.assertTrue(c.lines[0][1].startswith("屯野：2:53"))
        self.assertIn("你们一塔旁边", c.lines[1][1])
        self.assertIn("优势劣势", c.lines[2][1])
        self.assertEqual(p.cue(at(2, 57)).key, "lotus:3")                   # 屯野时间过了：只剩莲花
        self.assertEqual(p.cue(at(3, 5)).key, "lotus:3")
        self.assertIsNone(p.cue(at(3, 6)))
        self.assertEqual(p.cue(at(6, 0)).key, "lotus:6")
        self.assertIsNone(p.cue(at(9, 0)))                                   # 只提醒 3、6 分钟
        self.assertIn("对面一塔旁边", jungle.make(5, None).cue(at(3, 0)).lines[0][1])
        self.assertEqual(jungle.make(3, None).cue(at(5, 50)).title, "抢莲花 · 6:00")   # 三号位不屯野：只有莲花
        self.assertEqual(p.cue(at(2, 50), muted={"lotus"}).key, "stack:2")  # 关掉莲花，屯野照常

    def test_muted_and_custom_seconds(self):
        p = jungle.make(5, None, stack_second=54, pull_second=17)
        self.assertEqual(p.cue(at(4, 50)).title, "屯野 · 4:54")
        self.assertEqual(p.cue(at(4, 12)).title, "拉野 · 4:17")
        self.assertIsNone(p.cue(at(4, 50), muted={"stack"}))
        self.assertEqual(p.cue(at(4, 12), muted={"stack"}).kind, "pull")

    def test_core_stack_card_says_why(self):
        c = jungle.make(2, bench(1.5)).cue(at(1, 50))
        self.assertIn("平均屯 1.5 组", c.lines[1][1])


if __name__ == "__main__":
    unittest.main()
