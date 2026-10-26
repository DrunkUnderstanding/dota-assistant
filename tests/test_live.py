"""GSI 服务与播报文案。不联网、不出声。"""
import json
import socket
import time
import unittest
import urllib.request

from dota_assistant import advisor as adv
from dota_assistant.engine import Engine, Suggestion
from dota_assistant.gsi import GSIServer
from dota_assistant.heroes import HeroDB
from tests.test_engine import AXE, CM, HEROES, PL, TIDE, make_stats


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class GSITest(unittest.TestCase):
    def test_parses_post(self):
        port = free_port()
        srv = GSIServer(port).start()
        try:
            self.assertFalse(srv.state.alive)
            body = {"map": {"game_state": "DOTA_GAMERULES_STATE_HERO_SELECTION"},
                    "player": {"team_name": "dire"}, "hero": {"name": "npc_dota_hero_axe"}}
            req = urllib.request.Request(f"http://127.0.0.1:{port}/", data=json.dumps(body).encode(),
                                         method="POST", headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
            for _ in range(50):
                if srv.state.alive:
                    break
                time.sleep(0.02)
            self.assertTrue(srv.state.in_draft)
            self.assertEqual(srv.state.team, "dire")
            self.assertEqual(srv.state.my_hero_key, "axe")
        finally:
            srv.stop()
            srv.server.server_close()


class SpeechTextTest(unittest.TestCase):
    def advice(self, draft):
        eng = Engine(HeroDB(HEROES), make_stats({}))
        picks = [Suggestion(TIDE, 0.1, 0.1, in_pool=True)]
        targeted = [Suggestion(CM, 0.0, 0.0)]
        return adv.Advice(1, draft, picks if not draft.my_hero else [], targeted if not draft.my_hero else [], None)

    def test_new_enemy_announced_with_top_two(self):
        prev = adv.Draft(enemies=[PL])
        cur = adv.Draft(enemies=[PL, AXE])
        self.assertEqual(adv.speech(prev, self.advice(cur)), "对面拿了斧王。推荐潮汐猎人，或者水晶室女")

    def test_nothing_new_is_silent_unless_forced(self):
        d = adv.Draft(enemies=[PL])
        self.assertIsNone(adv.speech(d, self.advice(d)))
        self.assertEqual(adv.speech(d, self.advice(d), force=True), "推荐潮汐猎人，或者水晶室女")

    def test_after_my_pick_no_recommendation(self):
        prev = adv.Draft(enemies=[PL], my_hero=TIDE)
        cur = adv.Draft(enemies=[PL, AXE], my_hero=TIDE)
        self.assertEqual(adv.speech(prev, self.advice(cur)), "对面拿了斧王")


if __name__ == "__main__":
    unittest.main()
