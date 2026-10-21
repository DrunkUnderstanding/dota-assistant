"""打分引擎回归：用人造数据，不联网。python -m unittest"""
import unittest

from dota_assistant.engine import Engine, pct
from dota_assistant.heroes import Hero, HeroDB

AXE = Hero(2, "axe", "斧王", "Axe", ("Initiator", "Durable", "Carry"), "str")
PL = Hero(12, "phantom_lancer", "幻影长矛手", "Phantom Lancer", ("Carry", "Escape"), "agi")
TIDE = Hero(29, "tidehunter", "潮汐猎人", "Tidehunter", ("Initiator", "Durable"), "str")
CM = Hero(5, "crystal_maiden", "水晶室女", "Crystal Maiden", ("Support", "Disabler"), "int")
WK = Hero(42, "skeleton_king", "冥魂大帝", "Wraith King", ("Carry", "Support", "Durable"), "str")
HEROES = [AXE, PL, TIDE, CM, WK]


def make_stats(overrides):
    """所有对位默认 10000 局 50%，再按 overrides 改：{(a, b): (局数, a 胜率)}"""
    vs = {}
    for a in HEROES:
        for b in HEROES:
            if a.id != b.id:
                vs[(a.id, b.id)] = (10000, 5000)
    for (a, b), (g, wr) in overrides.items():
        vs[(a, b)] = (g, round(g * wr))
        vs[(b, a)] = (g, g - round(g * wr))
    return {"matches": 1, "vs": [[a, b, g, w] for (a, b), (g, w) in vs.items()], "syn": []}


class EngineTest(unittest.TestCase):
    def test_counter_ranks_first(self):
        eng = Engine(HeroDB(HEROES), make_stats({(TIDE.id, PL.id): (20000, 0.56)}))
        res = eng.suggest(position=3, enemies=[PL], position_filter=False)
        self.assertEqual(res[0].hero, TIDE)
        self.assertGreater(eng.advantage(TIDE.id, PL.id), 0)

    def test_small_sample_is_shrunk(self):
        small = Engine(HeroDB(HEROES), make_stats({(AXE.id, PL.id): (20, 0.8)}))
        big = Engine(HeroDB(HEROES), make_stats({(AXE.id, PL.id): (20000, 0.8)}))
        # 同样 80% 胜率，20 局的结论应远弱于 2 万局
        self.assertLess(small.advantage(AXE.id, PL.id), big.advantage(AXE.id, PL.id) / 5)

    def test_taken_heroes_excluded(self):
        eng = Engine(HeroDB(HEROES), make_stats({}))
        res = eng.suggest(position=1, enemies=[PL], allies=[CM], bans=[AXE], position_filter=False)
        self.assertEqual({s.hero for s in res}, {TIDE, WK})

    def test_position_filter(self):
        self.assertTrue(Engine.fits_by_roles(CM, 5))
        self.assertFalse(Engine.fits_by_roles(WK, 5))     # Carry 打头的不算五号位
        self.assertFalse(Engine.fits_by_roles(AXE, 1))    # Carry 只是附带标签
        self.assertTrue(Engine.fits_by_roles(AXE, 3))
        self.assertFalse(Engine.fits_by_roles(PL, 3))     # Carry 打头又不带 Initiator 的不算三号位

    def test_familiar_hero_and_personal_winrate(self):
        eng = Engine(HeroDB(HEROES), make_stats({}))

        def order(history):
            res = eng.suggest(position=1, position_filter=False, top=None, history=history)
            return [s.hero for s in res], res

        heroes, res = order({TIDE.id: (20, 14), WK.id: (20, 9), CM.id: (1, 1)})
        self.assertLess(heroes.index(TIDE), heroes.index(WK))   # 同样熟练，赢得多的排前
        self.assertLess(heroes.index(WK), heroes.index(AXE))    # 略低于平均但熟练，仍排在没用过的前面
        self.assertFalse(next(s for s in res if s.hero == CM).in_pool)  # 1 局不算你的英雄

        heroes, _ = order({WK.id: (20, 4)})
        self.assertGreater(heroes.index(WK), heroes.index(AXE))  # 20 局只赢 4 局，熟练度救不回来

    def test_position_data_overrides_roles(self):
        # 冥魂大帝在数据里主要打三号位，标签却是 Carry 打头
        positions = {WK.id: {1: (3000, 1500), 3: (5000, 2750), 5: (30, 15)},
                     AXE.id: {3: (15000, 7500), 1: (50, 20)}}
        eng = Engine(HeroDB(HEROES), make_stats({}), positions)
        self.assertTrue(eng.fits(WK, 3))
        self.assertFalse(eng.fits(WK, 5))
        self.assertFalse(eng.fits(AXE, 1))
        self.assertTrue(eng.fits(CM, 5))                       # 没有分位置数据的退回标签
        self.assertGreater(eng.base_at(WK.id, 3), eng.base_at(WK.id, 1))  # 三号位胜率 55% > 一号位 50%

    def test_history_by_position_bypasses_filter(self):
        eng = Engine(HeroDB(HEROES), make_stats({}), {CM.id: {5: (10000, 5000)}})
        hist = {CM.id: (10, 5)}
        self.assertNotIn(CM, [s.hero for s in eng.suggest(position=1, history=hist, top=None)])
        self.assertIn(CM, [s.hero for s in eng.suggest(position=1, history=hist, history_by_position=True,
                                                       top=None)])

    def test_lookup_by_alias(self):
        db = HeroDB(HEROES)
        self.assertEqual(db.find("潮汐"), TIDE)
        self.assertEqual(db.find("pl"), PL)
        self.assertEqual(db.find("Wraith King"), WK)


if __name__ == "__main__":
    unittest.main()
