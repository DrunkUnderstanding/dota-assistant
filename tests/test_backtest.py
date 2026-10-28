"""回测重放逻辑：人造数据，不联网。"""
import unittest

from dota_assistant.research.backtest import Tables, replay_picks, usable
from dota_assistant.domain.engine import Engine
from dota_assistant.core.heroes import HeroDB
from tests.test_engine import AXE, CM, HEROES, PL, TIDE, WK, make_stats


def match(picks, positions, radiant_win=True):
    """picks = [(英雄, 是否天辉)]，按选人顺序；positions = {英雄 id: 位置}"""
    return {
        "didRadiantWin": radiant_win,
        "pickBans": [{"isPick": True, "heroId": h.id, "order": i, "isRadiant": r} for i, (h, r) in enumerate(picks)],
        "players": [{"heroId": h.id, "isRadiant": r, "position": f"POSITION_{positions[h.id]}"} for h, r in picks],
    }


class BacktestTest(unittest.TestCase):
    def test_rank_uses_only_heroes_seen_before(self):
        # 潮汐克幻影长矛手：长矛手先出，后手的潮汐应排第一；先手的长矛手看不到潮汐，不受影响
        eng = Engine(HeroDB(HEROES), make_stats({(TIDE.id, PL.id): (20000, 0.6)}),
                     {h.id: {3: (1000, 500)} for h in HEROES})
        m = match([(PL, False), (TIDE, True), (AXE, True), (CM, False), (WK, True)],
                  {h.id: 3 for h in HEROES})
        rows = replay_picks(Tables(eng), [m])
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[1].rank, 1)                  # 潮汐
        self.assertAlmostEqual(rows[1].regret, 0)
        self.assertTrue(rows[1].won)
        self.assertFalse(rows[0].won)                      # 长矛手在夜魇，天辉赢了

    def test_tables_match_engine(self):
        # 回测用的矩阵打分必须和 Engine.suggest 一致（含对线加权、反制风险），不然回测测的不是同一个东西
        from dota_assistant.domain import engine as E
        positions = {AXE.id: {3: (900, 450), 1: (100, 50)}, PL.id: {1: (1000, 520)}, TIDE.id: {3: (1000, 530)},
                     CM.id: {5: (1000, 500)}, WK.id: {1: (500, 260), 3: (500, 240)}}
        eng = Engine(HeroDB(HEROES), make_stats({(TIDE.id, PL.id): (20000, 0.58), (AXE.id, CM.id): (3000, 0.53)}),
                     positions)
        t = Tables(eng)
        old = E.W_LANE, E.W_RISK
        E.W_LANE, E.W_RISK = 0.8, 0.2
        try:
            got = {s.hero.id: s.score for s in eng.suggest(position=1, enemies=[TIDE, CM], position_filter=False,
                                                          top=None)}
            arr = t.scores(1, [], [TIDE.id, CM.id], [TIDE.id, CM.id])
            for hid, sc in got.items():
                self.assertAlmostEqual(arr[t.idx[hid]], sc, places=6)
        finally:
            E.W_LANE, E.W_RISK = old

    def test_usable_requires_full_draft_and_positions(self):
        m = match([(h, i % 2 == 0) for i, h in enumerate(HEROES)], {h.id: 1 for h in HEROES})
        self.assertFalse(usable(m))                         # 只有 5 手
        self.assertFalse(usable(None))


if __name__ == "__main__":
    unittest.main()
