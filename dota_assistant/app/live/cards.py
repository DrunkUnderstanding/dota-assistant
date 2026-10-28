"""本局要点 / 如果选它：做要点卡片（数据页 + AI 打法页）、翻页。"""
from __future__ import annotations

import logging
import threading
import time

from ...ai import llm
from ...domain import brief
from ...domain.engine import POSITION_NAMES
from . import consts

log = logging.getLogger(__name__)


class CardsMixin:
    def _start_brief(self, key: str) -> None:
        draft = self.current_draft()
        if not draft.my_hero:
            return                               # GSI 还没给英雄，下一轮再试
        self.brief_match, self.brief, self._brief_stage = key, None, "lane"
        self.pick_key = None                     # 真的选完了，「如果选它」的卡片就不用了
        ids = (draft.my_hero.id, [h.id for h in draft.allies], [h.id for h in draft.enemies])
        lane_info = {"side": self.side(), "ally_pos": draft.ally_pos}
        log.info("比赛 %s：做本局要点（%s · 队友 %s · 对面 %s）", key, draft.my_hero.name_cn, consts._names(draft.allies),
                 consts._names(draft.enemies))

        def work():
            t0 = time.monotonic()
            try:
                res, err = self._brief_build(self.cfg, *ids, self.position, **lane_info), None
                log.info("本局要点做好了（%.1f 秒）", time.monotonic() - t0)
            except Exception as e:
                log.exception("本局要点没做出来")
                res, err = None, e
            self.submit(self._brief_ready, key, res, err, ids)
        threading.Thread(target=work, daemon=True, name="brief").start()

    def _brief_ready(self, key: str, res, err, ids: tuple = ()) -> None:
        if key != self.brief_match:
            return
        if err:
            self.message = f"本局要点没做出来：{err}"
            return
        self.brief, self.brief_page = res, "lane"
        if self.cfg.ai_plan:
            self._start_plan(key, res, ids)
        else:
            res.plan_note = "AI 打法没开（控制面板右上角「设置」里打开）"
        st = self.gsi.state if self.gsi else None
        if st and st.alive and st.in_match and st.clock_minutes >= 0:
            self._brief_stage = brief.stage_at(st.clock_minutes)   # 中途开的助手：当前阶段不弹，后面的阶段照弹
            self.message = f"本局要点已就绪（已经开打，按 {self.cfg.hotkey_brief} 查看）"
            return
        self.show_brief()

    def _start_plan(self, key: str, b: brief.Brief, ids: tuple) -> None:
        b.plan_note = "AI 正在写本局打法…（一般 30~60 秒，做好会补进各阶段的页里）"

        def work():
            t0 = time.monotonic()
            try:
                res, err = self._plan_build(self.cfg, *ids, b.lane_ids, b.text()), None
                log.info("AI 打法做好了（%.1f 秒）", time.monotonic() - t0)
            except llm.Unavailable as e:
                log.info("AI 打法跳过：%s", e)
                res, err = None, e
            except Exception as e:
                log.exception("AI 打法没做出来")
                res, err = None, e
            self.submit(self._plan_ready, key, res, err)
        threading.Thread(target=work, daemon=True, name="plan").start()

    def _plan_ready(self, key: str, plan, err) -> None:
        if key != self.brief_match or not self.brief:
            return
        if err:
            self.brief.plan_note = consts.plan_error(err)
        else:
            self.brief.plan = plan
            self.brief.plan_note = "" if plan.sections else "AI 给的建议出处都对不上资料，全部丢掉了"
        if self._showing_brief():                # 正在看要点：这一页补上 AI 写的
            self.coach_card = self._brief_view()
        else:
            self.message = f"本局打法已就绪，按 {self.cfg.hotkey_brief} 查看"

    def _showing_brief(self) -> bool:
        return bool(self.coach_card and self.coach_card.get("kind") == "brief")

    def _brief_view(self) -> dict:
        return self.brief.view(self.brief_page, f"{self.cfg.hotkey_brief} 翻页")

    def show_brief(self) -> None:
        """热键 / 面板按钮：没在看就打开当前阶段那页，正在看就翻到下一阶段（后期再按回到对线期）"""
        if not self._showing_brief() and self.pick_brief and self.pick_key:
            self.pick_page = consts._next_stage(self.pick_page)
            self.user_hidden = False
            return
        if not self.brief:
            self.message = "这局还没有本局要点（要等选完人、GSI 告诉我你的英雄）"
            return
        if self._showing_brief():
            self.brief_page = consts._next_stage(self.brief_page)
        else:
            st = self.gsi.state if self.gsi else None
            self.brief_page = brief.stage_at(st.clock_minutes) if st and st.alive and st.in_match else "lane"
        self.coach_card = self._brief_view()
        self.user_hidden = False

    # ---------- 选人时「如果选它」 ----------

    def explain_pick(self, hero_id: int) -> None:
        """点了某个推荐英雄：按当前局面做「如果选它」的要点（联网，后台），做好再让 AI 写打法"""
        h = self.db.by_id.get(hero_id)
        if h is None:
            raise ValueError("没有这个英雄")
        draft = self.current_draft()
        key = (hero_id, draft.key(), self.position)
        self.user_hidden = False
        if key == self.pick_key:
            return                               # 同一个局面已经在做 / 做好了，直接看
        self.pick_key, self.pick_brief, self.pick_page = key, None, "lane"
        self.pick_note = f"正在整理选{h.name_cn}的对线 / 装备数据…"
        ids = (hero_id, [a.id for a in draft.allies], [e.id for e in draft.enemies])
        lane_info = {"side": self.side(), "ally_pos": draft.ally_pos}
        log.info("如果选%s：做要点卡片（%d 号位）", h.name_cn, self.position)

        def work():
            t0 = time.monotonic()
            try:
                res, err = self._brief_build(self.cfg, *ids, self.position, **lane_info), None
                log.info("如果选%s：要点做好了（%.1f 秒）", h.name_cn, time.monotonic() - t0)
            except Exception as e:
                log.exception("如果选%s：要点没做出来", h.name_cn)
                res, err = None, e
            self.submit(self._pick_ready, key, res, err, ids)
        threading.Thread(target=work, daemon=True, name="pick").start()

    def _pick_ready(self, key: tuple, res, err, ids: tuple) -> None:
        if key != self.pick_key:
            return
        if err:
            self.pick_note = f"没做出来：{err}"
            return
        res.title = f"如果选{self.db.by_id[key[0]].name_cn} · {POSITION_NAMES[key[2]]}"
        self.pick_brief = res
        if not self.cfg.ai_plan:
            res.plan_note = "AI 打法没开（控制面板右上角「设置」里打开）"
            return
        res.plan_note = "AI 正在写选它怎么打…（一般 30~60 秒，做好会补进各阶段的页里）"

        def work():
            t0 = time.monotonic()
            try:
                plan, perr = self._plan_build(self.cfg, *ids, res.lane_ids, res.text()), None
                log.info("如果选它：AI 打法做好了（%.1f 秒）", time.monotonic() - t0)
            except llm.Unavailable as e:
                log.info("如果选它：AI 打法跳过：%s", e)
                plan, perr = None, e
            except Exception as e:
                log.exception("如果选它：AI 打法没做出来")
                plan, perr = None, e
            self.submit(self._pick_plan_ready, key, plan, perr)
        threading.Thread(target=work, daemon=True, name="pick-plan").start()

    def _pick_plan_ready(self, key: tuple, plan, err) -> None:
        if key != self.pick_key or not self.pick_brief:
            return
        if err:
            self.pick_brief.plan_note = consts.plan_error(err)
            return
        self.pick_brief.plan = plan
        self.pick_brief.plan_note = "" if plan.sections else "AI 给的建议出处都对不上资料，全部丢掉了"

    def set_brief_page(self, page: str) -> None:
        if page not in dict(brief.STAGES):
            raise ValueError(f"没有这一页 {page!r}")
        if self._showing_brief():
            self.brief_page = page
            self.coach_card = self._brief_view()
        elif self.pick_key:
            self.pick_page = page

    def pick_view(self) -> dict | None:
        if not self.pick_key:
            return None
        name = self.db.by_id[self.pick_key[0]].name_cn
        stale = self.prev_draft is not None and (self.prev_draft.key(), self.position) != self.pick_key[1:]
        if not self.pick_brief:
            return {"kind": "notice", "title": f"如果选{name}", "closable": True,
                    "lines": [{"kind": "dim", "text": self.pick_note}]}
        v = self.pick_brief.view(self.pick_page, f"点标签或按 {self.cfg.hotkey_brief} 翻页 · × 回到推荐")
        if stale:
            v["title"] += "（阵容变了，再点一次英雄更新）"
        return v
