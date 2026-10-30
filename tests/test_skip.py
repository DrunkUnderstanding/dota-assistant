"""推荐卡片上点 ×「这局不想玩」：从给我的推荐里去掉、后面的顺位补上来，点名字恢复，新一局清空。不联网、不碰 data/。"""
import json
import unittest

from dota_assistant.core.config import Config
from dota_assistant.domain import advisor as adv
from tests.test_engine import AXE, CM, PL, TIDE
from tests.test_webui import fake_advisor, make_live, wait


class AdvisorSkipTest(unittest.TestCase):
    def test_next_one_moves_up(self):
        a = fake_advisor(Config(position_filter=False, top=2, top_other=0))
        before = [s.hero for s in a.advise(adv.Draft(enemies=[PL]), 3).picks]
        advice = a.advise(adv.Draft(enemies=[PL], skip=[before[0]]), 3)
        self.assertNotIn(before[0], [s.hero for s in advice.picks + advice.targeted])
        self.assertEqual([s.hero for s in advice.picks][0], before[1])      # 第二名顺位到第一
        self.assertEqual(len(advice.picks), 2)                              # 名额不变，后面补上一个
        v = adv.render_view(advice, None)
        self.assertEqual(v["skipped"], [adv.hero_ref(before[0])])
        self.assertNotIn(before[0], v["bans"])                              # 不算禁用

    def test_not_a_ban_for_teammates(self):
        # 我不想玩潮汐，队友空位置照样能推荐潮汐
        a = fake_advisor(Config(position_filter=False, top=1, top_other=0))
        advice = a.advise(adv.Draft(allies=[CM], enemies=[PL], skip=[TIDE]), 3)
        self.assertNotIn(TIDE, [s.hero for s in advice.picks + advice.targeted])
        mates = [s.hero for t in advice.team for s in t.picks + t.targeted]
        self.assertIn(TIDE, mates)

    def test_key_ignores_skip(self):
        # 局面没变：「如果选它」的卡片不算过期
        self.assertEqual(adv.Draft(enemies=[PL]).key(), adv.Draft(enemies=[PL], skip=[AXE]).key())


class LiveSkipTest(unittest.TestCase):
    def setUp(self):
        self.live = make_live()
        self.live.slots["dire"][:1] = ["幻影长矛手"]
        self.live.start()
        wait(self.live.submit(self.live.refresh))

    def tearDown(self):
        self.live.close()

    def view(self):
        wait(self.live.submit(lambda: None))
        return json.loads(self.live.current_snapshot)["overlay"]["view"]

    def shown(self):
        return [it["id"] for g in self.view()["groups"] for it in g["items"]]

    def act(self, t, hero_id):
        wait(self.live.submit(self.live.action, {"type": t, "hero_id": hero_id}))

    def test_skip_unskip_and_new_draft(self):
        first = self.shown()[0]
        self.act("skip_hero", first)
        self.assertNotIn(first, self.shown())
        self.assertEqual([h["id"] for h in self.view()["skipped"]], [first])
        self.act("skip_hero", first)                                        # 点两次不重复
        self.assertEqual(self.live.skipped, [first])
        self.act("unskip_hero", first)
        self.assertEqual(self.shown()[0], first)
        self.assertEqual(self.view()["skipped"], [])

        self.act("skip_hero", first)
        wait(self.live.submit(self.live.clear))                             # 清空 = 新一局
        self.assertEqual(self.live.skipped, [])
        with self.assertRaises(ValueError):
            self.live.skip_hero(999999)

    def test_picked_by_someone_drops_from_list(self):
        # 不想玩的被别人选了：不再列在「这局不想玩」里
        self.act("skip_hero", AXE.id)
        wait(self.live.submit(self.live.action, {"type": "set_slot", "side": "dire", "index": 1, "text": "斧王"}))
        self.assertEqual(self.view()["skipped"], [])


if __name__ == "__main__":
    unittest.main()
