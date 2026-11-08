"""强克制（2026-11-08）：被已选敌人克到 AVOID_COUNTER 以上的不推荐；克对面 1~3 号位 HARD_COUNTER 以上的标「强克」、「克制最强」排前面。不联网、不碰 data/。"""
import unittest

from dota_assistant.core.config import Config
from dota_assistant.domain import advisor as adv
from dota_assistant.domain.engine import AVOID_COUNTER, HARD_COUNTER, Engine, Suggestion
from dota_assistant.core.heroes import HeroDB
from tests.test_engine import AXE, CM, HEROES, PL, TIDE, WK, make_stats
from tests.test_webui import fake_advisor

HARD = HARD_COUNTER / 25          # 门槛换成 logit（pct = logit × 25）
AVOID = AVOID_COUNTER / 25


def sug(hero, score, vs, base=0.0, cores=None):
    s = Suggestion(hero, score, base, vs=vs)
    s.cores = cores
    return s


class SuggestionTest(unittest.TestCase):
    def test_hard_by_any_position(self):
        # 被谁强克都算（黑鸟怕的小强是四号位）；差一点不算
        self.assertEqual(sug(AXE, 0, [(PL, 0.01), (CM, -AVOID - 0.01)], cores=frozenset({PL.id})).hard_by[0], CM)
        self.assertIsNone(sug(AXE, 0, [(CM, -AVOID + 0.01)]).hard_by)
        self.assertIsNone(sug(AXE, 0, []).hard_by)

    def test_hard_over_only_cores(self):
        s = sug(TIDE, 0, [(CM, HARD + 0.05), (PL, HARD + 0.01)])
        self.assertEqual([h for h, _ in s.hard_over], [CM, PL])          # 不知道位置：都算，从大到小
        s.cores = frozenset({PL.id})                                    # 水晶室女是辅助：克她不算强克
        self.assertEqual([h for h, _ in s.hard_over], [PL])
        tags = dict(s.reason_parts())
        self.assertIn("强克 幻影长矛手", tags["hard"])
        self.assertIn("克 水晶室女", tags["good"])                     # 克辅助照常写在「克」里

    def test_drop_and_order(self):
        a = sug(AXE, 0.30, [(PL, -AVOID - 0.01)])
        t = sug(TIDE, 0.05, [(PL, HARD + 0.01)], base=0.0)
        w = sug(WK, 0.20, [(PL, 0.10)], base=0.0)                      # 克制 + 配合比潮汐大，但不是强克
        keep, dropped = adv.drop_countered([a, w, t])
        self.assertEqual((keep, dropped), ([w, t], [a]))
        self.assertEqual(adv._by_situational(keep), [t, w])            # 强克的排前面


class AdviseTest(unittest.TestCase):
    def advisor(self):
        a = fake_advisor(Config(position_filter=False, top=2, top_other=2, familiar_games=20))
        # 斧王被幻影长矛手克 4.7 个点（按双方强度扣掉期望后），潮汐克幻影长矛手 4 个点以上
        a.engine = Engine(HeroDB(HEROES), make_stats({(TIDE.id, PL.id): (20000, 0.56), (AXE.id, PL.id): (20000, 0.40)}))
        return a

    def test_countered_not_recommended(self):
        a = self.advisor()
        advice = a.advise(adv.Draft(enemies=[PL]), 1)
        self.assertNotIn(AXE, [s.hero for s in advice.shown()])
        self.assertEqual([s.hero for s in advice.countered], [AXE])
        view = adv.render_view(advice, None)
        self.assertEqual(view["countered"], [{"id": AXE.id, "name": AXE.name_cn,
                                              "by": {"id": PL.id, "name": PL.name_cn}, "delta": -4.7}])
        self.assertTrue(any("被克太狠，没推荐：斧王（怕 幻影长矛手 -4.7%）" in t for _, t in adv.render_lines(advice, None)))
        tide = next(s for s in advice.shown() if s.hero == TIDE)
        self.assertEqual(tide.reason_parts()[0][0], "hard")
        # 剩余位置给队友的也不推荐被强克的
        advice = a.advise(adv.Draft(allies=[CM], enemies=[PL]), 1)
        self.assertNotIn(AXE, [s.hero for t in advice.team for s in t.picks + t.targeted])

    def test_enemy_support_not_hard_target(self):
        # 玩家把幻影长矛手定成五号位：潮汐克他不再算「强克」，但斧王照样因为被他强克不推荐
        advice = self.advisor().advise(adv.Draft(enemies=[PL], enemy_pos={PL.id: 5}), 1)
        tide = next(s for s in advice.shown() if s.hero == TIDE)
        self.assertEqual(tide.hard_over, [])
        self.assertEqual([s.hero for s in advice.countered], [AXE])


if __name__ == "__main__":
    unittest.main()
