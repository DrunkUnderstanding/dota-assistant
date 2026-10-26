"""后期团战提示（teamfight.py）：要等的大招、留打断、留控给对面核心、保人、反弹。不联网；有本地知识库时再抽查真实阵容。"""
import unittest

from dota_assistant import kb, teamfight


def hero(name: str, *abilities: kb.Entry) -> kb.HeroKB:
    return kb.HeroKB(0, name, name, "远程", 600, list(abilities), [], [])


STUN = kb.Entry("裂地尖刺", "s", text="敌人落地时被眩晕并受到伤害。", cooldown="12", targets_enemy=True)
BH = kb.Entry("黑洞", "bh", text="持续施法 - 召唤一个强引力漩涡吸住敌方单位。", cooldown="180/170/160",
              ultimate=True, channelled=True, pierces_immunity=True)
RAVAGE = kb.Entry("毁灭", "rv", text="猛击地面，伤害并眩晕附近所有敌方单位。", cooldown="150/145/140",
                  ultimate=True, pierces_immunity=False)
DUEL = kb.Entry("决斗", "du", text="强制与目标敌方英雄进行决斗。", cooldown="60/55/50", ultimate=True,
                pierces_immunity=True, targets_enemy=True)
NOVA = kb.Entry("脉冲新星", "pn", text="对附近敌人造成伤害。", cooldown="1", ultimate=True)
RAGE = kb.Entry("神之力量", "gs", text="获得攻击力加成。", cooldown="110/105/100", ultimate=True)
REINC = kb.Entry("绝冥再生", "re", text="倒下后会复活，附近的敌人会被减速。", cooldown="180/150/120", ultimate=True)
BLINK = kb.Entry("闪烁", "bl", text="短距离传送，能让他自由出入战场。", cooldown="15/12/9/6")
GRAVE = kb.Entry("薄葬", "sg", text="友军身受薄葬的福佑，在其保护下不论受到多大的伤害都不会阵亡。", cooldown="30/26/22/18")
SPIN = kb.Entry("剑刃风暴", "bf", text="使主宰获得减益免疫状态，对周围的敌方单位造成伤害。", cooldown="30/18")
COUNTER = kb.Entry("法术反制", "cs", text="创造一个反魔法外壳，抵挡和反弹所有指向性技能。", cooldown="15/3")


class TipsTest(unittest.TestCase):
    def texts(self, *args, **kw):
        return [w.text for w in teamfight.tips(*args, **kw)]

    def test_key_ults(self):
        # 冷却短的（脉冲新星）、只给自己加攻击的（神之力量）不算要等的大招
        self.assertEqual(teamfight.key_ults(hero("x", NOVA, RAGE, RAVAGE)), [RAVAGE])
        self.assertEqual(teamfight._cd(BH), 160)                 # 满级冷却

    def test_channel_interrupt_first(self):
        ws = teamfight.tips(hero("莱恩", STUN), [], [hero("潮汐猎人", RAVAGE), hero("谜团", BH)], 5)
        self.assertEqual(ws[0].kind, "good")
        self.assertEqual(ws[0].text, "谜团的黑洞（冷却 160 秒，黑皇杖挡不住）要持续施法：留你的裂地尖刺打断")
        self.assertIn("无视技能免疫（官方技能属性）", ws[0].quote)
        self.assertIn("潮汐猎人的毁灭（冷却 140 秒）没交前别扎堆；交了 140 秒内没有", ws[1].text)
        self.assertNotIn("黑皇杖", ws[1].text)                    # 辅助不提黑皇杖

    def test_bkb_for_core(self):
        ws = self.texts(hero("卓尔游侠"), [], [hero("潮汐猎人", RAVAGE), hero("军团指挥官", DUEL)], 1)
        self.assertIn("等军团指挥官的决斗（冷却 50 秒，黑皇杖挡不住）交了再进；交了 50 秒内没有", ws)   # 单体
        self.assertTrue(any("毁灭" in t and "开黑皇杖能躲" in t for t in ws))
        # 复活：杀两次
        self.assertTrue(any("绝冥再生" in t and "死了能复活" in t for t in self.texts(hero("x"), [], [hero("冥魂大帝", REINC)], 1)))

    def test_keep_control_for_core(self):
        enemies = [hero("敌法师", BLINK), hero("主宰", SPIN)]
        ws = self.texts(hero("莱恩", STUN), [], enemies, 5, {"敌法师": 1, "主宰": 2})
        self.assertIn("对面一号位敌法师有闪烁（冷却 6 秒）保命：骗出来再上你的裂地尖刺", ws)
        self.assertIn("对面中单主宰有剑刃风暴（冷却 18 秒）保命：骗出来再上你的裂地尖刺", ws)
        # 没有位置就不说留控给谁；自己没控制就用队友的
        self.assertFalse(any("留给" in t or "骗出来" in t for t in self.texts(hero("莱恩", STUN), [], enemies, 5)))
        ws = self.texts(hero("卓尔游侠"), [hero("莱恩", STUN)], [hero("美杜莎")], 1, {"美杜莎": 1})
        self.assertIn("队友莱恩的裂地尖刺留给对面一号位美杜莎", ws)

    def test_save_and_reflect(self):
        ws = self.texts(hero("莱恩", STUN), [], [hero("戴泽", GRAVE), hero("敌法师", COUNTER)], 5)
        self.assertIn("戴泽的薄葬（冷却 18 秒）能保人：先控戴泽或等它交了再集火", ws)
        self.assertIn("敌法师的法术反制会挡 / 弹指向技能：先骗掉再对他放裂地尖刺", ws)

    def test_my_channel_ult(self):
        ws = self.texts(hero("谜团", BH), [], [hero("莱恩", STUN)], 3)
        self.assertIn("你的黑洞要持续施法，会被莱恩的裂地尖刺打断：等它交了再放", ws)
        self.assertEqual(teamfight.tips(hero("x"), [], [], 1), [])

    def test_real_heroes(self):
        base = kb.KnowledgeBase.load()
        if base is None:
            self.skipTest("没有本地知识库")
        by = {h.name: h for h in base.heroes.values()}
        ws = self.texts(by["卓尔游侠"], [by["莱恩"]], [by["谜团"], by["虚空假面"], by["戴泽"], by["亚巴顿"]], 1, {"虚空假面": 1})
        self.assertTrue(any(t.startswith("谜团的黑洞") and "黑皇杖挡不住" in t and "打断" in t for t in ws), ws)
        self.assertTrue(any(t.startswith("虚空假面的时间结界") and "黑皇杖挡不住" in t for t in ws), ws)
        self.assertTrue(any("戴泽的薄葬" in t for t in ws), ws)
        self.assertFalse(any("回光返照" in t for t in ws), ws)    # 亚巴顿的大招是保自己，不是「要等的大招」


class ImmunityFieldTest(unittest.TestCase):
    def test_official_immunity_values(self):
        # 3 = 对敌方无视技能免疫（黑洞），4 = 不无视（毁灭），1 = 只作用于友方（薄葬），说不上无不无视
        ab = lambda v: kb._entry({"name_loc": "x", "name": "x", "immunity": v}, False).pierces_immunity
        self.assertEqual((ab(3), ab(4), ab(5), ab(1), ab(0)), (True, False, False, None, None))


if __name__ == "__main__":
    unittest.main()
