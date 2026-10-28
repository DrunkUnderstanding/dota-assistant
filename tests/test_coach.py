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

    def test_ally_cocoon_is_not_control(self):
        hits = [ab.name for ab, _, _ in self.hero("寒冬飞龙").with_tag(*kb.CONTROL_TAGS)]
        self.assertNotIn("极寒之拥", hits)
        self.assertIn("寒冬诅咒", hits)


if __name__ == "__main__":
    unittest.main()
