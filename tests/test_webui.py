"""网页界面：结构化视图、Live 主控（含局内缺口提醒）、本地服务。不联网、不出声、不开窗口。"""
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace

from dota_assistant import advisor as adv, config, grid
from dota_assistant import brief, coach, death, gameplan
from dota_assistant import live as live_mod
from dota_assistant.config import Config
from dota_assistant.engine import Engine, Suggestion
from dota_assistant.gsi import GameState
from pathlib import Path
from dota_assistant.heroes import HeroDB
from dota_assistant.live import Live
from dota_assistant.vision import Calibration
from dota_assistant.webui import WebServer, overlay_config
from tests.test_engine import AXE, CM, HEROES, PL, TIDE, make_stats


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


def fake_advisor(cfg: Config) -> adv.Advisor:
    a = adv.Advisor.__new__(adv.Advisor)
    a.cfg, a.db, a.me, a.pool, a.kb = cfg, HeroDB(HEROES), None, [], None
    a.engine = Engine(a.db, make_stats({(TIDE.id, PL.id): (20000, 0.56)}))
    return a


TMP = Path(tempfile.mkdtemp(prefix="dota-webui-"))


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

    def test_lanes_view(self):
        from dota_assistant import explain, lanes
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
        from dota_assistant.vision import DraftView, Slot
        view = DraftView([Slot(None, 0.63, PL.id), Slot(None, 0.63, TIDE.id)] + [Slot(None, 0.2, 0)] * 3,
                         [Slot(None, 0.2, 0)] * 5, [])
        self.live.grid.taken = {PL.id}
        self.live._confirm_with_grid(view)
        self.assertEqual([s.hero_id for s in view.radiant[:2]], [PL.id, None])

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

    def test_death_recap_then_respawn(self):
        """死亡：同一张卡片先说怎么死的，再列缺口；复活后自动收起"""
        death.DUMP_DIR = TMP / "deaths"
        self.live._death_names = ("phantom_lancer", {}, set())     # 不读知识库
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
        old = live_mod.DEATH_MIN_SHOW
        live_mod.DEATH_MIN_SHOW = 0
        try:
            self.assertTrue(self.push(20.3, deaths=1, alive=False)["visible"])   # 还没复活
            self.assertFalse(self.push(20.8, deaths=1)["visible"])              # 复活：收起
        finally:
            live_mod.DEATH_MIN_SHOW = old


class BriefLiveTest(unittest.TestCase):
    """策略时间弹本局要点 → 号角响收起 → 热键再看；有了要点就不再单独弹「开局提示」"""

    def setUp(self):
        self.built = []

        def build(cfg, my_id, ally_ids, enemy_ids, pos, **lane_info):
            self.built.append((my_id, sorted(ally_ids), sorted(enemy_ids), pos))
            b = brief.Brief("本局要点 · 幻影长矛手 一号位")
            b.add("对线", [("warn", "对上斧王比平时难打")])
            return b

        def prepare(cfg, my_id, enemy_ids, pos):
            return coach.Coach([], None, {}, None), ["对面能隐身"]

        self.plan_go = threading.Event()                        # 控制 AI 打法什么时候「写完」

        def plan(cfg, my_id, ally_ids, enemy_ids, lane_ids, text):
            self.plan_go.wait(5)
            return gameplan.Plan([("对线劣势时", [gameplan.Tip("退到塔下补刀", "束缚击原文", "将目标束缚")])])

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
        self.assertEqual(ov["view"]["kind"], "brief")
        self.assertEqual(ov["view"]["sections"][0]["lines"], [{"kind": "warn", "text": "对上斧王比平时难打"}])
        self.assertTrue(ov["visible"])

        wait(self.live.submit(self.live.show_brief))           # F6 翻到打法页：还在生成
        ov = json.loads(self.live.current_snapshot)["overlay"]
        self.assertEqual(ov["view"]["page"], "plan")
        self.assertIn("正在写", ov["view"]["sections"][0]["lines"][0]["text"])
        wait(self.live.submit(self.live.show_brief))           # 再按：回数据页
        self.plan_go.set()                                      # 写完：自动翻到打法页
        ov = self.push("STRATEGY_TIME")
        self.assertEqual(ov["view"]["page"], "plan")
        self.assertEqual(ov["view"]["sections"][-1]["lines"],
                         [{"kind": "warn", "text": "退到塔下补刀", "note": "束缚击原文"}])

        ov = self.push("PRE_GAME", -1)                          # 出门：要点还在，「开局提示」不单独弹
        self.assertEqual(ov["view"]["kind"], "brief")
        self.assertEqual(len(self.built), 1)
        self.assertFalse(self.push("GAME_IN_PROGRESS", 0.2)["visible"])          # 号角响：收起

        wait(self.live.submit(self.live.action, {"type": "show_brief"}))         # 热键 / 面板按钮再看：先看打法
        ov = json.loads(self.live.current_snapshot)["overlay"]
        self.assertTrue(ov["visible"] and ov["view"]["kind"] == "brief" and ov["view"]["page"] == "plan")
        ov = self.push("GAME_IN_PROGRESS", 0.5)                                  # 下一轮 tick：不能又被收起
        self.assertTrue(ov["visible"] and ov["view"]["kind"] == "brief")
        wait(self.live.submit(self.live.toggle_overlay))                         # F8 关掉
        self.assertIsNone(self.live.coach_card)


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
        self.assertEqual(v["page"], "data")
        self.assertIn("正在写", self.live.pick_brief.plan_note)

        wait(self.live.submit(self.live.action, {"type": "explain_pick", "hero_id": PL.id}))   # 同一局面再点：不重做
        self.assertEqual(len(self.built), 1)

        self.plan_go.set()
        v = self.view()
        self.assertEqual(v["page"], "plan")
        self.assertEqual(v["sections"][0]["lines"][0]["text"], "先补刀")
        wait(self.live.submit(self.live.action, {"type": "brief_page", "page": "data"}))
        self.assertEqual(self.view()["page"], "data")

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

    def test_no_path_traversal(self):
        for path in ("/../config.toml", "/%2e%2e/config.toml", "/portraits/../../config.toml"):
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
