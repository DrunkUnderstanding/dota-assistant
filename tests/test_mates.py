"""队友战绩：好友代码解析、联网结果整理（假接口）、记住查过的人、Live 里查 / 刷新 / 删。不联网。"""
import itertools
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import requests

from dota_assistant import data, mates
from dota_assistant.core.config import Config
from dota_assistant.heroes import HeroDB
from tests.test_engine import AXE, HEROES, PL
from tests.test_webui import make_live

TMP = Path(tempfile.mkdtemp(prefix="dota-mates-"))
ME, MATE = 1001, 2002
SEQ = itertools.count()   # 文件名编号：Windows 上 monotonic_ns 精度约 15 毫秒，两个测试会撞名


def m(hero, win, t=1_790_000_000, ranked=True):
    """OpenDota /players/{id}/matches 的一行：天辉（slot 0），赢没赢看 radiant_win"""
    return {"hero_id": hero, "player_slot": 0, "radiant_win": win, "start_time": t,
            "lobby_type": 7 if ranked else 0, "kills": 5, "deaths": 2, "assists": 9}


def fake_get(matches, profile=True, peers=()):
    calls = []

    def get(url, **params):
        calls.append((url, params))
        if url.endswith(f"/players/{MATE}"):
            return {"profile": {"personaname": "<b>队友</b>"} if profile else None,
                    "rank_tier": 74, "leaderboard_rank": None}
        if url.endswith("/matches"):
            return matches
        if url.endswith("/peers"):
            return list(peers)
        raise AssertionError(url)
    get.calls = calls
    return get


class ParseCodeTest(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(mates.parse_code(" 123456789 "), 123456789)
        self.assertEqual(mates.parse_code(str(mates.STEAM64_BASE + 42)), 42)          # Steam 64 位 ID
        self.assertEqual(mates.parse_code("https://www.opendota.com/players/86745912/matches"), 86745912)
        self.assertEqual(mates.parse_code("https://stratz.com/players/86745912"), 86745912)

    def test_bad(self):
        for bad in ("", "abc", "0", str(2 ** 33)):
            with self.assertRaises(ValueError):
                mates.parse_code(bad)


class FetchTest(unittest.TestCase):
    def test_summary(self):
        ms = [m(AXE.id, True)] * 8 + [m(PL.id, False)] * 4 + [m(AXE.id, False, ranked=False)] * 30
        get = fake_get(ms, peers=[{"account_id": 7, "with_games": 99, "with_win": 50},
                                  {"account_id": ME, "with_games": 10, "with_win": 7, "against_games": 40}])
        ranked = [(AXE.id, 3, True)] * 6 + [(PL.id, 1, False)] * 2 + [(AXE.id, 0, True)] * 2
        with mock.patch.object(data, "_get", get), \
                mock.patch.object(data, "_player_matches_stratz", return_value=ranked) as st:
            d = mates.fetch(Config(account_id=ME, stratz_token="t"), MATE)
        self.assertEqual(st.call_args.args[1:], (MATE, mates.SAMPLE))
        self.assertEqual(len(d["matches"]), mates.RECENT)
        self.assertEqual(sum(x["win"] for x in d["matches"]), 8)
        self.assertEqual(d["heroes"][0], [AXE.id, 38, 8])                  # 按局数排
        self.assertEqual(d["with_me"], [10, 7])                            # 只算同队的，对面的 40 局不算
        self.assertEqual(d["positions"], [[3, 6, 6], [1, 2, 0]])           # 未解析（0）不算
        self.assertEqual(d["positions_sample"], 10)

    def test_private_no_token_self(self):
        with mock.patch.object(data, "_get", fake_get([])):
            d = mates.fetch(Config(account_id=MATE), MATE)
        self.assertEqual(d["matches"], [])
        self.assertIsNone(d["with_me"])
        with mock.patch.object(data, "_get", fake_get([m(AXE.id, True)])):
            d = mates.fetch(Config(account_id=MATE), MATE)                 # 查自己：不查「和我一起」
        self.assertIsNone(d["with_me"])
        self.assertIn("STRATZ token", d["positions_note"])
        with mock.patch.object(data, "_get", fake_get([m(AXE.id, True)])):
            d = mates.fetch(Config(account_id=ME), MATE)                   # 没一起打过
        self.assertEqual(d["with_me"], [0, 0])

    def test_unknown_account(self):
        with mock.patch.object(data, "_get", fake_get([], profile=False)), self.assertRaises(LookupError):
            mates.fetch(Config(), MATE)

    def test_unknown_account_404(self):
        r = requests.Response()
        r.status_code = 404
        with mock.patch.object(data, "_get", side_effect=requests.HTTPError(response=r)),                 self.assertRaises(LookupError):
            mates.fetch(Config(), MATE)
        r.status_code = 503                                                 # 别的 HTTP 错误照常当联网失败
        with mock.patch.object(data, "_get", side_effect=requests.HTTPError(response=r)),                 self.assertRaises(requests.HTTPError):
            mates.fetch(Config(), MATE)

    def test_stratz_down(self):
        with mock.patch.object(data, "_get", fake_get([m(AXE.id, True)])), \
                mock.patch.object(data, "_player_matches_stratz", side_effect=data.APIError("NPGSQL TIMEOUT")):
            d = mates.fetch(Config(stratz_token="t"), MATE)
        self.assertIsNone(d["positions"])
        self.assertIn("NPGSQL TIMEOUT", d["positions_note"])
        self.assertEqual(len(d["heroes"]), 1)                              # 别的照样有


class StoreTest(unittest.TestCase):
    def test_keep_order_save_load_view(self):
        path = TMP / f"m-{next(SEQ)}.json"
        s = mates.Mates(path)
        for i in range(1, mates.KEEP + 3):
            s.touch(i)
        s.touch(3)
        self.assertEqual([e["account_id"] for e in s.items][:2], [3, mates.KEEP + 2])
        self.assertEqual(len(s.items), mates.KEEP)
        d = {"name": "x", "rank_tier": 74, "fetched_at": int(time.time()), "matches": [
            {"hero_id": AXE.id, "win": True, "kda": [1, 2, 3], "t": 0, "ranked": True}],
             "heroes": [[AXE.id, 1, 1]], "with_me": None, "positions": None, "positions_note": ""}
        s.done(3, d, "")
        s.remove(4)
        again = mates.Mates(path)
        self.assertEqual([e["account_id"] for e in again.items], [e["account_id"] for e in s.items])
        self.assertTrue(mates.Mates.fresh(again.get(3)))
        v = again.view(HeroDB(HEROES))[0]
        self.assertEqual(v["rank"], "超凡入圣 4 星")
        self.assertEqual(v["matches"][0]["name"], AXE.name_cn)
        self.assertFalse(v["private"])
        self.assertFalse(again.view(HeroDB(HEROES))[1]["loaded"])


class LiveMateTest(unittest.TestCase):
    def setUp(self):
        self.live = make_live()
        self.live.mates = mates.Mates(TMP / f"live-{next(SEQ)}.json")
        self.calls, self.gate = [], threading.Event()

        def fetch(cfg, aid):
            self.calls.append(aid)
            self.gate.wait(5)
            if aid == 404:
                raise LookupError("OpenDota 上没有好友代码 404")
            if aid == 500:
                raise data.APIError("断网")
            return {"account_id": aid, "name": "阿强", "rank_tier": 55, "fetched_at": int(time.time()),
                    "matches": [], "heroes": [], "with_me": None, "positions": None, "positions_note": ""}
        self.live._mate_fetch = fetch
        self.live.start()

    def tearDown(self):
        self.gate.set()
        self.live.close()

    def act(self, **a):
        return self.live.submit(self.live.action, a).result(timeout=5)

    def mates_view(self):
        self.live.submit(lambda: None).result(timeout=5)            # 等查询结果排进工作线程
        return json.loads(self.live.current_snapshot)["mates"]

    def wait_done(self):
        for _ in range(100):
            v = self.mates_view()
            if not any(x["busy"] for x in v):
                return v
            time.sleep(0.02)
        self.fail("一直在查")

    def test_query_refresh_remove(self):
        self.act(type="mate_query", code="2002")
        v = self.mates_view()
        self.assertTrue(v[0]["busy"])
        self.act(type="mate_query", code="2002")                     # 正在查：不再开一个
        self.gate.set()
        v = self.wait_done()
        self.assertEqual(self.calls, [2002])
        self.assertEqual(v[0]["name"], "阿强")
        self.assertTrue(v[0]["private"])
        msg = self.act(type="mate_query", code="2002")               # 刚查过：不重查
        self.assertIn("刚查过", msg)
        self.act(type="mate_query", code="2002", force=True)         # 点了刷新
        self.wait_done()
        self.assertEqual(self.calls, [2002, 2002])
        self.act(type="mate_remove", account_id=2002)
        self.assertEqual(self.mates_view(), [])

    def test_errors(self):
        self.gate.set()
        self.act(type="mate_query", code="404")
        self.act(type="mate_query", code="500")
        v = self.wait_done()
        self.assertIn("联网失败", v[0]["error"])
        self.assertIn("没有好友代码", v[1]["error"])
        with self.assertRaises(ValueError):
            self.act(type="mate_query", code="abc")


if __name__ == "__main__":
    unittest.main()
