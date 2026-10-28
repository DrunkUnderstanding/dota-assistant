"""选人：格子 / 局面 / 推荐、定时识别（顶栏、网格禁用、自动认位置）、帮认和记模板。"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime

from ...domain import advisor as adv
from ...vision import grid
from ...core.gsi_fields import SELECTION_STATE, STRATEGY_STATE
from ...core.heroes import Hero
from ...domain.engine import POSITION_NAMES
from ...vision.recognize import EMPTY, DraftView, confident, feature, recognize
from . import consts

log = logging.getLogger(__name__)


class DraftMixin:
    # ---------- 局面 ----------

    def current_draft(self) -> adv.Draft:
        team = {s: [h for t in self.slots[s] if (h := self._hero(t))] for s in consts.SIDES}
        side = self.side() or consts.SIDE_FALLBACK
        other = "dire" if side == "radiant" else "radiant"
        bans = [h for t in self.bans_text.replace("，", ",").split(",") if (h := self._hero(t))]
        if self.last_view and self.mode == "auto":
            bans += [self.db.by_id[i] for i in self.last_view.ids("bans") if i in self.db.by_id]
        if self.mode == "auto":
            bans += [self.db.by_id[i] for i in sorted(self.grid.bans) if i in self.db.by_id]
        bans = list({h.id: h for h in bans}.values())
        my = None
        if self.gsi and self.gsi.state.alive and self.gsi.state.my_hero_key:
            my = next((h for h in self.db if h.key == self.gsi.state.my_hero_key), None)
        allies = [h for h in team[side] if not my or h.id != my.id]
        # 队友排的位置（OCR）：格子顺序和 team_roles 一一对应
        ally_pos = {h.id: r for t, r in zip(self.slots[side], self.team_roles)
                    if r and (h := self._hero(t)) and h in allies}
        return adv.Draft(allies, team[other], bans, my, ally_pos)

    def refresh(self, force_speech: bool = False) -> None:
        draft = self.current_draft()
        advice = self.advisor.advise(draft, self.position)
        self.view = adv.render_view(advice, self.side())
        self.view["position_auto"] = self.position_auto
        if self.side() is None:                  # 友 / 敌是按默认的天辉分的，可能整个反了
            self.view["side_assumed"] = consts.SIDE_FALLBACK
        self.notice = None
        changed = self.prev_draft is None or draft.key() != self.prev_draft.key()
        if changed:
            log.info("局面：我 %s · 队友 %s · 对面 %s · 禁用 %s · %d 号位 → 推荐 %s",
                     draft.my_hero.name_cn if draft.my_hero else "未选", consts._names(draft.allies), consts._names(draft.enemies),
                     consts._names(draft.bans), self.position,
                     "、".join(f"{s.hero.name_cn}({s.winrate:.1f}%)" for s in advice.picks + advice.targeted) or "无")
        if force_speech or changed:
            text = adv.speech(self.prev_draft, advice, force=force_speech)
            if text:
                log.info("播报：%s", text)
                self.speaker.say(text)
        self.prev_draft = draft

    def draft_view(self) -> dict:
        """选人卡片；进了游戏（GSI：出门到比赛结束）就不再显示剩余位置"""
        st = self.gsi.state if self.gsi else None
        view = {**self.view, "slots": self.slots_view()} if self.view.get("kind") == "draft" else self.view
        if view.get("team") and st and st.alive and st.in_match:
            return {**view, "team": None}
        return view

    def slots_view(self) -> dict:
        """叠加层「顶栏对照」：双方 5 格按 Dota 顶栏的顺序，玩家一眼看出哪格没认出、点了帮认。
        state：ok 认出了（点了能改）/ ask 这格有人但没认出（金色「?」）/ empty 看着还没选（也能点）/ me 自己。
        ask 怎么定：策略时间起（GSI）人人都有英雄了，空着的都是没认出；选人时看这格比这局开头多出了彩色头像（_looks_picked）。
        没连 GSI 时不知道在不在选人，只给 empty"""
        st = self.gsi.state if self.gsi and self.gsi.state.alive else None
        game_state = st.game_state if st else None
        over = game_state is not None and game_state != SELECTION_STATE
        me = self._my_slot(st) if st else None
        my_hero = next((h for h in self.db if h.key == st.my_hero_key), None) if st and st.my_hero_key else None
        out: dict = {"ally": self.side() or consts.SIDE_FALLBACK}
        for side in consts.SIDES:
            cells = []
            for i in range(5):
                h = self._hero(self.slots[side][i])
                if me == (side, i):
                    h = h or my_hero
                    cells.append({"state": "me", "hero": adv.hero_ref(h) if h else None})
                    continue
                if h:
                    state = "ok"
                elif over or (game_state == SELECTION_STATE and self._looks_picked(side, i)):
                    state = "ask"
                else:
                    state = "empty"
                hint = self.hints[side][i]
                cells.append({"state": state, "hero": adv.hero_ref(h) if h else None,
                              "guess": hint.get("guess", "") if state == "ask" else "",
                              "manual": (side, i) in self._named, "pick": self._pick_view(side, i)})
            out[side] = cells
        out["asks"] = sum(c["state"] == "ask" for s in consts.SIDES for c in out[s])
        return out

    def _looks_picked(self, side: str, index: int) -> bool:
        """选人阶段这格看着是不是有人选定了：和这局开头那张不一样、而且多出了彩色（队友预选是半透明灰图，不算）"""
        if self._picked_cache[0] != (id(self._crops), self._crops_ver):
            self._picked_cache = ((id(self._crops), self._crops_ver), {})
        cache = self._picked_cache[1]
        if (side, index) not in cache:
            cur, blank = self._crops["sel"].get((side, index)), self._crops["blank"].get((side, index))
            cache[(side, index)] = (cur is not None and blank is not None and cur is not blank
                                    and float(feature(cur) @ feature(blank)) <= consts.SAME_CROP
                                    and consts._colorful(cur) >= consts._colorful(blank) + consts.PICKED_COLOR_GAIN)
        return cache[(side, index)]

    def clear(self) -> None:
        self.slots = {s: [""] * 5 for s in consts.SIDES}
        self._named.clear()
        self.hints = {s: [{} for _ in range(5)] for s in consts.SIDES}
        self.bans_text = ""
        self.grid.reset()
        self.prev_draft = None
        self.refresh()

    def show_notice(self, title: str, lines: list[tuple[str, str]]) -> None:
        """临时盖住选人推荐显示一段提醒（演示 / 局内提醒用）；下一次 refresh 会换回推荐"""
        self.notice = {"kind": "notice", "title": title, "lines": [{"kind": k, "text": t} for k, t in lines]}

    # ---------- 识别 ----------

    def should_scan(self) -> bool:
        if self.mode != "auto" or not (self.cal and self.cal.ready):
            return False
        if self.gsi and self.gsi.state.alive:
            return self.gsi.state.in_draft
        return self.scan_without_gsi

    def should_scan_bans(self) -> bool:
        """禁用只在选人阶段出现；不需要顶栏校准"""
        if self.mode != "auto":
            return False
        if self.gsi and self.gsi.state.alive:
            return self.gsi.state.game_state == SELECTION_STATE
        return self.scan_without_gsi

    def tick(self) -> None:
        self.coach_tick()                        # 先更新阶段（进新一局选人要先清掉上一局的禁用）
        if "side" in self.view and self.view["side"] != self.side():
            self.refresh()                       # GSI 连上 / 断开、阵营变了：友 / 敌要重新分
        scan, bans = self.should_scan(), self.should_scan_bans()
        if scan or bans:
            img = self.grab()                    # 顶栏识别和禁用识别共用一帧
            if bans and img is not None:
                self.scan_bans(img)              # 先看网格：顶栏认不准的格子拿「网格里已被选走」核对
                self.scan_role(img)
            if scan:
                self.scan(img=img)

    def scan_bans(self, img) -> None:
        boxes = grid.find_cells(img)
        # 只在 GSI 确认是选人阶段时建新布局，免得把桌面上别的图片墙当成选人网格
        # 格子位置连续两帧一样才建（刚进选人时有过场动画，2026-10-23 实测第一帧建出了 134 格的错图）
        selecting = bool(self.gsi and self.gsi.state.alive and self.gsi.state.game_state == SELECTION_STATE)
        sig = tuple(boxes)
        steady, self._grid_last_boxes = sig == self._grid_last_boxes, sig
        if (selecting and steady and not self._grid_building and time.monotonic() >= self._grid_retry_at
                and self.grid.needs_map(img, boxes)):
            self._grid_building = True
            log.info("选人网格：新布局（%d 格），后台建图", len(boxes))

            def work():
                t0 = time.monotonic()
                try:
                    gmap = self.grid.build(img, boxes)
                    log.info("选人网格建好了（%.1f 秒）：%d 格认出 %d 个，%s", time.monotonic() - t0, len(gmap.cells),
                             sum(h is not None for h in gmap.heroes), "干净画面" if gmap.clean else "画面里已有禁用，先凑合用")
                except Exception:
                    log.exception("选人网格建图失败")
                    gmap = None
                self.submit(self._grid_ready, gmap)
            threading.Thread(target=work, daemon=True, name="grid").start()
        if self.grid.update(img, boxes):
            log.info("网格识别：禁用 %s；已选 %s", consts._names(self._ids(self.grid.bans)), consts._names(self._ids(self.grid.taken)))
            self.refresh()

    def scan_role(self, img) -> None:
        """选人阶段每 3 秒试一次认自己的位置，认出来这局就不再认"""
        st = self.gsi.state if self.gsi else None
        if (self._role_busy or self._role_draft == self._drafts_seen or time.monotonic() < self._role_next
                or not (self.cal and self.cal.ready and st and st.alive and st.team)):
            return
        self._role_busy, self._role_next = True, time.monotonic() + consts.ROLE_RETRY
        draft, side, name, slot = self._drafts_seen, st.team, st.player_name, st.team_slot

        def work():
            try:
                res = self._read_roles(img, self.cal, side, name, slot)
            except Exception as e:               # 没有 OCR 之类：这局不再试
                log.warning("认位置失败：%s", e)
                res = None
            self.submit(self._role_ready, draft, res)
        threading.Thread(target=work, daemon=True, name="roles").start()

    def _role_ready(self, draft: int, res) -> None:
        self._role_busy = False
        if res is None:
            self._role_draft = draft
            return
        team, me = res
        if draft != self._drafts_seen or self._role_draft == draft:
            return                               # 已经换局 / 玩家这期间手动改了
        if me is None or team[me] is None:
            return                               # 没有位置这行字（加速、普通匹配）或者被挡住了：过几秒再试
        self._role_draft = draft
        self.team_roles = list(team)
        log.info("认出位置：我在第 %d 格，排的是 %d 号位（我方：%s）", me + 1, team[me],
                 " ".join(str(r) if r else "?" for r in team))
        if team[me] != self.position or not self.position_auto:
            self.position, self.position_auto = team[me], True
            self.message = f"自动认出你这局打{POSITION_NAMES[team[me]]}"
            self.refresh(force_speech=True)

    def _grid_ready(self, gmap: grid.GridMap | None) -> None:
        self._grid_building = False
        if gmap is None:
            self._grid_retry_at = time.monotonic() + 60
            return
        self.grid.add_map(gmap)

    def _ids(self, ids) -> list[Hero]:
        return [self.db.by_id[i] for i in sorted(ids) if i in self.db.by_id]

    def grab(self):
        if self.screen is None:
            self.screen = self._screen_factory()
        return self.screen.grab()

    def scan(self, force: bool = False, img=None) -> None:
        if not (self.cal and self.cal.ready):
            self.message = "还没校准：先运行 python -m dota_assistant calibrate <截图>"
            return
        # 校准的是选人界面的顶栏；进了游戏顶栏头像小一圈、位置也不一样，按选人的格子裁出来全是乱的。
        # 2026-10-28 实战：局内连按 F9，每次认出一套不同的错英雄，把选人时认对的对面三个全冲掉了，大招 / 买活小条跟着错
        st = self.gsi.state if self.gsi else None
        game_state = st.game_state if st and st.alive else None
        if game_state not in (None, SELECTION_STATE, STRATEGY_STATE):
            if force:
                self.message = "进了游戏不能重识别（顶栏和选人界面不一样，会认乱）；没认出 / 认错的英雄在控制面板对应格子里手动改"
                log.info("局内按了重识别：不认，%s", self.message)
            return
        # 策略时间画面换了，顶栏有的格子会认乱（同一局：选人时认对的潮汐猎人、主宰被换成修补匠、百戏大王）：只补空着的格子
        only_empty = game_state == STRATEGY_STATE
        img = self.grab() if img is None else img
        view = recognize(img, self.cal, self.bank, self.cfg.match_threshold, keep_images=True)
        self._confirm_with_grid(view)
        self.last_scan = time.time()
        key = view.key()
        if log.isEnabledFor(logging.DEBUG):                  # 每秒一次，只在 DEBUG 时记（调 match_threshold 用）
            log.debug("识别：天辉 %s | 夜魇 %s", *consts._view_text(self.db, view, consts.SIDES))
        stable = force or key == self.pending_key            # 连续两次一致才采用，躲开动画过程
        self.pending_key = key
        if not stable:
            return
        self._keep_crops(view, "strat" if only_empty else "sel")
        # 玩家告诉过的格子：头像后来变了样（刚选时盖着的数字徽章没了、策略时间换了样子、帮认时那格还没出头像）也接着记，
        # 已经认得出的 learn_slot 自己跳过，不会每秒重复存
        for side, i in sorted(self._named):
            if h := self._hero(self.slots[side][i]):
                self.learn_slot(side, i, h)
        changed = self.last_view is None or self.last_view.key() != key
        self.last_view = view
        if changed or force:
            log.info("识别结果%s：天辉 %s | 夜魇 %s | 禁用 %s", "变了" if changed else "（手动重识别）",
                     *consts._view_text(self.db, view))
            self.fill_slots(view, only_empty=only_empty)
            self.refresh(force_speech=force)

    def _keep_crops(self, view: DraftView, phase: str) -> None:
        for side in consts.SIDES:
            for i, s in enumerate(getattr(view, side)):
                if s.image is not None and s.image.size:
                    if phase == "sel":
                        self._crops["blank"].setdefault((side, i), s.image)
                    self._crops[phase][(side, i)] = s.image
        self._crops_ver += 1

    def learn_slot(self, side: str, index: int, h: Hero) -> int:
        """玩家告诉我们顶栏这格是 h：把这局在这格裁到的头像记成 h 的模板，返回记了几张。
        不记的：和这局一开始（还没人选）一样的（那格根本没出头像，例如 2026-10-28 断线的那个人，记了会把空位认成他）、
        两张一样的、本来就认得出的"""
        blank = self._crops["blank"].get((side, index))
        fb = feature(blank) if blank is not None else None
        seen, n = [], 0
        for phase in ("sel", "strat"):
            c = self._crops[phase].get((side, index))
            if c is None:
                continue
            f = feature(c)
            if (fb is not None and float(f @ fb) > consts.SAME_CROP) or any(float(f @ g) > consts.SAME_CROP for g in seen):
                continue
            seen.append(f)
            hid, score, gap = self.bank.best(c)
            if hid == h.id and confident(score, gap, self.cfg.match_threshold):
                continue
            self.bank.learn(h.id, c)
            n += 1
        if n:
            log.info("记住%s第 %d 格是%s：存了 %d 张模板（共学习 %d 张）",
                     consts._side_cn(side), index + 1, h.name_cn, n, self.bank.learned)
        return n

    def name_slot(self, side: str, index: int, hero_id: int) -> None:
        """顶栏「这格是谁」点了一个英雄：填进格子，顺手记成模板"""
        if side not in consts.SIDES or not 0 <= index < 5:
            raise ValueError("格子不存在")
        h = self.db.by_id.get(int(hero_id))
        if h is None:
            raise ValueError(f"没有这个英雄 {hero_id}")
        other = next(((sd, j) for sd in consts.SIDES for j, t in enumerate(self.slots[sd])
                      if (sd, j) != (side, index) and self._hero(t) == h), None)
        if other:
            self.message = f"{h.name_cn}已经在{consts._side_cn(other[0])}第 {other[1] + 1} 格了，先把那格改掉"
            return
        before = self.slots[side][index] or (f"?{self.hints[side][index].get('guess')}" if self.hints[side][index].get("guess") else "")
        self.slots[side][index] = h.name_cn
        self.hints[side][index] = {}
        self._named.add((side, index))
        n = self.learn_slot(side, index, h)
        self._record_fix(side, index, before, h, n, "帮认")
        self.message = f"{consts._side_cn(side)}第 {index + 1} 格填上{h.name_cn}" + (f"，这格头像记住了，下次认得" if n else "")
        log.info("帮认：%s", self.message)
        self.refresh()

    def _record_fix(self, side: str, index: int, before: str, h: Hero, learned: int, how: str) -> None:
        """记一次玩家纠正（data/fixes.jsonl）：模板本身已经存进 templates/ 了，这里是给人看的流水"""
        st = self.gsi.state if self.gsi and self.gsi.state.alive else None
        row = {"time": datetime.now().isoformat(timespec="seconds"), "match": st.match_id if st else "",
               "phase": (st.game_state or "").removeprefix("DOTA_GAMERULES_STATE_") if st else "",
               "side": side, "slot": index + 1, "before": before, "hero": h.name_cn, "hero_id": h.id,
               "learned": learned, "how": how}
        try:
            self.fix_file.parent.mkdir(parents=True, exist_ok=True)
            with self.fix_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("纠正记录写不了 %s：%s", self.fix_file, e)

    def pick_candidates(self, side: str, index: int) -> dict:
        """顶栏「这格是谁」的候选：除了已经在别的格子里的，所有英雄排个序——先是选人网格里被选走、还没分到格子、没被禁的（taken），
        再是别的，各自按和这格头像像不像排。页面先选属性、再把这个属性的全列出来（玩家 2026-10-28 要的），按这个顺序。
        只给 id（英雄名 / 图标 / 属性页面从 /api/heroes 拿一次），快照里 10 格都带也不大"""
        used = {h.id for sd in consts.SIDES for t in self.slots[sd] if (h := self._hero(t))}
        pool = set(self.grid.taken) - used - set(self.grid.bans)
        sims: dict[int, float] = {}
        # 选人界面裁的优先；这局没裁到过（中途开的助手）就用局内顶栏头像——只拿来排序（对的那个分数 0.3~0.6，常排前几，见第 37 节）
        for phases in (("sel", "strat"), ("game",)):
            for phase in phases:
                if (c := self._crops[phase].get((side, index))) is not None:
                    for hid, v in self.bank.scores(c).items():
                        sims[hid] = max(v, sims.get(hid, -2.0))
            if sims:
                break
        if not sims:                                         # 什么都没裁到：按名字
            sims = {i: 0.0 for i in self.db.by_id}
        ids = sorted((i for i in sims if i not in used), key=lambda i: (i not in pool, -sims[i], self.db.by_id[i].name_cn
                                                                       if i in self.db.by_id else ""))
        ids = [i for i in ids if i in self.db.by_id]
        return {"order": ids, "taken": [i for i in ids if i in pool]}

    def _confirm_with_grid(self, view: DraftView) -> None:
        """顶栏分数没过阈值、但最像的英雄在选人网格里确实已被选走（变暗没斜线）的，也算认出来。
        2026-10-23 实测：拉比克在顶栏只有 0.63（阈值 0.70），网格里已经变暗。"""
        for s in view.radiant + view.dire:
            if s.hero_id is None and s.guess in self.grid.taken and s.score >= consts.GRID_CONFIRM_MIN:
                s.hero_id = s.guess

    def fill_slots(self, view: DraftView, only_empty: bool = False) -> None:
        """正在手动改的格子由网页自己保护（输入框有焦点时不覆盖）；玩家这局告诉过我们的格子（_named）不动。
        only_empty：只往空格子里填认出来的英雄（已经有的不换、不清），填进去的英雄不能和别的格子重复"""
        for side in consts.SIDES:
            for i, s in enumerate(getattr(view, side)):
                if (side, i) in self._named and self.slots[side][i]:
                    continue
                if only_empty:
                    h = self.db.by_id.get(s.hero_id)
                    if (self.slots[side][i] or not h
                            or any(self._hero(t) == h for sd in consts.SIDES for t in self.slots[sd])):
                        continue
                self.slots[side][i] = self.db.by_id[s.hero_id].name_cn if s.hero_id in self.db.by_id else ""
                guess = self.db.by_id.get(s.guess)
                self.hints[side][i] = {"score": round(s.score, 2),
                                       "guess": guess.name_cn if guess and not s.hero_id else ""}

    def learn(self) -> None:
        if not self.last_view:
            self.message = "还没有识别结果可以记"
            return
        n = 0
        for side in consts.SIDES:
            for text, s in zip(self.slots[side], getattr(self.last_view, side)):
                if s.image is None:
                    continue
                h = self._hero(text)
                if text.strip() and not h:
                    continue                                   # 名字写错的格子跳过
                self.bank.learn(h.id if h else EMPTY, s.image)
                n += 1
        self.message = f"已记住 {n} 个格子，模板共 {self.bank.portraits} 张官方 + {self.bank.learned} 张学习"
        log.info(self.message)

    def _pick_view(self, side: str, index: int) -> dict:
        """顶栏帮认的候选；快照每秒都做，格子 / 裁图 / 网格 / 模板没变就用上次算的"""
        key = (tuple(self.slots["radiant"] + self.slots["dire"]), id(self._crops), self._crops_ver,
               len(self.grid.taken), len(self.grid.bans), self.bank.learned)
        if self._cand_cache[0] != key:
            self._cand_cache = (key, {})
        cache = self._cand_cache[1]
        if (side, index) not in cache:
            cache[(side, index)] = self.pick_candidates(side, index)
        return cache[(side, index)]
