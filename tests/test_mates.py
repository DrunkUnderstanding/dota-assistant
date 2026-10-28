"""队友战绩：好友代码解析、表现分、联网结果整理（假接口）、记住查过的人、Service（查 / 刷新 / 删 / 整局）、本地服务接口。不联网。"""
import itertools
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import requests

from dota_assistant.app.webui import WebServer
from dota_assistant.core.config import Config
from dota_assistant.core.heroes import HeroDB
from dota_assistant.domain import mates
from dota_assistant.sources import http
from dota_assistant.sources import player as sources_player   # 本文件下面有个 player() 造数据的函数，别名避开
from tests.test_engine import AXE, HEROES, PL
from tests.test_webui import make_live

TMP = Path(tempfile.mkdtemp(prefix="dota-mates-"))
ME, MATE = 1001, 2002
SEQ = itertools.count()   # 文件名编号：Windows 上 monotonic_ns 精度约 15 毫秒，两个测试会撞名
DB = HeroDB(HEROES)
# 一条直线的分位曲线：第 p 分位的值 = p × 1000（每项都一样，好算）
LINE = [{"percentile": p / 10, "value": p * 100} for p in range(1, 10)]
BENCH = {k: LINE for k in mates.SCORE_PARTS}


def m(hero, win, t=1_790_000_000, ranked=True, mid=1):
    """OpenDota /players/{id}/matches 的一行：天辉（slot 0），赢没赢看 radiant_win"""
    return {"match_id": mid, "hero_id": hero, "player_slot": 0, "radiant_win": win, "start_time": t,
            "duration": 1800, "game_mode": 22, "lobby_type": 7 if ranked else 0, "party_size": 2,
            "kills": 5, "deaths": 2, "assists": 9, "gold_per_min": 500, "xp_per_min": 600, "last_hits": 200,
            "denies": 10, "hero_damage": 20000, "tower_damage": 3000, "hero_healing": 0, "level": 25, "leaver_status": 0}


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


def no_bench(ids):
    return {}


class ParseCodeTest(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(mates.parse_code(" 123456789 "), 123456789)
        self.assertEqual(mates.parse_code(str(mates.STEAM64_BASE + 42)), 42)          # Steam 64 位 ID
        self.assertEqual(mates.parse_code("https://www.opendota.com/players/86745912/matches"), 86745912)
        self.assertEqual(mates.parse_code("https://stratz.com/players/86745912"), 86745912)

    def test_bad(self):
        for bad in ("", "abc", "0", str(2 ** 33), str(mates.ANONYMOUS)):
            with self.assertRaises(ValueError):
                mates.parse_code(bad)


class ScoreTest(unittest.TestCase):
    def test_percentile(self):
        self.assertAlmostEqual(mates.percentile(450, LINE), 0.45)                  # 两个分位中间按直线插
        self.assertAlmostEqual(mates.percentile(50, LINE), 0.05)                   # 比最低的分位还低：从 0 插
        self.assertEqual(mates.percentile(5000, LINE), 1.0)
        self.assertAlmostEqual(mates.percentile(300, LINE), 0.3)
        flat = [{"percentile": p / 10, "value": 0 if p <= 6 else 50} for p in range(1, 10)]
        self.assertAlmostEqual(mates.percentile(0, flat), 0.35)                    # 一串都是 0：取中间

    def test_score(self):
        p = {"duration": 600, "gold_per_min": 500, "xp_per_min": 500, "kills": 5000, "assists": 5000,
             "deaths": 1000, "last_hits": 5000, "hero_damage": 5000, "tower_damage": 500}
        s = mates.score(p, BENCH)
        parts = dict(s["parts"])
        self.assertEqual(parts["经济"], 50)
        self.assertEqual(parts["击杀"], 50)                                        # 每分钟 500
        self.assertEqual(parts["死亡少"], 90)                                      # 每分钟 100 = 10 分位，反过来
        self.assertEqual(s["score"], round(sum(parts.values()) / len(parts)))
        self.assertIsNone(mates.score(p, None))


class FetchTest(unittest.TestCase):
    def test_summary(self):
        ms = [m(AXE.id, True, mid=i) for i in range(8)] + [m(PL.id, False)] * 4 + [m(AXE.id, False, ranked=False)] * 30
        get = fake_get(ms, peers=[{"account_id": 7, "with_games": 99, "with_win": 50},
                                  {"account_id": ME, "with_games": 10, "with_win": 7, "against_games": 40}])
        ranked = [(AXE.id, 3, True)] * 6 + [(PL.id, 1, False)] * 2 + [(AXE.id, 0, True)] * 2
        asked = []
        rating = {"imp": 12, "award": "MVP", "pos": 2}
        with mock.patch.object(http, "_get", get), \
                mock.patch.object(sources_player, "_player_matches_stratz", return_value=ranked) as st, \
                mock.patch.object(mates, "stratz_ratings", return_value={0: rating}):
            d = mates.fetch(Config(account_id=ME, stratz_token="t"), MATE,
                            bench=lambda ids: asked.append(sorted(set(ids))) or {AXE.id: BENCH})
        self.assertEqual(st.call_args.args[1:], (MATE, mates.SAMPLE))
        self.assertEqual(asked, [sorted([AXE.id, PL.id])])                 # 只拉最近 20 局里的英雄
        self.assertEqual(len(d["matches"]), mates.RECENT)
        first = d["matches"][0]
        self.assertEqual((first["match_id"], first["kda"], first["mode"], first["party"]), (0, [5, 2, 9], "天梯 · 全英雄选择", 2))
        self.assertEqual((first["mode_short"], d["matches"][-1]["mode_short"]), ("天梯", "全英雄选择"))
        self.assertIsNotNone(first["perf"])
        self.assertIsNone(d["matches"][8]["perf"])                          # 幻影长矛手没拉到分位
        self.assertEqual(first["stratz"], rating)                           # STRATZ 解析了的局带它的评分
        self.assertIsNone(d["matches"][1]["stratz"])
        self.assertEqual(sum(x["win"] for x in d["matches"]), 8)
        self.assertEqual(d["heroes"][0], [AXE.id, 38, 8])                  # 按局数排
        self.assertEqual(d["with_me"], [10, 7])                            # 只算同队的，对面的 40 局不算
        self.assertEqual(d["positions"], [[3, 6, 6], [1, 2, 0]])           # 未解析（0）不算
        self.assertEqual(d["positions_sample"], 10)

    def test_private_no_token_self(self):
        with mock.patch.object(http, "_get", fake_get([])):
            d = mates.fetch(Config(account_id=MATE), MATE, bench=no_bench)
        self.assertEqual(d["matches"], [])
        self.assertIsNone(d["with_me"])
        with mock.patch.object(http, "_get", fake_get([m(AXE.id, True)])):
            d = mates.fetch(Config(account_id=MATE), MATE, bench=no_bench)   # 查自己：不查「和我一起」
        self.assertIsNone(d["with_me"])
        self.assertIn("STRATZ token", d["positions_note"])
        self.assertNotIn("stratz", d["matches"][0])                        # 没 token 不问 STRATZ
        with mock.patch.object(http, "_get", fake_get([m(AXE.id, True)])):
            d = mates.fetch(Config(account_id=ME), MATE, bench=no_bench)     # 没一起打过
        self.assertEqual(d["with_me"], [0, 0])

    def test_unknown_account(self):
        with mock.patch.object(http, "_get", fake_get([], profile=False)), self.assertRaises(LookupError):
            mates.fetch(Config(), MATE, bench=no_bench)

    def test_unknown_account_404(self):
        r = requests.Response()
        r.status_code = 404
        with mock.patch.object(http, "_get", side_effect=requests.HTTPError(response=r)), \
                self.assertRaises(LookupError):
            mates.fetch(Config(), MATE, bench=no_bench)
        r.status_code = 503                                                 # 别的 HTTP 错误照常当联网失败
        with mock.patch.object(http, "_get", side_effect=requests.HTTPError(response=r)), \
                self.assertRaises(requests.HTTPError):
            mates.fetch(Config(), MATE, bench=no_bench)

    def test_stratz_down(self):
        with mock.patch.object(http, "_get", fake_get([m(AXE.id, True)])), \
                mock.patch.object(http, "_stratz", side_effect=http.APIError("NPGSQL TIMEOUT")):
            d = mates.fetch(Config(stratz_token="t"), MATE, bench=no_bench)
        self.assertIsNone(d["matches"][0]["stratz"])                       # 评分查不到就不显示
        self.assertIsNone(d["positions"])
        self.assertIn("NPGSQL TIMEOUT", d["positions_note"])
        self.assertEqual(len(d["heroes"]), 1)                              # 别的照样有


class StratzTest(unittest.TestCase):
    def test_ratings_parsed(self):
        res = {"player": {"matches": [
            {"id": 1, "players": [{"imp": -7, "award": "TOP_SUPPORT", "position": "POSITION_4"}]},
            {"id": 2, "players": [{"imp": None, "award": None, "position": None}]},       # 还没解析
            {"id": 3, "players": [{"imp": 0, "award": "NONE", "position": None}]}]}}
        with mock.patch.object(http, "_stratz", return_value=res):
            r = mates.stratz_ratings(Config(stratz_token="t"), MATE)
        self.assertEqual(r, {1: {"imp": -7, "award": "最佳辅助", "pos": 4}, 3: {"imp": 0, "award": None, "pos": None}})
        with mock.patch.object(http, "_stratz", return_value={"player": None}):            # STRATZ 不认这个号
            self.assertEqual(mates.stratz_ratings(Config(stratz_token="t"), MATE), {})
        with mock.patch.object(http, "_stratz", return_value={"match": {"players": [
                {"heroId": AXE.id, "imp": 18, "award": "MVP", "position": "POSITION_3"}]}}):
            self.assertEqual(mates.stratz_match(Config(stratz_token="t"), 9), {AXE.id: {"imp": 18, "award": "MVP", "pos": 3}})
        with mock.patch.object(http, "_stratz", return_value={"match": None}):
            self.assertEqual(mates.stratz_match(Config(stratz_token="t"), 9), {})


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.dir = TMP / f"cache-{next(SEQ)}"
        for p in (mock.patch.object(mates, "MATCH_DIR", self.dir / "matches"),
                  mock.patch.object(mates, "BENCH_DIR", self.dir / "bench")):
            p.start()
            self.addCleanup(p.stop)

    def test_match_cached(self):
        full = {"match_id": 5, "players": [{"hero_id": 1}], "replay_url": "x", "duration": 100}
        with mock.patch.object(http, "_get", return_value=full) as get:
            a = mates.fetch_match(5)
            b = mates.fetch_match(5)
        self.assertEqual(get.call_count, 1)                                 # 打完的比赛不会变，第二次读文件
        self.assertEqual(a, b)
        self.assertNotIn("replay_url", a)                                   # 只存要用的
        r = requests.Response()
        r.status_code = 404
        with mock.patch.object(http, "_get", side_effect=requests.HTTPError(response=r)), \
                self.assertRaises(LookupError):
            mates.fetch_match(6)

    def test_benchmarks_cached_and_stale_fallback(self):
        with mock.patch.object(http, "_get", return_value={"result": BENCH}) as get:
            self.assertEqual(mates.hero_benchmarks(AXE.id), BENCH)
            self.assertEqual(mates.hero_benchmarks(AXE.id), BENCH)
        self.assertEqual(get.call_count, 1)
        path = self.dir / "bench" / f"{AXE.id}.json"
        path.write_text(json.dumps({"fetched_at": 0, "result": BENCH}), encoding="utf-8")   # 过期了
        with mock.patch.object(http, "_get", side_effect=http.APIError("断网")):
            self.assertEqual(mates.hero_benchmarks(AXE.id), BENCH)        # 拉不到用旧的
            self.assertIsNone(mates.hero_benchmarks(PL.id))               # 从来没有


def summary(aid, name="阿强"):
    return {"account_id": aid, "name": name, "rank_tier": 55, "fetched_at": int(time.time()),
            "matches": [], "heroes": [], "with_me": None, "positions": None, "positions_note": ""}


def player(slot, hero, aid, **kw):
    return {"player_slot": slot, "hero_id": hero, "account_id": aid, "personaname": f"p{slot}", "rank_tier": 74,
            "kills": 3, "deaths": 4, "assists": 5, "last_hits": 100, "denies": 5, "gold_per_min": 400,
            "xp_per_min": 500, "hero_damage": 12000, "tower_damage": 800, "hero_healing": 0, "level": 20,
            "net_worth": 15000, "item_0": 1, "item_1": 0, "backpack_0": 2, "item_neutral": 0, **kw}


def fake_match(mid):
    if mid == 404:
        raise LookupError("没有")
    players = [player(0, AXE.id, MATE, party_id=9), player(1, PL.id, mates.ANONYMOUS, personaname=None),
               player(2, AXE.id, ME, party_id=9, leaver_status=2),
               player(128, PL.id, 3003, party_id=7), player(129, AXE.id, None)]
    return {"match_id": mid, "start_time": 1, "duration": 1800, "radiant_win": False, "radiant_score": 20,
            "dire_score": 30, "game_mode": 23, "lobby_type": 0, "players": players}


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.calls, self.gate = [], threading.Event()
        self.gate.set()

        def fetch(cfg, aid):
            self.calls.append(aid)
            self.gate.wait(5)
            if aid == 404:
                raise LookupError("OpenDota 上没有好友代码 404")
            if aid == 500:
                raise http.APIError("断网")
            return summary(aid)
        self.svc = mates.Service(Config(account_id=ME), TMP / f"svc-{next(SEQ)}.json", fetch_player=fetch,
                                 fetch_match_fn=fake_match, db=DB,
                                 items={1: ("blink", "闪烁匕首"), 2: ("tango", "树之祭祀")})

    def test_query_refresh_remove(self):
        aid, msg = self.svc.query("2002")
        self.assertEqual((aid, msg), (2002, ""))
        v = self.svc.list()
        self.assertEqual(v[0]["name"], "阿强")
        self.assertTrue(v[0]["private"])
        self.assertFalse(v[0]["stale"])
        self.svc.query("2002")                                       # 刚查过：不重查
        self.assertEqual(self.calls, [2002])
        self.svc.query("2002", force=True)                           # 点了刷新（刚查完 5 秒内不算，先改旧）
        self.assertEqual(self.calls, [2002])
        self.svc.store.get(2002)["data"]["fetched_at"] -= 60
        self.svc.query("2002", force=True)
        self.assertEqual(self.calls, [2002, 2002])
        again = mates.Service(Config(), self.svc.store.path, db=DB)  # 存下来了，重开还在
        self.assertEqual([x["account_id"] for x in again.list()], [2002])
        self.svc.remove(2002)
        self.assertEqual(self.svc.list(), [])

    def test_errors(self):
        self.assertIn("没有好友代码", self.svc.query("404")[1])
        self.assertIn("联网失败", self.svc.query("500")[1])
        v = {x["account_id"]: x for x in self.svc.list()}
        self.assertFalse(v[404]["loaded"])
        self.assertIn("联网失败", v[500]["error"])
        with self.assertRaises(ValueError):
            self.svc.query("abc")

    def test_old_format_marked_stale(self):
        # 第一版存的最近几局没有比赛编号，点不进整局：标出来让页面自动重查
        old = {**summary(2002), "matches": [{"hero_id": AXE.id, "win": True, "kda": [1, 2, 3], "t": 0, "ranked": True}]}
        path = TMP / f"old-{next(SEQ)}.json"
        path.write_text(json.dumps([{"account_id": 2002, "data": old}]), encoding="utf-8")
        v = mates.Service(Config(), path, db=DB).list()[0]
        self.assertTrue(v["stale"])
        self.assertEqual(v["matches"][0]["name"], AXE.name_cn)

    def test_same_person_fetched_once(self):
        self.gate.clear()
        threads = [threading.Thread(target=self.svc.query, args=("2002",)) for _ in range(3)]
        for t in threads:
            t.start()
        time.sleep(0.1)
        self.gate.set()
        for t in threads:
            t.join(5)
        self.assertEqual(self.calls, [2002])

    def test_match_view(self):
        with mock.patch.object(mates, "benchmarks_for", return_value={AXE.id: BENCH}):
            v = self.svc.match(77, focus=MATE)
        self.assertEqual((v["mode"], v["score"], v["radiant_win"]), ("加速模式", [20, 30], False))
        rad, dire = v["teams"]["radiant"], v["teams"]["dire"]
        self.assertEqual((len(rad), len(dire)), (3, 2))
        self.assertTrue(rad[0]["focus"] and not rad[0]["me"])
        self.assertTrue(rad[2]["me"] and rad[2]["abandon"])
        self.assertEqual((rad[1]["name"], rad[1]["account_id"]), ("匿名玩家", None))
        self.assertEqual(dire[1]["name"], "匿名玩家")
        self.assertEqual(rad[0]["party"], rad[2]["party"])                 # 一起排的同一个编号
        self.assertIsNone(dire[0]["party"])                                # 一个人的 party 不标
        self.assertEqual(rad[0]["items"][0], {"key": "blink", "name": "闪烁匕首"})
        self.assertIsNone(rad[0]["items"][1])
        self.assertEqual(rad[0]["backpack"], [{"key": "tango", "name": "树之祭祀"}])
        self.assertEqual(rad[0]["hero"], AXE.name_cn)
        self.assertIsNotNone(rad[0]["perf"])
        self.assertIsNone(rad[1]["perf"])                                  # 幻影长矛手没分位数据
        self.assertIsNone(rad[0]["stratz"])                                # 没 token：不问 STRATZ、不说明
        self.assertEqual(v["stratz_note"], "")
        with self.assertRaises(LookupError):
            self.svc.match(404)

    def test_match_stratz_optional(self):
        asked, saved = [], []

        def fake_stratz(cfg, mid):
            asked.append(mid)
            if mid == 1:
                raise http.APIError("503")
            if mid == 2:
                return {}                                                  # 还没解析
            return {AXE.id: {"imp": 5, "award": None, "pos": 1}}           # 只拿到一部分人
        svc = mates.Service(Config(account_id=ME, stratz_token="t"), TMP / f"svc-{next(SEQ)}.json",
                            fetch_match_fn=fake_match, db=DB, items={}, stratz_match_fn=fake_stratz)
        with mock.patch.object(mates, "benchmarks_for", return_value={}), \
                mock.patch.object(mates, "save_match", side_effect=saved.append):
            self.assertIn("没查到", svc.match(1)["stratz_note"])
            self.assertIn("还没解析", svc.match(2)["stratz_note"])
            v = svc.match(3)
            self.assertEqual(v["teams"]["radiant"][0]["stratz"]["imp"], 5)
            self.assertIsNone(v["teams"]["radiant"][1]["stratz"])
            self.assertEqual(v["stratz_note"], "")
            svc.match(3)
            self.assertIn("还没解析", svc.match(2)["stratz_note"])        # 刚问过说没有：30 分钟内不再问
            svc._stratz_miss[2] -= mates.STRATZ_RETRY
            svc.match(2)
            svc.match(1)                                                  # 出错的不算「没有」，下次照问
        self.assertEqual(saved, [])                                        # 不齐不存，下次打开再问
        self.assertEqual(asked, [1, 2, 3, 3, 2, 1])

    def test_match_stratz_saved_when_complete(self):
        m = {**fake_match(5), "players": [player(0, AXE.id, None), player(128, PL.id, None)]}
        saved = []
        svc = mates.Service(Config(stratz_token="t"), TMP / f"svc-{next(SEQ)}.json", fetch_match_fn=lambda mid: m,
                            db=DB, items={}, stratz_match_fn=lambda cfg, mid: {AXE.id: {"imp": 1}, PL.id: {"imp": 2}})
        with mock.patch.object(mates, "benchmarks_for", return_value={}), \
                mock.patch.object(mates, "save_match", side_effect=saved.append):
            svc.match(5)
        self.assertEqual(saved[0]["stratz"], {str(AXE.id): {"imp": 1}, str(PL.id): {"imp": 2}})

        def never(cfg, mid):
            raise AssertionError("存过了不该再问 STRATZ")
        svc2 = mates.Service(Config(stratz_token="t"), TMP / f"svc-{next(SEQ)}.json", fetch_match_fn=lambda mid: saved[0],
                             db=DB, items={}, stratz_match_fn=never)
        with mock.patch.object(mates, "benchmarks_for", return_value={}):
            v = svc2.match(5)
        self.assertEqual(v["teams"]["dire"][0]["stratz"], {"imp": 2})      # 读存下的（JSON 里键是字符串）


class WebApiTest(unittest.TestCase):
    """本地服务的 /api/mates、/api/match（队友战绩页用）"""

    @classmethod
    def setUpClass(cls):
        cls.live = make_live().start()
        svc = mates.Service(Config(), TMP / f"web-{next(SEQ)}.json", fetch_player=lambda cfg, aid: summary(aid, "<b>x</b>"),
                            fetch_match_fn=fake_match, db=DB, items={})
        cls.srv = WebServer(cls.live, mates_service=svc).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.live.close()

    def call(self, path, body=None):
        req = urllib.request.Request(self.srv.url + path, method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_query_list_remove(self):
        code, res = self.call("/api/mates", {"op": "query", "code": "https://stratz.com/players/4321"})
        self.assertEqual((code, res["ok"], res["account_id"]), (200, True, 4321))
        self.assertEqual(res["mates"][0]["name"], "<b>x</b>")               # 原样给，页面负责转义
        self.assertEqual(self.call("/api/mates")[1]["mates"][0]["account_id"], 4321)
        code, res = self.call("/api/mates", {"op": "remove", "account_id": 4321})
        self.assertEqual((code, res["mates"]), (200, []))
        self.assertEqual(self.call("/api/mates", {"op": "query", "code": "abc"})[0], 400)
        self.assertEqual(self.call("/api/mates", {"op": "nope"})[0], 400)

    def test_match(self):
        with mock.patch.object(mates, "benchmarks_for", return_value={}):
            code, res = self.call(f"/api/match/77?p={MATE}")
            self.assertEqual(code, 200)
            self.assertTrue(res["match"]["teams"]["radiant"][0]["focus"])
            code, res = self.call("/api/match/404")
        self.assertEqual((code, res["ok"]), (404, False))

    def test_page_served(self):
        with urllib.request.urlopen(self.srv.url + "/mates.html", timeout=5) as r:
            self.assertIn("队友战绩", r.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
