"""对面阵容出装表（domain/itemvs.py）+ 并进出装顺序 / 出装竖条（itembuild.make / bar，第 80 节）。不联网。"""
import json
import tempfile
import unittest
from pathlib import Path

from dota_assistant import kb
from dota_assistant.domain import coach, itembuild, itemvs


def row(item, share, usual, ok=1, minute=None):
    """表里的一行：[装备, 对面有它时比例, 期望比例, 变化, z, 变化过校正, 人次, 胜率差, 胜率 z, 胜率过校正, 中位分钟]"""
    return [item, share, usual, round(share - usual, 3), 9.9, ok, 300, 0.0, 0.0, 0, minute]


TABLE = {"v": 2, "base": {"18_1": {"n": 1000, "items": {"monkey_king_bar": [0.25, 34.0, 0.0], "dust": [0.05, 20.0, 0.0]}}},
         "vs": {"18_1": {
             "44": {"n": 611, "rows": [row("monkey_king_bar", 0.63, 0.24, minute=29.0), row("greater_crit", 0.53, 0.75, minute=28.0)]},
             "1": {"n": 1633, "rows": [row("monkey_king_bar", 0.37, 0.24, minute=33.0)]},
             "62": {"n": 1188, "rows": [row("dust", 0.19, 0.05, minute=15.5), row("ward_sentry", 0.25, 0.17, ok=0)]},
         }}}


class ShiftTest(unittest.TestCase):
    def test_combine_enemies(self):
        sh = {s.key: s for s in itemvs.shifts(18, 1, [1, 44, 62, 99], TABLE)}
        mkb = sh["monkey_king_bar"]
        self.assertEqual((mkb.share, mkb.usual, mkb.minute, mkb.enemies, mkb.n), (0.63, 0.24, 29.0, [44, 1], 611))   # 取多得最多的
        self.assertTrue(mkb.strong)
        self.assertTrue(sh["greater_crit"].strong and sh["greater_crit"].lift < 0)   # 少买 22 个点
        self.assertFalse(sh["dust"].strong)                                          # 19% 的人买，不够 40%
        self.assertNotIn("ward_sentry", sh)                                          # 没过校正的不要
        self.assertEqual(itemvs.shifts(18, 2, [44], TABLE), [])                      # 表里没有这个位置
        self.assertEqual(itemvs.base_rate(TABLE, 18, 1, "dust"), (0.05, 20.0))

    def test_load_rejects_old_version(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.json"
            p.write_text(json.dumps({"v": 1}), encoding="utf-8")
            with self.assertLogs("dota_assistant.domain.itemvs", "WARNING"):
                self.assertIsNone(itemvs.load(p))
            p2 = Path(d) / "t2.json"                     # load 按路径缓存，换个文件
            p2.write_text(json.dumps(TABLE), encoding="utf-8")
            self.assertEqual(itemvs.load(p2)["v"], 2)

    def test_real_table_shape(self):
        """进 git 的那份表（knowledge/item_vs.json）：v2，每行 11 列，斯温 1 号位对上幻影刺客多买金箍棒"""
        t = itemvs.load()
        self.assertIsNotNone(t)
        rows = t["vs"]["18_1"]["44"]["rows"]
        self.assertTrue(all(len(r) == 11 for r in rows))
        mkb = next(s for s in itemvs.shifts(18, 1, [44], t) if s.key == "monkey_king_bar")
        self.assertTrue(mkb.strong)
        self.assertLess(mkb.minute, 34)                  # 对上幻刺比平时（34 分钟）早出


@unittest.skipUnless((kb.KB_DIR / "kb.json").exists(), "没有本地知识库，先 kb update")
class BuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = kb.KnowledgeBase.load()

    def bench(self, **items):
        rows = {str(self.base.item_ids[f"item_{k}"]): [[m, n]] for k, (n, m) in items.items()}
        return {"matches": 100, "boots_max": 0, "per_min": {}, "items": rows}

    def test_make_with_shifts(self):
        bench = self.bench(blink=(96, 17), greater_crit=(75, 28), black_king_bar=(87, 23))
        enemies = [self.base.heroes[44], self.base.heroes[1]]           # 幻影刺客（闪避）、敌法师
        needs, _ = coach.plan(self.base.heroes[18], enemies, self.base, [], bench, 1)
        old = itembuild.make(needs, bench, self.base)
        self.assertIn("monkey_king_bar", [s.key for s in old.tips])      # 没有表：金箍棒只作提示（缺大数据）
        b = itembuild.make(needs, bench, self.base, itemvs.shifts(18, 1, [44, 1], TABLE))
        self.assertNotIn("monkey_king_bar", [s.key for s in b.tips])     # 有表：进顺序，提示里拿掉
        mkb = next(s for s in b.steps if s.key == "monkey_king_bar")
        self.assertEqual((mkb.kind, mkb.minute, mkb.share, mkb.usual, mkb.label), ("counter", 29.0, 0.63, 0.24, "63% 平时 24"))
        self.assertEqual([w["hero"] for w in mkb.who][:1], [44])
        self.assertIn("闪避", mkb.why)                                  # 知识库的理由留着
        self.assertIn("平时 24%", mkb.why)
        crit = next(s for s in b.steps if s.key == "greater_crit")
        self.assertTrue(crit.less)
        self.assertEqual((crit.share, crit.usual), (0.53, 0.75))
        self.assertEqual([s.key for s in b.steps], ["blink", "black_king_bar", "greater_crit", "monkey_king_bar"])
        text = itembuild.text(itembuild.brief_lines(b)[0][1])
        self.assertIn("↓代达罗斯之殇", text)
        self.assertIn("★金箍棒 29′", text)

    def test_avoid_not_readded(self):
        """「别买」的（对面 2 个无视技能免疫的控制）表里多买也不再加回顺序"""
        needs = [coach.Need("avoid:bkb", "别买", ("black_king_bar",), 0, 0, tip=coach.AVOID_TIP, avoid=True)]
        sh = [itemvs.Shift("black_king_bar", 0.9, 0.6, 20.0, [44])]
        b = itembuild.make(needs, self.bench(black_king_bar=(87, 23)), self.base, sh)
        self.assertEqual([s.key for s in b.steps], [])
        self.assertEqual([s.kind for s in b.tips], ["avoid"])

    def test_bar(self):
        const = {"blink": {"cost": 2250}, "black_king_bar": {"cost": 4050}, "monkey_king_bar": {"cost": 4975},
                 "greater_crit": {"cost": 5100}}
        tree = coach.item_tree(const)
        b = itembuild.make([], self.bench(blink=(96, 17), black_king_bar=(87, 23), greater_crit=(75, 28)), self.base,
                           itemvs.shifts(18, 1, [44], TABLE))
        v = itembuild.bar(b, set(), 650, tree, const)
        self.assertEqual((v["next"]["item"], v["next"]["pct"], v["next"]["left"], v["next"]["ready"]), ("blink", 96, 1600, False))
        self.assertEqual([(c["item"], c.get("mark"), c.get("usual")) for c in v["after"]],
                         [("black_king_bar", None, None), ("greater_crit", "less", 75), ("monkey_king_bar", "counter", 24)])
        self.assertEqual([(c["item"], c["pct"], c["usual"], c["heroes"], c["got"]) for c in v["vs"]],
                         [("monkey_king_bar", 63, 24, [44], False)])
        v = itembuild.bar(b, {"blink"}, 5000, tree, const)                 # 钱够下一件：亮
        self.assertEqual((v["next"]["item"], v["next"]["left"], v["next"]["ready"]), ("black_king_bar", 0, True))
        v = itembuild.bar(b, {"blink", "black_king_bar", "greater_crit", "monkey_king_bar"}, 0, tree, const)
        self.assertIsNone(v["next"])
        self.assertTrue(v["vs"][0]["got"])


if __name__ == "__main__":
    unittest.main()
