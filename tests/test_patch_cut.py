"""版本更新以后的胜率只用更新以后的数据（玩家 2026-11-19 定）：上线时间怎么取、克制 / 配合按 start_time 切、
STRATZ 分位置 / 对线挑哪一周、版本是这周中间上的时候怎么办。全部假接口，不联网。"""
import unittest
from unittest import mock

from dota_assistant.core.config import Config
from dota_assistant.sources import http, stats

W = stats.WEEK
CUR = 2968 * W                    # 2026-11-19 00:00 UTC（星期四），STRATZ 的一周从这开始
P742 = ("7.42", CUR - 9513)       # 7.42 真上线：11-18 21:28 UTC
CFG = Config(rank_min=7, rank_max=8, days=14, stratz_token="x")


class PatchStartTest(unittest.TestCase):
    def fake_get(self, od, valve):
        def get(url, **params):
            if "constants/patch" in url:
                if isinstance(od, Exception):
                    raise od
                return od
            if isinstance(valve, Exception):
                raise valve
            return {"patches": valve}
        return get

    OD = [{"name": "7.41", "date": "2026-03-24T00:50:59.580Z"}, {"name": "7.42", "date": "2026-11-18T21:28:07.019Z"}]

    def test_take_later_of_two_sources(self):
        valve = [{"patch_number": "7.41f", "patch_timestamp": 1789455600},
                 {"patch_number": "7.42", "patch_timestamp": 1794988800}]   # 当天 08:00，比真上线早
        with mock.patch.object(http, "_get", self.fake_get(self.OD, valve)):
            self.assertEqual(stats.patch_start(log=lambda m: None), ("7.42", 1795037287))

    def test_letter_patch_from_valve(self):
        valve = [{"patch_number": "7.42", "patch_timestamp": 1794988800},
                 {"patch_number": "7.42a", "patch_timestamp": 1796000000}]  # OpenDota 只有大版本，小版本看 Valve
        with mock.patch.object(http, "_get", self.fake_get(self.OD, valve)):
            self.assertEqual(stats.patch_start(log=lambda m: None), ("7.42a", 1796000000))

    def test_one_source_down(self):
        down = http.APIError("x")
        with mock.patch.object(http, "_get", self.fake_get(down, [{"patch_number": "7.42", "patch_timestamp": 5}])):
            self.assertEqual(stats.patch_start(log=lambda m: None), ("7.42", 5))
        with mock.patch.object(http, "_get", self.fake_get(down, down)):
            self.assertIsNone(stats.patch_start(log=lambda m: None))


class StratzWeekTest(unittest.TestCase):
    def test_old_patch_keeps_default(self):
        self.assertIsNone(stats.stratz_week(("7.41f", CUR - 3 * W), now=CUR + 100))
        self.assertIsNone(stats.stratz_week(None, now=CUR + 100))
        self.assertIsNone(stats.stratz_week(("x", CUR - W), now=CUR + 100))     # 上个整周正好从上线那一刻开始

    def test_patch_before_this_week(self):
        self.assertEqual(stats.stratz_week(P742, now=CUR + 3600), CUR)          # 7.42：当周是上线以后才开始的
        self.assertEqual(stats.stratz_week(P742, now=CUR + W - 1), CUR)

    def test_patch_mid_week(self):
        self.assertIs(stats.stratz_week(("x", CUR + 86400), now=CUR + 2 * 86400), False)
        self.assertIsNone(stats.stratz_week(("x", CUR + 86400), now=CUR + 2 * W + 1))   # 再过一个整周就恢复默认


class FetchStatsTest(unittest.TestCase):
    def run_fetch(self, patch, now):
        sqls = []

        def sql(q):
            sqls.append(q)
            return [{"m": 9_095_964_277}] if "min(match_id)" in q else []
        with mock.patch.object(http, "_get", return_value=[{"match_id": 9_097_453_426}]), \
                mock.patch.object(http, "_sql", sql), mock.patch.object(stats.time, "time", return_value=now):
            out = stats.fetch_stats(CFG, log=lambda m: None, patch=patch)
        return out, sqls

    def test_new_patch_cuts_by_start_time(self):
        out, sqls = self.run_fetch(P742, now=CUR + 26 * 3600)
        self.assertEqual(out["since"], P742[1])
        self.assertEqual(out["patch"], "7.42")
        self.assertEqual(out["lo_id"], 9_095_964_276)                  # 从上线后第一局开始，不是往前 14 天
        chunks = [q for q in sqls if "min(match_id)" not in q]
        self.assertTrue(chunks)
        self.assertTrue(all(f"start_time >= {P742[1]}" in q for q in chunks))

    def test_min_query_timeout_falls_back_to_estimate(self):
        """explorer 查上线后第一局超时（2026-11-20 实测）：不让整次更新失败，按上线时长估算起点，分块照样按 start_time 切"""
        sqls = []

        def sql(q):
            sqls.append(q)
            if "min(match_id)" in q:
                raise http.APIError("OpenDota explorer 出错：Error: Query read timeout")
            return []
        now = CUR + 26 * 3600
        with mock.patch.object(http, "_get", return_value=[{"match_id": 9_097_453_426}]), \
                mock.patch.object(http, "_sql", sql), mock.patch.object(stats.time, "time", return_value=now):
            out = stats.fetch_stats(CFG, log=lambda m: None, patch=P742)
        ago = (now - P742[1]) / 86400
        self.assertEqual(out["since"], P742[1])
        self.assertEqual(out["lo_id"], 9_097_453_426 - int((ago + 1) * stats.IDS_PER_DAY) - 1)
        chunks = [q for q in sqls if "min(match_id)" not in q]
        self.assertTrue(chunks and all(f"start_time >= {P742[1]}" in q for q in chunks))

    def test_old_patch_unchanged(self):
        out, sqls = self.run_fetch(("7.41f", 1789455600), now=CUR)
        self.assertIsNone(out["since"])
        self.assertEqual(out["lo_id"], 9_097_453_426 - 14 * stats.IDS_PER_DAY)
        self.assertFalse(any("start_time" in q for q in sqls))


class StratzPositionsTest(unittest.TestCase):
    def test_week_passed(self):
        seen = {}

        def fake(cfg, q, **kw):
            seen.update(kw)
            return {"heroStats": {"stats": [{"heroId": 1, "position": "POSITION_1", "matchCount": 10, "winCount": 6}]}}
        with mock.patch.object(http, "_stratz", fake), mock.patch.object(stats.time, "time", return_value=CUR + 3600):
            out = stats.fetch_positions(CFG, P742, log=lambda m: None)
        self.assertEqual(seen["w"], CUR)
        self.assertEqual(out["heroes"]["1"]["1"], [10, 6])
        self.assertEqual(out["patch"], "7.42")

    def test_mid_week_sums_days_and_hours_after_patch(self):
        patch = ("7.42a", CUR + 86400 + 10 * 3600)        # 星期五 10:00 上的
        d1, d2 = CUR + 86400, CUR + 2 * 86400              # 上线那天（有一半旧版本，不要）、第二天
        h = d2 + 86400                                     # 第三天：按天的还没出，用按小时的

        def fake(cfg, q, b, p):
            self.assertEqual(b, ["DIVINE", "IMMORTAL"])
            if p != ["POSITION_2"]:
                return {"heroStats": {"d": [], "h": []}}
            return {"heroStats": {
                "d": [{"day": d1, "heroId": 5, "matchCount": 100, "winCount": 40},
                      {"day": d2, "heroId": 5, "matchCount": 50, "winCount": 30}],
                "h": [{"hour": patch[1] - 3600, "heroId": 5, "matchCount": 9, "winCount": 9},     # 上线前一小时：不要
                      {"hour": patch[1], "heroId": 5, "matchCount": 4, "winCount": 2},           # 上线那天按小时补
                      {"hour": d2 + 3600, "heroId": 5, "matchCount": 7, "winCount": 7},          # 第二天按天已经有了：不重复算
                      {"hour": h, "heroId": 5, "matchCount": 3, "winCount": 1}]}}
        with mock.patch.object(http, "_stratz", fake), mock.patch.object(stats.time, "time", return_value=h + 7200):
            out = stats.fetch_positions(CFG, patch, log=lambda m: None)
        self.assertEqual(out["heroes"]["5"], {"2": [50 + 4 + 3, 30 + 2 + 1]})
        self.assertEqual(out["min_minutes"], 0)

    def test_lanes_mid_week_none(self):
        with mock.patch.object(http, "_stratz", side_effect=AssertionError("不该调")), \
                mock.patch.object(stats.time, "time", return_value=CUR + 2 * 86400):
            self.assertIsNone(stats.fetch_lanes(CFG, [1], log=lambda m: None, patch=("x", CUR + 86400)))


if __name__ == "__main__":
    unittest.main()


class StartupUpdateTest(unittest.TestCase):
    """启动时要不要全量更新：克制数据是新版本上线前拉的马上更新，上线头几天一天一更"""

    def setUp(self):
        import json
        import os
        import tempfile
        import time
        from pathlib import Path
        from dota_assistant.app import start
        self.start, self.json, self.os, self.time = start, json, os, time
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        p = mock.patch.object(stats, "DATA_DIR", d)
        p.start()
        self.addCleanup(p.stop)
        (d / "heroes.json").write_text("[]", encoding="utf-8")
        self.path = stats.stats_path(CFG)

    def write(self, age_hours, **extra):
        now = self.time.time()
        self.path.write_text(self.json.dumps({"fetched_at": int(now - age_hours * 3600), **extra}), encoding="utf-8")
        self.os.utime(self.path, (now - age_hours * 3600,) * 2)
        return now

    def reason(self, patch):
        return self.start.full_update_reason(CFG, patch_start=lambda log: patch)

    def test_missing(self):
        self.assertIn("第一次", self.reason(None))

    def test_fresh_same_patch(self):
        now = self.write(5, patch="7.42", since=None)
        self.assertIsNone(self.reason(("7.42", int(now - 10 * 86400))))

    def test_new_patch_after_fetch(self):
        now = self.write(5)
        self.assertIn("7.42a 上线了", self.reason(("7.42a", int(now - 3600))))

    def test_early_patch_daily(self):
        now = self.write(25, patch="7.42", since=1)
        self.assertIn("一天一更", self.reason(("7.42", int(now - 2 * 86400))))
        self.write(5, patch="7.42", since=1)
        self.assertIsNone(self.reason(("7.42", int(now - 2 * 86400))))

    def test_offline_keeps_data(self):
        self.write(5)
        self.assertIsNone(self.reason(None))

    def test_three_days(self):
        self.write(73)
        self.assertIn("3 天", self.reason(None))
