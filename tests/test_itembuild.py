"""出装推荐：还差多少钱 / 下一件 / 出装顺序 / 针对装（用真实知识库 + 假的同分段数据）。不联网。"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from dota_assistant import coach, itembuild, kb
from dota_assistant.gsi import GameState

CONST = {
    "ogre_axe": {"cost": 1000}, "mithril_hammer": {"cost": 1600},
    "black_king_bar": {"cost": 4050, "components": ["ogre_axe", "mithril_hammer"]},   # 配方 1450
    "force_staff": {"cost": 2200}, "dragon_lance": {"cost": 2000},
    "hurricane_pike": {"cost": 4550, "components": ["force_staff", "dragon_lance"]},
}
TREE = coach.item_tree(CONST)


class MoneyTest(unittest.TestCase):
    def test_remaining_cost_counts_owned_parts(self):
        self.assertEqual(itembuild.remaining_cost("black_king_bar", set(), CONST), 4050)
        self.assertEqual(itembuild.remaining_cost("black_king_bar", {"ogre_axe"}, CONST), 3050)
        self.assertEqual(itembuild.remaining_cost("hurricane_pike", {"force_staff", "dragon_lance"}, CONST), 350)

    def test_next_line(self):
        b = itembuild.Build([itembuild.Step("dragon_lance", "魔龙枪", 15, "core"),
                             itembuild.Step("black_king_bar", "黑皇杖", 25, "counter"),
                             itembuild.Step("hurricane_pike", "飓风长戟", 30, "core")])
        self.assertEqual(itembuild.next_line(b, set(), 500, TREE, CONST), "下一件：魔龙枪，还差 1500 金；之后 黑皇杖、飓风长戟")
        # 飓风长戟里有魔龙枪：合成了就算有；钱够了要说出来
        line = itembuild.next_line(b, {"hurricane_pike", "ogre_axe"}, 3100, TREE, CONST)
        self.assertEqual(line, "下一件：黑皇杖（针对），钱够了（要 3050，身上 3100）")
        self.assertIsNone(itembuild.next_line(b, {"hurricane_pike", "black_king_bar"}, 0, TREE, CONST))

    def test_sell_cheapest_when_full(self):
        const = {**CONST, "branches": {"cost": 50, "qual": "consumable"}, "tango": {"cost": 90, "qual": "consumable"},
                 "magic_wand": {"cost": 450}, "aegis": {"cost": 0}}
        tree = coach.item_tree(const)
        full = ["magic_wand", "branches", "tango", "aegis", "force_staff", "mithril_hammer"]
        self.assertEqual(itembuild.sell_pick(full, "dragon_lance", tree, const), ("branches", 50))   # 树枝算，最便宜
        no_branch = ["magic_wand", "tango", "aegis", "force_staff", "mithril_hammer", "ogre_axe"]
        self.assertEqual(itembuild.sell_pick(no_branch, "dragon_lance", tree, const), ("magic_wand", 450))  # 吃的、盾不算
        self.assertIsNone(itembuild.sell_pick(no_branch[:5] + [""], "dragon_lance", tree, const))       # 有空格
        self.assertIsNone(itembuild.sell_pick(no_branch, "black_king_bar", tree, const))   # 身上有零件，合成会空出格子
        b = itembuild.Build([itembuild.Step("dragon_lance", "魔龙枪", 15, "core")])
        line = itembuild.next_line(b, set(no_branch), 0, tree, const, slots=no_branch, names={"magic_wand": "魔杖"})
        self.assertEqual(line, "下一件：魔龙枪，还差 2000 金；格子满了，先卖魔杖（450 金，身上最便宜）")

    def test_buyback_after_late(self):
        b = itembuild.Build([itembuild.Step("dragon_lance", "魔龙枪", 15, "core")])
        line = lambda minute, bb: itembuild.next_line(b, set(), 3000, TREE, CONST, buyback=bb, minute=minute)
        self.assertEqual(line(35, (1500, 0)), "下一件：魔龙枪，钱够了（要 2000，身上 3000），但买完只剩 1000，不够买活（1500）")
        self.assertEqual(line(20, (1500, 0)), "下一件：魔龙枪，钱够了（要 2000，身上 3000）")     # 后期前不提
        self.assertEqual(line(35, (1500, 60)), "下一件：魔龙枪，钱够了（要 2000，身上 3000）")    # 买活冷却中
        self.assertEqual(line(35, (900, 0)), "下一件：魔龙枪，钱够了（要 2000，身上 3000）")      # 买完还够

    def test_split_by_stage(self):
        b = itembuild.Build([itembuild.Step("dragon_lance", "魔龙枪", 15, "core"),
                             itembuild.Step("black_king_bar", "黑皇杖", 25, "counter", "对面控制多"),
                             itembuild.Step("hurricane_pike", "飓风长戟", 34, "core")])
        mid = itembuild.brief_lines(b, hi=30)
        self.assertEqual(mid[0], ("row", "魔龙枪 15′ → ★黑皇杖 25′"))
        late = itembuild.late_lines(b, 30)
        self.assertEqual(late[0], ("row", "飓风长戟 34′"))
        self.assertIn(("row", "针对这局的黑皇杖前面就该有，到后期还没有先补"), late)
        self.assertIn("最便宜", late[-1][1])
        self.assertEqual(itembuild.late_lines(itembuild.Build(), 30), [])        # 没有出装数据就没有这一节

    def test_gsi_inventory_and_buyback(self):
        st = GameState()
        st.update({"items": {"slot0": {"name": "item_magic_wand"}, "slot1": {"name": "empty"},
                             "slot6": {"name": "item_branches"}},
                   "hero": {"buyback_cost": 1800, "buyback_cooldown": 0}})
        self.assertEqual(st.inventory, ["magic_wand", "", "", "", "", ""])      # 背包不算
        self.assertEqual(st.buyback, (1800, 0.0))
        st.update({"hero": {}})
        self.assertIsNone(st.buyback)

    def test_gsi_gold(self):
        st = GameState()
        st.update({"player": {"gold": 1234}})
        self.assertEqual(st.gold, 1234)
        st.update({"player": {}})
        self.assertIsNone(st.gold)


@unittest.skipUnless((kb.KB_DIR / "kb.json").exists(), "没有本地知识库，先 kb update")
class RealKBTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = kb.KnowledgeBase.load()

    def hero(self, name):
        return next(h for h in self.base.heroes.values() if h.name == name)

    def bench(self, **items):
        """items：装备 key → (买的局数, 中位分钟)，总共 100 局"""
        rows = {str(self.base.item_ids[f"item_{k}"]): [[m, n]] for k, (n, m) in items.items()}
        return {"matches": 100, "boots_max": 0, "per_min": {}, "items": rows}

    def plan(self, me, enemies, bench, pos=1):
        return coach.plan(self.hero(me), [self.hero(e) for e in enemies], self.base, [], bench, pos)

    def test_counter_only_from_heroes_menu(self):
        # 对面敌法师（法术反制怕破坏）：会出白银之锋的英雄才推
        needs, _ = self.plan("斯温", ["敌法师"], self.bench(silver_edge=(10, 30)))
        need = next(n for n in needs if n.key == "break")
        self.assertEqual(need.items, ("silver_edge",))
        self.assertTrue(any("被动技能无效" in e for e in need.evidence), need.evidence)
        needs, _ = self.plan("斯温", ["敌法师"], self.bench(silver_edge=(1, 30)))     # 1% 的局才出
        self.assertNotIn("break", [n.key for n in needs])
        needs, _ = self.plan("斯温", ["敌法师"], None)                                # 没有同分段数据不推
        self.assertNotIn("break", [n.key for n in needs])

    def test_self_skip_linken_for_antimage(self):
        bench = self.bench(sphere=(20, 30))
        needs, notes = self.plan("敌法师", ["莱恩", "末日使者"], bench)
        self.assertNotIn("linken", [n.key for n in needs])
        self.assertTrue(any("法术反制" in n and "不用专门出" in n for n in notes), notes)
        needs, _ = self.plan("斯温", ["莱恩", "末日使者"], bench)
        self.assertIn("linken", [n.key for n in needs])

    def test_illusion_counter(self):
        # 能自己造一群幻象的才算；幽鬼的如影随形「无法控制」、暗影恶魔造的是别人的幻象，不算
        self.assertEqual({h.name for h in self.base.heroes.values() if coach._illusions(h)},
                         {"幻影长矛手", "娜迦海妖", "混沌骑士", "恐怖利刃"})
        needs, _ = self.plan("主宰", ["娜迦海妖"], self.bench(bfury=(30, 14), mjollnir=(10, 30)))
        need = next(n for n in needs if n.key == "illusion")
        self.assertEqual(need.items, ("bfury",))                     # 两件都出：挑买得多的
        self.assertTrue(any("锥形区域" in e for e in need.evidence), need.evidence)
        needs, _ = self.plan("主宰", ["幽鬼", "暗影恶魔"], self.bench(bfury=(30, 14)))
        self.assertNotIn("illusion", [n.key for n in needs])

    def test_shard_only_evasion_ignored(self):
        # 狼人的狼要魔晶才有闪避，不值得为它出金箍棒
        needs, _ = self.plan("斯温", ["狼人"], None)
        self.assertNotIn("mkb", [n.key for n in needs])

    def test_build_order_and_early_bkb(self):
        bkb = str(self.base.item_ids["item_black_king_bar"])
        bench = self.bench(dragon_lance=(90, 14), hurricane_pike=(60, 24), butterfly=(20, 32))
        bench["items"][bkb] = [[20, 10], [30, 10], [40, 10]]
        needs, _ = self.plan("斯温", ["风行者", "沉默术士", "冥魂大帝", "莱恩", "末日使者"], bench)
        bkb_need = next(n for n in needs if n.key == "bkb")
        self.assertEqual(bkb_need.minute, 20)          # 5 个控制：按出得早的 1/4
        self.assertTrue(all(n.tip for n in needs))     # 针对装都还没有大数据依据：只作提示
        b = itembuild.make(needs, bench, self.base)
        self.assertEqual([(s.key, s.kind) for s in b.steps],     # 顺序里只有同分段常买的（黑皇杖 30% 的局买：之后常见）
                         [("dragon_lance", "core"), ("hurricane_pike", "core"), ("black_king_bar", "common"),
                          ("butterfly", "common")])
        self.assertEqual([(s.key, s.minute) for s in b.tips],    # 金箍棒：风行者有闪避
                         [("black_king_bar", 20), ("monkey_king_bar", 30)])
        self.assertTrue(b.tips[0].urgent)
        lines = itembuild.brief_lines(b)
        self.assertIn("提示 · 黑皇杖", lines[1][1])
        self.assertIn(coach.NO_DATA, lines[1][1])
        # 以后有数据支持（tip 为空）的针对装照旧排进顺序
        for n in needs:
            n.tip = ""
        b = itembuild.make(needs, bench, self.base)
        self.assertEqual([(s.key, s.kind) for s in b.steps][:2], [("dragon_lance", "core"), ("black_king_bar", "counter")])
        self.assertEqual(b.tips, [])


@unittest.skipUnless((kb.KB_DIR / "kb.json").exists(), "没有本地知识库，先 kb update")
class DeathPickTest(unittest.TestCase):
    """死亡复盘的结构化结果 → 该补什么装备"""

    @classmethod
    def setUpClass(cls):
        cls.base = kb.KnowledgeBase.load()
        cls.tree = coach.item_tree(coach.items_const())

    def rec(self, cause="", **kw):
        from dota_assistant import death
        return death.Recap(600, 1, cause=cause, **kw)

    def timings(self, **shares):
        return {self.base.item_ids[f"item_{k}"]: (v, 20) for k, v in shares.items()}

    def pick(self, rec, owned=(), **shares):
        return itembuild.death_pick(rec, set(owned), self.timings(**shares), self.base, self.tree)

    def test_control_prefers_bkb_then_linken(self):
        rec = self.rec("control", secs={"stunned": 3})
        self.assertEqual(self.pick(rec, black_king_bar=0.8, sphere=0.1).key, "black_king_bar")
        p = self.pick(rec, ["black_king_bar"], black_king_bar=0.8, sphere=0.1)      # 已经有黑皇杖
        self.assertEqual(p.key, "sphere")
        self.assertIn("指向性技能", p.quote)
        self.assertIsNone(self.pick(rec, black_king_bar=0.01))                         # 这个英雄不出的不推
        self.assertIsNone(self.pick(self.rec("control", secs={"stunned": 3}, immune=2), black_king_bar=0.8))

    def test_bkb_blocked_suggests_linken_not_bkb(self):
        rec = self.rec("control", secs={"hexed": 3}, saves={"blocked": ["black_king_bar"]})
        self.assertEqual(self.pick(rec, ["black_king_bar"], sphere=0.1, aeon_disk=0.05).key, "sphere")
        self.assertEqual(itembuild.death_cause(rec).key, "control")          # 和普通被控死算同一种

    def test_unused_save_means_no_item(self):
        rec = self.rec("control", secs={"stunned": 3}, saves={"unused": ["black_king_bar"]})
        self.assertIsNone(self.pick(rec, ["black_king_bar"], sphere=0.5))
        rec = self.rec("control", secs={"stunned": 3}, saves={"unused": ["magic_wand"]})   # 魔杖没按不算
        self.assertEqual(self.pick(rec, black_king_bar=0.8).key, "black_king_bar")

    def test_escape_by_share_and_silence(self):
        rec = self.rec("low")
        self.assertEqual(self.pick(rec, force_staff=0.2, glimmer_cape=0.5).key, "glimmer_cape")
        self.assertEqual(self.pick(rec, ["hurricane_pike"], force_staff=0.6, glimmer_cape=0.1).key, "glimmer_cape")
        self.assertEqual(self.pick(self.rec(secs={"silenced": 4}), manta=0.5).key, "manta")
        self.assertIsNone(self.pick(self.rec(secs={"silenced": 4, "muted": 4}), manta=0.5))   # 锁闭时物品也用不了

    def test_repeat_death_tip(self):
        b = itembuild.Build([itembuild.Step("dragon_lance", "魔龙枪", 15, "core")])
        c = coach.Coach([], None, self.tree, self.base, b, coach.items_const())
        st = coach.State(20, set(), gold=0)
        rec = self.rec("control", secs={"stunned": 3})
        unused = self.rec("control", secs={"stunned": 3}, saves={"unused": ["blink"]})
        self.assertEqual(c.on_death(rec, st), [])                    # 第 1 次：不推
        self.assertEqual(c.on_death(self.rec("low"), st), [])         # 别的死法另算
        self.assertEqual(c.on_death(unused, st), [])                  # 第 2 次（跳刀没用上，照样算次数）：不推
        self.assertTrue(c.next_line(st).startswith("下一件：魔龙枪"))
        # 第 3 次：只有本局依据、没有大数据依据，只作提示，不动「下一件」
        self.assertEqual(c.on_death(rec, st), [("row", f"提示：本局第 3 次被控死，可以考虑黑皇杖：黑皇杖开了以后控不住你（{coach.NO_DATA}）")])
        self.assertTrue(c.next_line(st).startswith("下一件：魔龙枪"))


class ShopCardTest(unittest.TestCase):
    """死亡时卡片上带「回家买」：下一件 + 还差多少钱（钱来自 GSI player.gold）"""

    def setUp(self):
        from tests.test_webui import make_live
        b = itembuild.Build([itembuild.Step("black_king_bar", "黑皇杖", 20, "counter")])

        def prepare(cfg, my_id, enemy_ids, pos):
            return coach.Coach([], None, TREE, None, b, CONST), []

        self.live = make_live(coach_prepare=prepare)
        self.live.gsi = SimpleNamespace(state=GameState(), stop=lambda: None)
        self.live.slots["dire"][:1] = ["斧王"]
        self.live._death_names = ("phantom_lancer", {}, set())     # 不读知识库
        self.live.start()

    def tearDown(self):
        self.live.close()

    def push(self, minute, deaths=0, gold=0, items=()):
        from tests.test_webui import wait
        self.live.gsi.state.update({
            "map": {"matchid": "123", "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS", "clock_time": int(minute * 60)},
            "player": {"team_name": "radiant", "deaths": deaths, "last_hits": 40, "gold": gold},
            "hero": {"name": "npc_dota_hero_phantom_lancer", "alive": not deaths},
            "items": {f"slot{i}": {"name": f"item_{k}"} for i, k in enumerate(items)}})
        wait(self.live.submit(self.live.coach_tick))
        return json.loads(self.live.current_snapshot)["overlay"]

    def test_shop_line_on_death(self):
        from dota_assistant import death
        self.addCleanup(setattr, death, "DUMP_DIR", death.DUMP_DIR)
        death.DUMP_DIR = Path(tempfile.mkdtemp()) / "deaths"          # 死前推送存到临时目录
        self.push(0)
        for _ in range(50):
            if self.live.coach:
                break
            time.sleep(0.05)
        self.live.coach_card = None
        lines = self.push(12, deaths=1, gold=1000, items=["ogre_axe"])["view"]["lines"]
        self.assertEqual(lines[-2:], [{"kind": "head", "text": "回家买"},
                                      {"kind": "row", "text": "下一件：黑皇杖（针对），还差 2050 金"}])


if __name__ == "__main__":
    unittest.main()
