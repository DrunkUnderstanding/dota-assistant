"""叠加层上点英雄改位置（Live.set_hero_pos → Draft.enemy_pos / ally_pos / manual_pos）：分路、阵容胜率、猜对面最后一手都按改的来。
人造数据，不联网、不碰 data/。"""
import json
import unittest

from dota_assistant.domain import advisor as adv
from dota_assistant.domain import lanes, nextpick
from tests.test_engine import AXE, CM, PL, TIDE, WK
from tests.test_nextpick import CORE, OURS, SUPPORT, SUPPORT2, THEIRS, advisor, ids, positions
from tests.test_webui import make_live, wait


class DomainTest(unittest.TestCase):
    def test_open_positions_respect_fixed(self):
        their = ids(THEIRS)                                             # 平时各打 1~4 号位，剩五号位
        self.assertGreater(nextpick.open_positions(their, positions())[5], 0.99)
        # 玩家说打 1 号位的那个其实是 5 号位：空出来的就是 1 号位
        q = nextpick.open_positions(their, positions(), {THEIRS[0].id: 5})
        self.assertGreater(q[1], 0.99)
        self.assertAlmostEqual(sum(q.values()), 1.0)
        self.assertEqual(nextpick.open_positions(their, None, {h: p for h, p in zip(their, (1, 2, 3, 4))})[5], 1.0)

    def test_guess_follows_fixed_open_position(self):
        a = advisor()
        draft = adv.Draft(OURS, THEIRS)
        self.assertIn(a.advise(draft, 3).next[0].hero, (SUPPORT, SUPPORT2))   # 平时：空的是五号位
        moved = adv.Draft(OURS, THEIRS, enemy_pos={THEIRS[0].id: 5}, manual_pos={THEIRS[0].id: 5})
        advice = a.advise(moved, 3)
        self.assertEqual(advice.next[0].hero, CORE)                    # 空的是 1 号位：猜只打一号位的那个
        self.assertGreater(advice.next_open[1], 0.99)
        v = adv.render_view(advice, None)
        self.assertEqual(v["next"]["open"][0]["position"], 1)
        self.assertNotEqual(draft.key(), moved.key())                  # 改了位置算局面变了（分路 / 「如果选它」跟着变）

    def test_lanes_and_manual_flag(self):
        pos = {PL.id: {1: (900, 0)}, AXE.id: {3: (900, 0)}, CM.id: {5: (900, 0)}}
        lane = lanes.assign([PL.id, AXE.id], [CM.id], 1, None, pos, enemy_pos={PL.id: 2})
        self.assertEqual({g.hero: g.pos for g in lane.enemies}, {PL.id: 2, AXE.id: 3})
        v = lanes.view(lane, lambda i: {"id": i, "name": str(i)}, manual={PL.id})
        heroes = {h["id"]: h for r in v["rows"] for h in r["heroes"]}
        self.assertTrue(heroes[PL.id]["manual"] and heroes[PL.id]["sure"])
        self.assertFalse(heroes[AXE.id]["manual"])


class LivePosTest(unittest.TestCase):
    def setUp(self):
        self.live = make_live()
        self.live.slots["radiant"][:2] = ["斧王", "水晶室女"]          # 没 GSI：天辉当我方
        self.live.slots["dire"][:2] = ["幻影长矛手", "潮汐猎人"]
        self.live.start()
        wait(self.live.submit(self.live.refresh))

    def tearDown(self):
        self.live.close()

    def act(self, hero_id, pos):
        wait(self.live.submit(self.live.action, {"type": "set_hero_pos", "hero_id": hero_id, "pos": pos}))

    def draft(self):
        return wait(self.live.submit(self.live.current_draft))

    def test_set_swap_reset(self):
        self.act(PL.id, 3)
        self.act(AXE.id, 4)
        d = self.draft()
        self.assertEqual(d.enemy_pos, {PL.id: 3})
        self.assertEqual(d.ally_pos, {AXE.id: 4})
        self.assertEqual(d.manual_pos, {PL.id: 3, AXE.id: 4})
        self.act(TIDE.id, 3)                                            # 同一边已经定在 3 号位的改回自动猜
        self.assertEqual(self.draft().enemy_pos, {TIDE.id: 3})
        self.act(TIDE.id, 0)                                            # 改回自动猜
        self.assertEqual(self.draft().manual_pos, {AXE.id: 4})
        with self.assertRaises(ValueError):
            self.live.set_hero_pos(WK.id, 2)                            # 这局阵容里没有
        with self.assertRaises(ValueError):
            self.live.set_hero_pos(PL.id, 6)
        self.act(PL.id, 2)
        wait(self.live.submit(self.live.clear))
        self.assertEqual(self.live.hero_pos, {})

    def test_manual_beats_ocr(self):
        self.live.team_roles = [3, 5, None, None, None]                # 认出来：斧王 3 号位、水晶室女 5 号位
        self.assertEqual(self.draft().ally_pos, {AXE.id: 3, CM.id: 5})
        self.act(AXE.id, 5)                                            # 玩家说斧王打 5：认出来的水晶室女 5 号位不算了
        self.assertEqual(self.draft().ally_pos, {AXE.id: 5})

    def test_view_marks_manual(self):
        self.act(CM.id, 4)
        wait(self.live.submit(lambda: None))
        v = json.loads(self.live.current_snapshot)["overlay"]["view"]
        ally = next(t["ally"] for t in v["team"] if t["ally"] and t["ally"]["id"] == CM.id)
        self.assertTrue(ally["manual"])
        self.assertEqual(next(t["position"] for t in v["team"] if t["ally"] and t["ally"]["id"] == CM.id), 4)


if __name__ == "__main__":
    unittest.main()
