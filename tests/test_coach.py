"""局内缺口提醒：去重 / 装备合成 / 用真实知识库判断几个典型阵容。不联网。"""
import unittest

from dota_assistant.domain import coach
from dota_assistant import kb


class LogicTest(unittest.TestCase):
    def test_upgrade_counts_as_owned(self):
        tree = {"dragon_lance": {"hurricane_pike"}}
        self.assertTrue(coach.has({"hurricane_pike"}, "dragon_lance", tree))
        self.assertFalse(coach.has({"force_staff"}, "dragon_lance", tree))

    def test_reminder_dedupe(self):
        need = coach.Need("bkb", "缺黑皇杖", ("black_king_bar",), due=25)
        c = coach.Coach([need], None, {}, None)
        said = [m for m in (20, 25, 27, 31, 35, 45)
                if c.tick(coach.State(m, set()), checkpoint=True)]
        self.assertEqual(said, [25, 31])          # 到点才说；隔 6 分钟再说一次；最多两次
        self.assertEqual(c.tick(coach.State(50, {"black_king_bar"}), True), [])

    def test_avoid_need_never_nags(self):
        """「别买」（avoid）不是缺口：到点不说，同分段核心装「还没出」也不催这件"""
        avoid = coach.Need("avoid:bkb", "别出黑皇杖", ("black_king_bar",), tip=coach.AVOID_TIP, avoid=True)
        bench = {"matches": 100, "boots_max": 100, "per_min": {}, "items": {"116": [[20, 80]]}}

        class Base:                                   # check() 只用到 items / item_ids
            items = {"item_black_king_bar": kb.Entry("黑皇杖", "item_black_king_bar", "", cost=4050)}
            item_ids = {"item_black_king_bar": 116}
        self.assertEqual(coach.check([avoid], coach.State(40, set()), bench, {}, Base, False), [])
        self.assertEqual([n.key for n in coach.check([], coach.State(40, set()), bench, {}, Base, False)],
                         ["core:black_king_bar"])


@unittest.skipUnless((kb.KB_DIR / "kb.json").exists(), "没有本地知识库，先 kb update")
class RealKBTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = kb.KnowledgeBase.load()

    def hero(self, name):
        return next(h for h in self.base.heroes.values() if h.name == name)

    def plan(self, me, enemies):
        return coach.plan(self.hero(me), [self.hero(e) for e in enemies], self.base, [], None)

    def test_drow_vs_windranger_no_mkb(self):
        needs, notes = self.plan("卓尔游侠", ["风行者", "沉默术士", "冥魂大帝", "露娜", "灰烬之灵"])
        self.assertNotIn("mkb", [n.key for n in needs])
        self.assertTrue(any("克敌机先" in n for n in notes))
        self.assertIn("bkb", [n.key for n in needs])

    def test_other_carry_vs_evasion_gets_mkb(self):
        needs, _ = self.plan("斯温", ["风行者", "幻影刺客"])
        self.assertIn("mkb", [n.key for n in needs])

    def test_support_not_nagged_for_bkb(self):
        me, enemies = self.hero("司夜刺客"), [self.hero(e) for e in ["风行者", "沉默术士", "冥魂大帝", "幻影刺客"]]
        needs, notes = coach.plan(me, enemies, self.base, [], None, pos=4)
        self.assertEqual([n.key for n in needs], [])
        self.assertTrue(any("提醒核心早出黑皇杖" in n for n in notes), notes)
        self.assertTrue(any("提醒核心考虑金箍棒" in n for n in notes), notes)

    def test_slark_invis_note(self):
        _, notes = self.plan("幻影长矛手", ["斯拉克"])
        self.assertTrue(any("粉对它没用" in n for n in notes), notes)

    def test_two_bkb_piercers_avoid_bkb(self):
        """对面无视技能免疫的控制 ≥ 2 个（黑皇杖研究第 69 节的计数）：黑皇杖改成「别买」，不再推；出装顺序里拿掉、列成 ✗"""
        from dota_assistant.domain import itembuild
        self.assertEqual([a.name for a in coach.bkb_piercers(self.hero("马格纳斯"))], ["两极反转"])
        self.assertEqual(coach.bkb_piercers(self.hero("莉娜")), [])
        needs, _ = self.plan("斯温", ["马格纳斯", "兽王", "莉娜", "水晶室女", "莱恩"])
        keys = [n.key for n in needs]
        self.assertIn("avoid:bkb", keys)
        self.assertNotIn("bkb", keys)
        avoid = next(n for n in needs if n.key == "avoid:bkb")
        self.assertIn("两极反转", avoid.text)
        self.assertIn("原始咆哮", avoid.text)
        b = itembuild.make(needs, None, self.base)
        self.assertNotIn("black_king_bar", [s.key for s in b.steps])
        self.assertEqual([(s.kind, s.key) for s in b.tips if s.kind == "avoid"], [("avoid", "black_king_bar")])
        self.assertTrue(any("这局别出" in itembuild.text(line[1]) for line in itembuild.brief_lines(b)))
        # 卡片上少字多图：红叉的黑皇杖 + 验证过的胜率差 + 「挡不住」+ 两个英雄的头像和技能图标；整句悬停看
        line = next(x for x in itembuild.brief_lines(b) if "这局别出" in itembuild.text(x[1]))
        icons = line[1]["short"]
        self.assertEqual(icons[:3], [{"item": "black_king_bar", "name": "黑皇杖", "mark": "avoid"},
                                     {"tag": "胜率 −3.2", "tone": "bad"}, {"tag": "挡不住"}])
        self.assertEqual([(p["name"], p["ab_name"]) for p in icons[3:]], [("马格纳斯", "两极反转"), ("兽王", "原始咆哮")])
        # 只有 1 个能穿的：照旧推黑皇杖，尾巴提一句挡不住
        needs, _ = self.plan("斯温", ["马格纳斯", "莉娜", "斧王", "水晶室女", "莱恩"])
        bkb = next(n for n in needs if n.key == "bkb")
        self.assertIn("黑皇杖挡不住", bkb.text)
        # 辅助：只在开局提示里说一声
        needs, notes = coach.plan(self.hero("莱恩"), [self.hero(e) for e in ["马格纳斯", "兽王"]], self.base, [], None, pos=5)
        self.assertEqual(needs, [])
        self.assertTrue(any("黑皇杖价值低" in n for n in notes), notes)

    def test_reveal_note_only_for_invis_buyers(self):
        """对面能显形：只提醒同分段常出隐身装的英雄（需要同分段数据），只作提示"""
        bench = {"matches": 100, "boots_max": 100, "per_min": {},
                 "items": {str(self.base.item_ids["item_glimmer_cape"]): [[15, 30]]}}
        _, notes = coach.plan(self.hero("莱恩"), [self.hero("斯拉达")], self.base, [], bench, pos=5)
        self.assertTrue(any("能显形" in n and "微光披风" in n for n in notes), notes)
        _, notes = coach.plan(self.hero("莱恩"), [self.hero("斯拉达")], self.base, [], None, pos=5)
        self.assertFalse(any("能显形" in n for n in notes), notes)

    def test_ally_cocoon_is_not_control(self):
        hits = [ab.name for ab, _, _ in self.hero("寒冬飞龙").with_tag(*kb.CONTROL_TAGS)]
        self.assertNotIn("极寒之拥", hits)
        self.assertIn("寒冬诅咒", hits)


if __name__ == "__main__":
    unittest.main()
