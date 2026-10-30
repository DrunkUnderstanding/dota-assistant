"""推荐按熟练度分组（「推荐」只放会玩的、不熟的标出来，含双排队友）+ 每个推荐的胜率怎么来的。不联网、不碰 data/。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dota_assistant.core.config import Config
from dota_assistant.domain import advisor as adv
from dota_assistant.domain import engine as E
from dota_assistant.domain.engine import Engine, Suggestion, sigmoid
from dota_assistant.core.heroes import HeroDB
from dota_assistant.sources import http, player
from tests.test_engine import AXE, CM, HEROES, PL, TIDE, WK, make_stats
from tests.test_webui import fake_advisor

TMP = Path(tempfile.mkdtemp(prefix="dota-familiar-"))


def advisor(career=None, duo=None, familiar_games=20):
    a = fake_advisor(Config(position_filter=False, top=2, top_other=2, familiar_games=familiar_games))
    a.career, a.duo = career, duo
    return a


class SplitTest(unittest.TestCase):
    RES = [Suggestion(PL, 0.20, 0.10), Suggestion(TIDE, 0.15, 0.10), Suggestion(CM, 0.06, 0.03),
           Suggestion(AXE, 0.05, -0.05), Suggestion(WK, -0.01, -0.20)]

    def test_picks_only_familiar(self):
        # 综合第一的幻影长矛手不熟：推荐从会玩的里挑（潮汐、斧王），幻影长矛手按克制 + 配合进「针对这局」
        picks, targeted = adv.split_targeted(self.RES, 2, 2, True, familiar={TIDE.id, AXE.id})
        self.assertEqual([s.hero for s in picks], [TIDE, AXE])
        self.assertEqual([s.hero for s in targeted], [PL, CM])

    def test_no_draft_and_fallback(self):
        # 还没人选：只出会玩的；这个位置一个会玩的都没有：不分（免得推荐整组空着）
        self.assertEqual([s.hero for s in adv.split_targeted(self.RES, 2, 2, False, familiar={CM.id})[0]], [CM])
        self.assertEqual(adv.split_targeted(self.RES, 2, 2, True, familiar=set()),
                         adv.split_targeted(self.RES, 2, 2, True))


class AdviseTest(unittest.TestCase):
    def test_unfamiliar_counter_goes_to_targeted_with_tag(self):
        # 潮汐克幻影长矛手、综合第一，但我生涯只打过 5 局：不进推荐，进「针对这局」并标「不熟」
        a = advisor(career={TIDE.id: (5, 1), AXE.id: (300, 160), CM.id: (40, 20), WK.id: (25, 10)})
        advice = a.advise(adv.Draft(enemies=[PL]), 3)
        self.assertNotIn(TIDE, [s.hero for s in advice.picks])
        tide = next(s for s in advice.targeted if s.hero == TIDE)
        self.assertEqual(tide.reason_parts()[0], ("unfamiliar", "不熟：生涯 5 局 20%"))
        self.assertTrue(all(s.unfamiliar is None for s in advice.picks))
        v = adv.render_view(advice, None)
        self.assertEqual([(g["title"], g["note"]) for g in v["groups"]],
                         [("推荐", "你打过 20 局以上的"), ("针对这局", "克制 + 配合最多的，不管熟不熟")])
        lines = [t for _, t in adv.render_lines(advice, None)]
        self.assertIn("推荐（你打过 20 局以上的）", lines)
        # 两组显示一样（总胜率 + 同一行算法），「针对这局」只是按克制 + 配合（= 总胜率 − 自身）排
        b = {k: val for k, _, val in tide.breakdown()}
        self.assertIn(f"潮汐猎人  {tide.winrate:.1f}%（克制 {b['counter']:+.1f} 配合 {b['syn']:+.1f}）　不熟", "\n".join(lines))
        self.assertEqual([g["targeted"] for g in v["groups"]], [False, True])      # 网页上「针对这局」卡片单独标克制、配合
        self.assertIn(("dim", "　算法 " + tide.calc_text("三号位")), adv.render_lines(advice, None))
        self.assertAlmostEqual(tide.situational_pct, tide.winrate - sigmoid(tide.base) * 100)
        situ = [s.situational_pct for s in advice.targeted]
        self.assertEqual(situ, sorted(situ, reverse=True))

    def test_never_played_and_pool(self):
        # 生涯里没有的英雄 =「没玩过」；写进手写英雄池的算会玩
        a = advisor(career={AXE.id: (300, 160)})
        a.pool = [TIDE]
        advice = a.advise(adv.Draft(enemies=[PL]), 3)
        self.assertEqual({s.hero for s in advice.picks}, {TIDE, AXE})
        res = a._suggest(adv.Draft(enemies=[PL]), 3)
        a._split(res, 2, 2, True, a.career, a.pool)
        cm = next(s for s in res if s.hero == CM)
        self.assertEqual(cm.reason_parts()[0], ("unfamiliar", "不熟：没玩过"))

    def test_no_career_or_threshold_off_keeps_old_behavior(self):
        for a in (advisor(career=None), advisor(career={AXE.id: (300, 160)}, familiar_games=0)):
            advice = a.advise(adv.Draft(enemies=[PL]), 3)
            self.assertEqual(advice.picks[0].hero, TIDE)
            self.assertEqual(advice.familiar_min, 0)
            self.assertTrue(all(s.unfamiliar is None for s in advice.picks + advice.targeted))
            self.assertEqual(adv.render_view(advice, None)["groups"][0]["note"], "")

    def test_duo_after_my_pick(self):
        # 我选好了：空位置给队友的推荐按双排队友会玩的分；他只会斧王 → 斧王所在那个位置推荐只有斧王，潮汐进针对且标不熟
        # （同一个英雄只在一个空位置出现，这个小数据里分不出位置，都归到第一个空位置）
        a = advisor(career={TIDE.id: (500, 260)}, duo={"name": "老王", "career": {AXE.id: (100, 50)}})
        advice = a.advise(adv.Draft(enemies=[PL], my_hero=WK), 3)
        self.assertEqual(advice.duo, "老王")
        open_slots = [t for t in advice.team if t.open]
        axe_slot = next(t for t in open_slots if any(s.hero == AXE for s in t.picks))
        self.assertEqual([s.hero for s in axe_slot.picks], [AXE])
        tide = next(s for t in open_slots for s in t.targeted if s.hero == TIDE)
        self.assertEqual(tide.unfamiliar, (0, 0))
        v = adv.render_view(advice, None)
        self.assertEqual(v["duo"], "老王")
        row = next(r for r in v["team"] if r["picks"])
        self.assertNotIn("unfamiliar", row["picks"][0])
        self.assertEqual(next(p for p in row["targeted"] if p["id"] == TIDE.id)["unfamiliar"], [0, 0])
        line = next(t for _, t in adv.render_lines(advice, None) if t.startswith("剩余位置"))
        self.assertTrue(line.startswith("剩余位置：（给队友，按老王会玩的分）"))
        self.assertIn("潮汐猎人", line)
        self.assertIn("（不熟）", line)
        json.dumps(v, ensure_ascii=False)

    def test_without_duo_teammates_not_gated(self):
        # 没填双排队友：给队友的推荐不按我的熟练度分（那是给别人的）
        a = advisor(career={AXE.id: (300, 160)})
        advice = a.advise(adv.Draft(enemies=[PL], my_hero=WK), 3)
        self.assertIsNone(advice.duo)
        self.assertTrue(all(s.unfamiliar is None for t in advice.team for s in t.picks + t.targeted))


class BreakdownTest(unittest.TestCase):
    def test_steps_add_up_to_winrate(self):
        stats =make_stats({(TIDE.id, PL.id): (20000, 0.56)})
        stats["syn"] = [[min(TIDE.id, CM.id), max(TIDE.id, CM.id), 20000, 11000]]
        eng = Engine(HeroDB(HEROES), stats, positions={TIDE.id: {3: (6000, 3300), 1: (2000, 900)}})
        s = next(x for x in eng.suggest(position=3, allies=[CM], enemies=[PL], position_filter=False, top=None)
                 if x.hero == TIDE)
        steps = s.breakdown()
        kinds = [k for k, _, _ in steps]
        self.assertEqual(kinds, ["start", "pos", "shrink", "base", "counter", "syn", "other"])
        total = sum(v for k, _, v in steps if k != "base")
        self.assertAlmostEqual(total, s.winrate, places=9)
        b = {k: v for k, _, v in steps}
        self.assertAlmostEqual(b["base"], sigmoid(s.base) * 100, places=9)
        self.assertAlmostEqual(b["start"] + b["pos"] + b["shrink"], b["base"], places=9)
        self.assertGreater(b["counter"], 0)
        self.assertGreater(b["syn"], 0)
        self.assertGreater(b["pos"], 0)           # 潮汐打三号位比整体强
        self.assertAlmostEqual(b["other"], 0, places=9)
        text = s.calc_text("三号位")
        self.assertTrue(text.startswith(f"自身 {b['base']:.1f}%（整体 {b['start']:.1f}、三号位 +"))
        self.assertIn(f"＋克制 {b['counter']:.1f} ", text)
        self.assertTrue(text.endswith(f"＝ {s.winrate:.1f}%"))

    def test_negative_synergy_shown(self):
        # 负的配合和克制一样显示：网页「配合」一行每个队友一格（负的也有）、命令行一行、理由里「不配」
        s = Suggestion(TIDE, 0.0, 0.0, syn=[(CM, 0.08), (AXE, -0.08)])
        self.assertIn(("bad", "不配 斧王(-2.0%)"), s.reason_parts())
        self.assertIn(("syn", "配 水晶室女(+2.0%)"), s.reason_parts())
        advice = adv.Advice(3, adv.Draft(allies=[CM, AXE]), [s], [], None)
        item = adv.render_view(advice, None)["groups"][0]["items"][0]
        self.assertEqual(item["syn"], [{"id": CM.id, "name": CM.name_cn, "delta": 2.0},
                                       {"id": AXE.id, "name": AXE.name_cn, "delta": -2.0}])
        self.assertIn(("dim", "　配合 水晶室女 +2.0  斧王 -2.0"), adv.render_lines(advice, None))

    def test_view_and_bare_suggestion(self):
        # 手造的 Suggestion（没有整体 / 位置两步）也能算：起点就是自身强度
        s = Suggestion(TIDE, 0.2, 0.1, vs=[(PL, 0.1)])
        self.assertEqual([k for k, _, v in s.breakdown() if abs(v) > 1e-12 and k not in ("start", "base")], ["other"])
        advice = adv.Advice(3, adv.Draft(enemies=[PL]), [s], [], None)
        calc = adv.render_view(advice, None)["groups"][0]["items"][0]["calc"]
        self.assertEqual(calc[1], {"kind": "pos", "name": "三号位", "value": 0.0})
        self.assertIn(("dim", "　算法 " + s.calc_text("三号位")), adv.render_lines(advice, None))


class TeamRateTest(unittest.TestCase):
    """选它之后全队的胜率：卡片胜率（这手）+ 已选队友 + 已选敌人 + 阵营，和回测预测整局胜负的式子一致"""

    def engine(self):
        stats = make_stats({(TIDE.id, PL.id): (20000, 0.56), (CM.id, PL.id): (20000, 0.45), (AXE.id, WK.id): (20000, 0.53)})
        stats["syn"] = [[min(TIDE.id, CM.id), max(TIDE.id, CM.id), 20000, 11000],
                        [min(PL.id, WK.id), max(PL.id, WK.id), 20000, 10600]]
        return Engine(HeroDB(HEROES), stats)

    def test_same_formula_as_whole_draft(self):
        # 全队 logit = 我方各人自身强度之和 − 对面之和 + 我方每人对对面每人的克制 + 我方两两配合 − 对面两两配合（backtest.predict_drafts）
        eng = self.engine()
        allies, enemies = [CM], [PL, WK]
        s = next(x for x in eng.suggest(position=3, allies=allies, enemies=enemies, position_filter=False, top=None)
                 if x.hero == TIDE)
        s.team = eng.draft_rest([(a.id, None) for a in allies], [(e.id, None) for e in enemies])
        ours, theirs = [TIDE.id, CM.id], [PL.id, WK.id]
        whole = (sum(eng.base[h] for h in ours) - sum(eng.base[h] for h in theirs)
                 + sum(eng.advantage(a, e) for a in ours for e in theirs)
                 + eng.synergy(TIDE.id, CM.id) - eng.synergy(PL.id, WK.id))
        self.assertAlmostEqual(s.team_winrate(None), sigmoid(whole) * 100, places=9)
        self.assertAlmostEqual(s.team_winrate("radiant"), sigmoid(whole + E.RADIANT_EDGE) * 100, places=9)
        self.assertAlmostEqual(s.team_winrate("dire"), sigmoid(whole - E.RADIANT_EDGE) * 100, places=9)
        # 各步加起来正好等于全队胜率，第一步就是卡片上的胜率
        steps = s.team_breakdown("radiant")
        self.assertEqual([(k, n) for k, n, _ in steps], [("start", "这手"), ("ally", "队友"), ("enemy", "对面"), ("side", "天辉")])
        self.assertAlmostEqual(steps[0][2], s.winrate, places=9)
        self.assertAlmostEqual(sum(v for _, _, v in steps), s.team_winrate("radiant"), places=9)
        self.assertLess(steps[1][2], 0)           # 水晶室女被幻影长矛手克
        self.assertLess(steps[2][2], 0)           # 对面幻影长矛手 + 冥魂大帝配合好
        self.assertEqual(s.team_breakdown(None)[3], ("side", "阵营", 0.0))
        text = s.team_text("dire")
        self.assertTrue(text.startswith(f"全队 {s.team_winrate('dire'):.1f}%＝这手 {s.winrate:.1f} －队友 "))
        self.assertIn("－夜魇", text)

    def test_advise_shows_team_only_with_draft(self):
        a = advisor()
        a.engine = self.engine()
        empty = a.advise(adv.Draft(), 3)
        self.assertTrue(all(s.team is None for s in empty.picks))
        self.assertIsNone(adv.render_view(empty, "radiant")["groups"][0]["items"][0]["team"])
        self.assertFalse(any(t.startswith("　全队") for _, t in adv.render_lines(empty, "radiant")))
        advice = a.advise(adv.Draft(allies=[CM], enemies=[PL, WK]), 3)
        first = advice.picks[0]
        self.assertEqual(first.hero, TIDE)                         # 排名没变：全队那几项对每个候选都一样
        self.assertEqual(len({s.team for s in advice.picks + advice.targeted}), 1)
        self.assertIn(("dim", "　" + first.team_text("radiant")), adv.render_lines(advice, "radiant"))
        team = adv.render_view(advice, "radiant")["groups"][0]["items"][0]["team"]
        self.assertEqual(team["winrate"], round(first.team_winrate("radiant"), 1))
        self.assertEqual([c["kind"] for c in team["calc"]], ["start", "ally", "enemy", "side"])
        json.dumps(team, ensure_ascii=False)


class CareerDataTest(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp(dir=TMP))
        patches = [mock.patch.object(player, "PLAYER_FILE", d / "player.json"),
                   mock.patch.object(player, "DUO_FILE", d / "duo.json")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_fetch_career(self):
        rows = [{"hero_id": 2, "games": 30, "win": 16}, {"hero_id": 5, "games": 0, "win": 0}]
        with mock.patch.object(http, "_get", return_value=rows):
            self.assertEqual(player.fetch_career(1), {"2": [30, 16]})
        # 资料没公开：OpenDota 照样回全部英雄、局数全 0 → 不知道
        with mock.patch.object(http, "_get", return_value=[{"hero_id": 2, "games": 0, "win": 0}]):
            self.assertIsNone(player.fetch_career(1))
        with mock.patch.object(http, "_get", side_effect=RuntimeError("超时")):
            self.assertIsNone(player.fetch_career(1, log=lambda m: None))

    def test_duo_roundtrip_and_needs_update(self):
        cfg = Config(duo_account_id=7)
        self.assertTrue(player.needs_update(cfg))
        prof = {"profile": {"personaname": "老王"}}
        rows = [{"hero_id": 2, "games": 30, "win": 16}]
        with mock.patch.object(http, "_get", side_effect=lambda url, **kw: rows if url.endswith("/heroes") else prof):
            player.update_player(cfg, log=lambda m: None)
        self.assertEqual(player.load_duo(cfg), {"fetched_at": mock.ANY, "account_id": 7, "name": "老王",
                                                "career": {2: (30, 16)}})
        self.assertFalse(player.needs_update(cfg))
        self.assertIsNone(player.load_duo(Config(duo_account_id=8)))      # 换了人：旧的不用
        self.assertTrue(player.needs_update(Config(duo_account_id=8)))

    def test_old_player_file_without_career_needs_update(self):
        player.PLAYER_FILE.write_text(json.dumps({"account_id": 1, "heroes": {}}), encoding="utf-8")
        self.assertTrue(player.needs_update(Config(account_id=1)))
        self.assertIsNone(player.load_player(Config(account_id=1))["career"])
        player.PLAYER_FILE.write_text(json.dumps({"account_id": 1, "heroes": {}, "career": None}), encoding="utf-8")
        self.assertFalse(player.needs_update(Config(account_id=1)))      # 拉过、只是没公开：不每次启动都重拉


if __name__ == "__main__":
    unittest.main()
