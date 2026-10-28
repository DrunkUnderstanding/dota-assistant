"""GSI 服务与播报文案。不联网、不出声。"""
import json
import socket
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from dota_assistant.domain import advisor as adv
from dota_assistant.game import gsi
from dota_assistant.core import dota_install
from dota_assistant.domain.engine import Engine, Suggestion
from dota_assistant.game.gsi import GSIServer
from dota_assistant.core.heroes import HeroDB
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


def make_dota(lib: Path, real: bool = True) -> Path:
    """假 Steam 库里的 dota 2 beta；real=False 是挪库 / 卸载留下的空壳（只有 game\\dota\\cfg）"""
    d = lib / "steamapps" / "common" / "dota 2 beta"
    (d / "game" / "dota" / "cfg").mkdir(parents=True)
    if real:
        (d / "game" / "dota" / "pak01_dir.vpk").write_bytes(b"")
    return d


class DotaDirTest(unittest.TestCase):
    """2026-10-27 朋友的电脑：Steam 根目录下有个只剩 cfg 的 dota 2 beta 空壳，被当成 Dota，GSI 配置写进去游戏不读"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dota-dir-"))
        self.shell = make_dota(self.tmp / "steam", real=False)
        self.real = make_dota(self.tmp / "lib2")
        self.libs = [self.tmp / "steam", self.tmp / "lib2"]

    def test_skips_shell(self):
        self.assertFalse(dota_install.is_dota_dir(self.shell))
        self.assertEqual(dota_install.find_dota("", self.libs), self.real)
        self.assertIsNone(dota_install.find_dota("", self.libs[:1]))

    def test_configured_dir_first(self):
        other = make_dota(self.tmp / "lib3")
        self.assertEqual(dota_install.find_dota(str(other), self.libs), other)
        self.assertEqual(dota_install.find_dota(str(self.shell), self.libs), self.real)   # 填错了照样自动找

    def test_cfg_only_into_real_install(self):
        with self.assertRaises(FileNotFoundError):
            gsi.install_cfg(self.shell, 3000)
        path = gsi.ensure_cfg(self.real, 3000)
        self.assertEqual(path, gsi.cfg_path(self.real))
        self.assertIn("127.0.0.1:3000", path.read_text(encoding="utf-8"))
        self.assertIsNone(gsi.ensure_cfg(self.real, 3000))                       # 没变不重写
        self.assertEqual(gsi.ensure_cfg(self.real, 3001), path)                  # 端口变了重写


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
