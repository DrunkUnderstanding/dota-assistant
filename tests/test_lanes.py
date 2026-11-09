"""对面分路（lanes.py）：猜位置 + 把握、三路排法、对线提示。不联网。"""
import unittest

from dota_assistant import kb
from dota_assistant.domain import lanes


def hero(name: str, *abilities: kb.Entry, attack: str = "远程", rng: int = 600) -> kb.HeroKB:
    return kb.HeroKB(0, name, name, attack, rng, list(abilities), [], [])


# 位置局数：1 号英雄几乎只打 1，2 号 4 / 5 各半，3 号只打 2
POSITIONS = {1: {1: (950, 0), 2: (50, 0)}, 2: {4: (500, 0), 5: (500, 0)}, 3: {2: (1000, 0)},
             4: {3: (900, 0), 4: (100, 0)}, 5: {5: (600, 0), 4: (400, 0)}}


class GuessTest(unittest.TestCase):
    def test_confidence(self):
        gs = {g.hero: g for g in lanes.guess([1, 2, 3, 4, 5], POSITIONS)}
        self.assertEqual({h: g.pos for h, g in gs.items()}, {1: 1, 2: 4, 3: 2, 4: 3, 5: 5})
        self.assertGreater(gs[1].conf, 0.9)
        self.assertGreater(gs[3].conf, 0.9)
        self.assertLess(gs[2].conf, 0.7)          # 4 / 5 各半的那个没把握

    def test_partial_and_fixed(self):
        self.assertEqual([(g.hero, g.pos) for g in lanes.guess([2], POSITIONS)], [(2, 4)])
        gs = lanes.guess([2, 5], POSITIONS, taken={1}, fixed={2: 5})
        self.assertEqual({g.hero: (g.pos, g.conf) for g in gs}[2], (5, 1.0))
        self.assertEqual({g.hero: g.pos for g in gs}[5], 4)       # 5 号位被占了，只能 4
        self.assertEqual(lanes.guess([1], None), [])

    def test_rows_by_side(self):
        ln = lanes.assign([1, 2, 3, 4, 5], [], 1, "radiant", POSITIONS)
        rows = ln.rows()
        self.assertEqual([r.label for r in rows], ["上路", "中路", "下路"])
        # 我在天辉打一号位：走下路（天辉优势路），对上对面的三 / 四号位
        self.assertTrue(rows[2].mine)
        self.assertEqual(sorted(g.pos for g in rows[2].heroes), [3, 4])
        self.assertEqual(sorted(g.pos for g in rows[0].heroes), [1, 5])
        self.assertEqual([r.label for r in lanes.assign([1], [], 2, "dire", POSITIONS).rows()], ["上路", "中路", "下路"])
        dire = lanes.assign([1], [], 1, "dire", POSITIONS).rows()
        self.assertTrue(dire[0].mine)                             # 夜魇优势路在上路
        self.assertEqual(dire[2].heroes[0].hero, 1)               # 对面天辉一号位在下路
        self.assertEqual(dire[0].missing, 2)
        unknown = lanes.assign([1], [], 1, None, POSITIONS).rows()
        self.assertEqual([r.label for r in unknown], ["对面劣势路", "中路", "对面优势路"])

    def test_partner_and_text(self):
        ln = lanes.assign([1, 3], [5, 4], 1, "radiant", POSITIONS)
        self.assertEqual(ln.partner().hero, 5)
        text = lanes.lane_line(ln, lambda h: f"H{h}")
        self.assertIn("下路（你这路） 2 人未选", text)
        self.assertIn("你这路队友 H5（5号位", text)
        v = lanes.view(ln, lambda h: {"id": h, "name": f"H{h}"})
        self.assertEqual(v["partner"]["id"], 5)
        self.assertEqual([r["mine"] for r in v["rows"]], [False, False, True])


STUN = kb.Entry("裂地尖刺", "s", text="敌人落地时被眩晕并受到伤害。", cooldown="12")
ROOT = kb.Entry("缠绕", "r", text="缠绕目标。")
DRAIN = kb.Entry("法力吸取", "d", text="持续施法 - 吸取魔法。", channelled=True)
PHASE = kb.Entry("相位转移", "p", text="短暂地转移到另一个位面，从而躲避伤害。", channelled=True)
ULT_CH = kb.Entry("极寒领域", "u", text="召唤冰爆。", channelled=True, ultimate=True)
BLINK = kb.Entry("闪烁", "b", text="短距离传送，能让她自由出入战场。", cooldown="10")
SHARD_STUN = kb.Entry("魔晶技能", "g", text="眩晕敌人。", granted=True)
ULT_STUN = kb.Entry("法力虚空", "v", text="所有受到伤害的单位都会被短暂眩晕。", ultimate=True)


class LaneEdgeTest(unittest.TestCase):
    """选人卡片上的「对线」：比这个英雄平时每 100 条线多赢几条，按 LANE_K 条线收缩；太少的不显示"""

    def test_edge_shrunk_and_filtered(self):
        # 英雄 1 对所有人平时：1210 条线、净 0 → 平时 0
        matrix = {1: {2: (100, 30), 3: (100, -30), 4: (1000, 0), 5: (10, 0)}}
        out = {e.foe: e for e in lanes.lane_edges(matrix, 1, [2, 3, 5, 9])}
        self.assertEqual(set(out), {2, 3})                 # 5 只有 10 条线、9 没数据：不显示
        self.assertAlmostEqual(out[2].edge, 30 / (100 + lanes.LANE_K) * 100)   # 往平时（0）拉
        self.assertEqual(out[2].verdict, "好打")
        self.assertEqual(out[3].verdict, "难打")
        self.assertEqual(lanes.lane_edges(None, 1, [2]), [])
        self.assertEqual(lanes.lane_edges(matrix, 7, [2]), [])

    def test_lane_foes_by_guessed_position(self):
        positions = {10: {3: (900, 450), 1: (100, 50)}, 11: {5: (900, 450)}, 12: {2: (900, 450)}}
        self.assertEqual(lanes.lane_foes([10, 11, 12], positions, 1), [10])        # 一号位对上对面 3 / 4 号位
        self.assertEqual(lanes.lane_foes([10, 11, 12], positions, 2), [12])
        self.assertEqual(lanes.lane_foes([10, 11, 12], positions, 1, fixed={11: 4}), [10, 11])


class AdvisorLaneTest(unittest.TestCase):
    def test_cards_carry_lane(self):
        """推荐卡片带「对线」：只算和我这个位置对线的已选敌人（这里玩家定了幻影长矛手打 3 号位），不改胜率"""
        from dota_assistant.core.config import Config
        from dota_assistant.domain import advisor as adv
        from tests.test_engine import PL, TIDE
        from tests.test_webui import fake_advisor
        a = fake_advisor(Config(position_filter=False, top=5, top_other=5, familiar_games=0))
        before = {s.hero.id: s.winrate for s in a.advise(adv.Draft(enemies=[PL], enemy_pos={PL.id: 3}), 1).shown()}
        a.lanes = {1: {TIDE.id: {PL.id: (200, 60), 99: (800, 0)}}}
        advice = a.advise(adv.Draft(enemies=[PL], enemy_pos={PL.id: 3}), 1)
        tide = next(s for s in advice.shown() if s.hero == TIDE)
        self.assertEqual([(h, e.verdict) for h, e in tide.lane], [(PL, "好打")])
        self.assertEqual({s.hero.id: s.winrate for s in advice.shown()}, before)
        card = next(c for g in adv.render_view(advice, None)["groups"] for c in g["items"] if c["id"] == TIDE.id)
        self.assertEqual((card["lane"][0]["id"], card["lane"][0]["verdict"], card["lane"][0]["lanes"]), (PL.id, "好打", 200))
        # 对面打中路：不和一号位对线，不显示
        advice = a.advise(adv.Draft(enemies=[PL], enemy_pos={PL.id: 2}), 1)
        self.assertEqual(next(s for s in advice.shown() if s.hero == TIDE).lane, [])


class TipsTest(unittest.TestCase):
    def test_interrupt_channel(self):
        ws = lanes.tips(hero("莱恩", STUN), [hero("对手", DRAIN)])
        self.assertTrue(any(w.kind == "good" and "对手的法力吸取要持续施法，用你的裂地尖刺打断" in w.text for w in ws))
        # 躲进别的位面的持续施法打断不了；大招标「6 级后」
        ws = lanes.tips(hero("莱恩", STUN), [hero("帕克", PHASE), hero("水晶室女", ULT_CH)])
        self.assertFalse(any("相位转移" in w.text for w in ws))
        self.assertTrue(any(w.text.startswith("6 级后水晶室女的极寒领域") for w in ws))
        # 我自己的持续施法会被对面眩晕打断
        ws = lanes.tips(hero("莱恩", DRAIN), [hero("对手", STUN)])
        self.assertTrue(any(w.kind == "bad" and "你的法力吸取" in w.text for w in ws))

    def test_control_and_range(self):
        ws = lanes.tips(hero("近战", attack="近战", rng=150), [hero("甲", STUN), hero("乙", ROOT)])
        self.assertIn("对面同路两个控制", ws[0].text)
        self.assertIn("甲·裂地尖刺", ws[0].quote)
        self.assertTrue(any("你近战" in w.text and "官方数据" in w.quote for w in ws))
        ws = lanes.tips(hero("远", rng=625), [hero("甲", STUN, attack="近战", rng=150)])
        self.assertIn("冷却 12 秒", ws[0].text)
        self.assertTrue(any(w.kind == "good" and "你远程" in w.text for w in ws))
        self.assertEqual(lanes.tips(hero("远"), []), [])

    def test_escape_combo_and_ultimate(self):
        ws = lanes.tips(hero("我", STUN), [hero("痛苦女王", BLINK, ULT_STUN)], ally=hero("队友", ROOT))
        texts = [w.text for w in ws]
        self.assertTrue(any("痛苦女王有闪烁能跑（冷却 10 秒）：等闪烁交了再上你的裂地尖刺" in t for t in texts))
        self.assertTrue(any("你的裂地尖刺和队友队友的缠绕都能控" in t for t in texts))
        self.assertIn("6 级后小心痛苦女王的法力虚空（眩晕）", texts)
        # 我方没有控制：不说「等闪烁交了再上…」
        self.assertFalse(any("能跑" in t for t in ws_text(lanes.tips(hero("我"), [hero("痛苦女王", BLINK)]))))

    def test_granted_abilities_ignored(self):
        self.assertEqual(lanes.tips(hero("我"), [hero("对手", SHARD_STUN)]), [])


def ws_text(ws):
    return [w.text for w in ws]


class RealKBTest(unittest.TestCase):
    """有本地知识库时抽查真实英雄（标签是关键词匹配，防回归）"""

    @classmethod
    def setUpClass(cls):
        cls.base = kb.KnowledgeBase.load()
        if cls.base is None:
            raise unittest.SkipTest("没有本地知识库")

    def h(self, name):
        return next(h for h in self.base.heroes.values() if h.name == name)

    def test_hex_and_shackles_are_control(self):
        ss = self.h("暗影萨满")
        tags = {t for _, t, _ in lanes._hits(ss, kb.CONTROL_TAGS)}
        self.assertIn("妖术", tags)
        self.assertIn("缠绕 / 定身", {t for ab in ss.abilities if ab.name == "枷锁" for t in kb.tag_sentences(ab.text)})
        self.assertTrue(next(ab for ab in ss.abilities if ab.name == "枷锁").channelled)

    def test_drow_vs_lion(self):
        texts = ws_text(lanes.tips(self.h("卓尔游侠"), [self.h("莱恩")]))
        self.assertTrue(any("法力吸取要持续施法，用你的狂风打断" in t for t in texts))


if __name__ == "__main__":
    unittest.main()
