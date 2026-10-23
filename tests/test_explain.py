"""推荐理由里的机制对位（explain.py）。不联网；有本地知识库时再抽查真实英雄。"""
import unittest

from dota_assistant import explain, kb


def hero(name: str, *abilities: kb.Entry) -> kb.HeroKB:
    return kb.HeroKB(0, name, name, "远程", 600, list(abilities), [], [])


DROW = hero("卓尔游侠", kb.Entry("射手天赋", "a", text="射手天赋的触发效果拥有克敌机先。"),
            kb.Entry("狂风", "b", text="释放一阵狂风，同时使隐身敌人显形。"))
PA = hero("幻影刺客", kb.Entry("飘忽不定", "c", text="提升闪避敌人攻击的技艺。"))
RIKI = hero("力丸", kb.Entry("永久隐身", "d", text="力丸进入隐身状态。"))
DOOM = hero("末日使者", kb.Entry("末日", "e", text="造成伤害。", scepter="末日对敌人施加破坏效果。"))
AM = hero("敌法师", kb.Entry("法术反制", "f", text="被动提供魔法抗性。", notes=["破坏会使被动魔法抗性失效。"]))
LION = hero("莱恩", kb.Entry("裂地尖刺", "g", text="击晕敌人。", pierces_immunity=False))
SLARK = hero("斯拉克", kb.Entry("暗影之舞", "i", text="斯拉克变成隐身状态，不会被反隐手段发现。"))
JUGG = hero("主宰", kb.Entry("剑刃风暴", "h", text="主宰获得减益免疫，旋转着造成伤害。"))


class MatchupTest(unittest.TestCase):
    def test_counter_lines_with_quotes(self):
        ws = explain.matchups(DROW, [PA, RIKI])
        self.assertEqual([w.kind for w in ws], ["good", "good"])
        self.assertIn("幻影刺客的飘忽不定闪避对你无效", ws[0].text)
        self.assertIn("射手天赋", ws[0].text)
        self.assertIn("克敌机先", ws[0].quote)
        self.assertIn("力丸的永久隐身隐身藏不住", ws[1].text)

    def test_bad_side_limited_to_one(self):
        ws = explain.matchups(PA, [DROW])
        self.assertEqual([(w.kind, w.text) for w in ws], [("bad", "卓尔游侠的射手天赋无视闪避，你的飘忽不定闪避没用")])

    def test_scepter_only_marked(self):
        ws = explain.matchups(DOOM, [AM])
        self.assertEqual(ws[0].text, "末日（神杖）能施加破坏，敌法师的法术反制被破坏后失效")

    def test_immunity_dodges_non_piercing_control(self):
        ws = explain.matchups(JUGG, [LION])
        self.assertEqual(ws[0].text, "剑刃风暴有技能免疫，能躲莱恩的裂地尖刺")

    def test_unrevealable_invis_skipped(self):
        self.assertEqual(explain.matchups(DROW, [SLARK]), [])

    def test_nothing_to_say(self):
        self.assertEqual(explain.matchups(LION, [RIKI]), [])


@unittest.skipUnless(kb.KnowledgeBase.load(), "没有本地知识库")
class RealDataTest(unittest.TestCase):
    def test_drow_vs_windranger(self):
        base = kb.KnowledgeBase.load()
        by = {h.name: h for h in base.heroes.values()}
        ws = explain.matchups(by["卓尔游侠"], [by["风行者"]])
        self.assertTrue(ws and ws[0].kind == "good" and "风行者" in ws[0].text)
