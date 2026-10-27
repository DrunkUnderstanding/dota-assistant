"""对面大招冷却：挑哪个大招、按等级取冷却、减冷却（天赋 / 神杖 / 玲珑心）、游戏文件里的写法、点一下开始 / 再点取消、改等级重算。"""
import tempfile
import unittest
from pathlib import Path

from dota_assistant import kb, ultcd
from dota_assistant.ultcd import READY_SHOW, Tracker


def ab(name, cd, ultimate=True, granted=False):
    return kb.Entry(name=name, key=name, text="", cooldown=cd, ultimate=ultimate, granted=granted)


def hero(*abilities):
    return kb.HeroKB(1, "测试英雄", "test", "近战", 150, list(abilities), [], [])


class PickTest(unittest.TestCase):
    def test_pick(self):
        self.assertEqual(ultcd.pick(hero(ab("小技能", "13/10/7/4", ultimate=False), ab("末日", "140/130/120"))).name, "末日")
        self.assertIsNone(ultcd.pick(hero(ab("肢解", "30/25/20"))))                 # 冷却太短不记
        self.assertIsNone(ultcd.pick(hero(ab("射手天赋", ""))))                     # 被动大招没冷却
        self.assertIsNone(ultcd.pick(hero(ab("神杖技能", "100", granted=True))))
        self.assertEqual(ultcd.pick(hero(ab("短", "40/35/30"), ab("长", "100/90/80"))).name, "长")

    def test_level_and_cooldown(self):
        self.assertEqual([ultcd.guess_level(x) for x in (None, 3, 6, 11, 12, 17, 18, 30)], [1, 1, 1, 1, 2, 2, 3, 3])
        a = ab("绝冥再生", "180/150/120")
        self.assertEqual([ultcd.cooldown(a, lv) for lv in (1, 2, 3)], [180, 150, 120])
        self.assertEqual(ultcd.cooldown(ab("无敌斩", "120"), 3), 120)            # 官方只给一个数：各级一样


KV = """
"DOTAAbilities"
{
    // 注释里有 "AbilityCooldown" 也不算
    "skeleton_king_reincarnation"
    {
        "AbilityValues"
        {
            "AbilityCooldown"
            {
                "value"                 "180 150 120"
                "special_bonus_scepter" "-10"
            }
        }
    }
    "ursa_enrage"
    {
        "AbilityValues" { "AbilityCooldown" { "value" "60 45 30" "special_bonus_scepter" "=30 =24 =18" } }
    }
}
"""


class ModTest(unittest.TestCase):
    def test_parse_op(self):
        self.assertEqual(ultcd.parse_op("-10"), ("-", (10.0,)))
        self.assertEqual(ultcd.parse_op("=30 =24 =18"), ("=", (30.0, 24.0, 18.0)))
        self.assertEqual(ultcd.parse_op("-50%"), ("%", (50.0,)))
        self.assertEqual(ultcd.parse_op("+5"), ("-", (-5.0,)))            # 加冷却
        self.assertIsNone(ultcd.parse_op("10"))                           # 没符号：看不懂就不给勾
        self.assertIsNone(ultcd.parse_op("=30 -5"))

    def test_kv(self):
        self.assertEqual(ultcd.cd_ops(KV, "skeleton_king_reincarnation"), {"scepter": "-10"})
        self.assertEqual(ultcd.cd_ops(KV, "ursa_enrage"), {"scepter": "=30 =24 =18"})
        self.assertEqual(ultcd.cd_ops(KV, "nope"), {})

    def test_cooldown_with_mods(self):
        a = ab("末日", "140/130/120")
        talent = ultcd.Mod("talent:x", "", "-", (10,))
        octarine = ultcd.Mod("octarine", "", "%", (25,))
        set_to = ultcd.Mod("scepter", "", "=", (30, 24, 18))
        self.assertEqual(ultcd.cooldown(a, 3, [talent]), 110)
        self.assertEqual(ultcd.cooldown(a, 3, [octarine, talent]), 82.5)  # 先减秒再乘玲珑心，和勾的顺序无关
        self.assertEqual(ultcd.cooldown(a, 2, [talent, set_to]), 14)       # 先改成 24 再减 10

    def test_mods_list(self):
        a = kb.Entry(name="末日", key="doom_bringer_doom", text="", cooldown="140/130/120", ultimate=True,
                     cd_talents=[("special_bonus_unique_doom_9", 10, 2)], cd_scepter=[10], cd_shard=[5])
        h = kb.HeroKB(1, "末日使者", "doom_bringer", "近战", 175, [a], [], [], {"special_bonus_unique_doom_9": (20, "-10秒 末日冷却")})
        ms = ultcd.mods(h, a, None, {"doom_bringer_doom": {"scepter": "-10"}})
        self.assertEqual([(m.key, m.label) for m in ms], [("talent:special_bonus_unique_doom_9", "20 级天赋：-10秒 末日冷却"),
                                                          ("scepter", "神杖：-10秒 冷却")])   # 魔晶没取到写法：不给勾

    def test_update_ops(self):
        base = kb.KnowledgeBase.load()
        if not base:
            self.skipTest("没有知识库")
        old = ultcd.OPS_PATH
        with tempfile.TemporaryDirectory() as d:
            ultcd.OPS_PATH = Path(d) / "cd_ops.json"
            try:
                asked = []

                def read(hero):
                    asked.append(hero)
                    return KV
                ops = ultcd.update_ops(base, log=lambda m: None, reader=("测试", read))
                self.assertIn("skeleton_king", asked)
                self.assertNotIn("doom_bringer", asked)                    # 神杖不改冷却的不去读
                self.assertEqual(ops["skeleton_king_reincarnation"], {"scepter": "-10"})
                self.assertEqual(ultcd.load_ops(base), ops)
                self.assertEqual(ultcd.update_ops(base, log=lambda m: None, reader=("测试", None)), ops)   # 同版本不重取
            finally:
                ultcd.OPS_PATH = old


class TrackerTest(unittest.TestCase):
    def test_mark_cancel_ready(self):
        t = Tracker()
        self.assertTrue(t.mark("dire", 1, 600, 130))
        self.assertEqual(t.end("dire", 1, 650), 730)
        self.assertFalse(t.mark("dire", 1, 610, 130))                        # 点错了：取消
        self.assertIsNone(t.end("dire", 1, 610))
        t.mark("dire", 1, 600, 130)
        self.assertEqual(t.end("dire", 1, 730 + READY_SHOW - 1), 730)         # 好了：亮「大招好了」
        self.assertIsNone(t.end("dire", 1, 730 + READY_SHOW))                 # 再过一会儿变回技能名
        self.assertTrue(t.mark("dire", 1, 735, 130))                          # 好了又放：重新计时，不是取消

    def test_recool(self):
        t = Tracker()
        t.mark("radiant", 0, 1000, 140)
        t.recool("radiant", 0, 120)                                           # 右键改成 3 级：从放的时候重算
        self.assertEqual(t.end("radiant", 0, 1010), 1120)
        t.recool("radiant", 4, 120)                                           # 没在计时的不记
        self.assertNotIn(("radiant", 4), t.casts)
        t.levels[("radiant", 0)] = 3
        t.reset()
        self.assertEqual((t.casts, t.levels), ({}, {}))


class RealKBTest(unittest.TestCase):
    """本机有知识库就抽查玩家点名的两个：末日、骷髅王"""
    def setUp(self):
        self.base = kb.KnowledgeBase.load()
        if not self.base:
            self.skipTest("没有知识库（python -m dota_assistant kb update）")

    def by_key(self, key):
        return next(h for h in self.base.heroes.values() if h.key == key)

    def test_doom_and_wraith_king(self):
        doom = ultcd.pick(self.by_key("doom_bringer"))
        wk = ultcd.pick(self.by_key("skeleton_king"))
        self.assertEqual(doom.key, "doom_bringer_doom")
        self.assertEqual(wk.key, "skeleton_king_reincarnation")
        self.assertEqual(len(ultcd.cooldowns(doom)), 3)
        doom_h = self.by_key("doom_bringer")
        labels = [m.label for m in ultcd.mods(doom_h, doom, self.base, {})]
        self.assertIn("20 级天赋：-10秒 末日冷却", labels)                     # 和天赋原文一致
        self.assertIn("玲珑心：-25% 冷却", labels)


class PrefetchTest(unittest.TestCase):
    """顶栏图标一次下全（data.fetch_icons）：金币 + 每个英雄小图标 + 会给小条的大招；已有的不下；断网不写标记"""

    def run_fetch(self, d: Path, fetch):
        from unittest import mock
        from dota_assistant import data
        from dota_assistant.heroes import Hero, HeroDB
        db = HeroDB([Hero(1, "doom_bringer", "末日使者", "Doom", (), "str"), Hero(2, "pudge", "帕吉", "Pudge", (), "str")])
        base = mock.Mock(patch="7.41f", heroes={1: hero(ab("doom_bringer_doom", "140/130/120")),
                                                2: hero(ab("pudge_dismember", "30/25/20"))})
        asked = []

        def fake(rel):
            asked.append(rel)
            return fetch(rel)
        with (mock.patch.object(data, "ICON_DIR", d), mock.patch.object(data, "ICONS_DONE", d / "done.txt"),
              mock.patch.object(data, "load_heroes", return_value=db), mock.patch.object(data, "fetch_icon", fake),
              mock.patch.object(kb.KnowledgeBase, "load", return_value=base)):
            data.fetch_icons(log=lambda *_: None)
        return asked

    def test_fetch_all_skip_existing(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "icons").mkdir()
            (d / "icons" / "gold.png").write_bytes(b"x")
            asked = self.run_fetch(d, lambda rel: d / rel)
            self.assertEqual(sorted(asked), ["abilities/doom_bringer_doom.png", "heroes/icons/doom_bringer.png",
                                             "heroes/icons/pudge.png"])          # 金币已有；帕吉肢解不给小条不下
            self.assertEqual((d / "done.txt").read_text(encoding="utf-8"), "7.41f")

    def test_offline_stops(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            self.assertEqual(len(self.run_fetch(d, lambda rel: None)), 1)      # 第一张下不到就不试剩下的
            self.assertFalse((d / "done.txt").exists())


if __name__ == "__main__":
    unittest.main()
