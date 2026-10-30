"""买活 / 大招计时：顶栏小条、买活自动记（顶栏头像 + 复活倒计时 + 消息栏）、对面大招冷却。"""
from __future__ import annotations

import logging
import threading
import time

from ...core import config
from ...domain import buyback, coach, ultcd
from ...vision import buybackscan
from ...kb import cd_ops
from ...sources import assets
from . import consts

log = logging.getLogger(__name__)


class TimersMixin:
    # ---------- 买活计时 ----------

    def _buyback_tick(self, st, key: str) -> None:
        """换局清空；对上游戏时钟（页面自己走秒，这里只在差得多 / 暂停时改）；自己的冷却按 GSI 自动算"""
        if key != self._bb_match:
            self._bb_match = key
            self.buyback.reset()
            self.ults.reset()
            self._bb_watch.reset()
            self._bb_checks.clear()
            self._bb_orphans.clear()
            self._bb_scan_err = False
        m = st.raw.get("map") or {}
        clock = m.get("clock_time")
        # 进游戏（号角前出门）就显示，玩家 2026-10-26 要的「开局就可以显示」；号角前时钟是负数，倒计时照样算。
        # 这里只管在不在局里，买活 / 大招开没开用的时候现看 cfg（控制面板能随时开关，buyback_view）
        self._bb_show = bool(st.in_match and isinstance(clock, (int, float)))
        if self._bb_show and self.cfg.ult_timer:
            self._knowledge()                    # 第一次读知识库要一会儿，放在这里读，别拖慢推快照
        if not isinstance(clock, (int, float)):
            return
        if m.get("paused"):
            self._bb_paused = float(clock)
        else:
            self._bb_paused = None
            anchor = st.updated - clock
            if self._bb_anchor is None or abs(anchor - self._bb_anchor) > consts.CLOCK_SLACK:
                self._bb_anchor = anchor
        me = self._my_slot(st)
        if me and st.buyback:
            self.buyback.sync_self(*me, float(clock), st.buyback[1])
        if self._bb_show and self.cfg.buyback_timer and self.cfg.buyback_auto and self._bb_paused is None and clock >= 0:
            self._scan_buyback(float(clock), me)

    def _scan_buyback(self, clock: float, me) -> None:
        """截一帧 Dota 窗口看顶栏：刚死的在后台认复活倒计时，复活得比倒计时早 = 买活（buybackscan.py）"""
        try:
            img = self.grab()
            if getattr(self.screen, "source", "window") != "window":
                return                           # 截到的是整块屏幕（Dota 切出去了），顶栏不在那儿
            deaths, revs = self._bb_watch.update(img, clock, skip={me} if me else ())
            self._keep_game_crops(img)
        except Exception:
            if not self._bb_scan_err:
                self._bb_scan_err = True
                log.warning("买活自动记：截图 / 认顶栏出错，这局不再记日志", exc_info=True)
            return
        match = self._bb_match
        for d in deaths:
            def work(d=d):
                try:
                    sec = buybackscan.read_timer(d, lambda im: [w.text for w in self._ocr(im)])
                except Exception as e:           # 没有 OCR 之类
                    log.warning("买活自动记：复活倒计时没认（%s）", e)
                    sec = None
                self.submit(self._timer_read, match, d, sec)
            threading.Thread(target=work, daemon=True, name="buyback").start()
        for rev in revs:
            self._revived(rev)
        self._check_feed(img, match, clock, me)

    def _keep_game_crops(self, img) -> None:
        """没填英雄的格子留一张局内顶栏头像（活着、彩色的时候），给顶栏帮认的候选排序；每格留一次就够"""
        got = self._crops["game"]
        new = False
        for side in consts.SIDES:
            for i in range(5):
                if ((side, i) not in got and not self.slots[side][i]
                        and buybackscan.saturation(img, side, i) >= buybackscan.ALIVE_SAT):
                    got[(side, i)] = buybackscan.portrait_crop(img, side, i)
                    new = True
        if new:
            self._crops_ver += 1

    def _timer_read(self, match, d, sec) -> None:
        if match == self._bb_match:
            self._bb_watch.set_timer(d, sec)
            log.info("买活自动记：%s %s 死了，复活倒计时认到 %s", coach._mmss(d.clock / 60), self._slot_name(d.side, d.index), sec)

    def _slot_name(self, side: str, index: int) -> str:
        return self.slots[side][index] or f"{'天辉' if side == 'radiant' else '夜魇'}第 {index + 1} 个"

    def _revived(self, rev) -> None:
        """复活了：接下来几帧看消息栏有没有「买活了」、行首小图是不是他（buybackscan.FeedCheck），够帧数了在 _feed_done 定"""
        h = self._hero(self.slots[rev.side][rev.index])
        known = frozenset(x.key for side in consts.SIDES for t in self.slots[side] if (x := self._hero(t)))
        self._bb_checks.append(buybackscan.FeedCheck(rev, h.key if h else None, h.name_cn if h else None, known))
        log.info("买活自动记：%s %s 复活（灰了 %.0f 秒，倒计时 %s → 该在 %s 复活），看消息栏确认", coach._mmss(rev.clock / 60),
                 self._slot_name(rev.side, rev.index), rev.dead_for, rev.seconds,
                 coach._mmss(rev.expected / 60) if rev.expected is not None else "?")

    def _hero_icon(self, key: str, download: bool = True):
        """英雄小图标（Valve CDN，第一次下、之后读 data/icons）；下不到是 None（这次运行不再试）。
        第一次可能要联网下，只在后台线程里调。download=False：本地没有就算了（不在局内挨个下 127 张）"""
        if key not in self._hero_icons:
            rel = buybackscan.HERO_ICON.format(key)
            if not download and not (assets.ICON_DIR / rel).is_file():
                return None
            self._hero_icons[key] = buybackscan.load_icon(assets.fetch_icon(rel))
        return self._hero_icons[key]

    def _check_feed(self, img, match, clock: float, me=None) -> None:
        """每个还在确认的格子拿这一帧看一次消息栏（后台线程，OCR 一次约 0.5 秒；上一帧还没认完就跳过这帧）"""
        todo = [c for c in self._bb_checks if not c.busy]
        if not todo:
            return
        keys = {h.key for side in consts.SIDES for t in self.slots[side] if (h := self._hero(t))}
        for c in todo:
            c.busy = True

        def work():
            # 行首小图和全部英雄比（格子没填的也认得出；本地有的才用，这局的英雄没有就现下）
            icons = {h.key: ic for h in self.db if (ic := self._hero_icon(h.key, download=h.key in keys)) is not None}
            for c in todo:
                c.has_icon = c.hero in icons
            try:
                lines = buybackscan.read_feed(img, self._ocr, icons)
            except Exception as e:               # 没有 OCR 之类
                log.warning("买活自动记：消息栏没认（%s）", e)
                lines = []
            feed = buybackscan.feed_crop(img).copy()
            self.submit(self._feed_read, match, todo, lines, feed, clock, me)
        threading.Thread(target=work, daemon=True, name="buyback").start()

    def _feed_read(self, match, checks, lines, feed, clock: float = 0.0, me=None) -> None:
        if match != self._bb_match:
            return
        if lines:
            log.info("买活自动记：消息栏 %s", "；".join(f"「{ln.text}」小图 {ln.hero or '?'} {ln.score:.2f}" for ln in lines))
        for c in checks:
            if c not in self._bb_checks:
                continue
            c.add(lines, feed)
            bb, why = c.verdict
            if bb is not None:
                self._bb_checks.remove(c)
                self._feed_done(c, bb, why)
        self._feed_orphans(lines, checks, clock, me)

    def _feed_orphans(self, lines, checks, clock: float, me) -> None:
        """「买活了」那行认出是这局某个英雄，但他不在确认的里头：顶栏没看出他死过（2026-10-27 裂魂人死了马上买活，
        头像灰不到 2 帧），有 FEED_AGREE 帧都看到就按第一次看到的时刻记（买活是在这之前一点）"""
        claimed = {k for c in (*checks, *self._bb_checks) for k in (c.hero, c.found) if k}
        seen = {ln.hero for ln in lines if ln.hero and ln.hero not in claimed}
        self._bb_orphans = {k: v for k, v in self._bb_orphans.items() if clock - v[0] <= consts.ORPHAN_WINDOW}
        for side in consts.SIDES:
            for i, t in enumerate(self.slots[side]):
                if (h := self._hero(t)) is None or h.key not in seen or (side, i) == me:
                    continue
                at = self._bb_orphans.setdefault(h.key, [])
                at.append(clock)
                if len(at) >= buybackscan.FEED_AGREE and self.buyback.start(side, i, at[0]):
                    when = coach._mmss(at[0] / 60)
                    log.info("买活自动记：%s %s → 买活（顶栏没看出他死过，消息栏 %d 帧认到他买活）", when, h.name_cn, len(at))
                    self.message = f"{when} {h.name_cn}买活了（消息栏认到的），冷却 8:00 开始倒计时；认错了点他头像下面的小条取消"

    def _feed_done(self, c, bb: bool, why: str) -> None:
        rev = c.rev
        name, when = self._slot_name(rev.side, rev.index), coach._mmss(rev.clock / 60)
        if bb and c.hero is None and (h := next((h for h in self.db if h.key == c.found), None)):
            name += f"（格子没填，消息栏小图是{h.name_cn}）"
        log.info("买活自动记：%s %s → %s（%s；倒计时那边：%s）", when, name, "买活" if bb else "不是买活", why,
                 {True: "早复活了", False: "按时复活", None: "没认出倒计时"}[rev.buyback])
        try:
            buybackscan.save_sample(rev, ("buyback" if bb else "respawn") + f"-{rev.seconds}", feed=c.feed)
        except OSError:
            log.warning("买活样本没存下来", exc_info=True)
        if bb and self.buyback.start(rev.side, rev.index, rev.clock):
            self.message = f"{when} {name}买活了（消息栏认到的），冷却 8:00 开始倒计时；认错了点他头像下面的小条取消"

    def _game_clock(self) -> float | None:
        if self._bb_paused is not None:
            return self._bb_paused
        return time.time() - self._bb_anchor if self._bb_anchor is not None else None

    def _my_slot(self, st) -> tuple[str, int] | None:
        """我在顶栏哪一格：GSI 的 team_slot，没有就在格子里找我的英雄"""
        side = st.team
        if side not in consts.SIDES:
            return None
        if st.team_slot is not None and 0 <= st.team_slot < 5:
            return side, st.team_slot
        i = next((i for i, t in enumerate(self.slots[side]) if (h := self._hero(t)) and h.key == st.my_hero_key), None)
        return (side, i) if i is not None else None

    def mark_buyback(self, side: str, index: int) -> None:
        """顶栏头像下面的小条被点了：这个英雄刚买活，开始 8 分钟倒计时；正在倒计时就取消（点错了）"""
        if side not in consts.SIDES or not 0 <= index < 5:
            raise ValueError("格子不存在")
        clock = self._game_clock()
        if not self.cfg.buyback_timer:
            self.message = "买活计时关着，控制面板「顶栏计时」里打开"
            return
        if not self._bb_show or clock is None:
            self.message = "进游戏后才能记买活"
            return
        if (side, index) in self.buyback.auto:
            self.message = "自己的买活冷却按 GSI 自动算，不用点"
            return
        name = self.slots[side][index] or f"{'天辉' if side == 'radiant' else '夜魇'}第 {index + 1} 个"
        if self.buyback.mark(side, index, clock):
            self.message = f"{name}买活了，冷却 {buyback.COOLDOWN // 60}:00 开始倒计时（点错了再点一下取消）"
        else:
            self.message = f"已取消{name}的买活计时"
        log.info("买活计时 %s（游戏时间 %s）", self.message, coach._mmss(clock / 60))

    def set_timer(self, key: str, on: bool) -> None:
        """控制面板「顶栏计时」开关：买活 / 对面大招小条马上出 / 收，写进 config.toml。
        关掉时这局已经在走的倒计时不清，再打开接着显示"""
        setattr(self.cfg, key, on)
        path = config.save({key: on}, self.config_file)
        log.info("顶栏计时已保存到 %s：%s = %s", path, key, on)
        self.message = f"{consts.TIMER_SWITCHES[key]}已{'打开' if on else '关闭'}"

    def buyback_view(self) -> dict:
        """给 topbar.html：冷却结束的游戏时钟 + 时钟锚点，页面自己每秒算剩多少（快照不用每秒变）"""
        clock = self._game_clock()
        if (not self._bb_show or not (self.cfg.buyback_timer or self.cfg.ult_timer)
                or clock is None or self._bb_anchor is None):
            return {"show": False}
        st = self.gsi.state
        me = self._my_slot(st)
        return {"show": True, "anchor": round(self._bb_anchor * 1000), "paused": self._bb_paused,
                "ready_show": buyback.READY_SHOW, "ult_ready_show": ultcd.READY_SHOW, "my_side": st.team,
                "buyback_on": self.cfg.buyback_timer,
                "slots": {s: [{"name": self.slots[s][i], "end": self.buyback.end(s, i, clock), "me": me == (s, i),
                               "auto": (s, i) in self.buyback.auto, "ult": self._ult_view(s, i, clock),
                               "pick": self._pick_view(s, i)}
                              for i in range(5)] for s in consts.SIDES}}

    # ---------- 对面大招冷却 ----------

    def _knowledge(self):
        if self._kb_base is None:
            from ... import kb
            self._kb_base = kb.KnowledgeBase.load() or False
            self._kb_ops = cd_ops.load_ops(self._kb_base or None)
        return self._kb_base or None

    def _ult_of(self, side: str, index: int):
        """顶栏这一格是对面的、填了英雄、知识库里有要计时的大招：(英雄, 大招, 能勾的减冷却)，否则 None"""
        mine = self.side()
        if not self.cfg.ult_timer or mine not in consts.SIDES or side == mine:
            return None
        h = self._hero(self.slots[side][index])
        base = self._knowledge() if h else None
        hk = base.heroes.get(h.id) if base else None
        ab = cd_ops.pick(hk) if hk else None
        return (hk, ab, ultcd.mods(hk, ab, base, self._kb_ops)) if ab else None

    def _ult_cd(self, side: str, index: int, ab, mods) -> tuple[int, bool, list, float]:
        """(按几级大招算, 等级是不是菜单里选的, 勾了的减冷却, 算出来的冷却)"""
        key = (side, index)
        lv = self.ults.levels.get(key)
        fixed = lv is not None
        if not fixed:
            lv = ultcd.guess_level(self.gsi.state.level if self.gsi else None)
        on = [m for m in mods if m.key in self.ults.mods.get(key, ())]
        return lv, fixed, on, ultcd.cooldown(ab, lv, on)

    def _ult_check(self, side: str, index: int):
        if side not in consts.SIDES or not 0 <= index < 5:
            raise ValueError("格子不存在")
        clock = self._game_clock()
        if not self._bb_show or clock is None:
            self.message = "进游戏后才能记大招"
            return None
        u = self._ult_of(side, index)
        if not u:
            self.message = "这一格没有要计时的大招（不是对面 / 没填英雄 / 知识库里没有）"
            return None
        return clock, *u

    def mark_ult(self, side: str, index: int) -> None:
        """对面头像下面的大招小条被点了：他刚放了大招，开始倒计时；正在倒计时就取消（点错了）"""
        if not (u := self._ult_check(side, index)):
            return
        clock, hk, ab, mods = u
        lv, _, on, cd = self._ult_cd(side, index, ab, mods)
        extra = "、".join(m.label.split("：")[0] for m in on)
        if self.ults.mark(side, index, clock, cd):
            self.message = (f"{hk.name}放了{ab.name}，按 {lv} 级{'+' + extra if extra else ''}算冷却 {cd:g} 秒开始倒计时"
                            "（点错了再点一下取消，右键改等级 / 减冷却）")
        else:
            self.message = f"已取消{hk.name}{ab.name}的计时"
        log.info("大招计时 %s（游戏时间 %s）", self.message, coach._mmss(clock / 60))

    def set_ult(self, side: str, index: int, level=None, mod=None, on=None) -> None:
        """右键菜单：选大招等级 / 勾减冷却，这局一直按选的算；正在冷却的跟着重算（从放的时候算起）"""
        if not (u := self._ult_check(side, index)):
            return
        _, hk, ab, mods = u
        key = (side, index)
        if level is not None:
            if int(level) not in range(1, len(ultcd.ULT_LEVELS) + 1):
                raise ValueError("大招等级只有 1~3")
            self.ults.levels[key] = int(level)
        if mod is not None:
            if mod not in {m.key for m in mods}:
                raise ValueError(f"{ab.name}没有这项减冷却：{mod}")
            got = self.ults.mods.setdefault(key, set())
            got.add(mod) if on else got.discard(mod)
        lv, _, picked, cd = self._ult_cd(side, index, ab, mods)
        self.ults.recool(side, index, cd)
        extra = "、".join(m.label.split("：")[0] for m in picked)
        self.message = f"{hk.name}的{ab.name}按 {lv} 级{'+' + extra if extra else ''}算：冷却 {cd:g} 秒"
        log.info("大招计时 %s", self.message)

    def _ult_view(self, side: str, index: int, clock: float) -> dict | None:
        u = self._ult_of(side, index)
        if not u:
            return None
        hk, ab, mods = u
        lv, fixed, on, cd = self._ult_cd(side, index, ab, mods)
        return {"hero": hk.name, "name": ab.name, "key": ab.key, "end": self.ults.end(side, index, clock), "level": lv, "fixed": fixed,
                "cd": cd, "cds": "/".join(f"{c:g}" for c in cd_ops.cooldowns(ab)),
                "mods": [{"key": m.key, "label": m.label, "on": m in on} for m in mods]}
