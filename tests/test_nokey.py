"""不填 STRATZ token、调不了 AI 时：该能用的照常用，少的部分说清楚缺什么，不联网 STRATZ、不报错。"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from dota_assistant.domain import brief
from dota_assistant import kb, live
from dota_assistant.app import start
from dota_assistant.domain import coach
from dota_assistant.ai import gameplan, llm
from dota_assistant.app import update
from dota_assistant.core.config import Config
from dota_assistant.sources import assets, http, stats

NO_KEY = dict(stratz_token="", ai_api_key="", ai_base_url="")
SOURCE = "敌方斧王：反击螺旋，被攻击时有几率旋转"


def no_stratz(*a, **kw):
    raise AssertionError("没填 token 不该调 STRATZ")


class UnavailableTest(unittest.TestCase):
    def test_reasons(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(llm.unavailable(Config(ai_provider="claude_code", **NO_KEY)), llm.NO_CLAUDE)
        with mock.patch("shutil.which", return_value=r"C:\npm\claude.cmd"):
            self.assertEqual(llm.unavailable(Config(ai_provider="claude_code", **NO_KEY)), "")   # 用 Claude Code 自己的登录
        self.assertIn("API Key", llm.unavailable(Config(ai_provider="anthropic", **NO_KEY)))
        self.assertIn("接口地址", llm.unavailable(Config(ai_provider="openai", **NO_KEY)))
        self.assertEqual(llm.unavailable(Config(ai_provider="openai", stratz_token="", ai_api_key="",
                                                ai_base_url="http://127.0.0.1:11434/v1")), "")   # 本地模型可以不要 key

    def test_claude_code_missing_message(self):
        with mock.patch("shutil.which", return_value=None), \
                mock.patch("subprocess.run", side_effect=FileNotFoundError(2, "系统找不到指定的文件")):
            out, _, err = llm.ask_json("x", "s", {}, "m", provider="claude_code")
        self.assertIsNone(out)
        self.assertEqual(err, llm.NO_CLAUDE)


class GameplanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.object(gameplan, "PLAN_DIR", Path(self.tmp.name)),
                        mock.patch.object(gameplan, "facts", return_value=SOURCE),
                        mock.patch.object(kb, "load_rules", return_value=[]),
                        mock.patch.object(kb, "load_mistakes", return_value=[])]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_skip_without_calling_ai(self):
        cfg = Config(ai_provider="anthropic", **NO_KEY)
        with mock.patch.object(llm, "ask_json", side_effect=AssertionError("不该调 AI")):
            with self.assertRaises(llm.Unavailable) as cm:
                gameplan.generate(cfg, 1, [], [2], [], "数据", base=object())
        self.assertIn("API Key", str(cm.exception))

    def test_cached_plan_still_shown(self):
        """以前做过的阵容有缓存：调不了 AI 也照样给"""
        cfg = Config(ai_provider="anthropic", ai_model="m", **NO_KEY)
        res = {"sections": [{"phase": "对线期（0~10 分钟）", "tips": [{"text": "先补刀", "basis": "b", "quote": "被攻击时有几率旋转"}]}]}
        with mock.patch.object(llm, "ask_json", return_value=(res, 0.0, "")):
            with mock.patch.object(llm, "unavailable", return_value=""):
                gameplan.generate(cfg, 1, [], [2], [], "数据", base=object())       # 先写一份缓存
        with mock.patch.object(llm, "ask_json", side_effect=AssertionError("不该调 AI")):
            got = gameplan.generate(cfg, 1, [], [2], [], "数据", base=object())
        self.assertEqual(got.sections[0][1][0].text, "先补刀")


class NoteTest(unittest.TestCase):
    def test_plan_error_wording(self):
        self.assertIn("数据页不受影响", live.plan_error(llm.Unavailable("没填 Anthropic API Key")))
        self.assertTrue(live.plan_error(RuntimeError("超时")).startswith("AI 打法没做出来"))

    def test_start_note(self):
        with mock.patch.object(stats, "positions_source", return_value=None), mock.patch("shutil.which", return_value=None):
            note = start.no_key_note(Config(ai_plan=True, ai_provider="claude_code", **NO_KEY))
        self.assertIn("没有分位置数据", note)
        self.assertIn("Claude Code", note)
        with mock.patch.object(stats, "positions_source", return_value="opendota"):
            self.assertIn("OpenDota 分路凑的", start.no_key_note(Config(ai_plan=False, **NO_KEY)))
        with mock.patch.object(stats, "positions_source", return_value="stratz"):      # 打包版带着 STRATZ 的分位置数据
            note = start.no_key_note(Config(ai_plan=False, **NO_KEY))
        self.assertNotIn("分位置数据", note)
        self.assertIn("STRATZ token", note)
        with mock.patch("shutil.which", return_value="claude"):
            self.assertEqual(start.no_key_note(Config(stratz_token="x" * 20)), "")


class OpenDotaPositionsTest(unittest.TestCase):
    """没 token 时用 OpenDota 分路凑分位置数据：优势路 / 劣势路按定位标签拆，胜场记一半"""

    def test_split_by_roles(self):
        from tests.test_engine import AXE, CM, HEROES, PL
        from dota_assistant.core.heroes import HeroDB
        rows = lambda *lg: [{"lane_role": r, "time": 900, "games": str(g), "wins": "0"} for r, g in lg]
        pos = update.positions_from_lane_roles(HeroDB(HEROES), {
            PL.id: rows((1, 900), (2, 100)),           # 幻影长矛手：一号位标签，优势路几乎都算一号位
            CM.id: rows((1, 800), (3, 200)),           # 水晶室女：辅助标签，优势路算五号位、劣势路算四号位
            AXE.id: rows((3, 1000), (4, 10))})         # 斧王：三 / 四号位标签都对得上，劣势路对半
        self.assertEqual(pos[str(PL.id)]["1"], [810, 405])
        self.assertEqual(pos[str(PL.id)]["5"], [90, 45])
        self.assertEqual(pos[str(PL.id)]["2"], [100, 50])
        self.assertGreater(pos[str(CM.id)]["5"][0], pos[str(CM.id)]["1"][0])
        self.assertGreater(pos[str(CM.id)]["4"][0], pos[str(CM.id)]["3"][0])
        self.assertEqual(pos[str(AXE.id)]["3"][0], 500)
        self.assertNotIn("0", pos[str(AXE.id)])
        self.assertNotIn("29", pos)                    # 没数据的英雄不凑

    def test_load_prefers_stratz(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            od = d / "positions_opendota.json"
            od.write_text('{"heroes": {"1": {"1": [10, 5]}}}', encoding="utf-8")
            cfg = Config(rank_min=7, rank_max=8)
            with mock.patch.object(stats, "DATA_DIR", d), mock.patch.object(stats, "OPENDOTA_POSITIONS", od):
                self.assertEqual(stats.positions_source(cfg), "opendota")
                self.assertEqual(stats.load_positions(cfg), {1: {1: (10, 5)}})
                (d / "positions_r7-8.json").write_text('{"heroes": {"1": {"2": [7, 4]}}}', encoding="utf-8")
                self.assertEqual(stats.positions_source(cfg), "stratz")
                self.assertEqual(stats.load_positions(cfg), {1: {2: (7, 4)}})


class PlanLiveTest(unittest.TestCase):
    """本局要点照常出；AI 打法那页写明用不了，不当成出错"""

    def test_brief_shown_plan_skipped(self):
        from tests.test_webui import make_live, wait

        def build(cfg, my_id, ally_ids, enemy_ids, pos, **kw):
            b = brief.Brief("本局要点")
            b.add("对线", [("row", "对面有闪避")])
            return b

        def plan(*a):
            raise llm.Unavailable(llm.NO_CLAUDE)

        lv = make_live(brief_build=build, plan_build=plan)
        lv.slots["dire"][:1] = ["斧王"]
        lv.start()
        try:
            wait(lv.submit(lv.refresh))
            wait(lv.submit(lv.action, {"type": "explain_pick", "hero_id": 12}))
            for _ in range(50):
                time.sleep(0.05)
                wait(lv.submit(lambda: None))
                if lv.pick_brief and "用不了" in lv.pick_brief.plan_note:
                    break
            self.assertEqual(lv.pick_brief.sections[0][0], "对线")
            self.assertIn("Claude Code", lv.pick_brief.plan_note)
            self.assertIn("数据页不受影响", lv.pick_brief.plan_note)
        finally:
            lv.close()


@unittest.skipUnless((kb.KB_DIR / "kb.json").exists(), "没有本地知识库，先 kb update")
class RealKBTest(unittest.TestCase):
    """真实知识库：没 token、没分位置数据，本局要点和局内提醒都做得出来，而且不碰 STRATZ"""

    @classmethod
    def setUpClass(cls):
        cls.base = kb.KnowledgeBase.load()
        cls.ids = {h.name: i for i, h in cls.base.heroes.items()}

    def build(self, positions):
        enemies = [self.ids[n] for n in ("幻影刺客", "斧王", "水晶室女")]
        with mock.patch.object(http, "_stratz", side_effect=no_stratz), \
                mock.patch.object(stats, "load_positions", return_value=positions):
            b = brief.build(Config(**NO_KEY), self.ids["敌法师"], [self.ids["潮汐猎人"]], enemies, 1, base=self.base)
        return b, [t for t, _ in b.sections]

    def test_brief_without_token(self):
        b, titles = self.build(None)
        self.assertIn("说明", titles)
        self.assertIn("STRATZ token", b.text())
        self.assertIn("团战", titles)
        self.assertNotIn("对线", titles)                      # 连分位置数据都没有：分路猜不了，不编
        self.assertNotIn("起手", titles)                      # 起手装要 STRATZ，没有就不编
        self.assertNotIn("强势期", titles)

    def test_brief_with_opendota_positions(self):
        """OpenDota 分路凑的分位置数据：照样能猜对面分路、给这路的对线提示"""
        lane = lambda r: [{"lane_role": r, "games": "1000", "wins": "500"}]
        lane_roles = {self.ids["幻影刺客"]: lane(1), self.ids["斧王"]: lane(3), self.ids["水晶室女"]: lane(1),
                      self.ids["潮汐猎人"]: lane(3)}
        raw = update.positions_from_lane_roles(assets.load_heroes(), lane_roles)
        b, titles = self.build({int(h): {int(p): tuple(gw) for p, gw in v.items()} for h, v in raw.items()})
        self.assertIn("对线", titles)
        self.assertIn("你这路对面：斧王", b.text())

    def test_coach_without_token(self):
        with mock.patch.object(http, "_stratz", side_effect=no_stratz):
            c, notes = coach.prepare(Config(**NO_KEY), self.ids["敌法师"], [self.ids["幻影刺客"]], 1)
        self.assertIsNone(c.bench)
        self.assertIsInstance(notes, list)


if __name__ == "__main__":
    unittest.main()
