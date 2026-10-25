"""死亡复盘：用手写的 GSI 推送序列（字段按 Valve GSI 的写法）"""
import unittest

from dota_assistant import death
from dota_assistant.gsi import GameState

BKB = {"name": "item_black_king_bar", "cooldown": 0, "can_cast": True}
NAMES = {"black_king_bar": "黑皇杖", "blink": "闪烁匕首", "magic_wand": "魔杖", "drow_ranger_glacier": "冰川"}


def push(clock, hp, deaths=0, alive=True, items=None, abilities=None, gold=1000, **flags):
    return {"map": {"clock_time": clock, "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
            "player": {"deaths": deaths, "gold": gold},
            "hero": {"alive": alive, "health": hp * 10, "max_health": 1000, "mana_percent": 60,
                     "xpos": 3000, "ypos": 5500, "respawn_seconds": 0 if alive else 34,
                     "buyback_cost": 1500, "buyback_cooldown": 0, **flags},
            "items": items or {}, "abilities": abilities or {}}


def series(hps, step=0.5, t0=100.0, **kw):
    """[(时间戳, 推送)]：每个血量一条，最后加一条「刚死」"""
    out = [(t0 + i * step, push(600 + int(i * step), hp, **kw)) for i, hp in enumerate(hps)]
    out.append((t0 + len(hps) * step, push(600 + int(len(hps) * step), 0, deaths=1, alive=False, gold=700)))
    return out


class AnalyzeTest(unittest.TestCase):
    def test_burst(self):
        rec = death.analyze(series([100] * 10 + [70, 30]), "radiant", NAMES)
        self.assertEqual(rec.deaths, 1)
        self.assertTrue(rec.title.startswith("死亡复盘 · 10:06"))
        self.assertIn("被瞬间打爆：1.5 秒从 100% 血到死", rec.lines[0][1])
        texts = [t for _, t in rec.lines]
        self.assertIn("死在上路（对面半场）", texts)               # 天辉视角，(3000, 5500) 在夜魇上路
        self.assertIn("复活 34 秒 · 掉了 300 金 · 买活 1500 金（还差 800）", texts)

    def test_controlled(self):
        hist = series([100, 90, 80, 70, 60, 50, 40, 30, 20, 10])
        for _, r in hist[2:-1]:
            r["hero"]["stunned"] = True
        rec = death.analyze(hist, "radiant", NAMES)
        self.assertTrue(rec.lines[0][1].startswith("被控死：5.0 秒内从 100% 血到死，其中 4.0 秒被眩晕"), rec.lines)
        self.assertEqual((rec.cause, rec.secs["stunned"]), ("control", 4.0))     # 结构化结果，按死法补装用

    def test_regen_resets_fight_start_and_low_hp(self):
        # 先掉到 40 回到 80（回城回血），再从 80 慢慢掉：这波从 80 开始；残血拖了很久
        hps = [100, 70, 40, 60, 80] + [80 - 5 * i for i in range(1, 16)]
        rec = death.analyze(series(hps, step=1.0), "dire", NAMES)
        self.assertTrue(rec.lines[0][1].startswith("残血拖太久"), rec.lines)
        rec2 = death.analyze(series([45, 40, 35, 30, 25, 20, 15, 10], step=1.0), "dire", NAMES)
        self.assertIn("这波开始时只有 45% 血", [t for _, t in rec2.lines])

    def test_save_items(self):
        blink = {"name": "item_blink", "cooldown": 0, "can_cast": True}
        wand = {"name": "item_magic_wand", "cooldown": 0, "can_cast": True, "charges": 12}
        glacier = {"name": "drow_ranger_glacier", "level": 1, "cooldown": 0, "can_cast": True}
        hist = series([100, 80, 60, 40, 20], items={"slot0": BKB, "slot1": blink, "slot2": wand},
                      abilities={"ability3": glacier})
        hist[2][1]["items"] = {**hist[2][1]["items"], "slot0": {**BKB, "cooldown": 94}}   # 中途开了黑皇杖
        for _, r in hist[3:-1]:
            r["items"] = hist[2][1]["items"]
        rec = death.analyze(hist, "radiant", NAMES, {"drow_ranger_glacier"})
        texts = [t for _, t in rec.lines]
        self.assertIn("闪烁匕首、魔杖（12 层）、冰川 当时是好的，没用上", texts)
        self.assertIn("用了黑皇杖，还是没活下来", texts)
        self.assertIn(("warn", "闪烁匕首、魔杖（12 层）、冰川 当时是好的，没用上"), rec.lines)

    def test_blink_locked_by_damage(self):
        """跳刀挨打后 3 秒不能用：不算用过，也不算「没用上」"""
        blink = {"name": "item_blink", "cooldown": 0, "can_cast": True}
        hist = series([100, 80, 60, 40, 20], items={"slot0": blink})
        for _, r in hist[1:-1]:
            r["items"] = {"slot0": {**blink, "cooldown": 3, "can_cast": False}}
        texts = [t for _, t in death.analyze(hist, "radiant", NAMES).lines]
        self.assertIn("闪烁匕首 一直在挨打，用不了（受到英雄攻击后 3 秒内不能用）", texts)
        self.assertFalse(any("用了" in t for t in texts))

    def test_stunned_whole_time_blocks_items(self):
        hist = series([100, 60, 20], items={"slot0": BKB}, stunned=True)
        rec = death.analyze(hist, "radiant", NAMES)
        self.assertIn("黑皇杖 是好的，但一直被控着用不了", [t for _, t in rec.lines])

    def test_no_health_or_no_death(self):
        self.assertIsNone(death.analyze([(1.0, push(600, 100))]))       # 历史里没有死亡
        hist = series([100, 50])
        for _, r in hist:
            for k in ("health", "max_health"):
                r["hero"].pop(k)
        rec = death.analyze(hist, "radiant")
        self.assertEqual(rec.lines, [("dim", "复活 34 秒 · 掉了 300 金 · 买活 1500 金（还差 800）")])

    def test_region(self):
        self.assertEqual(death.region(-6500, -6000, "radiant"), "自家高地")
        self.assertEqual(death.region(100, 0, "radiant"), "中路（对面半场）")
        self.assertEqual(death.region(-2000, 1000, "dire"), "对面野区")


class HistoryTest(unittest.TestCase):
    def test_only_in_match_and_trimmed(self):
        st = GameState()
        st.update({"map": {"game_state": "DOTA_GAMERULES_STATE_HERO_SELECTION"}}, now=0)
        self.assertEqual(st.history(), [])
        for t in range(100):
            st.update(push(t, 100), now=float(t))
        h = st.history()
        self.assertEqual(h[0][0], 99 - 60)
        self.assertEqual(len(h), 61)
        st.update({"map": {"game_state": "DOTA_GAMERULES_STATE_POST_GAME"}}, now=100)
        self.assertEqual(st.history(), [])


if __name__ == "__main__":
    unittest.main()
