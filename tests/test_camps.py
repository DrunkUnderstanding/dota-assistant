"""小地图野点标记：哪一刻圈哪个野点 / 莲花池、写几秒。"""
import unittest

from dota_assistant.domain import camps, jungle


def at(m, s):
    return m * 60 + s


def xy(name):
    return camps._BY_NAME[name][2:4]


def where(mark):
    return mark["u"], mark["v"]


class MapTest(unittest.TestCase):
    def test_uv_corners(self):
        self.assertEqual(camps.uv(-camps.BOUND, camps.BOUND), (0, 0))       # 左上角 = 夜魇那边的左上
        self.assertEqual(camps.uv(camps.BOUND, -camps.BOUND), (1, 1))
        self.assertEqual(camps.uv(0, 0), (0.5, 0.5))

    def test_table(self):
        self.assertEqual(len(camps.CAMPS), 28)
        self.assertEqual(len(camps._BY_NAME), 28)
        for team, (small, large) in camps.PULL.items():
            self.assertEqual(camps._BY_NAME[small][1], camps.SMALL, team)
            self.assertEqual(camps._BY_NAME[large][1], camps.LARGE, team)


class MarksTest(unittest.TestCase):
    def test_nothing_without_plan_or_outside_windows(self):
        self.assertEqual(camps.marks(None, "radiant", at(1, 10)), [])
        p = jungle.make(5, None)
        self.assertEqual(camps.marks(p, "radiant", at(1, 30), xy("good_1")), [])   # 拉野过了、屯野还早
        self.assertEqual(camps.marks(jungle.make(1, None), "radiant", at(1, 50), xy("good_8")), [])   # 一号位不屯

    def test_block(self):
        four = camps.marks(jungle.make(4, None), "dire", -30)
        self.assertEqual([(m["kind"], m["label"], m["at"]) for m in four], [("block", "封野", 60)])
        self.assertEqual(where(four[0]), camps.uv(*xy("good_1")))          # 夜魇四号位去封天辉的拉野点
        five = camps.marks(jungle.make(5, None), "radiant", 10)
        self.assertEqual(where(five[0]), camps.uv(*xy("good_1")))          # 天辉五号位看自家那个
        self.assertEqual(camps.marks(jungle.make(4, None), "dire", 60), [])
        self.assertEqual(camps.marks(jungle.make(4, None), None, -30), [])  # 不知道阵营不画

    def test_pull_fallback_range(self):
        """没有游戏文件：五号位拉自家小野，pull_second ~ +5 秒"""
        p = jungle.make(5, None)
        ms = camps.marks(p, "dire", at(2, 0))                              # 2:15 起拉，提前 20 秒亮
        self.assertEqual([(m["label"], m["at"], m["end"]) for m in ms], [("拉 :15~:20", at(2, 15), at(2, 20))])
        self.assertEqual(where(ms[0]), camps.uv(*xy("evil_2")))
        self.assertEqual(camps.marks(p, "dire", at(2, 23)), ms)             # 过完 3 秒还在
        self.assertEqual(camps.marks(p, "dire", at(2, 24)), [])
        self.assertEqual(camps.marks(p, "dire", at(jungle.PULL_UNTIL, 10)), [])
        self.assertEqual(camps.marks(jungle.make(4, None), "dire", at(2, 10)), [])   # 四号位不拉

    def test_stack_near_hero_by_type(self):
        p = jungle.make(4, None)
        hero = xy("good_8")                                                # 站在天辉远古旁边
        ms = camps.marks(p, "radiant", at(1, 40), hero)
        self.assertEqual(ms[0]["label"], "远古 :51~:53")                    # 远古早 2 秒
        self.assertEqual(ms[0]["at"], at(1, 51))
        self.assertTrue(ms[0]["main"])
        self.assertTrue(all(not m["main"] for m in ms[1:]))
        self.assertLessEqual(len(ms), camps.NEAR_MAX)
        self.assertIn("中野 :53~:55", [m["label"] for m in ms])                 # good_7 中野在旁边
        self.assertEqual(camps.marks(p, "radiant", at(1, 40)), [])          # 不知道英雄在哪不圈
        self.assertEqual(camps.marks(p, "radiant", at(1, 40), (0, 0)), [])  # 河道中间，附近没野点
        self.assertEqual(camps.marks(p, "radiant", at(1, 40), hero, muted={"stack"}), [])

    def test_stack_custom_second(self):
        p = jungle.make(4, None, stack_second=55)
        ms = camps.marks(p, "radiant", at(3, 50), xy("good_2"))
        self.assertEqual((ms[0]["label"], ms[0]["at"]), ("大野 :54~:56", at(3, 54)))

    def test_lotus_pool_by_lane(self):
        top, bot = camps.uv(*camps.LOTUS["top"]), camps.uv(*camps.LOTUS["bot"])
        cases = [(4, "radiant", top), (3, "radiant", top), (5, "radiant", bot), (4, "dire", bot), (5, "dire", top)]
        for pos, team, pool in cases:
            ms = [m for m in camps.marks(jungle.make(pos, None), team, at(2, 50)) if m["kind"] == "lotus"]
            self.assertEqual([(where(m), m["label"], m["at"]) for m in ms], [(pool, "莲花 3:00", 180)], (pos, team))
        self.assertEqual(camps.marks(jungle.make(3, None), "radiant", at(2, 40)), [])   # 提前 15 秒才亮


if __name__ == "__main__":
    unittest.main()


TIMINGS = '''// 节选自游戏文件 scripts/creep_pull_timings.txt
"CREEP_PULL_TIMINGS"
{
	"neutralcamp_good_1"
	{
		"stack_start"	"55"
		"stack_end"		"57"
		"radiant_pull"
		{
			"pull_1_start"	"19"
			"pull_1_end"	"21"
			"pull_2_start"	"49"
		}
	}
	"neutralcamp_good_2"
	{
		"stack_start"	"52"
		"radiant_pull" { "pull_1_start" "27" }
		"dire_pull" { "pull_1_start" "17" "pull_arrow_direction" "90" }
	}
	"neutralcamp_good_8" { "stack_start" "52" }
	"neutralcamp_evil_1"
	{
		"stack_start"	"54"
		"stack_end"		"56"
		"radiant_pull" { "pull_1_start" "19" "pull_1_end" "20" "pull_2_start" "49" "pull_2_end" "50" }
		"dire_pull" { "pull_1_start" "27" "pull_1_end" "29" }
	}
}
'''


class TimingsTest(unittest.TestCase):
    def setUp(self):
        self.t = camps.parse_timings(TIMINGS)

    def test_parse(self):
        self.assertEqual(self.t["good_1"], camps.Timing((55, 57), {"radiant": ((19, 21), (49, 49))}))   # 第二次没写止 = 起
        self.assertEqual(self.t["good_2"], camps.Timing((52, 52), {"radiant": ((27, 27),), "dire": ((17, 17),)}))
        self.assertEqual(camps.load_timings(None), {})

    def test_pull_plan_by_position(self):
        P = jungle.Pull
        self.assertEqual(camps.pull_plan(self.t, "radiant", 5),
                         (P("good_1", "small", 19, 21), P("good_1", "small", 49, 49), P("good_2", "big", 27, 27)))
        off = (P("evil_1", "off", 19, 20), P("evil_1", "off", 49, 50))                # 劣势路拉夜魇的大野，每分钟两次
        self.assertEqual(camps.pull_plan(self.t, "radiant", 4), off)
        self.assertEqual(camps.pull_plan(self.t, "radiant", 3), off)
        self.assertEqual(camps.pull_plan(self.t, "dire", 4), (P("good_2", "off", 17, 17),))
        self.assertEqual(camps.pull_plan(self.t, "dire", 5), (P("evil_1", "big", 27, 29),))       # evil_2 不在节选里
        self.assertEqual(camps.pull_plan(self.t, "radiant", 1), ())
        self.assertIsNone(camps.pull_plan({}, "radiant", 5))
        self.assertIsNone(camps.pull_plan(self.t, None, 5))

    def test_marks_use_per_camp_seconds(self):
        p = jungle.make(5, None, pulls=camps.pull_plan(self.t, "radiant", 5))
        ms = camps.marks(p, "radiant", at(2, 10), timings=self.t)           # 小野 :19 前 20 秒起亮；大野 :27 也在窗口里
        self.assertEqual([(m["label"], m["at"], m["end"]) for m in ms],
                         [("拉 :19~:21", at(2, 19), at(2, 21)), ("拉大野 :27", at(2, 27), at(2, 27))])
        self.assertEqual(ms[0]["box"], list(camps._BY_NAME["good_1"][4]))
        ms = camps.marks(p, "radiant", at(3, 40), xy("good_8"), timings=self.t)
        self.assertEqual([(m["label"], m["at"]) for m in ms if m["kind"] == "pull"], [("拉 :49", at(3, 49))])   # 第二波
        ms = [m for m in ms if m["kind"] == "stack"]
        self.assertEqual((ms[0]["label"], ms[0]["at"]), ("远古 :52", at(3, 52)))
        stack = camps.marks(p, "radiant", at(3, 40), xy("good_1"), timings=self.t)
        self.assertIn(("小野 :55~:57", at(3, 55)), [(m["label"], m["at"]) for m in stack])
        rows = {m["label"]: m["row"] for m in stack if (m["x"], m["y"]) == xy("good_1")}
        self.assertEqual(rows, {"拉 :49": 0, "小野 :55~:57": 1})               # 同一个野点：先到的拉野在第 0 行，屯野往上叠
        off = jungle.make(4, None, pulls=camps.pull_plan(self.t, "radiant", 4))
        ms = [m for m in camps.marks(off, "radiant", at(2, 5), timings=self.t) if m["kind"] == "pull"]
        self.assertEqual([(m["label"], where(m)) for m in ms], [("劣势路拉大野 :19~:20", camps.uv(*xy("evil_1")))])
