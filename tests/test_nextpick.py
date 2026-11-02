"""猜对面最后一手（domain/nextpick.py）+ 假设对面拿了它给我的推荐（advisor.next_guesses）。人造数据，不联网、不碰 data/。"""
import math
import unittest

from dota_assistant.core.config import Config
from dota_assistant.core.heroes import Hero, HeroDB
from dota_assistant.domain import advisor as adv
from dota_assistant.domain import nextpick
from dota_assistant.domain.engine import Engine, Suggestion

H = [Hero(300 + i, f"h{i}", f"英雄{i}", f"Hero{i}", (), "str") for i in range(14)]
OURS, THEIRS = H[0:4], H[4:8]               # 我方已选 4 个、对面已选 4 个
SUPPORT, SUPPORT2, CORE = H[8], H[9], H[10]  # 对面还能选的：两个五号位（一样流行）、一个一号位


def stats(over=None):
    """所有对位 10000 局 50%，over = {(a, b): a 对 b 的胜率}"""
    vs = {(a.id, b.id): (10000, 5000) for a in H for b in H if a != b}
    for (a, b), wr in (over or {}).items():
        vs[(a, b)] = (10000, round(10000 * wr))
        vs[(b, a)] = (10000, 10000 - round(10000 * wr))
    return {"matches": 1, "vs": [[a, b, g, w] for (a, b), (g, w) in vs.items()], "syn": []}


def positions():
    """对面已选 4 个各打 1~4 号位，SUPPORT / SUPPORT2 只打五号位，其余英雄五个位置一样多"""
    pos = {h.id: {p: (1000, 500) for p in range(1, 6)} for h in H}
    for i, h in enumerate(THEIRS):
        pos[h.id] = {i + 1: (5000, 2500)}
    pos[SUPPORT.id] = pos[SUPPORT2.id] = {5: (5000, 2500)}
    pos[CORE.id] = {1: (5000, 2500)}
    return pos


def engine(over=None, pos=True):
    return Engine(HeroDB(H), stats(over), positions() if pos else None)


def ids(hs):
    return [h.id for h in hs]


class NextPickTest(unittest.TestCase):
    def test_open_position(self):
        q = nextpick.open_positions(ids(THEIRS), positions())
        self.assertGreater(q[5], 0.99)                                  # 1~4 号位都有人了，剩五号位
        self.assertAlmostEqual(sum(q.values()), 1.0)
        self.assertEqual(nextpick.open_positions(ids(THEIRS), None), {p: 0.2 for p in range(1, 6)})

    def test_predict_by_open_position(self):
        taken = set(ids(OURS + THEIRS))
        res = nextpick.predict(engine(), ids(THEIRS), ids(OURS), taken)
        self.assertFalse(taken & {h for h, _ in res})                   # 选过的不会再猜
        self.assertAlmostEqual(sum(p for _, p in res), 1.0)
        self.assertEqual({h for h, _ in res[:2]}, {SUPPORT.id, SUPPORT2.id})   # 缺五号位：只打五号位的两个排前面
        self.assertLess(dict(res)[CORE.id], 0.01)                       # 一号位英雄几乎不可能
        # 被禁 / 被选的不猜
        res = nextpick.predict(engine(), ids(THEIRS), ids(OURS), taken | {SUPPORT.id})
        self.assertEqual(res[0][0], SUPPORT2.id)

    def test_counter_raises_probability(self):
        # SUPPORT2 克我方 4 人：一样流行时排到 SUPPORT 前面，概率比例 = exp(W_ADV × 克制合计)
        eng = engine({(SUPPORT2.id, o.id): 0.55 for o in OURS})
        res = dict(nextpick.predict(eng, ids(THEIRS), ids(OURS), set(ids(OURS + THEIRS))))
        adv_sum = lambda h: sum(eng.advantage(h.id, o.id) for o in OURS)
        self.assertAlmostEqual(res[SUPPORT2.id] / res[SUPPORT.id],
                               math.exp(nextpick.W_ADV * (adv_sum(SUPPORT2) - adv_sum(SUPPORT))), places=6)
        self.assertGreater(res[SUPPORT2.id], res[SUPPORT.id])

    def test_no_positions(self):
        # 没有分位置数据：只按流行度（这里都一样）
        res = nextpick.predict(engine(pos=False), ids(THEIRS), ids(OURS), set(ids(OURS + THEIRS)))
        self.assertAlmostEqual(res[0][1], res[-1][1])


def advisor(cfg=None, career=None, over=None):
    a = adv.Advisor.__new__(adv.Advisor)
    a.cfg = cfg or Config(position_filter=False, top=1, top_other=1)
    a.db, a.me, a.pool, a.kb, a.duo = HeroDB(H), None, [], None, None
    a.career = career
    a.engine = engine(over)
    return a


class AdviseNextTest(unittest.TestCase):
    def test_only_when_i_am_last_pick(self):
        a = advisor()
        self.assertTrue(a.advise(adv.Draft(OURS, THEIRS), 3).next)
        self.assertEqual(a.advise(adv.Draft(OURS[:3], THEIRS), 3).next, [])          # 队友还没选完
        self.assertEqual(a.advise(adv.Draft(OURS, [*THEIRS, CORE]), 3).next, [])     # 对面选完了
        self.assertEqual(a.advise(adv.Draft(OURS, THEIRS, my_hero=H[11]), 3).next, [])   # 我选好了
        self.assertIsNone(adv.render_view(a.advise(adv.Draft(OURS[:3], THEIRS), 3), None)["next"])

    def test_picks_assume_the_guess(self):
        # H[12] 克 SUPPORT：假设对面拿 SUPPORT 时 H[12] 排第一，对位里有 SUPPORT
        a = advisor(over={(H[12].id, SUPPORT.id): 0.6})
        advice = a.advise(adv.Draft(OURS, THEIRS), 3)
        self.assertEqual(len(advice.next), nextpick.SHOW)
        g = next(g for g in advice.next if g.hero == SUPPORT)
        self.assertEqual(g.picks[0].hero, H[12])
        self.assertEqual([e for e, _ in g.picks[0].vs], [*THEIRS, SUPPORT])
        self.assertNotIn(SUPPORT, [s.hero for s in g.picks])            # 假设被对面拿了，不会推荐给我
        self.assertEqual(len(g.picks), adv.NEXT_TOP + adv.NEXT_OTHER)   # 不分熟不熟：前 NEXT_TOP + NEXT_OTHER 个，不跟 cfg.top
        v = adv.render_view(advice, None)["next"]
        self.assertEqual(v["title"], adv.NEXT_TITLE)
        self.assertEqual([x["id"] for x in v["guesses"]], [g.hero.id for g in advice.next])
        sv = next(x for x in v["guesses"] if x["id"] == SUPPORT.id)
        self.assertEqual(sv["items"][0]["id"], H[12].id)
        self.assertEqual(sv["items"][0]["vs"][-1]["id"], SUPPORT.id)
        self.assertAlmostEqual(sum(x["prob"] for x in v["guesses"]), v["cover"], delta=1)
        lines = [t for _, t in adv.render_lines(advice, None)]
        self.assertTrue(any(t.startswith(adv.NEXT_TITLE) for t in lines))
        self.assertIn(f"假设对面拿{advice.next[0].hero.name_cn}", lines)

    def test_skip_applies(self):
        # 「这局不想玩」的在假设里也不推荐
        a = advisor(over={(H[12].id, SUPPORT.id): 0.6})
        advice = a.advise(adv.Draft(OURS, THEIRS, skip=[H[12]]), 3)
        self.assertNotIn(H[12], [s.hero for g in advice.next for s in g.picks])

    def test_not_next(self):
        # 头像上点 × 排除的：不再猜它，后面的补上来，概率在剩下的里重新归一化；视图列出来能恢复；不进 key()
        a = advisor()
        before = a.advise(adv.Draft(OURS, THEIRS), 3).next
        first = before[0].hero
        advice = a.advise(adv.Draft(OURS, THEIRS, not_next=[first]), 3)
        self.assertNotIn(first, [g.hero for g in advice.next])
        self.assertEqual(advice.next[0].hero, before[1].hero)
        self.assertEqual(len(advice.next), nextpick.SHOW)
        self.assertGreater(advice.next[0].prob, before[1].prob)
        self.assertEqual(adv.render_view(advice, None)["next"]["excluded"], [adv.hero_ref(first)])
        self.assertIn(first, [s.hero for s in a._suggest(adv.Draft(OURS, THEIRS, not_next=[first]), 3)])   # 不算禁用，我照样能选它
        self.assertEqual(adv.Draft(OURS, THEIRS).key(), adv.Draft(OURS, THEIRS, not_next=[first]).key())


class SplitNextTest(unittest.TestCase):
    F = [Hero(400 + i, f"f{i}", f"熟{i}", f"F{i}", (), "str") for i in range(3)]
    U = [Hero(500 + i, f"u{i}", f"生{i}", f"U{i}", (), "str") for i in range(3)]
    RES = sorted([Suggestion(F[0], 0.10, 0), Suggestion(F[1], 0.05, 0), Suggestion(F[2], -0.1, 0),
                  Suggestion(U[0], 0.30, 0), Suggestion(U[1], 0.2, 0), Suggestion(U[2], -0.05, 0)], key=lambda s: -s.score)

    def names(self, res):
        return [s.hero.name_cn for s in res]

    def test_split(self):
        fam = {h.id for h in self.F}
        self.assertEqual(self.names(adv.split_next(self.RES, 2, 1, fam)), ["熟0", "熟1", "生0"])
        self.assertEqual(self.names(adv.split_next(self.RES, 1, 3, fam)), ["熟0", "生0", "生1"])   # 不到 50% 的不熟不进
        self.assertEqual(self.names(adv.split_next(self.RES, 2, 1, None)), ["生0", "生1", "熟0"])  # 不分：前 top + other 个
        self.assertEqual(adv.split_next(self.RES, 2, 1, set()), adv.split_next(self.RES, 2, 1, None))


if __name__ == "__main__":
    unittest.main()
