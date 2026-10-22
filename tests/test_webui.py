"""网页界面：结构化视图、Live 主控（含局内缺口提醒）、本地服务。不联网、不出声、不开窗口。"""
import json
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace

from dota_assistant import advisor as adv
from dota_assistant import coach
from dota_assistant.config import Config
from dota_assistant.engine import Engine, Suggestion
from dota_assistant.gsi import GameState
from pathlib import Path
from dota_assistant.heroes import HeroDB
from dota_assistant.live import Live
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
    a.cfg, a.db, a.me, a.pool = cfg, HeroDB(HEROES), None, []
    a.engine = Engine(a.db, make_stats({(TIDE.id, PL.id): (20000, 0.56)}))
    return a


TMP = Path(tempfile.mkdtemp(prefix="dota-webui-"))


def make_live(overlay_file: Path | None = None, coach_prepare=None) -> Live:
    cfg = Config(position_filter=False, top=2, top_other=2)
    return Live(cfg, screen_factory=lambda: None, use_gsi=False, use_hotkeys=False, speaker=FakeSpeaker(),
                advisor=fake_advisor(cfg), bank=FakeBank(), calibration=None,
                overlay_file=overlay_file or TMP / f"overlay-{time.monotonic_ns()}.json",
                coach_prepare=coach_prepare)


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
        self.assertEqual(v["threat"]["delta"], 2.0)
        json.dumps(v, ensure_ascii=False)          # 能直接转 JSON


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
        finally:
            again.close()

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

    def push(self, minute, state="DOTA_GAMERULES_STATE_GAME_IN_PROGRESS", deaths=0, items=()):
        st = self.live.gsi.state
        st.raw = {"map": {"matchid": "123", "game_state": state, "clock_time": int(minute * 60)},
                  "player": {"team_name": "radiant", "deaths": deaths, "last_hits": 40},
                  "hero": {"name": "npc_dota_hero_phantom_lancer", "alive": True},
                  "items": {f"slot{i}": {"name": f"item_{k}"} for i, k in enumerate(items)}
                           | {"slot8": {"name": "empty"}}}
        st.updated = time.time()
        wait(self.live.submit(self.live.coach_tick))
        return json.loads(self.live.current_snapshot)["overlay"]

    def test_gsi_items_parsed(self):
        self.push(0, items=("black_king_bar", "tango"))
        self.assertEqual(self.live.gsi.state.items, {"black_king_bar", "tango"})

    def test_popup_on_death_and_checkpoint_then_close(self):
        ov = self.push(0)
        for _ in range(50):                                     # 后台线程准备好
            if self.live.coach:
                break
            time.sleep(0.05)
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
