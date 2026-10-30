"""局内：缺口提醒、分阶段弹本局要点、野区提醒、对线记录、死亡复盘和死亡截图。"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from ...domain import brief, coach, death, itembuild, jungle, lanelog
from ...core.gsi_fields import SELECTION_STATE, STRATEGY_STATE
from ...vision.recognize import write_image
from . import consts

log = logging.getLogger(__name__)


class InGameMixin:
    # ---------- 局内缺口提醒 ----------

    def coach_tick(self) -> None:
        self._take_death_shots()
        st = self.gsi.state if self.gsi else None
        selecting = bool(st and st.alive and st.game_state == SELECTION_STATE)
        if selecting and not self._in_selection:
            self._drafts_seen += 1
            self.grid.reset()                    # 新一局：上一局的禁用不算
            self._crops = {"blank": {}, "sel": {}, "strat": {}, "game": {}}
            self._named.clear()
            self.skipped.clear()                 # 上一局「不想玩」的不带到这一局
            # 上一局点 × 收起的叠加层、点开的「如果选它」卡片，新一局选人时自动恢复，不用再按热键
            self.user_hidden = False
            self.pick_key = None
            if self.position_auto:               # 上一局自动认的位置不带到这一局（这局可能是加速，没有位置字样）
                self.position, self.position_auto = self._base_position, False
            self.team_roles = []
            log.info("进入选人阶段（第 %d 次）：叠加层自动显示", self._drafts_seen)
        self._in_selection = selecting
        strategy = bool(st and st.alive and st.game_state == STRATEGY_STATE)
        if not (st and st.alive and (st.in_match or strategy)):
            self.coach_card = None               # 比赛结束 / 回到选人：收起卡片
            self.jungle_cue = None
            self._bb_show = False
            return
        # 天梯的 matchid 选人时就有；本地 / 机器人局一直是 0（2026-10-23 实测），连开两局会被当成同一局
        key = st.match_id if st.match_id not in ("", "0") else f"本地{self._drafts_seen}"
        if key != self.brief_match:              # 策略时间就做；错过了（中途开助手）进游戏也补一张
            self._start_brief(key)
        self._buyback_tick(st, key)
        if strategy:
            return
        if st.clock_minutes >= 0 and key != self._horn_match:
            self._horn_match = key               # 号角响了：本局要点收起一次，之后按热键再看就不收了
            if self._showing_brief():
                self.coach_card = None
                self.message = f"本局要点已收起，到中期 / 后期会再弹；想看按 {self.cfg.hotkey_brief}"
        if key != self.coach_match:
            self._start_coach(key)
            return
        self._jungle_tick(st)
        self._lane_log(st, key)
        minute = st.clock_minutes
        died = st.deaths > self._coach_deaths
        self._coach_deaths = st.deaths
        respawned = (st.raw.get("hero") or {}).get("alive") is True
        if self._death_shown and respawned and time.monotonic() - self._death_shown >= consts.DEATH_MIN_SHOW:
            self._death_shown = 0.0              # 复活了：死亡卡片收起（别的卡片不动）
            if self.coach_card and self.coach_card.get("death"):
                self.coach_card = None
        recap = self._death_recap(st) if died else None
        passed = [c for c in coach.CHECKPOINTS if self._coach_checkpoint < c <= minute] if self.coach else []
        if passed:
            self._coach_checkpoint = passed[-1]
        needs, nxt, by_death = [], None, []
        if self.coach and (died or passed):     # coach 是 None：还在准备（后台线程）或者准备失败
            cst = coach.State(minute, st.items, st.last_hits, st.net_worth, st.gold, st.inventory, st.buyback)
            needs = self.coach.tick(cst, checkpoint=bool(passed))
            by_death = self.coach.on_death(recap, cst) if recap else []   # 先按死法调顺序，再说下一件
            nxt = self.coach.next_parts(cst)
        lines = [("row", f"提示：{n.text}（{n.tip}）") if n.tip else ("warn" if n.priority == 0 else "row", n.text)
                 for n in needs]
        # 死了正好回家买东西：每次死亡都说下一件买什么；时间点的提醒只在本来就要弹的时候顺带一句
        shop = ([("head", "回家买")] if died else []) + by_death + ([("row" if died else "dim", nxt)] if nxt else [])             if (nxt or by_death) and (died or lines) else []
        if recap and recap.lines:
            self._show_coach(recap.title, recap.lines + ([("head", "还缺")] + lines if lines else []) + shop,
                             on_death=True)
        elif lines or shop:
            self._show_coach(f"局内提醒 · {coach._mmss(minute)} {'死亡' if died else '时间点'}", lines + shop,
                             on_death=died)
        self._stage_tick(minute)

    def _stage_tick(self, minute: float) -> None:
        """到了中期 / 后期弹一次本局要点的那一页；正弹着别的卡片（死亡复盘、缺口提醒）就等它关了再弹"""
        stage, order = brief.stage_at(minute), [p for p, _ in brief.STAGES]
        if not self.brief or order.index(stage) <= order.index(self._brief_stage):
            return
        if self.coach_card and not self._showing_brief():
            return
        self._brief_stage = stage
        if not self.brief.has(stage):            # 例如没开 AI 时后期那页是空的
            return
        self.brief_page = stage
        self.coach_card = self._brief_view()
        self.user_hidden = False
        log.info("本局要点：%s，弹%s那页", coach._mmss(minute), dict(brief.STAGES)[stage])

    def _jungle_tick(self, st) -> None:
        """到点前几秒亮一条野区提醒；死了不提醒（做不了，也别挡死亡复盘）"""
        alive = (st.raw.get("hero") or {}).get("alive") is not False
        clock = round(st.clock_minutes * 60)     # 分钟换回秒有浮点误差（4:07 会算成 4:06.999）
        cue = self.jungle.cue(clock, self._cue_muted) if self.jungle and alive else None
        if cue and cue != self.jungle_cue:
            log.info("野区提醒「%s」：%s", cue.title, " / ".join(t for _, t in cue.lines))
            self.user_hidden = False             # 和局内提醒一样，新的一条总要让人看见
        self.jungle_cue = cue

    def _lane_log(self, st, key: str) -> None:
        """3:00 / 6:00 / 10:00 把自己的 GSI 数据原样记一笔，赛后 lane-review 对照 STRATZ 的对线结果。
        过了 20 秒才开助手的那个时刻不补记（数据已经不是那一刻的）"""
        clock = round(st.clock_minutes * 60)
        for m in lanelog.LOG_MINUTES:
            if m in self._lane_logged or not m * 60 <= clock <= m * 60 + 20:
                continue
            self._lane_logged.add(m)
            hero = st.raw.get("hero") or {}
            ratio = lambda a, b: hero[a] / hero[b] if hero.get(a) is not None and hero.get(b) else None
            draft = self.current_draft()
            rec = {"at": datetime.now().isoformat(timespec="seconds"), "match": key, "minute": m, "clock": clock,
                   "hero_key": st.my_hero_key, "hero_id": hero.get("id") or (draft.my_hero.id if draft.my_hero else None),
                   "hero_name": draft.my_hero.name_cn if draft.my_hero else None, "pos": self.position, "team": st.team,
                   "player": {k: v for k, v in (st.raw.get("player") or {}).items() if not isinstance(v, (dict, list))},
                   "hero": {k: v for k, v in hero.items() if not isinstance(v, (dict, list))},
                   "hp": ratio("health", "max_health"), "mana": ratio("mana", "max_mana"),
                   "items": sorted(st.items), "cue": self.jungle_cue.title if self.jungle_cue else None}
            try:
                lanelog.log_moment(rec)
                log.info("对线记录：%d:00 已记下（赛后 python -m dota_assistant lane-review 对照）", m)
            except OSError:
                log.warning("对线记录没写进去", exc_info=True)

    def mute_cue(self) -> None:
        """点 × / 按 F8 关掉正显示的野区提醒：这局不再提醒这一种"""
        kind = self.jungle_cue.kind
        self._cue_muted.add(kind)
        self.jungle_cue = None
        self.message = f"这局不再提醒{jungle.LABELS[kind]}（config.toml 里 jungle_cues = false 能全部关掉）"
        log.info(self.message)

    def _death_recap(self, st) -> death.Recap | None:
        """刚死：把死前那段推送整理成「怎么死的」，顺手原样存一份（核对字段 / 调阈值用）"""
        if self._death_names is None or self._death_names[0] != st.my_hero_key:
            from ... import kb
            base = kb.KnowledgeBase.load()
            self._death_names = (st.my_hero_key, *(death.names_from_kb(base, st.my_hero_key) if base else ({}, set())))
        history = st.history()
        rec = death.analyze(history, st.team, *self._death_names[1:])
        try:
            path = death.dump(history, rec)
        except OSError:
            log.warning("死亡复盘：推送没存下来", exc_info=True)
        else:
            now = time.monotonic()
            self._death_shots = [(now + d, path.with_name(f"{path.stem}-{d}s.png")) for d in consts.DEATH_SHOT_DELAYS]
            self._take_death_shots()
        if rec is None:
            log.info("死亡复盘：历史里没有死前的推送（刚开助手？）")
        return rec

    def _take_death_shots(self) -> None:
        """到点的死亡截图截下来（只截 Dota 窗口，同 F10）"""
        now = time.monotonic()
        due = [p for t, p in self._death_shots if t <= now]
        if not due:
            return
        self._death_shots = [(t, p) for t, p in self._death_shots if t > now]
        try:
            img = self.grab()
            for p in due:
                write_image(p, img)
        except Exception:                        # 截不到不影响别的
            log.warning("死亡截图没截下来", exc_info=True)

    def _start_coach(self, key: str) -> None:
        """敌方阵容用选人时格子里的（识别 / 手动填的），自己的英雄用 GSI 的"""
        draft = self.current_draft()
        if not draft.my_hero:
            return                               # GSI 还没给英雄，下一轮再试
        self.coach_match, self.coach = key, None
        self.jungle, self.jungle_cue, self._cue_muted, self._lane_logged = None, None, set(), set()
        self._coach_deaths = self.gsi.state.deaths  # 中途开助手：之前的死亡不算
        my_id, enemy_ids, pos = draft.my_hero.id, [h.id for h in draft.enemies], self.position
        log.info("比赛 %s：准备局内提醒（%s · %d 号位 · 对面 %s）", key, draft.my_hero.name_cn, pos, consts._names(draft.enemies))

        def work():
            t0 = time.monotonic()
            try:
                res, err = self._coach_prepare(self.cfg, my_id, enemy_ids, pos), None
                log.info("局内提醒准备好了（%.1f 秒）", time.monotonic() - t0)
            except Exception as e:
                log.exception("局内提醒准备失败")
                res, err = None, e
            self.submit(self._coach_ready, key, res, err)
        threading.Thread(target=work, daemon=True, name="coach").start()

    def _coach_ready(self, key: str, res, err) -> None:
        if key != self.coach_match:
            return
        if err:
            self.message = f"局内提醒没准备好：{err}"
            return
        self.coach, notes = res
        if self.cfg.jungle_cues:
            self.jungle = jungle.make(self.position, getattr(self.coach, "bench", None),
                                      self.cfg.stack_second, self.cfg.pull_second)
            log.info("野区提醒：%d 号位 · %s", self.position,
                     "、".join(jungle.LABELS[k] for k in sorted(self.jungle.kinds)) or "不提醒")
        st = self.gsi.state
        self._coach_deaths = st.deaths
        self._coach_checkpoint = max((c for c in coach.CHECKPOINTS if c < st.clock_minutes), default=0)
        self.message = "局内提醒已就绪"
        if notes and not self.brief:             # 本局要点里已经有这些了
            self._show_coach("开局提示", [("row", n) for n in notes])

    def _show_coach(self, title: str, lines: list[tuple], on_death: bool = False) -> None:
        """lines：(kind, 文字或片段列表)，片段里的装备在界面上显示图标（itembuild.line_view）"""
        self.coach_card = {"kind": "notice", "title": title, "closable": True, "death": on_death,
                           "lines": [{"kind": k, **itembuild.line_view(t)} for k, t in lines]}
        self._death_shown = time.monotonic() if on_death else 0.0
        log.info("弹卡片「%s」：%s", title, " / ".join(itembuild.text(t) for _, t in lines))
        self.user_hidden = False                 # 新提醒总要让人看见；不想看就关掉这张卡片
        if self.coach_voice:
            self.speaker.say("；".join(itembuild.text(t) for _, t in lines))
