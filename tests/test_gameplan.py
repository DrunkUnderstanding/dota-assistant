"""AI 打法：出处核对、整理顺序、缓存格式。不调大模型。"""
import unittest

from dota_assistant import gameplan

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


if __name__ == "__main__":
    unittest.main()
