"""对线优劣：STRATZ 数据压缩、特征、模型、实战记录对照。不联网。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from dota_assistant import lanestate as ls
from dota_assistant.core.config import Config
from dota_assistant.sources import http

TMP = Path(tempfile.mkdtemp(prefix="dota-lane-"))


def player(hero, radiant, pos, lv=(), k=(), d=(), nw=600, stack=0, lotus_at=None, account=None):
    inv = [{"item0": None}] * 15
    if lotus_at is not None:
        inv = inv[:]
        inv[lotus_at + ls.INV_OFFSET] = {"item0": {"itemId": 4204}}
    return {"heroId": hero, "isRadiant": radiant, "position": f"POSITION_{pos}", "steamAccountId": account,
            "stats": {"level": [-89, *lv], "killEvents": [{"time": t} for t in k], "deathEvents": [{"time": t} for t in d],
                      "assistEvents": [], "networthPerMinute": [nw] * 13,
                      "campStack": [0, 0, 0] + [stack] * 10, "itemUsed": [{"itemId": 4204, "count": 1}] if lotus_at else [],
                      "inventoryReport": inv}}


def raw_match(mid=1, bottom="DIRE_VICTORY"):
    # 天辉优势路（下）：1 号位 + 5 号位，被夜魇 3 / 4 号位压了：死 2 次、等级落后
    ps = [player(1, True, 1, lv=(170,), d=(150,), account=42), player(5, True, 5, d=(160,), lotus_at=3),
          player(2, True, 2, lv=(100, 170)), player(3, True, 3, lv=(120,)), player(4, True, 4),
          player(11, False, 3, lv=(100, 170), k=(150,)), player(14, False, 4, lv=(110,), k=(160,), stack=1),
          player(12, False, 2, lv=(100, 170)), player(13, False, 1, lv=(120,)), player(15, False, 5)]
    return {"id": mid, "didRadiantWin": False, "topLaneOutcome": "TIE", "midLaneOutcome": "TIE",
            "bottomLaneOutcome": bottom, "players": ps}


class DataTest(unittest.TestCase):
    def test_lane_mapping(self):
        self.assertEqual([ls.lane_of(p, True) for p in (1, 2, 3, 4, 5)], ["bot", "mid", "top", "top", "bot"])
        self.assertEqual([ls.lane_of(p, False) for p in (1, 3)], ["top", "bot"])
        self.assertEqual(ls.outcome({"bot": "DIRE_STOMP"}, "bot", True), -1)
        self.assertEqual(ls.outcome({"bot": "DIRE_STOMP"}, "bot", False), 1)
        self.assertEqual(ls.outcome({"bot": "TIE"}, "bot", False), 0)

    def test_compact_and_features(self):
        m = ls.compact(raw_match())
        self.assertIsNone(ls.compact({**raw_match(), "bottomLaneOutcome": None}))   # 没解析完
        rs = {r.hero: r for r in ls.rows([m], 3)}
        carry = rs[1]
        self.assertEqual((carry.y, carry.win), (-1, False))
        self.assertEqual(carry.f["自己死亡"], 1)
        self.assertEqual(carry.f["自己等级"], 2)
        self.assertEqual(carry.f["本路击杀差"], -2)                  # 两人各死一次、没杀人
        self.assertEqual(carry.f["本路等级差"], (2 + 1) / 2 - (3 + 2) / 2)
        self.assertTrue(rs[5].lotus and not rs[5].stacked)            # 3:00 那朵莲花在背包里
        self.assertTrue(rs[14].stacked and rs[14].y == 1)             # 夜魇四号位 2:53 屯了野

    def test_model_learns_direction(self):
        # 本路击杀差越大越优：模型猜「劣」的概率应该随击杀差下降
        rng = np.random.default_rng(0)
        rows = []
        for i in range(600):
            kd = int(rng.integers(-3, 4))
            y = 1 if kd + rng.normal(0, 1) > 0.7 else (-1 if kd + rng.normal(0, 1) < -0.7 else 0)
            f = {k: 0 for feats in ls.FEATURE_SETS.values() for k in feats} | {"本路击杀差": kd}
            rows.append(ls.Row(i // 10, 1 + i % 5, 3, f, y, y > 0, False, False, 0))
        model = ls.Model(ls.FEATURE_SETS[ls.LANE_SET]).fit(rows)
        lo = model.proba([ls.Row(0, 1, 3, {**rows[0].f, "本路击杀差": -3}, 0, False, False, False, 0)])[0]
        hi = model.proba([ls.Row(0, 1, 3, {**rows[0].f, "本路击杀差": 3}, 0, False, False, False, 0)])[0]
        self.assertGreater(lo[0], 0.6)
        self.assertGreater(hi[2], 0.6)
        P = ls.cross_proba(rows, ls.FEATURE_SETS[ls.LANE_SET])
        self.assertEqual(P.shape, (600, 3))
        self.assertIn("猜对", "\n".join(ls.evaluate(rows)))


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.log = TMP / f"log-{id(self)}.jsonl"
        self.cache = TMP / f"cache-{id(self)}.json"
        self.patches = [mock.patch.object(ls, "LOG_FILE", self.log), mock.patch.object(ls, "CACHE", self.cache)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_review_against_stratz(self):
        self.assertIn("还没有实战记录", ls.review(Config()))
        for minute in (3, 6):
            ls.log_moment({"match": "777", "minute": minute, "hero_id": 1, "hero_name": "敌法师", "pos": 1,
                           "player": {"kills": 0, "deaths": 1, "assists": 0}, "hero": {"level": 2},
                           "hp": 0.4, "mana": 0.8, "cue": "屯野 2:53 / 抢莲花 3:00" if minute == 3 else None})
        ls.log_moment({"match": "本地1", "minute": 3, "hero_name": "敌法师", "pos": 1})
        matches = [ls.compact(raw_match(i, bottom)) for i, bottom in enumerate(["DIRE_VICTORY", "RADIANT_VICTORY", "TIE"] * 20)]
        self.cache.write_text(json.dumps({"tried": [], "matches": matches}), encoding="utf-8")
        with mock.patch.object(http, "_stratz", return_value={"match": raw_match(777)}):
            text = ls.review(Config(stratz_token="x", account_id=42))
        self.assertIn("比赛 777 · 敌法师 · 1 号位 · 输 · 对线结果：劣", text)
        self.assertIn("3:00 你：击杀 0 死亡 1 助攻 0 等级 2 血 40% 蓝 80% · 当时提醒「屯野 2:53 / 抢莲花 3:00」", text)
        self.assertIn("只看自己：猜", text)
        self.assertIn("看全本路（击杀差 -2、等级差 -1.0）", text)
        self.assertIn("本地 / 机器人局", text)
        with mock.patch.object(http, "_stratz", return_value={"match": {**raw_match(777), "bottomLaneOutcome": None}}):
            self.assertIn("还没解析完", ls.review(Config(stratz_token="x")))


if __name__ == "__main__":
    unittest.main()
