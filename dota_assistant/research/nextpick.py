"""回测：对面最后一手猜得准不准（domain/nextpick.py），python -m dota_assistant next-test。

样本 = 统计数据之后的天梯全英雄选择（backtest.fetch_matches，带选人顺序），每场看第 10 手（整局最后一手）：
那一队已选 4 个、另一队 5 个都选了。「只看我方 4 个」= 另一队的前 4 手（玩家要的情形：我是我方最后一手，对面最后一手在我之后），
「看我方 5 个」= 连我这手也算上。对局按 match_id 排序后前一半网格搜权重、后一半验证。
"""
from __future__ import annotations

import math

from ..core.config import Config
from ..domain import nextpick
from ..domain.engine import Engine
from ..sources import assets
from ..sources import stats as stats_mod
from . import backtest

GRID_ADV = (0.0, 1.0, 1.5, 2.0, 2.5, 3.0)
GRID_SYN = (0.0, 0.5, 1.0)


def rows(eng: Engine, matches: list[dict]) -> list[dict]:
    out = []
    for m in sorted(matches, key=lambda m: m["id"]):
        picks = sorted((p for p in m["pickBans"] if p["isPick"]), key=lambda p: p["order"])
        last = picks[-1]
        if any(p["heroId"] not in eng.base for p in picks):
            continue
        their = [p["heroId"] for p in picks[:-1] if p["isRadiant"] == last["isRadiant"]]
        ours = [p["heroId"] for p in picks if p["isRadiant"] != last["isRadiant"]]
        bans = {b["bannedHeroId"] for b in m["pickBans"] if not b["isPick"] and b.get("wasBannedSuccessfully")}
        taken = {p["heroId"] for p in picks[:-1]} | bans
        pos = {p["heroId"]: int(p["position"][-1]) for p in m["players"]}
        if len(their) != 4 or len(ours) != 5:
            continue
        q = nextpick.open_positions(their, eng.positions)
        out.append({"y": last["heroId"], "ypos": pos.get(last["heroId"]), "q": q,
                    "f4": nextpick.features(eng, their, ours[:4], taken), "f5": nextpick.features(eng, their, ours, taken)})
    return out


def score(eng: Engine, rs: list[dict], feats: str, w_adv: float, w_syn: float) -> dict:
    """平均对数似然、猜中率（真选的在前 1 / 3 / 5 / 10 个里）、最可能那个的平均概率"""
    ll, hit, top = 0.0, [0, 0, 0, 0], 0.0
    for r in rs:
        d = nextpick.distribution(eng, r[feats], r["q"], w_adv, w_syn)
        p = d.get(r["y"], 0.0)
        ll += math.log(max(p, 1e-12))
        rank = sum(1 for v in d.values() if v > p) + 1
        for i, k in enumerate((1, 3, 5, 10)):
            hit[i] += rank <= k
        top += max(d.values())
    n = max(1, len(rs))
    return {"ll": ll / n, "hit": [h / n for h in hit], "top": top / n}


def line(name: str, s: dict) -> str:
    h = s["hit"]
    return (f"  {name}：猜中 第 1 个 {h[0]:.1%}、前 3 {h[1]:.1%}、前 5 {h[2]:.1%}、前 10 {h[3]:.1%}；"
            f"平均对数似然 {s['ll']:.4f}；最可能那个平均给 {s['top']:.1%}")


def run(cfg: Config, n: int = 4000, log=print) -> str:
    stats = stats_mod.load_stats(cfg)
    if not (stats.get("hi_id") and cfg.stratz_token):
        raise RuntimeError("需要统计数据（update）和 STRATZ token")
    eng = Engine(assets.load_heroes(), stats, stats_mod.load_positions(cfg))
    matches = backtest.fetch_matches(cfg, stats["hi_id"], n, log)
    rs = rows(eng, matches)
    half = len(rs) // 2
    train, test = rs[:half], rs[half:]
    log(f"  {len(rs)} 场可用，前 {half} 场网格搜权重 …")
    grid = {(a, s): score(eng, train, "f4", a, s)["ll"] for a in GRID_ADV for s in GRID_SYN}
    best = max(grid, key=grid.get)
    pos_ok = sum(1 for r in rs if max(r["q"], key=r["q"].get) == r["ypos"]) / max(1, len(rs))
    L = [f"{len(rs)} 场样本外对局（match_id > {stats['hi_id']}），看每场最后一手；结果是后 {len(test)} 场（验证集）",
         f"对面空着的位置猜对：{pos_ok:.1%}",
         line("只看流行度（这个位置谁选得多）", score(eng, test, "f4", 0, 0)),
         line(f"现在的模型：流行度 + 克制 ×{nextpick.W_ADV:g} + 配合 ×{nextpick.W_SYN:g}，只看我方 4 个",
              score(eng, test, "f4", nextpick.W_ADV, nextpick.W_SYN)),
         line("同上，但连我这手也看（对面会不会针对我最后选的）", score(eng, test, "f5", nextpick.W_ADV, nextpick.W_SYN)),
         f"训练集网格最好：克制 ×{best[0]:g}、配合 ×{best[1]:g}（对数似然 {grid[best]:.4f}）",
         line("训练集最好的权重放到验证集", score(eng, test, "f4", *best))]
    return "\n".join(L)
