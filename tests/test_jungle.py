"""野区计时提醒：谁提醒什么、哪几秒亮。"""
import unittest

from dota_assistant.domain import jungle


def bench(stacks10):
    return {"per_min": {"10": {"cs": 40, "networth": 4000, "stacks": stacks10}}}


def at(m, s):
    return m * 60 + s


class MakeTest(unittest.TestCase):
    def test_kinds_by_position(self):
        self.assertEqual(jungle.make(5, None).kinds, {"block", "pull", "stack"})
        self.assertEqual(jungle.make(4, None).kinds, {"block", "stack"})   # 没同分段数据，辅助照样提醒
        self.assertEqual(jungle.make(3, None).kinds, set())                # 抢莲花不提醒了（玩家 2026-11-03）
        self.assertEqual(jungle.make(1, None).kinds, set())

    def test_core_stackers_from_data(self):
        self.assertEqual(jungle.make(2, bench(1.5)).kinds, {"stack"})        # 炼金 / 米波这类中单
        self.assertEqual(jungle.make(2, bench(0.2)).kinds, set())
        self.assertEqual(jungle.make(1, {"per_min": {}}).kinds, set())


class TimelineTest(unittest.TestCase):
    """Dota 时钟正下方那块倒数用的：这局全部动作 (开始, 结束, 种类, 词)"""
    def test_five_safe_lane(self):
        safe = (jungle.Pull("good_1", "small", 19, 21), jungle.Pull("good_2", "big", 27, 29))
        t = jungle.make(5, None, pulls=safe).timeline()
        self.assertEqual(t[0], (60, 65, "block", "看拉野点"))
        self.assertEqual(t[1:4], [(at(1, 19), at(1, 21), "pull", "拉野"), (at(1, 27), at(1, 29), "pull", "拉大野"),
                                  (at(1, 53), at(1, 55), "stack", "屯野")])
        self.assertEqual(t[-1], (at(11, 53), at(11, 55), "stack", "屯野"))   # 屯野到 12 分钟前、拉野到 10 分钟前
        self.assertEqual(max(a for a, _, k, _ in t if k == "pull"), at(9, 27))
        self.assertEqual(t, sorted(t))

    def test_four_and_muted(self):
        off = (jungle.Pull("evil_1", "off", 19, 20),)
        p = jungle.make(4, None, pulls=off)
        self.assertEqual(p.timeline()[:2], [(60, 60, "block", "封野"), (at(1, 19), at(1, 20), "pull", "拉大野")])
        self.assertEqual({k for _, _, k, _ in p.timeline({"stack", "block"})}, {"pull"})
        self.assertEqual(jungle.make(1, None).timeline(), [])


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

    def test_pull_fallback_for_five(self):
        """读不到游戏文件：五号位按 pull_second ~ +5 秒拉自家拉野点，别的位置不提醒拉野"""
        p = jungle.make(5, None)
        c = p.cue(at(1, 10))
        self.assertEqual((c.kind, c.title), ("pull", "拉野 · 1:15~1:20"))
        self.assertTrue(c.lines[0][1].startswith("1:15~1:20 "))
        self.assertEqual(p.cue(at(3, 20)).key, "pull:3:15")                 # 一段时间里一直亮着
        self.assertIsNone(jungle.make(4, None).cue(at(3, 15)))
        self.assertIsNone(p.cue(at(jungle.PULL_UNTIL, 15)))

    def test_pull_windows_from_game_file(self):
        """游戏文件给的时间段：五号位小野 + 大野合成一张，劣势路（三 / 四号位）拉对面大野"""
        safe = (jungle.Pull("good_1", "small", 19, 21), jungle.Pull("good_2", "big", 27, 29))
        p = jungle.make(5, None, pulls=safe)
        c = p.cue(at(2, 11))                                                 # 最早那次前 8 秒亮
        self.assertEqual(c.title, "拉野 · 2:19 / 2:27")
        self.assertEqual([t[:9] for _, t in c.lines[:2]], ["2:19~2:21", "2:27~2:29"])
        self.assertEqual(p.cue(at(2, 32)).key, "pull:2:19")                 # 最晚那次过完 3 秒才收
        self.assertIsNone(p.cue(at(2, 33)))

    def test_two_pulls_a_minute(self):
        """游戏文件里每分钟两波兵：:19 / :27 一张，:49 / :57 另一张"""
        P = jungle.Pull
        p = jungle.make(5, None, pulls=(P("good_1", "small", 19, 21), P("good_2", "big", 27, 29),
                                        P("good_1", "small", 49, 51), P("good_2", "big", 57, 59)))
        self.assertEqual([len(g) for g in p.pull_groups()], [2, 2])
        first, second = p.cue(at(3, 20)), p.cue(at(3, 45))
        self.assertEqual((first.key, first.title), ("pull:3:19", "拉野 · 3:19 / 3:27"))
        self.assertEqual((second.key, second.title), ("pull:3:49", "拉野 · 3:49 / 3:57"))
        self.assertEqual([t[:9] for _, t in second.lines], ["3:49~3:51", "3:57~3:59"])
        self.assertEqual(p.cue(at(4, 2)).key, "pull:3:49")                  # :59 过完 3 秒才收（到下一分钟 :02）
        self.assertEqual(p.cue(at(3, 47)).key, "pull:3:49")                 # 和 :53 屯野同时亮：先到的拉野在前
        self.assertIsNone(p.cue(at(3, 36)))                                  # 两波中间不亮
        self.assertEqual(p.cue(at(3, 52)).key, "stack:3")                   # :51 拉完了，:53 的屯野接上
        self.assertEqual(len(p.cue(at(1, 45)).lines), 2)                     # 1 分钟的第二张不再多说一句
        off = jungle.make(4, None, pulls=(P("evil_1", "off", 19, 20), P("evil_1", "off", 49, 50)))
        self.assertEqual(off.cue(at(4, 45)).title, "劣势路拉野 · 4:49~4:50")
        off = jungle.make(4, None, pulls=(jungle.Pull("evil_1", "off", 19, 20),))
        self.assertIn("pull", off.kinds)
        c = off.cue(at(1, 15))
        self.assertEqual(c.title, "劣势路拉野 · 1:19~1:20")
        self.assertIn("对面野区", c.lines[0][1])
        self.assertEqual(jungle.make(3, None, pulls=(jungle.Pull("evil_1", "off", 19, 20),)).kinds, {"pull"})
        self.assertEqual(jungle.make(1, None, pulls=()).kinds, set())

    def test_no_lotus(self):
        """抢莲花不提醒了：3:00 前只有屯野"""
        self.assertEqual(jungle.make(4, None).cue(at(2, 50)).key, "stack:2")
        self.assertIsNone(jungle.make(4, None).cue(at(2, 57)))
        self.assertIsNone(jungle.make(3, None).cue(at(5, 50)))

    def test_muted_and_custom_seconds(self):
        p = jungle.make(5, None, stack_second=54, pull_second=17)
        self.assertEqual(p.cue(at(4, 50)).title, "屯野 · 4:54")
        self.assertEqual(p.cue(at(4, 12)).title, "拉野 · 4:17~4:22")
        self.assertIsNone(p.cue(at(4, 50), muted={"stack"}))
        self.assertEqual(p.cue(at(4, 12), muted={"stack"}).kind, "pull")

    def test_core_stack_card_says_why(self):
        c = jungle.make(2, bench(1.5)).cue(at(1, 50))
        self.assertIn("平均屯 1.5 组", c.lines[1][1])


if __name__ == "__main__":
    unittest.main()
