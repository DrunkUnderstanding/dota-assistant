"""AI 打法：出处核对、整理顺序、缓存格式。不调大模型。"""
import unittest

from dota_assistant.ai import gameplan
from dota_assistant import kb

SOURCE = """# 敌方 风行者（官方原文）
【束缚击】（冷却 16/14/12/10）
  将目标束缚在正对其后的一个敌方单位或树上。若目标后面没有其它敌方单位或树木，则眩晕时间减少至0.6秒。"""


class ParseTest(unittest.TestCase):
    def test_quote_must_be_in_source(self):
        res = {"sections": [
            {"phase": "后期（30 分钟后）", "tips": [{"text": "后期", "basis": "b", "quote": "束缚在正对其后"}]},
            {"phase": "对线期（0~10 分钟）", "tips": [
                {"text": "别站树前", "basis": "束缚击原文", "quote": "将目标束缚在正对其后的一个敌方单位或树上。"},
                {"text": "编的", "basis": "x", "quote": "束缚击冷却 5 秒"},
                {"text": "空格不同也算", "basis": "x", "quote": "眩晕时间减少至 0.6 秒"}]}]}
        plan = gameplan.parse(res, SOURCE)
        self.assertEqual([p for p, _ in plan.sections], ["对线期（0~10 分钟）", "后期（30 分钟后）"])   # 固定顺序
        self.assertEqual([t.text for t in plan.sections[0][1]], ["别站树前", "空格不同也算"])
        self.assertEqual([t.text for t in plan.dropped], ["编的"])

    def test_json_roundtrip(self):
        plan = gameplan.Plan([("对线劣势时", [gameplan.Tip("a", "b", "c")])], [gameplan.Tip("x", "y", "z")], 0.2, 30)
        back = gameplan.Plan.from_json(plan.to_json())
        self.assertEqual(back.sections[0][1][0].text, "a")
        self.assertEqual(back.dropped[0].quote, "z")
        self.assertIn("依据：b", back.text())


@unittest.skipUnless((kb.KB_DIR / "kb.json").exists(), "没有本地知识库，先 kb update")
class FactsTest(unittest.TestCase):
    def test_mistakes_go_with_heroes(self):
        base = kb.KnowledgeBase.load()
        hid = {h.name: i for i, h in base.heroes.items()}
        ms = [kb.Mistake("司夜刺客没有破坏", "复仇施加破坏", hero="司夜刺客"),
              kb.Mistake("帕吉能驱散", "不能", hero="帕吉"),
              kb.Mistake("金箍棒能破隐身", "不能", item="金箍棒")]
        text = gameplan.facts(base, [], hid["卓尔游侠"], [], [hid["司夜刺客"]], [], "数据", ms)
        self.assertIn("✗ 司夜刺客没有破坏", text)
        self.assertNotIn("帕吉能驱散", text)                     # 不在本局的英雄不给
        self.assertIn("# 常见误判", text)
        self.assertIn("金箍棒能破隐身", text)

    def test_lane_partner_gets_full_text(self):
        # 同路队友给技能全文（写两人怎么配合要用），其他队友只给摘要
        base = kb.KnowledgeBase.load()
        hid = {h.name: i for i, h in base.heroes.items()}
        text = gameplan.facts(base, [], hid["卓尔游侠"], [hid["暗影萨满"], hid["帕克"]], [hid["莱恩"]],
                              [hid["莱恩"], hid["暗影萨满"]], "数据")
        self.assertIn("# 敌方 莱恩（你的对线对手）", text)
        self.assertIn("# 队友 暗影萨满（你的同路队友）", text)
        self.assertIn("【枷锁】", text)
        self.assertNotIn("# 队友 帕克", text)
        self.assertIn("帕克", text.split("# 队友（机制摘要）")[1])


if __name__ == "__main__":
    unittest.main()
