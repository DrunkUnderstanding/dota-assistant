"""本局打法：把本局要点里的数字 + 十个英雄的 Valve 官方技能原文交给大模型，写前 / 中 / 后期怎么打、对线劣势怎么办。

大模型凭自身理解排选人不如统计引擎（HANDOFF 第 10 节），所以这里：
- 只给资料，要求只依据资料说话；
- 每条建议必须附一段从资料里原样复制的话（quote），程序核对它确实在资料里，对不上的整条丢掉；
- 卡片上标明「AI 生成，未验证」。
走玩家的 Claude Code 订阅（llm.py），结果按阵容缓存在 data/plans/，同一阵容不重复问。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field

from . import kb, llm
from .config import DATA_DIR, Config

PLAN_DIR = DATA_DIR / "plans"
PHASES = ("对线期（0~10 分钟）", "中期（10~30 分钟）", "后期（30 分钟后）", "对线劣势时")
MIN_QUOTE = 4

SYSTEM = """你是 Dota 2 教练，给一名超凡入圣分段的天梯玩家写「这局怎么打」。版本以资料为准。
只能依据用户给的资料：本局统计数据、Valve 官方技能 / 天赋原文、玩家规则。
资料里没有的数值、时间点、技能效果一律不要说；拿不准就不写这一条。

要求：
- 四段：对线期（0~10 分钟）、中期（10~30 分钟）、后期（30 分钟后）、对线劣势时。前三段各 2 条，「对线劣势时」3 条。
  挑这局最要紧的写，显示空间很小。
- 每条是具体动作：做什么、什么时候、针对谁的哪个技能。不超过 30 个字，不加句号。不要空话（「注意走位」「好好发育」「团队配合」）。
- 按玩家的位置写（一号位 / 中单 / 三号位是核心，四 / 五号位是辅助）。
- 用上本局数据里的对线难易、强势期、关键装备时间、对面要防的技能。
- 「对线劣势时」：对线被压时怎么少亏、怎么找回节奏，要针对这局对线对手的具体技能（例如冷却时间、施法条件）。
- basis：依据，不超过 10 个字（例如「束缚击原文」「强势期数据」）。
- quote：从资料里原样复制一段 6~40 个字，作为这条的出处，一个字都不能改；程序会核对，对不上的整条作废。"""

SCHEMA = {
    "type": "object",
    "properties": {"sections": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "phase": {"type": "string", "enum": list(PHASES)},
            "tips": {"type": "array", "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "basis": {"type": "string"}, "quote": {"type": "string"}},
                "required": ["text", "basis", "quote"], "additionalProperties": False}}},
        "required": ["phase", "tips"], "additionalProperties": False}}},
    "required": ["sections"], "additionalProperties": False,
}


@dataclass
class Tip:
    text: str
    basis: str
    quote: str


@dataclass
class Plan:
    sections: list[tuple[str, list[Tip]]] = field(default_factory=list)
    dropped: list[Tip] = field(default_factory=list)      # quote 在资料里找不到的
    cost: float = 0.0
    seconds: float = 0.0

    def to_json(self) -> dict:
        return {"sections": [[p, [t.__dict__ for t in ts]] for p, ts in self.sections],
                "dropped": [t.__dict__ for t in self.dropped], "cost": self.cost, "seconds": self.seconds}

    @classmethod
    def from_json(cls, d: dict) -> Plan:
        return cls([(p, [Tip(**t) for t in ts]) for p, ts in d["sections"]], [Tip(**t) for t in d["dropped"]],
                   d.get("cost", 0.0), d.get("seconds", 0.0))

    def text(self) -> str:
        L = []
        for p, ts in self.sections:
            L.append(f"【{p}】")
            L += [f"  · {t.text}\n      依据：{t.basis} ——「{t.quote}」" for t in ts]
        if self.dropped:
            L.append(f"（{len(self.dropped)} 条出处对不上资料，已丢掉：" + "；".join(t.text for t in self.dropped) + "）")
        return "\n".join(L)


def facts(base: kb.KnowledgeBase, rules: list[kb.Rule], my_id: int, ally_ids: list[int], enemy_ids: list[int],
          lane_ids: list[int], brief_text: str) -> str:
    """给大模型的全部资料。自己和对面五个人给技能全文，队友给机制摘要"""
    hero = lambda i: base.heroes[i]
    L = ["# 本局数据（统计）", brief_text, "", "# 你的英雄（官方原文）", kb.render_hero(hero(my_id), rules, base.patch)]
    for i in enemy_ids:
        if i in base.heroes:
            mark = "（你的对线对手）" if i in lane_ids else ""
            L += ["", f"# 敌方 {hero(i).name}{mark}（官方原文）", kb.render_hero(hero(i), rules, base.patch)]
    allies = [kb.hero_brief(hero(i), rules) for i in ally_ids if i in base.heroes]
    if allies:
        L += ["", "# 队友（机制摘要）", *allies]
    general = [r.text for r in rules if not r.hero]
    if general:
        L += ["", "# 玩家规则", *general]
    return "\n".join(L)


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s).replace("“", "「").replace("”", "」")


def parse(res: dict, source: str) -> Plan:
    """按固定顺序整理；quote 必须原样出现在资料里"""
    src = _norm(source)
    got: dict[str, list[Tip]] = {}
    plan = Plan()
    for sec in res.get("sections") or []:
        for t in sec.get("tips") or []:
            tip = Tip(str(t.get("text", "")).strip(), str(t.get("basis", "")).strip(), str(t.get("quote", "")).strip())
            if len(_norm(tip.quote)) >= MIN_QUOTE and _norm(tip.quote) in src and tip.text:
                got.setdefault(sec.get("phase"), []).append(tip)
            else:
                plan.dropped.append(tip)
    plan.sections = [(p, got[p]) for p in PHASES if got.get(p)]
    return plan


def generate(cfg: Config, my_id: int, ally_ids: list[int], enemy_ids: list[int], lane_ids: list[int],
             brief_text: str, base: kb.KnowledgeBase | None = None) -> Plan:
    """几十秒，放后台线程调。同一份资料（阵容 + 数据）读缓存"""
    base = base or kb.KnowledgeBase.load()
    if base is None:
        raise RuntimeError("还没有知识库，先运行 python -m dota_assistant kb update")
    source = facts(base, kb.load_rules(), my_id, ally_ids, enemy_ids, lane_ids, brief_text)
    key = hashlib.sha1(f"{cfg.ai_model}\n{SYSTEM}\n{source}".encode("utf-8")).hexdigest()[:16]
    path = PLAN_DIR / f"{key}.json"
    if path.exists():
        return Plan.from_json(json.loads(path.read_text(encoding="utf-8")))
    t0 = time.time()
    res, cost, err = llm.ask_json(source, SYSTEM, SCHEMA, cfg.ai_model, timeout=240)
    if res is None:
        raise RuntimeError(f"AI 没给出结果：{err}")
    plan = parse(res, source)
    plan.cost, plan.seconds = cost, time.time() - t0
    PLAN_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan.to_json(), ensure_ascii=False), encoding="utf-8")
    return plan
