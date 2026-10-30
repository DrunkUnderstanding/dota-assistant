"""网页界面：结构化视图、Live 主控（含局内缺口提醒）、本地服务。不联网、不出声、不开窗口。"""
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace

import cv2
import numpy as np

from dota_assistant.domain import advisor as adv
from dota_assistant.vision import grid
from dota_assistant.core import config
from dota_assistant.domain import brief
from dota_assistant.vision import buybackscan
from dota_assistant.domain import lanelog
from dota_assistant.domain import coach
from dota_assistant.domain import death
from dota_assistant.ai import gameplan
from dota_assistant.app.live import consts as live_consts
from dota_assistant.app.live import draft as live_draft
from dota_assistant.domain import mates as mates_mod
from dota_assistant.core.config import Config
from dota_assistant.domain.engine import Engine, Suggestion
from dota_assistant.game.gsi import GameState
from pathlib import Path
from dota_assistant.core.heroes import HeroDB
from dota_assistant.app.live import Live
from dota_assistant.vision.recognize import Calibration
from dota_assistant.app.webui import WebServer, overlay_config
from tests.test_engine import AXE, CM, HEROES, PL, TIDE, WK, make_stats


class FakeSpeaker:
    def __init__(self):
        self.enabled = True
        self.said: list[str] = []

    def say(self, text):
        if self.enabled:
            self.said.append(text)

    def close(self):
        pass


class FakeBank:
    portraits, learned = 0, 0

    def scores(self, img):
        return {}


def fake_advisor(cfg: Config) -> adv.Advisor:
    a = adv.Advisor.__new__(adv.Advisor)
    a.cfg, a.db, a.me, a.pool, a.kb = cfg, HeroDB(HEROES), None, [], None
    a.career, a.duo = None, None             # 没生涯数据：推荐不按熟练度分（FamiliarTest 里再填）
    a.engine = Engine(a.db, make_stats({(TIDE.id, PL.id): (20000, 0.56)}))
    return a


TMP = Path(tempfile.mkdtemp(prefix="dota-webui-"))
lanelog.LOG_FILE = TMP / "lane_log.jsonl"                      # 3 / 6 / 10 分钟的对线记录不写本机 data/
live_consts.FIX_FILE = TMP / "fixes.jsonl"                           # 帮认记录也不写本机 data/
mates_mod.MATES_FILE = TMP / "mates.json"                         # 查过的队友也不读写本机 data/


def no_brief(*args, **kw):
    raise RuntimeError("测试里不联网")


def make_live(overlay_file: Path | None = None, coach_prepare=None, brief_build=no_brief, plan_build=no_brief) -> Live:
    cfg = Config(position_filter=False, top=2, top_other=2, voice=True)
    live = Live(cfg, screen_factory=lambda: None, use_gsi=False, use_hotkeys=False, speaker=FakeSpeaker(),
                advisor=fake_advisor(cfg), bank=FakeBank(), calibration=Calibration(),   # 空校准，不读本机 calibration.json
                overlay_file=overlay_file or TMP / f"overlay-{time.monotonic_ns()}.json",
                coach_prepare=coach_prepare, brief_build=brief_build, plan_build=plan_build,
                ban_watcher=grid.BanWatcher(TMP / f"grid-{time.monotonic_ns()}.json"))   # 不读本机 data/grid_map.json
    live.config_file = TMP / f"config-{time.monotonic_ns()}.toml"                  # 账号设置不写本机 config.toml
    return live


def wait(fut):
    return fut.result(timeout=5)


class RenderViewTest(unittest.TestCase):
    def test_structure_matches_lines(self):
        s = Suggestion(TIDE, 0.2, 0.1, vs=[(PL, 0.1)], in_pool=True)
        advice = adv.Advice(3, adv.Draft(allies=[CM], enemies=[PL]), [s], [], (PL, 0.08))
        v = adv.render_view(advice, "dire")
        self.assertEqual(v["side_name"], "夜魇")
        self.assertEqual(v["enemies"], [{"id": PL.id, "name": PL.name_cn}])
        item = v["groups"][0]["items"][0]
        self.assertEqual(item["id"], TIDE.id)
        self.assertEqual("；".join(t["text"] for t in item["tags"]), s.reason())
        self.assertEqual(item["tags"][0]["kind"], "good")
        self.assertEqual(item["vs"], [{"id": PL.id, "name": PL.name_cn, "delta": 2.5}])   # 对每个敌人的克制，百分点
        self.assertEqual(v["threat"]["delta"], 2.0)
        json.dumps(v, ensure_ascii=False)          # 能直接转 JSON
        self.assertIsNone(v["lanes"])              # 没有分位置数据：不猜分路

    def test_targeted_group(self):
        # 综合前 2 个之外，按「克制 + 配合」挑：综合排第 4 的斧王克制最多排第一；综合胜率不到 50% 的冥魂大帝不要
        res = [Suggestion(PL, 0.20, 0.18), Suggestion(TIDE, 0.15, 0.10), Suggestion(CM, 0.06, 0.03),
               Suggestion(AXE, 0.05, -0.05), Suggestion(WK, -0.01, -0.20)]
        picks, targeted = adv.split_targeted(res, 2, 2, has_draft=True)
        self.assertEqual([s.hero for s in picks], [PL, TIDE])
        self.assertEqual([s.hero for s in targeted], [AXE, CM])
        self.assertEqual(adv.split_targeted(res, 2, 2, has_draft=False), (res[:4], []))   # 没人选：只有综合推荐
        self.assertEqual(targeted[0].reason(), "克制 + 配合合计 +2.5%")       # 单项都不显著时说合计，不说「版本强势」
        advice = adv.Advice(1, adv.Draft(enemies=[]), picks, targeted, None)
        self.assertEqual([g["title"] for g in adv.render_view(advice, None)["groups"]], ["推荐", "针对这局"])
        self.assertIn(("head", "针对这局（克制 + 配合最多的）"), adv.render_lines(advice, None))
        self.assertEqual(adv.best(advice), picks)                             # 播报只念综合推荐

    def test_lanes_view(self):
        from dota_assistant.domain import explain
        from dota_assistant.domain import lanes
        positions = {AXE.id: {3: (900, 0)}, CM.id: {5: (900, 0)}, PL.id: {1: (900, 0)}}
        ln = lanes.assign([AXE.id, PL.id], [CM.id], 1, None, positions)
        tip = explain.Why("bad", "小心斧王", "斧王·狂战士之吼：…")
        advice = adv.Advice(1, adv.Draft(allies=[CM], enemies=[AXE, PL], my_hero=TIDE), [], [], None,
                            lanes=ln, lane_tips=[tip])
        v = adv.render_view(advice, "radiant")["lanes"]
        self.assertEqual([r["label"] for r in v["rows"]], ["上路", "中路", "下路"])
        mine = v["rows"][2]
        self.assertTrue(mine["mine"])
        self.assertEqual([(h["name"], h["pos"]) for h in mine["heroes"]], [("斧王", 3)])
        self.assertEqual(mine["missing"], 1)
        self.assertEqual(v["partner"]["name"], "水晶室女")
        self.assertEqual(v["tips"], [tip.__dict__])
        lines = [t for _, t in adv.render_lines(advice, "radiant")]
        self.assertTrue(any(t.startswith("分路（猜）：上路 幻影长矛手（1号位）") for t in lines))
        self.assertIn("　✗ 小心斧王", lines)
        json.dumps(v, ensure_ascii=False)


class TeamSlotsTest(unittest.TestCase):
    def advisor(self, positions=None):
        a = fake_advisor(Config(position_filter=False, top=2, top_other=2))
        a.engine.positions = positions
        return a

    def test_open_positions_for_teammates(self):
        # 我打一号位还没选；队友水晶室女猜五号位 → 我的一号位不列英雄（看大卡片），二~四号位是给队友的推荐；
        # 大卡片里已经给我的潮汐猎人不再列，同一个英雄只在一个空位置出现（2026-10-30 玩家要求）
        a = fake_advisor(Config(position_filter=False, top=1, top_other=0))
        a.engine.positions = {CM.id: {5: (900, 0)}, PL.id: {1: (900, 0)}}
        advice = a.advise(adv.Draft(allies=[CM], enemies=[PL]), 1)
        self.assertEqual([s.hero for s in advice.picks], [TIDE])
        self.assertTrue(advice.team[0].mine and not advice.team[0].picks)
        self.assertEqual([t.position for t in advice.team if t.open], [2, 3, 4])
        taken = advice.team[4]
        self.assertEqual((taken.ally.hero, taken.ally.pos), (CM.id, 5))
        listed = [s.hero for t in advice.team for s in t.picks + t.targeted]
        self.assertEqual(sorted(h.id for h in listed), sorted([AXE.id, WK.id]))     # 只剩斧王、冥魂大帝，各出现一次
        v = adv.render_view(advice, "radiant")["team"]
        self.assertEqual([r["current"] for r in v], [True, False, False, False, False])
        self.assertEqual([r["mine"] for r in v], [True, False, False, False, False])
        self.assertEqual(v[4]["ally"]["name"], "水晶室女")
        json.dumps(v, ensure_ascii=False)
        line = next(t for _, t in adv.render_lines(advice, None) if t.startswith("剩余位置"))
        self.assertTrue(line.startswith("剩余位置：（给队友）"))
        self.assertNotIn("潮汐猎人", line)
        self.assertTrue(line.endswith("你：一号位"))

    def test_ally_takes_my_position(self):
        # 选人界面认出水晶室女打一号位（优先于按比例猜）：我当前的一号位被占，标出来；不再「推荐打 X 号位」
        a = self.advisor({CM.id: {5: (900, 0)}})
        advice = a.advise(adv.Draft(allies=[CM], enemies=[PL], ally_pos={CM.id: 1}), 1)
        self.assertFalse(advice.team[0].open)
        self.assertFalse(any(t.mine for t in advice.team))
        line = next(t for _, t in adv.render_lines(advice, None) if t.startswith("剩余位置"))
        self.assertIn("队友占了：一号位（当前） 水晶室女", line)
        self.assertNotIn("推荐打", line)

    def test_no_allies(self):
        a = self.advisor()
        self.assertEqual(a.advise(adv.Draft(enemies=[PL]), 1).team, [])
        self.assertIsNone(adv.render_view(a.advise(adv.Draft(enemies=[PL]), 1), None)["team"])

    def test_after_my_pick_recommends_for_allies(self):
        # 我选好潮汐猎人打三号位：我占三号位（队友不会被猜到这里），空位置推荐给队友（不含已选的），不再推荐我打哪个位置
        a = self.advisor({CM.id: {3: (900, 0), 5: (100, 0)}})
        advice = a.advise(adv.Draft(allies=[CM], enemies=[PL], my_hero=TIDE), 3)
        me = advice.team[2]
        self.assertTrue(me.me)
        self.assertEqual(me.ally.hero, TIDE.id)
        self.assertEqual(advice.team[4].ally.hero, CM.id)
        self.assertEqual([t.position for t in advice.team if t.open], [1, 2, 4])
        picked = {s.hero for t in advice.team for s in t.picks}
        self.assertTrue(picked and not picked & {TIDE, CM, PL})
        listed = [s.hero.id for t in advice.team for s in t.picks + t.targeted]
        self.assertEqual(len(listed), len(set(listed)))                     # 同一个英雄只在一个空位置出现
        v = adv.render_view(advice, None)["team"]
        self.assertEqual([r["me"] for r in v], [False, False, True, False, False])
        self.assertFalse(any(r["mine"] for r in v))
        line = next(t for _, t in adv.render_lines(advice, None) if t.startswith("剩余位置"))
        self.assertTrue(line.startswith("剩余位置：（给队友）"))
        self.assertIn("三号位 你 潮汐猎人", line)
        # 只有我选了、队友一个没选：也显示
        self.assertEqual(len(a.advise(adv.Draft(enemies=[PL], my_hero=TIDE), 3).team), 5)


class LiveTest(unittest.TestCase):
    def setUp(self):
        self.live = make_live().start()

    def tearDown(self):
        self.live.close()

    def snap(self):
        return json.loads(self.live.current_snapshot)

    def test_slot_edit_updates_view_and_speech(self):
        wait(self.live.submit(self.live.action, {"type": "set_slot", "side": "dire", "index": 0, "text": "PL"}))
        s = self.snap()
        self.assertEqual(s["slots"]["dire"][0]["hero_id"], PL.id)
        view = s["overlay"]["view"]
        self.assertEqual([h["id"] for h in view["enemies"]], [PL.id])
        self.assertEqual(view["groups"][0]["items"][0]["id"], TIDE.id)    # 克制 PL 的潮汐排第一
        self.assertIn("对面拿了幻影长矛手", self.live.speaker.said[-1])

    def test_bad_name_marked_invalid(self):
        wait(self.live.submit(self.live.action, {"type": "set_slot", "side": "radiant", "index": 2, "text": "不存在"}))
        slot = self.snap()["slots"]["radiant"][2]
        self.assertFalse(slot["valid"])
        self.assertIsNone(slot["hero_id"])

    def test_settings_and_clear(self):
        wait(self.live.submit(self.live.action, {"type": "set", "key": "side_mode", "value": "dire"}))
        wait(self.live.submit(self.live.action, {"type": "set", "key": "position", "value": 5}))
        wait(self.live.submit(self.live.action, {"type": "set_slot", "side": "radiant", "index": 0, "text": "斧王"}))
        s = self.snap()
        self.assertEqual(s["overlay"]["view"]["side"], "dire")
        self.assertEqual(s["overlay"]["view"]["position"], 5)
        self.assertEqual([h["id"] for h in s["overlay"]["view"]["enemies"]], [AXE.id])   # 我在夜魇，天辉是对面
        wait(self.live.submit(self.live.action, {"type": "clear"}))
        self.assertEqual(self.snap()["slots"]["radiant"][0]["text"], "")

    def test_unknown_side_is_flagged(self):
        # 阵营自动、GSI 没连上：先当天辉算，但卡片要标出来（以前悄悄当天辉，人在夜魇时友敌整个反了）
        wait(self.live.submit(self.live.action, {"type": "set_slot", "side": "radiant", "index": 0, "text": "斧王"}))
        view = self.snap()["overlay"]["view"]
        self.assertIsNone(view["side"])
        self.assertEqual(view["side_assumed"], "radiant")
        self.assertEqual([h["id"] for h in view["allies"]], [AXE.id])
        wait(self.live.submit(self.live.action, {"type": "set", "key": "side_mode", "value": "dire"}))
        view = self.snap()["overlay"]["view"]
        self.assertNotIn("side_assumed", view)
        self.assertEqual([h["id"] for h in view["enemies"]], [AXE.id])

    def test_gsi_connects_mid_draft_resplits_teams(self):
        wait(self.live.submit(self.live.action, {"type": "set_slot", "side": "radiant", "index": 0, "text": "斧王"}))
        state = GameState()
        state.update({"player": {"team_name": "dire"}})       # 真实游戏第一条推送就只有 draft / player / provider
        self.live.gsi = SimpleNamespace(state=state, stop=lambda: None)
        wait(self.live.submit(self.live.tick))
        view = self.snap()["overlay"]["view"]
        self.assertEqual(view["side"], "dire")
        self.assertEqual([h["id"] for h in view["enemies"]], [AXE.id])

    def test_gsi_status_explains_missing_dota(self):
        self.live.use_gsi, self.live.gsi = True, SimpleNamespace(state=GameState(), stop=lambda: None)
        self.live.dota_dir = None
        self.assertEqual(self.live.status()["gsi"]["state"], "nodota")
        self.live.dota_dir = Path("E:/SteamLibrary/steamapps/common/dota 2 beta")
        g = self.live.status()["gsi"]
        self.assertEqual(g["state"], "off")
        self.assertIn("-gamestateintegration", g["tip"])

    def test_dota_dir_setting(self):
        dota = TMP / f"lib-{time.monotonic_ns()}" / "dota 2 beta"
        (dota / "game" / "dota").mkdir(parents=True)
        save = lambda **kw: wait(self.live.submit(self.live.action, {"type": "save_settings", **kw}))
        with self.assertRaises(ValueError):                     # 空壳：没有资源包
            save(dota_dir=str(dota))
        (dota / "game" / "dota" / "pak01_dir.vpk").write_bytes(b"")
        self.live.use_gsi = True
        save(dota_dir=f'"{dota / "game" / "dota"}"')            # 选深了一层、带引号也认
        self.assertEqual(config.load(self.live.config_file).dota_dir, str(dota))
        self.assertEqual(self.live.dota_dir, dota)
        self.assertTrue((dota / "game" / "dota" / "cfg" / "gamestate_integration").is_dir())   # GSI 配置马上写进去
        self.assertIn("重开 Dota", self.live.message)
        self.assertEqual(self.snap()["config"]["dota_found"], str(dota))

    def test_unknown_action_rejected(self):
        with self.assertRaises(ValueError):
            wait(self.live.submit(self.live.action, {"type": "rm -rf"}))

    def test_toggle_and_notice(self):
        wait(self.live.submit(self.live.action, {"type": "toggle_overlay"}))
        self.assertFalse(self.snap()["overlay"]["visible"])
        wait(self.live.submit(self.live.show_notice, "局内提醒 · 10:00", [("warn", "该出黑皇杖了")]))
        self.assertEqual(self.snap()["overlay"]["view"]["kind"], "notice")
        wait(self.live.submit(self.live.action, {"type": "refresh"}))
        self.assertEqual(self.snap()["overlay"]["view"]["kind"], "draft")   # 刷新后换回推荐

    def test_unlock_drag_and_lock_persist(self):
        self.assertTrue(self.snap()["overlay"]["locked"])
        wait(self.live.submit(self.live.action, {"type": "toggle_overlay"}))
        wait(self.live.submit(self.live.action, {"type": "toggle_lock"}))
        s = self.snap()
        self.assertFalse(s["overlay"]["locked"])
        self.assertTrue(s["overlay"]["visible"])                  # 解锁时就算隐藏过也要显示出来，好摆位置
        self.assertIn("F7", s["status"]["message"])
        wait(self.live.submit(self.live.action, {"type": "overlay_moved", "x": 0.5, "y": 0.1}))
        with self.assertRaises(ValueError):
            wait(self.live.submit(self.live.action, {"type": "overlay_moved", "x": 50, "y": 0.1}))
        wait(self.live.submit(self.live.action, {"type": "set", "key": "overlay_locked", "value": True}))
        self.assertTrue(self.snap()["overlay"]["locked"])
        self.assertFalse(self.snap()["overlay"]["visible"])
        again = make_live(self.live.overlay_file)                  # 重启后沿用位置和固定状态
        try:
            self.assertEqual(again.overlay_pos, {"x": 0.5, "y": 0.1})
            self.assertTrue(again.overlay_locked)
            self.assertEqual(overlay_config(again.cfg, again.overlay_pos)["x"], 0.5)
            self.assertEqual(overlay_config(again.cfg)["x"], again.cfg.overlay_x)
            # 拉过宽度：记下来，之后只拖位置不丢宽度
            again.action({"type": "overlay_moved", "x": 0.5, "y": 0.1, "width": 720})
            again.action({"type": "overlay_moved", "x": 0.4, "y": 0.1})
            self.assertEqual(overlay_config(again.cfg, again.overlay_pos)["width"], 720)
            with self.assertRaises(ValueError):
                again.overlay_moved(0.4, 0.1, 50)
            third = make_live(self.live.overlay_file)
            try:
                self.assertEqual(third.overlay_pos, {"x": 0.4, "y": 0.1, "width": 720})
            finally:
                third.close()
            self.assertEqual(overlay_config(again.cfg)["width"], again.cfg.overlay_width)
            self.assertNotIn("height", overlay_config(again.cfg, again.overlay_pos))   # 没拉过高度 = 到屏幕底
            # 拉过高度、拖到副屏（副屏在主屏左边，左上角是负数）：都记下来，之后只拖位置不丢
            again.action({"type": "overlay_moved", "x": 0.2, "y": 0.3, "height": 500, "screen": {"x": -1920, "y": 0}})
            again.action({"type": "overlay_moved", "x": 0.25, "y": 0.3})
            cfg = overlay_config(again.cfg, again.overlay_pos)
            self.assertEqual((cfg["width"], cfg["height"], cfg["screen"]), (720, 500, {"x": -1920, "y": 0}))
            for bad in ({"height": 50}, {"screen": {"x": 1.5, "y": 0}}, {"screen": {"x": True, "y": 0}}, {"screen": [0, 0]}):
                with self.assertRaises(ValueError):
                    again.action({"type": "overlay_moved", "x": 0.2, "y": 0.3, **bad})
            fourth = make_live(self.live.overlay_file)
            try:
                self.assertEqual(fourth.overlay_pos, {"x": 0.25, "y": 0.3, "width": 720, "height": 500,
                                                      "screen": {"x": -1920, "y": 0}})
            finally:
                fourth.close()
        finally:
            again.close()

    def test_overlay_style(self):
        self.live.config_file.write_text('overlay_opacity = 0.85   # 不透明度\n', encoding="utf-8")
        set_ = lambda **kw: wait(self.live.submit(self.live.action, {"type": "set", **kw}))
        set_(key="overlay_opacity", value=0.6, save=False)                    # 拖滑块：叠加层马上变，先不写文件
        self.assertEqual(self.snap()["overlay"]["style"]["opacity"], 0.6)
        self.assertEqual(config.load(self.live.config_file).overlay_opacity, 0.85)
        set_(key="overlay_opacity", value=0.05)                               # 松手：写进去，太透的收到 0.3
        set_(key="overlay_color", value="#1A2B3C")
        saved = config.load(self.live.config_file)
        self.assertEqual((saved.overlay_opacity, saved.overlay_color), (0.3, "#1a2b3c"))
        self.assertIn("# 不透明度", self.live.config_file.read_text(encoding="utf-8"))
        self.assertEqual(self.snap()["overlay"]["style"], {"opacity": 0.3, "color": "#1a2b3c"})
        with self.assertRaises(ValueError):
            set_(key="overlay_color", value="red")

    def test_close_button(self):
        wait(self.live.submit(self.live.action, {"type": "toggle_lock"}))
        wait(self.live.submit(self.live.action, {"type": "close_overlay"}))
        s = self.snap()
        self.assertFalse(s["overlay"]["visible"])                 # 解锁着点 × 也能关，顺便固定
        self.assertTrue(s["overlay"]["locked"])
        self.assertIn("F8", s["status"]["message"])
        wait(self.live.submit(self.live.action, {"type": "toggle_overlay"}))
        self.assertTrue(self.snap()["overlay"]["visible"])        # 热键重新打开
        self.live.coach_card = {"kind": "notice", "title": "x", "lines": [], "closable": True}
        wait(self.live.submit(self.live.action, {"type": "close_overlay"}))
        self.assertIsNone(self.live.coach_card)                   # 弹着提醒时只关提醒
        self.assertFalse(self.live.user_hidden)

    def test_settings_saved_and_masked(self):
        self.live.config_file.write_text('account_id = 0   # 好友代码\nstratz_token = ""\ndays = 14\n', encoding="utf-8")
        save = lambda **kw: wait(self.live.submit(self.live.action, {"type": "save_settings", **kw}))
        save(account_id="123456789", stratz_token="eyJabc.def-1234", ai_provider="openai", ai_model="deepseek-chat",
             ai_api_key="sk-secret-5678", ai_base_url="https://api.deepseek.com/v1", ai_plan=False)
        saved = config.load(self.live.config_file)
        self.assertEqual((saved.account_id, saved.stratz_token, saved.days), (123456789, "eyJabc.def-1234", 14))
        self.assertEqual((saved.ai_provider, saved.ai_model, saved.ai_api_key, saved.ai_plan),
                         ("openai", "deepseek-chat", "sk-secret-5678", False))
        self.assertIn("# 好友代码", self.live.config_file.read_text(encoding="utf-8"))   # 注释原样保留
        self.assertEqual(self.live.cfg.ai_api_key, "sk-secret-5678")                    # 不用重启，下一局就用
        c = self.snap()["config"]
        self.assertEqual((c["stratz_token"], c["ai_api_key"]), ({"set": True, "tail": "1234"}, {"set": True, "tail": "5678"}))
        for secret in ("eyJabc", "sk-secret"):
            self.assertNotIn(secret, self.live.current_snapshot)                         # 完整密钥不发给页面
        save(account_id="123456789")
        self.assertEqual(config.load(self.live.config_file).stratz_token, "eyJabc.def-1234")   # 没传 = 不改
        save(stratz_token="")
        self.assertFalse(self.snap()["config"]["stratz_token"]["set"])
        for bad in ({"account_id": "abc"}, {"stratz_token": "Bearer xyz"}, {"ai_provider": "gpt"},
                    {"ai_model": " "}, {"ai_base_url": "api.deepseek.com"}):
            with self.assertRaises(ValueError):
                save(**bad)

    def test_hotkeys_rebind(self):
        self.live.config_file.write_text('hotkey_brief = "F6"   # 本局要点\n', encoding="utf-8")
        save = lambda **kw: wait(self.live.submit(self.live.action, {"type": "save_settings", **kw}))
        save(hotkeys={"hotkey_brief": "f11", "hotkey_shot": "F12"})
        saved = config.load(self.live.config_file)
        self.assertEqual((saved.hotkey_brief, saved.hotkey_shot), ("F11", "F12"))
        self.assertIn("# 本局要点", self.live.config_file.read_text(encoding="utf-8"))
        keys = {h["text"]: h["key"] for h in self.snap()["status"]["hotkeys"]}
        self.assertEqual(keys["本局要点 / 翻页"], "F11")
        self.assertIn({"field": "hotkey_brief", "key": "F11", "text": "本局要点 / 翻页"}, self.snap()["config"]["hotkeys"])
        for bad in ({"hotkey_brief": "F8"},                    # 和「显隐」撞键
                    {"hotkey_brief": "Ctrl+A"}, {"hotkey_nope": "F2"}):
            with self.assertRaises(ValueError):
                save(hotkeys=bad)
        self.assertEqual(self.live.cfg.hotkey_brief, "F11")    # 报错不改

    def test_rescan_without_calibration_reports(self):
        msg = wait(self.live.submit(self.live.action, {"type": "rescan"}))
        self.assertIn("还没校准", msg)


class CoachLiveTest(unittest.TestCase):
    """GSI 推送 → 开局建 Coach → 死亡 / 每 5 分钟弹卡片 → 关闭 / 比赛结束收起"""

    def setUp(self):
        self.prepared = []

        def prepare(cfg, my_id, enemy_ids, pos):
            self.prepared.append((my_id, sorted(enemy_ids), pos))
            need = coach.Need("bkb", "缺黑皇杖", ("black_king_bar",), due=20, priority=0)
            return coach.Coach([need], None, {}, None), ["对面能隐身，提醒辅助带粉"]

        self.live = make_live(coach_prepare=prepare)
        self.live.gsi = SimpleNamespace(state=GameState(), stop=lambda: None)   # 不开端口，直接改推送内容
        self.live.slots["dire"][:2] = ["斧王", "潮汐猎人"]
        self.live.start()

    def tearDown(self):
        self.live.close()

    def push(self, minute, state="DOTA_GAMERULES_STATE_GAME_IN_PROGRESS", deaths=0, items=(), hp=None, alive=True,
             now=None):
        st = self.live.gsi.state
        hero = {"name": "npc_dota_hero_phantom_lancer", "alive": alive}
        if hp is not None:
            hero |= {"health": hp, "max_health": 100}
        st.update({"map": {"matchid": "123", "game_state": state, "clock_time": int(minute * 60)},
                   "player": {"team_name": "radiant", "deaths": deaths, "last_hits": 40},
                   "hero": hero,
                   "items": {f"slot{i}": {"name": f"item_{k}"} for i, k in enumerate(items)}
                            | {"slot8": {"name": "empty"}}}, now)
        wait(self.live.submit(self.live.coach_tick))
        return json.loads(self.live.current_snapshot)["overlay"]

    def wait_coach(self):
        for _ in range(50):                                     # 后台线程准备好
            if self.live.coach:
                return
            time.sleep(0.05)

    def test_team_kept_after_pick_until_game(self):
        # 选人阶段选好了英雄：剩余位置还在（给队友的推荐）；出门进游戏就不显示了
        self.live.slots["radiant"][:2] = ["幻影长矛手", "水晶室女"]
        self.live.coach_card = None
        self.push(-1, state="DOTA_GAMERULES_STATE_HERO_SELECTION")
        wait(self.live.submit(self.live.refresh))               # 实际是顶栏识别到我的英雄时刷新
        ov = json.loads(self.live.current_snapshot)["overlay"]
        self.assertEqual(ov["view"]["my_hero"]["name"], "幻影长矛手")
        self.assertTrue(any(r["me"] for r in ov["view"]["team"]))
        self.push(-1, state="DOTA_GAMERULES_STATE_PRE_GAME")
        self.live.coach_card = None
        wait(self.live.submit(self.live.publish))
        self.assertIsNone(json.loads(self.live.current_snapshot)["overlay"]["view"]["team"])

    def test_new_draft_restores_closed_overlay(self):
        # 上一局点 × 收起了叠加层：进下一局选人自动显示，不用按 F8
        self.live.user_hidden = True
        self.live.gsi.state.update({"map": {"matchid": "1", "game_state": "DOTA_GAMERULES_STATE_HERO_SELECTION"},
                                    "player": {"team_name": "radiant"}})
        wait(self.live.submit(self.live.coach_tick))
        self.assertFalse(self.live.user_hidden)

    def test_auto_position_from_role_label(self):
        # 天梯选人：顶栏认出我排的是 4 号位 → 推荐按 4 号位；下一局（加速，认不出）回到原来的位置
        calls = []
        def fake_roles(img, cal, side, name, slot):
            calls.append((side, name, slot))
            return [3, 5, 2, 4, 1], 3
        self.live._read_roles = fake_roles
        self.live.cal = SimpleNamespace(ready=True)
        st = self.live.gsi.state
        st.update({"map": {"matchid": "1", "game_state": "DOTA_GAMERULES_STATE_HERO_SELECTION"},
                   "player": {"team_name": "radiant", "name": "醉后才知道", "team_slot": 3}})
        wait(self.live.submit(self.live.coach_tick))
        wait(self.live.submit(self.live.scan_role, object()))
        for _ in range(50):
            if self.live.position_auto:
                break
            time.sleep(0.05)
        self.assertEqual((self.live.position, self.live.position_auto), (4, True))
        self.assertEqual(calls, [("radiant", "醉后才知道", 3)])
        self.assertTrue(json.loads(self.live.current_snapshot)["overlay"]["view"]["position_auto"])
        # 队友排的位置也记下来，猜同路队友用（格子顺序对应）
        self.live.slots["radiant"][:2] = ["斧王", "水晶室女"]
        self.assertEqual(self.live.current_draft().ally_pos, {AXE.id: 3, CM.id: 5})
        wait(self.live.submit(self.live.scan_role, object()))            # 这局认出来了，不再认
        self.assertEqual(len(calls), 1)

        st.update({"map": {"matchid": "2", "game_state": "DOTA_GAMERULES_STATE_PRE_GAME"}, "player": {"team_name": "radiant"}})
        wait(self.live.submit(self.live.coach_tick))
        st.update({"map": {"matchid": "3", "game_state": "DOTA_GAMERULES_STATE_HERO_SELECTION"}, "player": {"team_name": "radiant"}})
        wait(self.live.submit(self.live.coach_tick))
        self.assertEqual((self.live.position, self.live.position_auto), (self.live.cfg.position, False))
        self.assertEqual(self.live.current_draft().ally_pos, {})       # 新一局：上一局的队友位置不算

    def test_manual_position_wins(self):
        self.live._read_roles = lambda *a: ([None, None, None, 4, None], 3)
        self.live.cal = SimpleNamespace(ready=True)
        self.live.gsi.state.update({"map": {"matchid": "1", "game_state": "DOTA_GAMERULES_STATE_HERO_SELECTION"},
                                    "player": {"team_name": "radiant"}})
        wait(self.live.submit(self.live.coach_tick))
        wait(self.live.submit(self.live.action, {"type": "set", "key": "position", "value": 2}))
        wait(self.live.submit(self.live.scan_role, object()))
        time.sleep(0.2)
        self.assertEqual((self.live.position, self.live.position_auto), (2, False))

    def test_top_bar_confirmed_by_grid(self):
        # 顶栏分数没过阈值，但网格里这个英雄已被选走：算认出
        from dota_assistant.vision.recognize import DraftView, Slot
        view = DraftView([Slot(None, 0.63, PL.id), Slot(None, 0.63, TIDE.id)] + [Slot(None, 0.2, 0)] * 3,
                         [Slot(None, 0.2, 0)] * 5, [])
        self.live.grid.taken = {PL.id}
        self.live._confirm_with_grid(view)
        self.assertEqual([s.hero_id for s in view.radiant[:2]], [PL.id, None])

    def test_no_top_bar_scan_after_draft(self):
        # 2026-10-28 实战：局内按 F9 按选人界面的格子裁顶栏，认出一套乱的，把对面英雄冲掉了
        from dota_assistant.vision.recognize import DraftView, Slot
        junk = DraftView([Slot(WK.id, 0.73, WK.id)] + [Slot(None, 0.3, 0)] * 4,
                         [Slot(None, 0.3, 0), Slot(CM.id, 0.71, CM.id)] + [Slot(None, 0.3, 0)] * 3, [])
        orig, live_draft.recognize = live_draft.recognize, lambda *a, **kw: junk
        self.addCleanup(setattr, live_draft, "recognize", orig)
        self.live.cal = SimpleNamespace(ready=True)
        self.live.slots["dire"][:3] = ["斧王", "潮汐猎人", ""]
        st = self.live.gsi.state
        st.update({"map": {"matchid": "1", "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS", "clock_time": 600},
                   "player": {"team_name": "radiant"}})
        wait(self.live.submit(self.live.scan, True, object()))
        self.assertEqual(self.live.slots["dire"][:3], ["斧王", "潮汐猎人", ""])
        self.assertEqual(self.live.slots["radiant"][0], "")
        self.assertIn("不能重识别", self.live.message)
        # 策略时间：只往空格子里填，已认出的不换不清
        st.update({"map": {"matchid": "1", "game_state": "DOTA_GAMERULES_STATE_STRATEGY_TIME", "clock_time": -80},
                   "player": {"team_name": "radiant"}})
        wait(self.live.submit(self.live.scan, True, object()))
        self.assertEqual(self.live.slots["dire"][:3], ["斧王", "潮汐猎人", ""])     # 第 2 格认成 CM 也不换
        self.assertEqual(self.live.slots["radiant"][0], WK.name_cn)                 # 空格子补上
        # 选人阶段照常整排写
        st.update({"map": {"matchid": "1", "game_state": "DOTA_GAMERULES_STATE_HERO_SELECTION"},
                   "player": {"team_name": "radiant"}})
        wait(self.live.submit(self.live.scan, True, object()))
        self.assertEqual(self.live.slots["dire"][:3], ["", CM.name_cn, ""])

    def test_name_slot_learns_crop(self):
        # 选人时没认出来的格子，玩家在顶栏挑了是谁：填进格子 + 把那格头像记成模板；那格根本没出头像（和开局一样）的不记
        from dota_assistant.vision.recognize import TemplateBank, write_image
        rng = np.random.default_rng(7)
        def rand_pic():                                                   # 色块图：缩放以后还认得出（纯噪声一缩就糊了）
            return cv2.resize(rng.integers(0, 255, (6, 8, 3), dtype=np.uint8), (64, 36), interpolation=cv2.INTER_NEAREST)
        pic = {h.id: rand_pic() for h in (AXE, CM, PL, TIDE)}
        pdir, tdir = Path(tempfile.mkdtemp(dir=TMP)), Path(tempfile.mkdtemp(dir=TMP))
        for hid, img in pic.items():
            write_image(pdir / f"{hid}.png", img)
        self.live.bank = TemplateBank(pdir, tdir)
        blank = rand_pic()
        wk = rand_pic()            # 白虎在顶栏的样子，模板里没有
        like_tide = np.clip(pic[TIDE.id].astype(int) + rng.integers(-40, 40, (36, 64, 3)), 0, 255).astype(np.uint8)
        c = self.live._crops
        c["blank"].update({("dire", 3): blank, ("dire", 4): blank, ("dire", 2): blank})
        c["sel"].update({("dire", 3): wk, ("dire", 4): blank.copy(), ("dire", 2): like_tide})
        c["strat"][("dire", 3)] = wk.copy()                               # 和选人时一样：只记一张
        self.live.grid.taken = {PL.id, TIDE.id, AXE.id}
        self.live.slots["dire"][:2] = ["斧王", "潮汐猎人"]

        cands = self.live.pick_candidates("dire", 2)
        self.assertEqual((cands["order"][:1], cands["taken"]), ([PL.id], [PL.id]))     # 网格里剩下的先给
        self.assertNotIn(AXE.id, cands["order"])                                        # 已经在别的格子里的不给
        self.live.slots["dire"][1] = ""
        self.assertEqual(self.live.pick_candidates("dire", 2)["order"][:2], [TIDE.id, PL.id])  # 按像不像排

        wait(self.live.submit(self.live.action, {"type": "name_slot", "side": "dire", "index": 3, "hero_id": WK.id}))
        self.assertEqual(self.live.slots["dire"][3], WK.name_cn)
        self.assertEqual(self.live.bank.learned, 1)
        self.assertEqual(self.live.bank.match(wk)[0], WK.id)
        self.assertEqual(len(list((tdir / str(WK.id)).glob("*.png"))), 1)
        self.assertIn("记住", self.live.message)
        # 断线那种：整局那格都和开局一样，不记（不然以后空位会被认成他）
        wait(self.live.submit(self.live.action, {"type": "name_slot", "side": "dire", "index": 4, "hero_id": CM.id}))
        self.assertEqual((self.live.slots["dire"][4], self.live.bank.learned), (CM.name_cn, 1))
        # 已经在别的格子里的英雄不能再填
        wait(self.live.submit(self.live.action, {"type": "name_slot", "side": "dire", "index": 2, "hero_id": WK.id}))
        self.assertEqual(self.live.slots["dire"][2], "")
        self.assertIn("已经在夜魇第 4 格", self.live.message)
        # 控制面板手填也记
        wait(self.live.submit(self.live.action, {"type": "set_slot", "side": "dire", "index": 2, "text": "PL"}))
        self.assertEqual(self.live.bank.learned, 2)
        # 新一局：上一局的裁图清掉
        self.live.gsi.state.update({"map": {"matchid": "2", "game_state": "DOTA_GAMERULES_STATE_HERO_SELECTION"},
                                    "player": {"team_name": "radiant"}})
        wait(self.live.submit(self.live.coach_tick))
        self.assertEqual(self.live._crops["sel"], {})
        # 这局选人时没裁到（中途开的助手）：拿局内顶栏头像给候选排序；页面先选属性再列这个属性的全部（属性从 /api/heroes 拿）
        self.live.slots["dire"] = [""] * 5
        self.live.grid.taken = set()
        screen = np.zeros((1080, 1920, 3), np.uint8)
        box = buybackscan._box(screen, "dire", 2, *buybackscan.PORTRAIT)
        box[:] = cv2.resize(pic[AXE.id], (box.shape[1], box.shape[0]))           # 潮汐那张上面已经被记成 PL 了，换斧王
        self.live._keep_game_crops(screen)
        self.assertEqual(list(self.live._crops["game"]), [("dire", 2)])            # 其余格子是黑的（死了 / 没东西）不留
        self.assertEqual(self.live.pick_candidates("dire", 2)["order"][0], AXE.id)
        self.assertEqual(sorted(self.live.pick_candidates("dire", 0)["order"]), sorted(h.id for h in HEROES))  # 什么都没裁到：全部英雄
        self.assertEqual({h["attr"] for h in self.live.heroes()}, {h.attr for h in HEROES})

    def test_name_slot_skips_empty_look(self):
        # 2026-10-29 实战：开局那张是切画面时裁的（和后来的空位不一样），帮认时把一张空位裁图（段位旗 + 段位条）记成了风行者，
        # 下一局空格全认成风行者。像别的格子开局样子 / 别的格子裁图的不记，策略时间真出了头像的那张照记
        from dota_assistant.vision.recognize import TemplateBank, write_image
        rng = np.random.default_rng(3)
        def rand_pic():
            return cv2.resize(rng.integers(0, 255, (6, 8, 3), dtype=np.uint8), (64, 36), interpolation=cv2.INTER_NEAREST)
        def jitter(img):
            return np.clip(img.astype(int) + rng.integers(-12, 12, img.shape), 0, 255).astype(np.uint8)
        pdir, tdir = Path(tempfile.mkdtemp(dir=TMP)), Path(tempfile.mkdtemp(dir=TMP))
        for h in (AXE, CM, PL, TIDE):
            write_image(pdir / f"{h.id}.png", rand_pic())
        self.live.bank = TemplateBank(pdir, tdir)
        empty = rand_pic()                                                # 空位的样子：每格差不多
        c = self.live._crops
        for sd in live_consts.SIDES:
            for k in range(5):
                c["blank"][(sd, k)] = jitter(empty)
        c["blank"][("dire", 4)] = rand_pic()                              # 这格开局裁到的是切画面那一帧
        c["sel"][("dire", 4)] = jitter(empty)                             # 选人结束时这格还空着
        c["sel"][("radiant", 0)] = jitter(empty)
        wk = rand_pic()
        c["strat"][("dire", 4)] = wk                                      # 策略时间出了头像
        wait(self.live.submit(self.live.action, {"type": "name_slot", "side": "dire", "index": 4, "hero_id": WK.id}))
        self.assertEqual(self.live.slots["dire"][4], WK.name_cn)
        self.assertEqual(self.live.bank.learned, 1)                       # 只记了策略时间那张
        self.assertEqual(self.live.bank.match(wk)[0], WK.id)
        self.assertNotEqual(self.live.bank.match(jitter(empty))[0], WK.id)

    def test_slots_view_marks_unrecognized(self):
        # 选人界面叠加层的「顶栏对照」：没认出的格子要让玩家看得出来（金色 ?），帮填的格子选人时识别也不冲掉
        from dota_assistant.vision.recognize import DraftView, Slot
        grey = np.full((36, 64, 3), 60, np.uint8)                         # 空位 / 队友预选：灰的
        hover = grey.copy()
        hover[5:25, 10:50] = 140                                           # 半透明的预选头像：变了样子但还是灰的
        picked = grey.copy()
        picked[:28] = (40, 160, 230)                                       # 选定的头像：彩色
        c = self.live._crops
        for k in range(5):
            c["blank"][("dire", k)] = grey
        c["sel"].update({("dire", 0): grey.copy(), ("dire", 1): hover, ("dire", 2): picked})
        self.live.slots["dire"] = ["", "", "", AXE.name_cn, ""]
        st = self.live.gsi.state
        st.update({"map": {"matchid": "1", "game_state": "DOTA_GAMERULES_STATE_HERO_SELECTION"},
                   "player": {"team_name": "radiant", "team_slot": 1}, "hero": {"name": "npc_dota_hero_phantom_lancer"}})
        wait(self.live.submit(self.live.refresh))
        v = self.live.snapshot()["overlay"]["view"]["slots"]
        self.assertEqual(v["ally"], "radiant")
        self.assertEqual([x["state"] for x in v["dire"]], ["empty", "empty", "ask", "ok", "empty"])
        self.assertEqual(v["asks"], 1)
        self.assertEqual((v["radiant"][1]["state"], v["radiant"][1]["hero"]["id"]), ("me", PL.id))   # 自己那格：GSI 的英雄
        self.assertIn("order", v["dire"][2]["pick"])
        # 策略时间起人人都有英雄：空着的都是没认出
        st.update({"map": {"matchid": "1", "game_state": "DOTA_GAMERULES_STATE_STRATEGY_TIME"},
                   "player": {"team_name": "radiant", "team_slot": 1}})
        v = self.live.snapshot()["overlay"]["view"]["slots"]
        self.assertEqual([x["state"] for x in v["dire"]], ["ask", "ask", "ask", "ok", "ask"])
        # 选人阶段帮填了第 3 格：后面识别整排重写，这格不动（别的格照常写）
        learned = []
        self.live.learn_slot = lambda side, i, h: learned.append((side, i, h.id)) and 0   # 记模板另有测试（test_name_slot_learns_crop）
        st.update({"map": {"matchid": "1", "game_state": "DOTA_GAMERULES_STATE_HERO_SELECTION"},
                   "player": {"team_name": "radiant", "team_slot": 1}})
        wait(self.live.submit(self.live.action, {"type": "name_slot", "side": "dire", "index": 2, "hero_id": TIDE.id}))
        self.assertTrue(self.live.snapshot()["overlay"]["view"]["slots"]["dire"][2]["manual"])
        empty = [Slot(None, 0.3, 0)] * 5
        view = DraftView(empty, [Slot(None, 0.3, 0)] * 3 + [Slot(CM.id, 0.8, CM.id), Slot(None, 0.3, 0)], [])
        orig, live_draft.recognize = live_draft.recognize, lambda *a, **kw: view
        self.addCleanup(setattr, live_draft, "recognize", orig)
        self.live.cal = SimpleNamespace(ready=True)
        wait(self.live.submit(self.live.scan, True, object()))
        self.assertEqual(self.live.slots["dire"], ["", "", TIDE.name_cn, CM.name_cn, ""])
        self.assertEqual(learned, [("dire", 2, TIDE.id)] * 2)          # 帮认时记一次；之后每次识别那格头像变了样也接着记
        rows = [json.loads(x) for x in self.live.fix_file.read_text(encoding="utf-8").splitlines()]
        self.assertEqual({k: rows[-1][k] for k in ("side", "slot", "hero", "phase", "how")},
                         {"side": "dire", "slot": 3, "hero": TIDE.name_cn, "phase": "HERO_SELECTION", "how": "帮认"})
        # 控制面板清空这格 = 不再护着；新一局也不护着
        wait(self.live.submit(self.live.action, {"type": "set_slot", "side": "dire", "index": 2, "text": ""}))
        self.assertNotIn(("dire", 2), self.live._named)
        wait(self.live.submit(self.live.action, {"type": "name_slot", "side": "dire", "index": 2, "hero_id": TIDE.id}))
        self.live._in_selection = False
        st.update({"map": {"matchid": "2", "game_state": "DOTA_GAMERULES_STATE_HERO_SELECTION"},
                   "player": {"team_name": "radiant"}})
        wait(self.live.submit(self.live.coach_tick))
        self.assertEqual(self.live._named, set())

    def test_local_games_with_zero_matchid(self):
        # 本地 / 机器人局 matchid 一直是 0：回到选人阶段就算新的一局，本局要点要重做
        def go(state):
            self.live.gsi.state.update({"map": {"matchid": "0", "game_state": f"DOTA_GAMERULES_STATE_{state}", "clock_time": -60},
                                        "player": {"team_name": "radiant"}, "hero": {"name": "npc_dota_hero_phantom_lancer"}})
            wait(self.live.submit(self.live.coach_tick))
        go("HERO_SELECTION"); go("PRE_GAME")
        first = self.live.brief_match
        self.assertIsNotNone(first)
        go("HERO_SELECTION"); go("PRE_GAME")
        self.assertNotEqual(self.live.brief_match, first)

    def test_gsi_items_parsed(self):
        self.push(0, items=("black_king_bar", "tango"))
        self.assertEqual(self.live.gsi.state.items, {"black_king_bar", "tango"})

    def test_popup_on_death_and_checkpoint_then_close(self):
        ov = self.push(0)
        self.wait_coach()
        self.assertEqual(self.prepared, [(PL.id, [AXE.id, TIDE.id], 1)])
        ov = self.push(0.1)
        self.assertEqual(ov["view"]["title"], "开局提示")
        self.assertTrue(ov["visible"] and ov["view"]["closable"])

        wait(self.live.submit(self.live.action, {"type": "dismiss_coach"}))
        self.assertFalse(self.push(5)["visible"])               # 局内没提醒：叠加层收起
        self.assertFalse(self.push(10)["visible"])              # 黑皇杖 20 分钟才开始提醒

        ov = self.push(21, deaths=1)
        self.assertIn("死亡", ov["view"]["title"])
        self.assertEqual(ov["view"]["lines"], [{"kind": "warn", "text": "缺黑皇杖"}])
        self.assertTrue(ov["visible"])
        self.assertFalse(any("黑皇杖" in t for t in self.live.speaker.said))   # 局内提醒默认不念

        wait(self.live.submit(self.live.toggle_overlay))        # 热键：先关提醒，不是隐藏整个叠加层
        self.assertFalse(self.live.user_hidden)
        self.assertFalse(self.push(22, deaths=1)["visible"])    # 没再死：不再弹
        self.assertFalse(self.push(25, deaths=1, items=("black_king_bar",))["visible"])   # 补上了

        self.push(26, deaths=2)
        self.assertIsNone(self.live.coach_card)                 # 死了但已经不缺
        self.live.coach_card = {"kind": "notice", "title": "x", "lines": [], "closable": True}
        self.push(40, state="DOTA_GAMERULES_STATE_POST_GAME")
        self.assertIsNone(self.live.coach_card)                 # 比赛结束收起

    def test_jungle_cues_for_support(self):
        """五号位：号角前弹封野，到点前几秒亮屯野 / 拉野；有卡片时叠在顶上；死了不亮；点 × 这局不再提醒这一种"""
        self.live.position = 5
        self.push(-1.5, state="DOTA_GAMERULES_STATE_PRE_GAME")
        self.wait_coach()
        wait(self.live.submit(self.live.action, {"type": "dismiss_coach"}))      # 关掉「开局提示」
        self.assertEqual(self.live.jungle.kinds, {"block", "pull", "stack", "lotus"})
        ov = self.push(-0.5, state="DOTA_GAMERULES_STATE_PRE_GAME")
        self.assertTrue(ov["visible"] and ov["view"]["jungle"])
        self.assertEqual(ov["view"]["title"], "封野 · 1:00 前")
        self.assertFalse(self.push(1.5)["visible"])                               # 1:30：没到点
        ov = self.push(1 + 50 / 60)
        self.assertEqual(ov["view"]["title"], "屯野 · 1:53")

        self.live.coach_card = {"kind": "notice", "title": "局内提醒", "lines": [], "closable": True}
        ov = self.push(2 + 10 / 60)
        self.assertEqual(ov["view"]["title"], "局内提醒")                          # 卡片还在，提醒叠在顶上
        self.assertEqual(ov["view"]["cue"]["title"], "拉野 · 2:15")
        wait(self.live.submit(self.live.toggle_overlay))                          # 先关卡片
        self.assertEqual(self.live.jungle_cue.kind, "pull")
        wait(self.live.submit(self.live.toggle_overlay))                          # 再关提醒：这局不再提醒拉野
        self.assertFalse(self.push(3 + 12 / 60)["visible"])
        self.assertEqual(self.push(3 + 50 / 60)["view"]["title"], "屯野 · 3:53")   # 屯野照样提醒
        self.assertFalse(self.push(4 + 12 / 60)["visible"])
        self.assertFalse(self.push(4 + 50 / 60, alive=False)["visible"])          # 死了不提醒
        self.assertIsNone(self.live.jungle_cue)
        self.assertEqual(self.push(5 + 45 / 60)["view"]["title"], "屯野 5:53 / 抢莲花 6:00")  # 5:45 正好提前 15 秒（秒数换算不能差 1 秒）
        self.assertEqual(self.push(7 + 45 / 60)["view"]["title"], "屯野 · 7:53")

    def test_no_jungle_cues_for_carry_without_data(self):
        self.push(0)
        self.wait_coach()
        self.assertEqual(self.live.jungle.kinds, set())

    def test_lane_moments_logged(self):
        """3:00 / 6:00 / 10:00 各记一笔自己的 GSI 数据；同一刻不重复记；过了 20 秒才到的不补记"""
        log_file = TMP / f"lane-{time.monotonic_ns()}.jsonl"
        old, lanelog.LOG_FILE = lanelog.LOG_FILE, log_file
        try:
            self.push(0)
            self.wait_coach()
            self.push(2.9)
            self.push(3 + 5 / 60)
            self.push(3 + 10 / 60)
            self.push(6 + 30 / 60)                                  # 6:30 才收到：不补记
            self.push(10)
            recs = [json.loads(x) for x in log_file.read_text(encoding="utf-8").splitlines()]
        finally:
            lanelog.LOG_FILE = old
        self.assertEqual([(r["minute"], r["clock"]) for r in recs], [(3, 185), (10, 600)])
        self.assertEqual((recs[0]["match"], recs[0]["pos"], recs[0]["hero_key"]), ("123", 1, "phantom_lancer"))
        self.assertEqual(recs[0]["player"]["last_hits"], 40)

    def test_death_recap_then_respawn(self):
        """死亡：同一张卡片先说怎么死的，再列缺口；复活后自动收起"""
        death.DUMP_DIR = TMP / "deaths"
        self.live._death_names = ("phantom_lancer", {}, set())     # 不读知识库
        self.live.screen, self.live._screen_factory = None, lambda: SimpleNamespace(grab=lambda: np.zeros((9, 16, 3), np.uint8))
        self.push(0)
        self.wait_coach()
        wait(self.live.submit(self.live.action, {"type": "dismiss_coach"}))
        t = 1000.0
        for i, hp in enumerate([100] * 6 + [60, 20]):
            self.push(19.5 + i / 120, hp=hp, now=t + i * 0.5)          # 19 分半：黑皇杖还没到提醒时间
        ov = self.push(20.2, deaths=1, alive=False, now=t + 4)
        self.assertEqual(ov["view"]["title"], "死亡复盘 · 20:12 · 第 1 次")
        lines = ov["view"]["lines"]
        self.assertTrue(lines[0]["text"].startswith("被瞬间打爆"), lines)
        self.assertEqual(lines[-2:], [{"kind": "head", "text": "还缺"}, {"kind": "warn", "text": "缺黑皇杖"}])
        self.assertEqual(len(list(death.DUMP_DIR.glob("*.json"))), 1)   # 死前推送存了一份
        self.assertEqual(len(list(death.DUMP_DIR.glob("*-0s.png"))), 1)   # 死的那一刻截了 Dota 窗口；2 秒后那张还没到点
        old = live_consts.DEATH_MIN_SHOW
        live_consts.DEATH_MIN_SHOW = 0
        try:
            self.assertTrue(self.push(20.3, deaths=1, alive=False)["visible"])   # 还没复活
            self.assertFalse(self.push(20.8, deaths=1)["visible"])              # 复活：收起
        finally:
            live_consts.DEATH_MIN_SHOW = old


    def test_buyback_timer(self):
        """进游戏（号角前出门）顶栏就有 10 个小条；点对面英雄开始 8 分钟倒计时、再点取消；自己的按 GSI 自动算、点了不改；暂停时钟停；换局清空"""
        st = self.live.gsi.state

        def go(clock, paused=False, cd=None, matchid="123", state="DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"):
            hero = {"name": "npc_dota_hero_phantom_lancer", "alive": True}
            if cd is not None:                    # 真实推送：buyback_cooldown 是冷却结束时的 game_time（比 clock_time 多出号角前那段）
                hero |= {"buyback_cost": 1500, "buyback_cooldown": cd and clock + 90 + cd}
            st.update({"map": {"matchid": matchid, "game_state": state, "clock_time": clock, "game_time": clock + 90,
                               "paused": paused},
                       "player": {"team_name": "radiant", "team_slot": 1}, "hero": hero})
            wait(self.live.submit(self.live.coach_tick))
            return json.loads(self.live.current_snapshot)["buyback"]

        self.assertFalse(go(-75, state="DOTA_GAMERULES_STATE_STRATEGY_TIME")["show"])   # 策略时间还没进游戏
        bb = go(-30, state="DOTA_GAMERULES_STATE_PRE_GAME")                       # 号角前出门就显示
        self.assertTrue(bb["show"])
        self.assertAlmostEqual(bb["anchor"] / 1000, time.time() + 30, delta=2)
        wait(self.live.submit(self.live.action, {"type": "buyback", "side": "dire", "index": 0}))
        self.assertAlmostEqual(json.loads(self.live.current_snapshot)["buyback"]["slots"]["dire"][0]["end"], 450, delta=2)
        wait(self.live.submit(self.live.action, {"type": "buyback", "side": "dire", "index": 0}))   # 取消，下面接着用这局
        bb = go(1500)
        self.assertTrue(bb["show"])
        self.assertAlmostEqual(bb["anchor"] / 1000, time.time() - 1500, delta=2)   # 页面按这个自己走秒
        self.assertEqual(bb["slots"]["dire"][0]["name"], "斧王")
        self.assertTrue(bb["slots"]["radiant"][1]["me"])                         # GSI team_slot

        msg = wait(self.live.submit(self.live.action, {"type": "buyback", "side": "dire", "index": 0}))
        self.assertIn("斧王买活了", msg)
        end = json.loads(self.live.current_snapshot)["buyback"]["slots"]["dire"][0]["end"]
        self.assertAlmostEqual(end, 1500 + 480, delta=2)
        msg = wait(self.live.submit(self.live.action, {"type": "buyback", "side": "dire", "index": 0}))
        self.assertIn("已取消斧王", msg)
        self.assertIsNone(json.loads(self.live.current_snapshot)["buyback"]["slots"]["dire"][0]["end"])

        bb = go(1510, cd=480)                                                     # 自己买活了：GSI 给冷却
        self.assertEqual(bb["slots"]["radiant"][1]["end"], 1990)
        self.assertTrue(bb["slots"]["radiant"][1]["auto"])
        msg = wait(self.live.submit(self.live.action, {"type": "buyback", "side": "radiant", "index": 1}))
        self.assertIn("GSI 自动算", msg)

        wait(self.live.submit(self.live.action, {"type": "buyback", "side": "dire", "index": 4}))
        bb = go(1520, paused=True, cd=470)
        self.assertEqual(bb["paused"], 1520)                                      # 暂停：页面停在这一秒
        with self.assertRaises(ValueError):
            wait(self.live.submit(self.live.action, {"type": "buyback", "side": "dire", "index": 5}))
        bb = go(10, matchid="456")                                                # 换局清空
        self.assertTrue(all(x["end"] is None for side in bb["slots"].values() for x in side))
        self.assertFalse(go(0, state="DOTA_GAMERULES_STATE_POST_GAME")["show"])  # 比赛结束收起

    def test_timer_switches(self):
        """控制面板「顶栏计时」：买活 / 对面大招开关马上生效、写进 config.toml；关着点小条不记，再打开接着显示原来的倒计时；两个都关顶栏整条收起"""
        from dota_assistant import kb
        axe = self.live.db.find("斧王")
        cull = kb.Entry(name="淘汰之刃", key="axe_culling_blade", text="", cooldown="80/75/70", ultimate=True)
        self.live._kb_base = SimpleNamespace(heroes={axe.id: kb.HeroKB(axe.id, "斧王", "axe", "近战", 150, [cull], [], [], {})}, items={})
        self.live._kb_ops = {}
        self.live.config_file.write_text('buyback_timer = true  # 买活计时\nult_timer = true\n', encoding="utf-8")
        self.live.gsi.state.update({"map": {"matchid": "123", "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS", "clock_time": 900},
                                    "player": {"team_name": "radiant", "team_slot": 1},
                                    "hero": {"name": "npc_dota_hero_phantom_lancer", "alive": True, "level": 8}})
        wait(self.live.submit(self.live.coach_tick))
        act = lambda **kw: wait(self.live.submit(self.live.action, kw))
        set_ = lambda key, on: act(type="set", key=key, value=on)
        bb = lambda: json.loads(self.live.current_snapshot)["buyback"]
        act(type="buyback", side="dire", index=0)
        act(type="ult", side="dire", index=0)
        self.assertEqual((bb()["buyback_on"], bb()["slots"]["dire"][0]["ult"]["name"]), (True, "淘汰之刃"))

        self.assertEqual(set_("buyback_timer", False), "买活计时已关闭")
        self.assertFalse(bb()["buyback_on"])
        self.assertIsNotNone(bb()["slots"]["dire"][0]["ult"])                      # 大招小条还在
        self.assertIn("买活计时关着", act(type="buyback", side="dire", index=1))
        self.assertIsNone(bb()["slots"]["dire"][1]["end"])
        self.assertFalse(json.loads(self.live.current_snapshot)["settings"]["buyback_timer"])
        saved = config.load(self.live.config_file)
        self.assertEqual((saved.buyback_timer, saved.ult_timer), (False, True))
        self.assertIn("# 买活计时", self.live.config_file.read_text(encoding="utf-8"))

        self.assertEqual(set_("ult_timer", False), "对面大招计时已关闭")
        self.assertFalse(bb()["show"])                                             # 两个都关：整条收起
        self.assertFalse(config.load(self.live.config_file).ult_timer)

        set_("buyback_timer", True)
        set_("ult_timer", True)
        slot = bb()["slots"]["dire"][0]
        self.assertAlmostEqual(slot["end"], 900 + 480, delta=2)                    # 关之前记的接着显示
        self.assertAlmostEqual(slot["ult"]["end"], 900 + 80, delta=2)
        self.assertTrue(json.loads(self.live.current_snapshot)["settings"]["ult_timer"])

    def test_buyback_auto_from_screenshot(self):
        """顶栏头像变灰 → 后台认倒计时 → 变回彩色后看消息栏：「买活了」+ 行首小图是他，两帧都对上才自动计时、存样本；
        消息栏没有买活（倒计时说早复活了也一样）不记"""
        from tests.test_buyback import feed_line, feed_words, hero_icon, topbar
        buybackscan.SAMPLES_DIR = TMP / f"bb-{time.monotonic_ns()}"
        frame = {"img": topbar(), "feed": []}
        self.live.screen = SimpleNamespace(grab=lambda: frame["img"], source="window")
        # 小的图是顶栏那一格（认倒计时），大的是消息栏
        self.live._ocr = lambda im: frame["feed"] if im.shape[1] > 600 else [SimpleNamespace(text=str(frame["timer"]))] * 3
        icons = {"axe": hero_icon(1), "tidehunter": hero_icon(2)}
        self.live._hero_icon = lambda k, download=True: icons.get(k)   # 不联网下小图标
        st = self.live.gsi.state

        def go(clock):
            st.update({"map": {"matchid": "123", "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS",
                               "clock_time": clock}, "player": {"team_name": "radiant", "team_slot": 1},
                       "hero": {"name": "npc_dota_hero_phantom_lancer", "alive": True}})
            wait(self.live.submit(self.live.coach_tick))
            for _ in range(100):                                  # OCR 在后台线程
                if not any(t.name == "buyback" for t in threading.enumerate()):
                    break
                time.sleep(0.02)
            wait(self.live.submit(self.live.publish))
            return json.loads(self.live.current_snapshot)["buyback"]["slots"]["dire"][0]

        def die(side, i, clocks, timer):
            for clock in clocks:
                frame["timer"] = timer - (clock - clocks[0])
                frame["img"] = topbar({(side, i)}, {(side, i): frame["timer"]})
                go(clock)

        go(1500)
        die("dire", 0, range(1501, 1505), 40)                    # 斧王死了，倒计时 40 → 37
        self.assertEqual(self.live._bb_watch.slots[("dire", 0)].expected, 1541)
        frame["img"] = feed_line(topbar(), icons["axe"])          # 1510 就活了，消息栏「大王师傅123 买活了」
        frame["feed"] = feed_words("大王师傅123 [GOOER] 买活了")
        self.assertIsNone(go(1510)["end"])                        # 彩色一帧还不算复活
        self.assertIsNone(go(1511)["end"])                        # 复活了，消息栏第一帧对上
        slot = go(1512)                                           # 第二帧也对上：记
        self.assertAlmostEqual(slot["end"], 1510 + 480, delta=1)
        self.assertIn("斧王买活了（消息栏认到的）", self.live.message)
        self.assertEqual(len(list(buybackscan.SAMPLES_DIR.glob("*-dire1-buyback-*.png"))), 1)
        self.assertEqual(len(list((buybackscan.SAMPLES_DIR / "feed").glob("*-dire1-buyback-*.png"))), 1)

        # 潮汐：倒计时说早复活了（以前会记），但消息栏的「买活了」是斧王的小图 → 不记
        die("dire", 1, range(1520, 1524), 60)
        frame["img"] = feed_line(topbar(), icons["axe"])
        for clock in range(1530, 1530 + 1 + buybackscan.FEED_TRIES):
            go(clock)
        self.assertIsNone(json.loads(self.live.current_snapshot)["buyback"]["slots"]["dire"][1]["end"])
        self.assertEqual(len(list(buybackscan.SAMPLES_DIR.glob("*-dire2-respawn-*.png"))), 1)

        # 天辉第 1 个：倒计时走完正常复活，消息栏没东西
        frame["feed"] = []
        die("radiant", 0, (1540, 1541, 1542), 9)
        frame["img"] = topbar()
        for clock in range(1550, 1551 + 1 + buybackscan.FEED_TRIES):
            go(clock)
        self.assertIsNone(json.loads(self.live.current_snapshot)["buyback"]["slots"]["radiant"][0]["end"])
        self.assertEqual(len(list(buybackscan.SAMPLES_DIR.glob("*-radiant1-respawn-*.png"))), 1)

        # 夜魇第 3 个格子没填（选人时没认出来）：消息栏小图是格子里没有的英雄 → 算他的
        icons["skeleton_king"] = hero_icon(3)
        die("dire", 2, range(1560, 1564), 50)
        frame["img"] = feed_line(topbar(), icons["skeleton_king"])
        frame["feed"] = feed_words("啊哈 买活了")
        for clock in range(1570, 1574):
            go(clock)
        self.assertAlmostEqual(json.loads(self.live.current_snapshot)["buyback"]["slots"]["dire"][2]["end"], 1570 + 480, delta=1)
        self.assertIn("消息栏小图是冥魂大帝", self.live.message)

        # 潮汐死了马上买活，顶栏没看出死过：别人复活时顺带看到消息栏两帧都是潮汐的「买活了」→ 也记
        frame["feed"] = []
        die("radiant", 0, (1580, 1581, 1582), 9)
        frame["img"] = feed_line(topbar(), icons["tidehunter"])
        frame["feed"] = feed_words("某人 买活了")
        for clock in range(1590, 1594):
            go(clock)
        self.assertAlmostEqual(json.loads(self.live.current_snapshot)["buyback"]["slots"]["dire"][1]["end"], 1591 + 480, delta=1)
        self.assertIn("潮汐猎人买活了", self.live.message)

    def test_ult_timer(self):
        """对面头像下面的大招小条：只给对面、按自己等级猜大招等级；点一下开始、再点取消；
        右键菜单选等级 / 勾天赋、神杖、玲珑心，正在冷却的跟着重算；换局清空。天赋 / 神杖数值是编的，只测算法"""
        from dota_assistant import kb
        st = self.live.gsi.state
        axe = self.live.db.find("斧王")
        cull = kb.Entry(name="淘汰之刃", key="axe_culling_blade", text="", cooldown="80/75/70", ultimate=True,
                        cd_talents=[("special_bonus_axe_cd", 10, 2)], cd_scepter=[20])
        hero = kb.HeroKB(axe.id, "斧王", "axe", "近战", 150, [cull], [], [], {"special_bonus_axe_cd": (20, "-10秒 淘汰之刃冷却")})
        octarine = kb.Entry(name="玲珑心", key="item_octarine_core", text="", stats=["+25% 冷却缩减"])
        self.live._kb_base = SimpleNamespace(heroes={axe.id: hero}, items={"octarine_core": octarine})
        self.live._kb_ops = {"axe_culling_blade": {"scepter": "-20"}}

        def go(clock, level=8, matchid="123"):
            st.update({"map": {"matchid": matchid, "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS", "clock_time": clock},
                       "player": {"team_name": "radiant", "team_slot": 1},
                       "hero": {"name": "npc_dota_hero_phantom_lancer", "alive": True, "level": level}})
            wait(self.live.submit(self.live.coach_tick))
            return json.loads(self.live.current_snapshot)["buyback"]["slots"]

        slots = go(900)
        u = slots["dire"][0]["ult"]
        self.assertEqual((u["name"], u["level"], u["cd"], u["cds"], u["end"]), ("淘汰之刃", 1, 80, "80/75/70", None))
        self.assertIsNone(slots["radiant"][0]["ult"])                              # 自己这边不给
        self.assertIsNone(slots["dire"][1]["ult"])                                 # 知识库里没有的英雄不给
        self.assertEqual(go(900, level=12)["dire"][0]["ult"]["level"], 2)          # 12 级：按 2 级大招算

        msg = wait(self.live.submit(self.live.action, {"type": "ult", "side": "dire", "index": 0}))
        self.assertIn("斧王放了淘汰之刃，按 2 级算冷却 75 秒", msg)
        self.assertAlmostEqual(json.loads(self.live.current_snapshot)["buyback"]["slots"]["dire"][0]["ult"]["end"], 975, delta=2)
        u = json.loads(self.live.current_snapshot)["buyback"]["slots"]["dire"][0]["ult"]
        self.assertEqual([m["label"] for m in u["mods"]], ["20 级天赋：-10秒 淘汰之刃冷却", "神杖：-20秒 冷却", "玲珑心：-25% 冷却"])
        set_ult = lambda **kw: wait(self.live.submit(self.live.action, {"type": "ult_set", "side": "dire", "index": 0} | kw))
        msg = set_ult(level=3)
        self.assertIn("按 3 级算：冷却 70 秒", msg)
        u = json.loads(self.live.current_snapshot)["buyback"]["slots"]["dire"][0]["ult"]
        self.assertEqual((u["level"], u["fixed"]), (3, True))
        self.assertAlmostEqual(u["end"], 970, delta=2)                             # 从放的时候按新冷却重算
        self.assertEqual(go(905, level=6)["dire"][0]["ult"]["level"], 3)          # 改过就不再按自己等级猜
        set_ult(mod="talent:special_bonus_axe_cd", on=True)
        msg = set_ult(mod="octarine", on=True)
        self.assertIn("按 3 级+20 级天赋、玲珑心算：冷却 45 秒", msg)                # (70 - 10) × 0.75
        set_ult(mod="scepter", on=True)
        u = json.loads(self.live.current_snapshot)["buyback"]["slots"]["dire"][0]["ult"]
        self.assertEqual((u["cd"], [m["on"] for m in u["mods"]]), (30, [True, True, True]))   # (70 - 10 - 20) × 0.75
        self.assertAlmostEqual(u["end"], 930, delta=2)
        set_ult(mod="scepter", on=False)
        self.assertEqual(json.loads(self.live.current_snapshot)["buyback"]["slots"]["dire"][0]["ult"]["cd"], 45)
        with self.assertRaises(ValueError):
            set_ult(mod="shard", on=True)                                          # 没有这项
        with self.assertRaises(ValueError):
            set_ult(level=4)
        msg = wait(self.live.submit(self.live.action, {"type": "ult", "side": "dire", "index": 0}))
        self.assertIn("已取消斧王淘汰之刃", msg)
        msg = wait(self.live.submit(self.live.action, {"type": "ult", "side": "radiant", "index": 0}))
        self.assertIn("没有要计时的大招", msg)

        wait(self.live.submit(self.live.action, {"type": "ult", "side": "dire", "index": 0}))
        u = go(10, matchid="456")["dire"][0]["ult"]                                # 换局清空
        self.assertEqual((u["end"], u["fixed"], u["level"], u["cd"]), (None, False, 1, 80))
        self.assertFalse(any(m["on"] for m in u["mods"]))


class BriefLiveTest(unittest.TestCase):
    """策略时间弹本局要点对线期那页 → 号角响收起 → 10 / 30 分钟弹中期 / 后期 → 关了热键再看；有了要点就不再单独弹「开局提示」"""

    def setUp(self):
        self.built = []

        def build(cfg, my_id, ally_ids, enemy_ids, pos, **lane_info):
            self.built.append((my_id, sorted(ally_ids), sorted(enemy_ids), pos))
            b = brief.Brief("本局要点 · 幻影长矛手 一号位")
            b.add("对线", [("warn", "对上斧王比平时难打")])
            b.add("出装", [("row", "相位鞋 → 散夜对剑")])
            return b

        def prepare(cfg, my_id, enemy_ids, pos):
            return coach.Coach([], None, {}, None), ["对面能隐身"]

        self.plan_go = threading.Event()                        # 控制 AI 打法什么时候「写完」

        def plan(cfg, my_id, ally_ids, enemy_ids, lane_ids, text):
            self.plan_go.wait(5)
            return gameplan.Plan([("对线劣势时", [gameplan.Tip("退到塔下补刀", "束缚击原文", "将目标束缚")]),
                                  ("中期（10~30 分钟）", [gameplan.Tip("带线等黑皇杖", "强势期数据", "越拖越强")]),
                                  ("后期（30 分钟后）", [gameplan.Tip("分推牵制", "幻象原文", "制造幻象")])])

        self.live = make_live(coach_prepare=prepare, brief_build=build, plan_build=plan)
        self.live.gsi = SimpleNamespace(state=GameState(), stop=lambda: None)
        self.live.slots["radiant"][:1] = ["水晶室女"]
        self.live.slots["dire"][:2] = ["斧王", "潮汐猎人"]
        self.live.start()

    def tearDown(self):
        self.live.close()

    def push(self, state, minute=-1.5):
        st = self.live.gsi.state
        st.raw = {"map": {"matchid": "9", "game_state": f"DOTA_GAMERULES_STATE_{state}", "clock_time": int(minute * 60)},
                  "player": {"team_name": "radiant", "deaths": 0}, "hero": {"name": "npc_dota_hero_phantom_lancer"}}
        st.updated = time.time()
        wait(self.live.submit(self.live.coach_tick))
        time.sleep(0.1)                                         # 后台线程做完再排进来
        wait(self.live.submit(lambda: None))
        return json.loads(self.live.current_snapshot)["overlay"]

    def test_strategy_brief_then_horn(self):
        self.assertIsNone(self.push("HERO_SELECTION")["view"].get("sections"))   # 选人中不做
        ov = self.push("STRATEGY_TIME")
        self.assertEqual(self.built, [(PL.id, [CM.id], [AXE.id, TIDE.id], 1)])
        v = ov["view"]
        self.assertEqual((v["kind"], v["page"]), ("brief", "lane"))           # 策略时间只弹对线期那页
        self.assertEqual([t["label"] for t in v["tabs"]], ["对线期", "中期", "后期"])
        self.assertEqual(v["sections"][0]["lines"], [{"kind": "warn", "text": "对上斧王比平时难打"}])
        self.assertNotIn("出装", [s["title"] for s in v["sections"]])         # 出装在中期那页
        self.assertIn("正在写", v["sections"][-1]["lines"][0]["text"])
        self.assertTrue(ov["visible"])

        pages = []
        for _ in range(3):                                      # F6 翻页：中期 → 后期 → 回到对线期
            wait(self.live.submit(self.live.show_brief))
            pages.append(json.loads(self.live.current_snapshot)["overlay"]["view"]["page"])
        self.assertEqual(pages, ["mid", "late", "lane"])
        self.plan_go.set()                                      # 写完：补进当前这页，不翻页
        ov = self.push("STRATEGY_TIME")
        self.assertEqual(ov["view"]["page"], "lane")
        self.assertEqual(ov["view"]["sections"][-1], {"title": "对线劣势时 · AI", "lines": [
            {"kind": "warn", "text": "退到塔下补刀", "note": "束缚击原文"}]})
        self.assertNotIn("带线等黑皇杖", json.dumps(ov["view"], ensure_ascii=False))   # 中期的不塞进来

        ov = self.push("PRE_GAME", -1)                          # 出门：要点还在，「开局提示」不单独弹
        self.assertEqual(ov["view"]["kind"], "brief")
        self.assertEqual(len(self.built), 1)
        self.assertFalse(self.push("GAME_IN_PROGRESS", 0.2)["visible"])          # 号角响：收起
        self.assertIn("中期", self.live.message)
        self.push("GAME_IN_PROGRESS", 5)
        self.assertIsNone(self.live.coach_card)                                  # 对线期中间不再弹

        ov = self.push("GAME_IN_PROGRESS", 10.1)                                 # 到中期：弹中期那页
        v = ov["view"]
        self.assertTrue(ov["visible"])
        self.assertEqual((v["kind"], v["page"]), ("brief", "mid"))
        self.assertEqual([s["title"] for s in v["sections"]], ["出装", "怎么打 · AI"])
        wait(self.live.submit(self.live.toggle_overlay))                         # F8 关掉：告诉我怎么找回来
        self.assertIsNone(self.live.coach_card)
        self.assertIn("再看", self.live.message)
        self.assertIsNone(self.push("GAME_IN_PROGRESS", 12).get("view", {}).get("page"))   # 关了不会自己再弹

        wait(self.live.submit(self.live.action, {"type": "show_brief"}))         # 热键 / 面板按钮再看：当前阶段那页
        ov = json.loads(self.live.current_snapshot)["overlay"]
        self.assertTrue(ov["visible"] and ov["view"]["kind"] == "brief" and ov["view"]["page"] == "mid")
        ov = self.push("GAME_IN_PROGRESS", 12.5)                                 # 下一轮 tick：不能又被收起
        self.assertTrue(ov["visible"] and ov["view"]["kind"] == "brief")
        wait(self.live.submit(self.live.action, {"type": "brief_page", "page": "lane"}))   # 点标签翻回对线期
        self.assertEqual(self.live.coach_card["page"], "lane")
        wait(self.live.submit(self.live.toggle_overlay))

        wait(self.live.submit(self.live._show_coach, "局内提醒", [("warn", "缺黑皇杖")]))
        self.assertEqual(self.push("GAME_IN_PROGRESS", 30.2)["view"]["title"], "局内提醒")   # 别的卡片弹着：后期先等
        wait(self.live.submit(self.live.toggle_overlay))
        v = self.push("GAME_IN_PROGRESS", 30.4)["view"]                          # 关了以后再弹后期那页
        self.assertEqual((v["kind"], v["page"]), ("brief", "late"))
        self.assertEqual(v["sections"][0]["lines"][0]["text"], "分推牵制")


class PickLiveTest(unittest.TestCase):
    """选人时点推荐英雄：先出「如果选它」的数据页，AI 写完翻到打法页；× 回到推荐；阵容变了标出来"""

    def setUp(self):
        self.built = []
        self.plan_go = threading.Event()

        def build(cfg, my_id, ally_ids, enemy_ids, pos, **lane_info):
            self.built.append((my_id, sorted(ally_ids), sorted(enemy_ids), pos))
            b = brief.Brief("本局要点")
            b.add("对线", [("row", "对上斧王和平时差不多")])
            return b

        def plan(cfg, my_id, ally_ids, enemy_ids, lane_ids, text):
            self.plan_go.wait(5)
            return gameplan.Plan([("对线期（0~10 分钟）", [gameplan.Tip("先补刀", "强势期数据", "对上斧王")])])

        self.live = make_live(brief_build=build, plan_build=plan)
        self.live.slots["dire"][:1] = ["斧王"]
        self.live.start()
        wait(self.live.submit(self.live.refresh))

    def tearDown(self):
        self.live.close()

    def view(self):
        time.sleep(0.1)                                         # 后台线程做完再排进来
        wait(self.live.submit(lambda: None))
        return json.loads(self.live.current_snapshot)["overlay"]["view"]

    def test_click_pick_then_plan_then_close(self):
        wait(self.live.submit(self.live.action, {"type": "explain_pick", "hero_id": PL.id}))
        v = self.view()
        self.assertEqual(self.built, [(PL.id, [], [AXE.id], 1)])
        self.assertEqual(v["kind"], "brief")
        self.assertTrue(v["title"].startswith("如果选幻影长矛手"))
        self.assertEqual(v["page"], "lane")
        self.assertIn("正在写", self.live.pick_brief.plan_note)

        wait(self.live.submit(self.live.action, {"type": "explain_pick", "hero_id": PL.id}))   # 同一局面再点：不重做
        self.assertEqual(len(self.built), 1)

        self.plan_go.set()                                      # 写完：补进对线期那页
        v = self.view()
        self.assertEqual(v["page"], "lane")
        self.assertEqual(v["sections"][-1]["lines"][0]["text"], "先补刀")
        wait(self.live.submit(self.live.action, {"type": "brief_page", "page": "mid"}))
        self.assertEqual(self.view()["page"], "mid")
        wait(self.live.submit(self.live.show_brief))            # 热键接着翻
        self.assertEqual(self.view()["page"], "late")
        with self.assertRaises(ValueError):
            self.live.set_brief_page("plan")

        wait(self.live.submit(self.live.action, {"type": "set_slot", "side": "dire", "index": 1, "text": "潮汐猎人"}))
        self.assertIn("阵容变了", self.view()["title"])

        wait(self.live.submit(self.live.close_overlay))          # ×：回到推荐，叠加层不收起
        snap = json.loads(self.live.current_snapshot)["overlay"]
        self.assertEqual(snap["view"]["kind"], "draft")
        self.assertTrue(snap["visible"])

    def test_build_error_shown(self):
        self.live._brief_build = no_brief
        wait(self.live.submit(self.live.action, {"type": "explain_pick", "hero_id": TIDE.id}))
        v = self.view()
        self.assertEqual(v["kind"], "notice")
        self.assertIn("测试里不联网", v["lines"][0]["text"])


class WebServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live = make_live().start()
        cls.srv = WebServer(cls.live).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.live.close()

    def get(self, path):
        with urllib.request.urlopen(self.srv.url + path, timeout=5) as r:
            return r.status, r.headers.get("Content-Type"), r.read()

    def post(self, body):
        req = urllib.request.Request(self.srv.url + "/api/action", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_static_and_api(self):
        code, ctype, body = self.get("/panel.html")
        self.assertEqual(code, 200)
        self.assertIn("text/html", ctype)
        self.assertIn("控制面板".encode(), body)
        _, _, body = self.get("/api/heroes")
        self.assertEqual({h["id"] for h in json.loads(body)}, {h.id for h in HEROES})
        _, _, body = self.get("/api/config")
        self.assertIn("opacity", json.loads(body))

    def test_skips_chromium_unsafe_port(self):
        """随机端口分到 Chromium 拒绝的端口（例如 6669）就换一个，不然 Electron 界面打不开"""
        from unittest import mock
        from dota_assistant.app import webui
        from dota_assistant.core.log import HTTPServer
        probe = HTTPServer(("127.0.0.1", 0), None)
        bad = probe.server_address[1]
        probe.server_close()
        calls = []

        def first_bad(addr, handler):
            calls.append(addr)
            return HTTPServer(("127.0.0.1", bad) if len(calls) == 1 else addr, handler)
        with mock.patch.object(webui, "UNSAFE_PORTS", {bad}), mock.patch.object(webui, "HTTPServer", first_bad):
            srv = WebServer(self.live)
        try:
            self.assertEqual(len(calls), 2)
            self.assertNotEqual(srv.server.server_address[1], bad)
        finally:
            srv.server.server_close()

    def test_no_path_traversal(self):
        for path in ("/../config.toml", "/%2e%2e/config.toml", "/portraits/../../config.toml", "/items/../../config.toml"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.get(path)
            self.assertEqual(cm.exception.code, 404)

    def test_icons_cached_and_rejected(self):
        """顶栏小条的图标：本地有就直接给（不联网）；路径不对一律 404，不去下"""
        from unittest import mock
        from dota_assistant.sources import assets
        with (tempfile.TemporaryDirectory() as d, mock.patch.object(assets, "ICON_DIR", Path(d)),
              mock.patch.object(assets, "_download_icon", side_effect=AssertionError("不该联网"))):
            (Path(d) / "icons").mkdir()
            (Path(d) / "icons" / "gold.png").write_bytes(b"fake png")
            code, ctype, body = self.get("/icons/icons/gold.png")
            self.assertEqual((code, ctype, body), (200, "image/png", b"fake png"))
            for path in ("/icons/../config.toml", "/icons/abilities/Doom.png", "/icons/items/blink.png",
                         "/icons/abilities/../../x.png"):
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    self.get(path)
                self.assertEqual(cm.exception.code, 404)

    def test_action_roundtrip(self):
        code, res = self.post({"type": "set_slot", "side": "dire", "index": 1, "text": "斧王"})
        self.assertEqual((code, res["ok"]), (200, True))
        _, _, body = self.get("/api/state")
        self.assertEqual(json.loads(body)["slots"]["dire"][1]["hero_id"], AXE.id)
        code, res = self.post({"type": "nope"})
        self.assertEqual((code, res["ok"]), (400, False))

    def test_events_stream_pushes_snapshot(self):
        with urllib.request.urlopen(self.srv.url + "/api/events", timeout=5) as r:
            self.assertIn("text/event-stream", r.headers.get("Content-Type"))
            line = r.readline().decode()
            self.assertTrue(line.startswith("data: "))
            self.assertIn("slots", json.loads(line[6:]))
            self.post({"type": "set_slot", "side": "radiant", "index": 4, "text": "冰女"})
            deadline = time.time() + 5
            while time.time() < deadline:
                line = r.readline().decode()
                if line.startswith("data: ") and json.loads(line[6:])["slots"]["radiant"][4]["text"] == "冰女":
                    break
            else:
                self.fail("改了格子没有推送新快照")


if __name__ == "__main__":
    unittest.main()
