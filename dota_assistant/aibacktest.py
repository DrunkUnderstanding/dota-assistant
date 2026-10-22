"""AI 选人盲测：拿统计数据之后的新对局，在选人进行到一半时让大模型推荐，再和真实胜负对答案。

流程（玩家 2026-10-21 提出的验证方法）：
1. 取第 3~6 手、且双方都已经有英雄的选人时刻（对面 1~2 手、我方 1~2 手已出）。
2. 候选 = 实际选的英雄 + 这个位置随机 11 个可选英雄，打乱顺序。随机候选是为了公平：
   不能用统计引擎挑候选，否则等于先替 AI 做了一半判断。
3. AI（Claude Code 无头模式，用玩家现有订阅，不另外花 API 钱）只看到：位置、双方已选、禁用、候选。
   不给对局 id、不给胜负、不给实际选了谁，也不给任何工具（不能上网查）。
   这些对局都在 2026-10，晚于模型训练截止，它不可能记得结果。
4. 同一批时刻、同一组候选，统计引擎也排一次。比较两者的「区分度」：
   实际选的英雄排得越靠前，这一队是不是越常赢（排名分与胜负的相关系数，越大越好）。

结果逐条追加到 data/ai_backtest.jsonl，可以分几次跑、累积样本。
"""
from __future__ import annotations

import json
import random
from concurrent.futures import ThreadPoolExecutor
import numpy as np

from . import backtest as bt, data, llm
from .config import DATA_DIR, Config
from .engine import POSITION_NAMES, Engine

OUT = DATA_DIR / "ai_backtest.jsonl"
N_CANDIDATES = 12
BATCH = 20               # 一次调用里放几个选人时刻

SYSTEM = """你是 Dota 2 选人顾问，熟悉当前版本（7.41）天梯全英雄选择、超凡入圣～冠绝一世分段。
用户会给出若干个选人进行到一半的局面，每个局面有一组候选英雄。
请按「这一手选它，这一队最终赢的可能性」从高到低给每个局面的候选全部排序。
考虑：英雄在这个位置的强度、对已选敌方英雄的克制、与已选队友的配合、阵容完整性（控制、伤害类型、前后期）、
被对面后手针对的风险。只依据你自己对 Dota 的理解，没有其他资料。
每个局面都必须返回，order 必须恰好包含该局面的全部候选编号，各出现一次。"""

SCHEMA = {
    "type": "object",
    "properties": {"rankings": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "order": {"type": "array", "items": {"type": "string"}}},
        "required": ["id", "order"], "additionalProperties": False}}},
    "required": ["rankings"], "additionalProperties": False,
}


def build_moments(cfg: Config, n: int, seed: int = 1) -> list[dict]:
    """从回测缓存的对局里抽选人时刻（不联网，回测缓存不够时先跑一次 backtest）"""
    stats = data.load_stats(cfg)
    matches = bt.fetch_matches(cfg, stats["hi_id"], 3000)
    eng = Engine(data.load_heroes(), stats, data.load_positions(cfg))
    t = bt.Tables(eng)
    rng = random.Random(seed)
    out = []
    for m in rng.sample(matches, len(matches)):
        pos_of = {p["heroId"]: int(p["position"][-1]) for p in m["players"]}
        bans = [b["bannedHeroId"] for b in m["pickBans"] if not b["isPick"] and b.get("wasBannedSuccessfully")]
        picks = sorted((p for p in m["pickBans"] if p["isPick"]), key=lambda p: p["order"])
        cand_moments = []
        for k in range(2, 6):
            p = picks[k]
            hero, rad = p["heroId"], p["isRadiant"]
            before = picks[:k]
            allies = [q["heroId"] for q in before if q["isRadiant"] == rad]
            enemies = [q["heroId"] for q in before if q["isRadiant"] != rad]
            if allies and enemies and hero in t.idx and hero in pos_of:
                cand_moments.append((k, hero, rad, allies, enemies))
        if not cand_moments:
            continue
        k, hero, rad, allies, enemies = rng.choice(cand_moments)     # 每场最多取一个时刻，样本更独立
        pos = pos_of[hero]
        taken = set(allies) | set(enemies) | set(bans)
        pool = [int(t.ids[i]) for i in np.flatnonzero(t.valid & t.fits[:, pos])
                if int(t.ids[i]) not in taken and int(t.ids[i]) != hero]
        if len(pool) < N_CANDIDATES - 1:
            continue
        cands = rng.sample(pool, N_CANDIDATES - 1) + [hero]
        rng.shuffle(cands)
        s = t.scores(pos, allies, enemies, list(taken))
        engine_order = sorted(cands, key=lambda h: -s[t.idx[h]])
        out.append({"key": f"{m['id']}-{k}", "match": m["id"], "order_in_draft": k, "pos": pos,
                    "allies": allies, "enemies": enemies, "bans": bans, "cands": cands, "picked": hero,
                    "won": rad == m["didRadiantWin"], "engine_order": engine_order})
        if len(out) >= n:
            break
    return out


def _name(db, h: int) -> str:
    x = db.by_id[h]
    return f"{x.name_cn}/{x.name_en}"


def prompt_for(db, batch: list[dict]) -> str:
    """只放选人时看得到的信息；局面用 m1、m2… 编号，候选用 a、b、c… 编号，不出现对局 id 和英雄 id"""
    L = []
    for i, mo in enumerate(batch, 1):
        letters = "abcdefghijklmnop"
        L.append(f"局面 m{i}：我打{POSITION_NAMES[mo['pos']]}，第 {mo['order_in_draft'] + 1} 手")
        L.append("  我方已选：" + "、".join(_name(db, h) for h in mo["allies"]))
        L.append("  敌方已选：" + "、".join(_name(db, h) for h in mo["enemies"]))
        if mo["bans"]:
            L.append("  已禁用：" + "、".join(_name(db, h) for h in mo["bans"] if h in db.by_id))
        L.append("  候选：" + "；".join(f"{letters[j]}={_name(db, h)}" for j, h in enumerate(mo["cands"])))
    return "\n".join(L)


def ask(prompt: str, model: str) -> tuple[dict | None, float, str]:
    """一次无头调用。返回 (结构化结果, 折合 API 价格, 错误信息)"""
    return llm.ask_json(prompt, SYSTEM, SCHEMA, model)


def run(cfg: Config, n: int = 300, model: str = "claude-opus-5-5", workers: int = 3, log=print) -> str:
    db = data.load_heroes()
    done = {}
    if OUT.exists():
        for line in OUT.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if r.get("model") == model:
                done[r["key"]] = r
    moments = [m for m in build_moments(cfg, n) if m["key"] not in done]
    log(f"选人时刻：共 {n}，已有结果 {len(done)}，本次要问 {len(moments)}（每次 {BATCH} 个）")
    batches = [moments[i:i + BATCH] for i in range(0, len(moments), BATCH)]
    cost = 0.0

    def one(batch):
        res, c, err = ask(prompt_for(db, batch), model)
        rows = []
        got = {r["id"]: r["order"] for r in (res or {}).get("rankings", [])}
        for i, mo in enumerate(batch, 1):
            order = got.get(f"m{i}")
            letters = "abcdefghijklmnop"[:len(mo["cands"])]
            if not order or sorted(order) != sorted(letters):
                continue                                       # 格式不对的局面丢弃，不猜
            rows.append({**mo, "model": model, "ai_order": [mo["cands"][letters.index(x)] for x in order]})
        return rows, c, err

    with ThreadPoolExecutor(workers) as ex:
        for i, (rows, c, err) in enumerate(ex.map(one, batches), 1):
            cost += c
            with OUT.open("a", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
                    done[r["key"]] = r
            log(f"  第 {i}/{len(batches)} 批：有效 {len(rows)}/{BATCH}{'，' + err if err else ''}")
    log(f"本次折合 API 价格约 ${cost:.2f}（用订阅额度，不另外扣钱）")
    return report(list(done.values()), db)


def _sep(rows: list[dict], key: str) -> float:
    x = np.array([1 - r[key].index(r["picked"]) / (len(r[key]) - 1) for r in rows])
    y = np.array([r["won"] for r in rows], float)
    return float(np.corrcoef(x, y)[0, 1]) if len(rows) > 2 else 0.0


def report(rows: list[dict], db=None) -> str:
    if not rows:
        return "没有结果"
    n = len(rows)
    se = 1 / np.sqrt(n)
    L = [f"样本：{n} 个选人时刻（各来自不同对局，第 3~6 手，双方都已有英雄），每个时刻 {N_CANDIDATES} 个候选",
         f"区分度 = 实际选的英雄排名越靠前 ↔ 越赢 的相关系数；样本 {n} 时的噪声约 ±{se * 1.96:.3f}\n",
         f"  AI 排序     区分度 {_sep(rows, 'ai_order'):+.4f}",
         f"  统计引擎     区分度 {_sep(rows, 'engine_order'):+.4f}"]
    both = [{**r, "avg": sorted(r["cands"], key=lambda h: r["ai_order"].index(h) + r["engine_order"].index(h))}
            for r in rows]
    L.append(f"  两者平均名次  区分度 {_sep(both, 'avg'):+.4f}")
    L.append("\n实际选的英雄在排序里的位置 → 这一队胜率")
    for name, key in (("AI", "ai_order"), ("统计引擎", "engine_order")):
        for lo, hi, label in ((0, 2, "前 3"), (3, 7, "第 4~8"), (8, 11, "第 9~12")):
            sub = [r for r in rows if lo <= r[key].index(r["picked"]) <= hi]
            if sub:
                w = sum(r["won"] for r in sub) / len(sub)
                L.append(f"  {name:4} {label:6} {w * 100:5.1f}%（{len(sub)} 次）")
    agree = np.mean([r["ai_order"][0] == r["engine_order"][0] for r in rows])
    rho = np.mean([np.corrcoef([r["ai_order"].index(h) for h in r["cands"]],
                               [r["engine_order"].index(h) for h in r["cands"]])[0, 1] for r in rows])
    L.append(f"\nAI 和统计引擎第一推荐相同的比例 {agree * 100:.0f}%，排序平均相关 {rho:.2f}")
    text = "\n".join(L)
    (DATA_DIR / "ai_backtest_report.txt").write_text(text, encoding="utf-8")
    return text
