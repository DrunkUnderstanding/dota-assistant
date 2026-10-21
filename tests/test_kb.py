"""知识库：文本整理、标签抽取、规则加载。不联网；有本地知识库时再抽查几条真实数据。"""
import tempfile
import unittest
from pathlib import Path

from dota_assistant import kb


class FillTest(unittest.TestCase):
    def test_placeholders_and_html(self):
        ab = {"special_values": [{"name": "chance", "values_float": [30, 35, 40]},
                                 {"name": "radius", "values_float": [300]}],
              "durations": [1.5, 1.5]}
        text = kb.fill("<h1>被动</h1>%chance%%% 几率，%radius% 范围，持续%abilityduration%秒，%unknown%", ab)
        self.assertEqual(text, "【被动】30/35/40% 几率，300 范围，持续1.5秒，[unknown]")


class TagTest(unittest.TestCase):
    def test_break_applied_vs_vulnerable(self):
        self.assertIn("施加破坏", kb.tag_sentences("末日对敌人施加破坏效果。"))
        tags = kb.tag_sentences("破坏会使被动魔法抗性失效。")
        self.assertNotIn("施加破坏", tags)
        self.assertIn("怕破坏", tags)

    def test_true_strike_and_evasion(self):
        tags = kb.tag_sentences("射手天赋的触发效果拥有克敌机先。")
        self.assertIn("克敌机先", tags)
        self.assertNotIn("提供闪避", kb.tag_sentences("每次攻击均有80%几率无视闪避。"))
        self.assertIn("无视闪避", kb.tag_sentences("每次攻击均有80%几率无视闪避。"))

    def test_dispel_type_line_is_not_a_dispel(self):
        self.assertNotIn("驱散", kb.tag_sentences("驱散类型：弱驱散"))
        self.assertIn("不可驱散", kb.tag_sentences("破坏的负面效果不能被驱散。"))


class RulesTest(unittest.TestCase):
    def test_load(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "rules.toml"
            p.write_text('[[rule]]\nhero = "卓尔游侠"\nitem = "金箍棒"\ntext = "低优先级"\nsource = "玩家原话"\n',
                         encoding="utf-8")
            r = kb.load_rules(p)
            self.assertEqual((r[0].hero, r[0].item, r[0].source), ("卓尔游侠", "金箍棒", "玩家原话"))

    def test_repo_rules_parse(self):
        for r in kb.load_rules():
            self.assertTrue(r.text and r.source in ("玩家原话", "实现假设"), r)


@unittest.skipUnless((kb.KB_DIR / "kb.json").exists(), "没有本地知识库，先 kb update")
class RealDataTest(unittest.TestCase):
    """抽查几条官方数据，版本更新后如果挂了，先看是机制真改了还是抽取规则坏了"""

    @classmethod
    def setUpClass(cls):
        cls.base = kb.KnowledgeBase.load()

    def hero(self, name):
        return next(h for h in self.base.heroes.values() if h.name == name)

    def test_drow_marksmanship_true_strike(self):
        self.assertIn("克敌机先", self.hero("卓尔游侠").tags())

    def test_item_lookup_and_tags(self):
        self.assertEqual(self.base.item("BKB").name, "黑皇杖")
        self.assertIn("提供技能免疫", self.base.item("BKB").tags())
        self.assertIn("无视闪避", self.base.item("MKB").tags())
        self.assertIn("施加破坏", self.base.item("白银之锋").tags())
        self.assertIn("抵挡 / 反馈指向技能", self.base.item("林肯").tags())

    def test_talents_have_values(self):
        missing = [(h.name, t) for h in self.base.heroes.values() for _, a, b in h.talents for t in (a, b) if "?" in t]
        self.assertLess(len(missing), 10, missing)


if __name__ == "__main__":
    unittest.main()
