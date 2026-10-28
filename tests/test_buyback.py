"""买活计时：点一下开始 / 再点取消、冷却结束后亮一会儿「可买活」、自己的按 GSI 同步；
截图自动记：顶栏头像变灰 → 认复活倒计时 → 复活后消息栏「买活了」+ 行首小图对上几帧才记（合成的图，OCR 换成假的）。"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

from dota_assistant.domain import buyback
from dota_assistant.vision import buybackscan as bs
from dota_assistant.domain.buyback import COOLDOWN, READY_SHOW, Tracker


def topbar(dead=(), timers=None, size=(1080, 1920), badge=False) -> np.ndarray:
    """合成一帧：10 个彩色头像（位置同 buybackscan），dead 里的变灰、下面写倒计时 timers[(阵营, 格)]；
    badge：灰头像左下角画一个彩色小图标（真实顶栏上的神杖 / 魔晶 / 橙色三角，死了也是彩色的）"""
    h, w = size
    s = h / 1080
    img = np.full((h, w, 3), 40, np.uint8)
    for side in bs.SIDES:
        for i in range(5):
            cx = int(w / 2 + (bs.CENTER0[side] + bs.PITCH * i) * s)
            x0, x1, y0, y1 = cx - int(28 * s), cx + int(28 * s), int(4 * s), int(40 * s)
            hue = (i * 30 + (0 if side == "radiant" else 15)) % 180
            img[y0:y1, x0:x1] = cv2.cvtColor(np.uint8([[[hue, 200, 180]]]), cv2.COLOR_HSV2BGR)[0, 0]
            if (side, i) in dead:
                roi = img[y0:y1, x0:x1]
                img[y0:y1, x0:x1] = cv2.cvtColor(cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
                if badge:                        # 约占量饱和度区域的 1/4，平均饱和度会被抬到 40 以上
                    cv2.rectangle(img, (cx - int(24 * s), int(18 * s)), (cx - int(4 * s), int(38 * s)), (0, 110, 230), -1)
                t = (timers or {}).get((side, i))
                if t is not None:
                    cv2.rectangle(img, (cx - int(18 * s), int(44 * s)), (cx + int(18 * s), int(64 * s)), (20, 20, 20), -1)
                    cv2.putText(img, str(t), (cx - int(14 * s), int(61 * s)), cv2.FONT_HERSHEY_SIMPLEX, 0.6 * s,
                                (255, 255, 255), max(1, int(2 * s)))
    return img


def hero_icon(seed: int) -> np.ndarray:
    """假的英雄小图标：32×32 带透明通道，4×4 的随机色块（每个 seed 不一样，互相比不像）"""
    rng = np.random.default_rng(seed)
    rgb = cv2.resize(rng.integers(0, 256, (4, 4, 3), dtype=np.uint8), (32, 32), interpolation=cv2.INTER_NEAREST)
    alpha = np.full((32, 32, 1), 255, np.uint8)
    alpha[:2] = 0                                # 上面两行透明（真图标边上也是透明的）
    return np.dstack([rgb, alpha])


def feed_line(img: np.ndarray, icon: np.ndarray, y: float = 626) -> np.ndarray:
    """在消息栏 y 那一行（1080 高的坐标）行首贴上英雄小图（边长 22，和实战截图一样）"""
    s = img.shape[0] / 1080
    n = round(22 * s)
    t = cv2.resize(icon, (n, n), interpolation=cv2.INTER_AREA).astype(np.float32)
    x0, y0 = round(19 * s), round(y * s - n / 2)
    roi = img[y0:y0 + n, x0:x0 + n].astype(np.float32)
    a = t[..., 3:] / 255
    img[y0:y0 + n, x0:x0 + n] = (t[..., :3] * a + roi * (1 - a)).astype(np.uint8)
    return img


def feed_words(text: str, y: float = 626, x: float = 45, s: float = 1.0, line: int = 0) -> list:
    """假 OCR 给 read_feed 的字：坐标换成「消息栏那块、放大 FEED_SCALE 倍」里的"""
    h = 16 * s * bs.FEED_SCALE
    return [SimpleNamespace(line=line, text=text, x=(x - bs.FEED[0]) * s * bs.FEED_SCALE,
                            y=(y - bs.FEED[1]) * s * bs.FEED_SCALE - h / 2, w=200 * s, h=h)]


class FeedTest(unittest.TestCase):
    """消息栏「<英雄小图> 玩家名 买活了」：找带「买活」的行，行首小图在这局英雄里认"""
    icons = {"axe": hero_icon(1), "pudge": hero_icon(2), "lion": hero_icon(3)}

    def test_icon_tells_who(self):
        img = feed_line(topbar(), self.icons["axe"])
        lines = bs.read_feed(img, lambda im: feed_words("大王师傅123 [GOOER] 买活了"), self.icons)
        self.assertEqual([(ln.hero, round(ln.y)) for ln in lines], [("axe", 626)])
        self.assertGreater(lines[0].score, 0.95)
        img = feed_line(topbar(), self.icons["pudge"])
        self.assertEqual(bs.read_feed(img, lambda im: feed_words("夭王师傅12新买活了"), self.icons)[0].hero, "pudge")

    def test_4k(self):
        img = feed_line(topbar(size=(2160, 3840)), self.icons["lion"], y=664)
        lines = bs.read_feed(img, lambda im: feed_words("eins 买活了", y=664, s=2), self.icons)
        self.assertEqual(lines[0].hero, "lion")

    def test_no_icon_match_or_no_text(self):
        img = topbar()                                         # 行首没有这局英雄的小图
        self.assertIsNone(bs.read_feed(img, lambda im: feed_words("某人 买活了"), self.icons)[0].hero)
        img = feed_line(topbar(), self.icons["axe"])
        self.assertEqual(bs.read_feed(img, lambda im: feed_words("如同神一般被终结"), self.icons), [])

    def test_chat_line_uses_name_not_icon(self):
        """聊天行「eins（司夜刺客）买活了!」字从 x≈560 开始：行首那儿的小图是别的消息的，不认（实战里认成了獸）"""
        img = feed_line(topbar(), self.icons["axe"], y=746)
        lines = bs.read_feed(img, lambda im: feed_words("eins [k-on.]（司夜刺客）买活了！", y=746, x=566), self.icons)
        self.assertEqual([ln.hero for ln in lines], [None])
        c = bs.FeedCheck(self.rev(), "nyx_assassin", "司夜刺客")
        c.add(lines, None)
        self.assertEqual(c.hits, 1)                            # 靠英雄名对上

    @staticmethod
    def rev(buyback_by_timer: bool | None = True) -> bs.Revival:
        expected = None if buyback_by_timer is None else (141 if buyback_by_timer else 115)
        return bs.Revival("dire", 0, 115, 14, expected, 40, None, np.zeros((76, 62, 3), np.uint8))

    def line(self, hero, text="xx 买活了"):
        return bs.FeedLine(text, 626, hero)

    def test_needs_two_frames(self):
        c = bs.FeedCheck(self.rev(), "axe", "斧王")
        c.add([self.line("axe")], None)
        self.assertEqual(c.verdict, (None, ""))                # 一帧不够
        c.add([], None)
        c.add([self.line("axe")], None)
        self.assertTrue(c.verdict[0])                          # 第二帧对上就定，不用等满 FEED_TRIES 帧
        self.assertEqual(c.seen, 3)

    def test_one_frame_or_other_hero_is_not_buyback(self):
        c = bs.FeedCheck(self.rev(), "axe", "斧王")
        c.add([self.line("axe")], None)
        for _ in range(bs.FEED_TRIES - 1):
            c.add([self.line("pudge")], None)                  # 消息栏是帕吉买活
        self.assertIs(c.verdict[0], False)
        self.assertIn("只有 1 帧", c.verdict[1])
        c = bs.FeedCheck(self.rev(), "axe", "斧王")
        for _ in range(bs.FEED_TRIES):
            c.add([self.line("pudge")], None)
        self.assertEqual(c.verdict, (False, "消息栏的买活是别的英雄的"))
        c = bs.FeedCheck(self.rev(), "axe", "斧王")
        for _ in range(bs.FEED_TRIES):
            c.add([], None)
        self.assertEqual(c.verdict, (False, "消息栏没有买活"))  # 倒计时说早复活了也不记：没有买活消息

    def test_many_icons_need_margin(self):
        """和全部英雄比：第一名要比第二名高出 ICON_MARGIN 才算（两个几乎一样的小图分不开就不认）"""
        many = {f"h{i}": hero_icon(10 + i) for i in range(30)} | self.icons
        img = feed_line(topbar(), self.icons["axe"])
        line = bs.read_feed(img, lambda im: feed_words("某人 买活了"), many)[0]
        self.assertEqual(line.hero, "axe")
        twin = self.icons["axe"].copy()
        twin[16:, 16:, :3] = twin[16:, 16:, :3] // 2 + 60      # 和斧王只差一角
        line = bs.read_feed(img, lambda im: feed_words("某人 买活了"), many | {"twin": twin})[0]
        self.assertIsNone(line.hero)

    def test_unknown_slot_uses_icon_outside_lineup(self):
        """格子没填（选人时没认出来）：小图认出是这局格子里没有的英雄，就算这格的；格子里有的英雄不算；
        倒计时说按时复活的不算（是别的没填的格子在买活）。2026-10-27 军团指挥官买活就是这样漏的"""
        known = frozenset({"axe", "pudge"})
        hinted = self.rev(None)
        hinted.hint = True                                     # 倒计时只认到一次（84），也说早复活了
        c = bs.FeedCheck(hinted, None, None, known)
        c.add([self.line("pudge"), self.line("legion_commander")], None)
        c.add([self.line("legion_commander")], None)
        self.assertEqual((c.verdict[0], c.found), (True, "legion_commander"))
        c = bs.FeedCheck(self.rev(True), None, None, known)    # 格子外两个英雄：取最下面（最新）那行
        c.add([bs.FeedLine("x 买活了", 600, "storm_spirit"), bs.FeedLine("y 买活了", 640, "legion_commander")], None)
        self.assertEqual(c.found, "legion_commander")
        for timer in (None, False):                            # 没有早复活的迹象 / 倒计时说按时复活：小图说明不了是这一格
            c = bs.FeedCheck(self.rev(timer), None, None, known)
            for _ in range(bs.FEED_TRIES):
                c.add([self.line("legion_commander")], None)
            self.assertIs(c.verdict[0], False, timer)
        c = bs.FeedCheck(self.rev(True), None, None, known)
        for _ in range(bs.FEED_TRIES):
            c.add([self.line("pudge")], None)                  # 格子里有的英雄：是别人
        self.assertIs(c.verdict[0], False)
        c = bs.FeedCheck(self.rev(True), None, None, known)    # 两帧是两个不同的英雄：不算
        c.add([self.line("legion_commander")], None)
        c.add([self.line("lion")], None)
        self.assertEqual(c.hits, 1)

    def test_fallback_without_icon(self):
        """他的小图标下不到：消息栏有「买活」行（认不出是谁）几帧 + 倒计时也说早复活了，才记"""
        for timer, want in ((True, True), (False, False), (None, False)):
            c = bs.FeedCheck(self.rev(timer), "axe", "斧王", has_icon=False)
            for _ in range(bs.FEED_TRIES):
                c.add([self.line(None)], None)
            self.assertIs(c.verdict[0], want, timer)


class TrackerTest(unittest.TestCase):
    def test_mark_then_cancel(self):
        t = Tracker()
        self.assertTrue(t.mark("dire", 2, 1500))
        self.assertEqual(t.end("dire", 2, 1600), 1500 + COOLDOWN)
        self.assertFalse(t.mark("dire", 2, 1510))              # 点错了：取消
        self.assertIsNone(t.end("dire", 2, 1510))

    def test_ready_then_idle(self):
        t = Tracker()
        t.mark("radiant", 0, 1000)
        end = 1000 + COOLDOWN
        self.assertEqual(t.end("radiant", 0, end + READY_SHOW - 1), end)   # 冷却好了：还亮着「可买活」
        self.assertIsNone(t.end("radiant", 0, end + READY_SHOW))           # 过一会儿变回空条
        self.assertTrue(t.mark("radiant", 0, end + 5))                     # 冷却好了又买活：重新计时，不是取消

    def test_self_synced_from_gsi(self):
        t = Tracker()
        t.sync_self("dire", 3, 2000, 470)
        self.assertEqual(t.end("dire", 3, 2000), 2470)
        self.assertEqual(t.auto, {("dire", 3)})
        t.sync_self("dire", 3, 2001, 470)                     # 推送晚到 1 秒：差得不多不改
        self.assertEqual(t.end("dire", 3, 2001), 2470)
        t.sync_self("dire", 3, 2010, 300)                     # 差得多：按 GSI 改
        self.assertEqual(t.end("dire", 3, 2010), 2310)
        t.sync_self("dire", 3, 2020, 0)                       # GSI 说没在冷却
        self.assertIsNone(t.end("dire", 3, 2020))
        t.reset()
        self.assertEqual((t.ends, t.auto), ({}, set()))

    def test_cooldown_is_eight_minutes(self):
        self.assertEqual(buyback.COOLDOWN, 480)

    def test_gsi_buyback_cooldown_is_end_game_time(self):
        """2026-10-26 实战推送：hero.buyback_cooldown 一直是 2817（买活时 game_time 2337 + 480），不是还剩几秒"""
        from dota_assistant.core.gsi_fields import buyback_left
        from dota_assistant.gsi import GameState
        push = lambda cd, gt: {"map": {"clock_time": gt - 246, "game_time": gt}, "hero": {"buyback_cost": 1634, "buyback_cooldown": cd}}
        self.assertEqual(buyback_left(push(2817, 2753)), 64)
        self.assertEqual(buyback_left(push(2817, 2900)), 0)       # 冷却过了，字段还留着
        self.assertEqual(buyback_left(push(0, 2000)), 0)          # 没买过活
        st = GameState()
        st.update(push(2817, 2813))
        self.assertEqual(st.buyback, (1634, 4))


class WatcherTest(unittest.TestCase):
    def test_saturation_alive_vs_dead(self):
        img = topbar(dead={("dire", 2)})
        self.assertGreater(bs.saturation(img, "radiant", 0), bs.ALIVE_SAT)
        self.assertLess(bs.saturation(img, "dire", 2), bs.DEAD_SAT)
        img = topbar(dead={("dire", 2)}, badge=True)              # 角上彩色小图标：中位数照样是灰的
        self.assertLess(bs.saturation(img, "dire", 2), bs.DEAD_SAT)

    def test_one_colored_frame_is_not_revival(self):
        """2026-10-26 实战：狙击手死着，彩色一帧又灰回去，被记成买活。现在要连续 REVIVE_FRAMES 帧彩色"""
        w = bs.Watcher()
        dead = {("radiant", 0)}
        for t in (10, 11):
            w.update(topbar(dead, badge=True), t)
        self.assertEqual(w.update(topbar(), 12)[1], [])            # 只彩色一帧
        self.assertEqual(w.update(topbar(dead, badge=True), 13)[1], [])
        self.assertIsNotNone(w.slots[("radiant", 0)].dead_since)  # 还是同一次死
        self.assertEqual(w.update(topbar(), 20)[1], [])
        revs = w.update(topbar(), 21)[1]
        self.assertEqual([(r.clock, r.dead_for) for r in revs], [(20, 10)])   # 复活时刻记第一帧彩色的

    def run_life(self, revive_at: float, timer: int | None = 40, size=(1080, 1920)):
        """夜魇第 3 个在 100 秒死、倒计时 timer 秒，revive_at 秒变回彩色；OCR 当认到 timer"""
        w = bs.Watcher()
        d, r = w.update(topbar(size=size), 99)
        self.assertEqual((d, r), ([], []))
        dead = {("dire", 2)}
        self.assertEqual(w.update(topbar(dead, size=size), 100), ([], []))       # 灰一帧不算（躲特效）
        deaths, _ = w.update(topbar(dead, {("dire", 2): timer}, size), 101)
        self.assertEqual([(x.side, x.index) for x in deaths], [("dire", 2)])
        w.set_timer(deaths[0], timer)
        deaths, _ = w.update(topbar(dead, {("dire", 2): timer - 1}, size), 102)   # 第二次认到的对得上才算
        w.set_timer(deaths[0], timer - 1)
        for t in range(103, int(revive_at)):
            self.assertEqual(w.update(topbar(dead, size=size), t), ([], []))     # 认出来了就不再送 OCR
        self.assertEqual(w.update(topbar(size=size), revive_at), ([], []))     # 彩色一帧还不算
        _, revs = w.update(topbar(size=size), revive_at + 1)
        self.assertEqual(len(revs), 1)
        self.assertEqual(revs[0].clock, revive_at)
        return revs[0]

    def test_buyback_vs_respawn(self):
        rev = self.run_life(115)                                   # 101 + 40 = 141 才该复活，115 就活了
        self.assertTrue(rev.buyback)
        self.assertEqual((rev.expected, rev.seconds, rev.dead_for), (141, 39, 15))   # 记的是第二次认到的
        self.assertFalse(self.run_life(141).buyback)               # 倒计时走完复活
        self.assertFalse(self.run_life(139).buyback)               # 每秒一帧的误差内
        self.assertTrue(self.run_life(115, size=(2160, 3840)).buyback)   # 4K：位置按图高缩放

    def test_unreadable_timer_not_recorded(self):
        w = bs.Watcher()
        dead = {("radiant", 4)}
        w.update(topbar(dead), 10)
        sent = [len(w.update(topbar(dead), t)[0]) for t in range(11, 18)]
        self.assertEqual(sent, [1] * bs.OCR_TRIES + [0] * (7 - bs.OCR_TRIES))   # 认不出最多送 OCR_TRIES 次
        w.update(topbar(), 18)
        _, revs = w.update(topbar(), 19)
        self.assertIsNone(revs[0].buyback)                         # 不知道是不是买活：不自动记

    def test_misread_needs_agreement(self):
        """10 认成 1：和下一帧认到的 9 对不上，不算；再下一帧认到 8 和 9 对上了才算"""
        w = bs.Watcher()
        dead = {("dire", 0)}
        w.update(topbar(dead), 10)
        for clock, sec in ((11, 1), (12, 9)):
            w.set_timer(w.update(topbar(dead), clock)[0][0], sec)
        self.assertIsNone(w.slots[("dire", 0)].expected)
        w.set_timer(w.update(topbar(dead), 13)[0][0], 8)
        self.assertEqual(w.slots[("dire", 0)].expected, 21)

    def test_single_read_gives_hint(self):
        """死了马上买活，倒计时只来得及认到一次（2026-10-27 军团指挥官：认到 84、一秒后就复活了）：
        核对不上不算 buyback，但记个「早复活的迹象」；认到的几次有一次说按时就没有"""
        for reads, want in (((84,), True), ((84, 2), False)):
            w = bs.Watcher()
            dead = {("dire", 0)}
            w.update(topbar(dead), 10)
            for k, sec in enumerate(reads):
                w.set_timer(w.update(topbar(dead), 11 + k)[0][0], sec)
            w.update(topbar(), 13)
            rev = w.update(topbar(), 14)[1][0]
            self.assertIsNone(rev.buyback)
            self.assertIs(rev.early, want, reads)

    def test_stale_ocr_result_ignored(self):
        w = bs.Watcher()
        dead = {("radiant", 0)}
        w.update(topbar(dead), 10)
        d1 = w.update(topbar(dead), 11)[0][0]
        w.update(topbar(), 12)
        w.update(topbar(), 13)                                     # 复活了，OCR 结果才回来
        w.set_timer(d1, 30)
        self.assertEqual(w.slots[("radiant", 0)].expected, None)

    def test_skip_self_and_tiny_frames(self):
        w = bs.Watcher()
        dead = {("radiant", 1)}
        w.update(topbar(dead), 10)
        self.assertEqual(w.update(topbar(dead), 11, skip={("radiant", 1)}), ([], []))
        self.assertEqual(w.update(np.zeros((9, 16, 3), np.uint8), 12), ([], []))

    def test_parse_seconds(self):
        self.assertEqual(bs.parse_seconds(["37", "37", "3", "37"]), 37)
        self.assertEqual(bs.parse_seconds(["1O0", "100"]), 100)
        self.assertIsNone(bs.parse_seconds(["", "买活"]))

    def test_sample_saved(self):
        rev = self.run_life(115)
        d = Path(tempfile.mkdtemp(prefix="dota-bb-"))
        path = bs.save_sample(rev, "buyback-40", d, feed=np.zeros((460, 900, 3), np.uint8))
        self.assertTrue(path.name.endswith("-dire3-buyback-40.png") and path.exists())
        self.assertTrue((d / "feed" / path.name).exists())        # 消息栏那块另存一张，核对用


class TrackerStartTest(unittest.TestCase):
    def test_auto_start_does_not_cancel_manual(self):
        t = Tracker()
        t.mark("dire", 1, 100)                                     # 玩家先点了
        self.assertFalse(t.start("dire", 1, 103))                  # 截图认出来的不覆盖、也不取消
        self.assertEqual(t.end("dire", 1, 200), 100 + COOLDOWN)
        self.assertTrue(t.start("radiant", 0, 50))


if __name__ == "__main__":
    unittest.main()
