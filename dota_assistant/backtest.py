"""回测：拿统计数据之后的新对局（样本外），检验推荐和胜负到底有没有关系。

两项检验：
1. 选人检验：按真实选人顺序重放，每一手只用「轮到他时能看到的」信息（之前的选人 + 禁用），
   给这个位置的所有候选打分，看他实际选的英雄排第几、比最佳差多少，再按这些分组统计实际胜率。
   如果推荐有用，排名靠前 / 差距小的那组应该赢得更多。
2. 阵容检验：只看双方最终阵容，用引擎的打分预测哪边赢，算准确率和校准。
   拆开看「只用英雄强度」「再加克制」「再加配合」各贡献多少。

注意：这是相关性，不是因果。选得好的人往往也打得好，所以排名靠前组的胜率差里有一部分是玩家水平。
"""
from __future__ import annotations

import json
import math
import random
from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from .core.config import DATA_DIR, Config
from .sources import assets, http
from .sources import stats as stats_mod
from .domain import engine as E
from .domain.engine import LANE_OPPONENTS, MIN_GAMES, RISK_TOP, W_SYN, Engine, sigmoid

CACHE = DATA_DIR / "backtest_matches.json"
STRATZ_BATCH = 20          # 一次 GraphQL 请求里用别名查几场（每场是单独的 match(id:)）
_FIELDS = ("id didRadiantWin durationSeconds "
           "pickBans { isPick heroId order isRadiant bannedHeroId wasBannedSuccessfully } "
           "players { heroId isRadiant position }")


# ---------- 取样本外对局 ----------

def fetch_matches(cfg: Config, after_id: int, n: int, log=print) -> list[dict]:
    """统计数据之后、同分段的天梯全英雄选择对局，带选人顺序和每人位置（STRATZ）。结果缓存。"""
    cached = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    # 可用对局数总比请求的少一些（缺选人 / 位置），按「当时请求了多少」判断缓存够不够
    if cached.get("after_id") == after_id and cached.get("requested", 0) >= n:
        return cached["matches"][:n]

    bracket = stats_mod._bracket_sql(cfg)
    q = (f"SELECT match_id FROM public_matches WHERE match_id > {after_id} AND match_id <= {after_id + 3_000_000} "
         f"AND game_mode = 22 AND lobby_type = 7 AND duration >= {stats_mod.MIN_DURATION} {bracket} "
         f"ORDER BY random() LIMIT {int(n * 1.4) + 20}")
    ids = [r["match_id"] for r in http._sql(q)]
    log(f"  OpenDota 抽到 {len(ids)} 场候选，去 STRATZ 取选人顺序和位置 …")
    out = []
    for i in range(0, len(ids), STRATZ_BATCH):
        chunk = ids[i:i + STRATZ_BATCH]
        query = "{ " + " ".join(f"m{j}: match(id: {mid}) {{ {_FIELDS} }}" for j, mid in enumerate(chunk)) + " }"
        try:
            res = http._stratz(cfg, query)
        except Exception as e:                      # 个别批次失败不影响整体
            log(f"  第 {i // STRATZ_BATCH + 1} 批失败：{e}")
            continue
        out += [m for m in res.values() if usable(m)]
        if (i // STRATZ_BATCH) % 20 == 0:
            log(f"  已取 {min(i + STRATZ_BATCH, len(ids))}/{len(ids)}，可用 {len(out)}")
        if len(out) >= n:
            break
    CACHE.write_text(json.dumps({"after_id": after_id, "requested": n, "matches": out}), encoding="utf-8")
    return out[:n]


def usable(m: dict | None) -> bool:
    if not m or m.get("didRadiantWin") is None:
        return False
    picks = [p for p in (m.get("pickBans") or []) if p["isPick"] and p.get("order") is not None]
    pos = [p.get("position") for p in m.get("players") or []]
    return len(picks) == 10 and len(pos) == 10 and all(p and p.startswith("POSITION_") for p in pos)


# ---------- 矩阵化打分（逻辑全部来自 Engine，这里只是一次性算好查表） ----------

class Tables:
    def __init__(self, eng: Engine):
        ids = sorted(h.id for h in eng.db)
        self.idx = {h: i for i, h in enumerate(ids)}
        self.ids = np.array(ids)
        n = len(ids)
        self.adv = np.zeros((n, n))
        self.syn = np.zeros((n, n))
        self.base = np.zeros((n, 6))          # [:, 0] = 不分位置
        self.fits = np.zeros((n, 6), bool)
        self.share = np.zeros((n, 6))         # 英雄打各位置的占比
        self.valid = np.array([eng.games.get(h, 0) >= MIN_GAMES for h in ids])
        for a in ids:
            i = self.idx[a]
            self.base[i, 0] = eng.base.get(a, 0)
            for p in range(1, 6):
                self.base[i, p] = eng.base_at(a, p)
                self.fits[i, p] = eng.fits(eng.db.by_id[a], p)
                self.share[i, p] = eng.position_share(a, p) or 0
            for b in ids:
                if a != b:
                    j = self.idx[b]
                    self.adv[i, j] = eng.advantage(a, b)
                    self.syn[i, j] = eng.synergy(a, b)

        self.lane_mask = np.zeros((6, 6))     # [我的位置, 敌方位置] = 1 表示对线
        for me, opp in LANE_OPPONENTS.items():
            self.lane_mask[me, list(opp)] = 1

    def scores(self, pos: int, allies: list[int], enemies: list[int], taken: list[int] = (),
               w_lane: float | None = None, w_risk: float | None = None) -> np.ndarray:
        """和 Engine.suggest 的打分一致（不含个人项）；w_lane / w_risk 不给就用 engine 里的常数"""
        w_lane = E.W_LANE if w_lane is None else w_lane
        w_risk = E.W_RISK if w_risk is None else w_risk
        s = self.base[:, pos].copy()
        if enemies:
            ei = [self.idx[e] for e in enemies]
            weight = 1 + w_lane * (self.share[ei] @ self.lane_mask[pos])
            s += self.adv[:, ei] @ weight
        if allies:
            s += W_SYN * self.syn[:, [self.idx[a] for a in allies]].sum(axis=1)
        remaining = 5 - len(enemies)
        if w_risk and remaining > 0:
            s -= w_risk * remaining * self.risk(taken)
        return s

    def risk(self, taken: list[int]) -> np.ndarray:
        """每个英雄：还没被选的英雄里最克它的 RISK_TOP 个的平均优势"""
        a = self.adv.T.copy()                 # a[h, e] = e 对 h 的优势
        blocked = ~self.valid
        blocked[[self.idx[h] for h in taken if h in self.idx]] = True
        a[:, blocked] = -np.inf
        np.fill_diagonal(a, -np.inf)
        k = min(RISK_TOP, a.shape[1])
        top = np.partition(a, -k, axis=1)[:, -k:]
        top[np.isinf(top)] = np.nan               # 可选的不足 RISK_TOP 个时，和 Engine 一样只平均剩下的
        with np.errstate(all="ignore"):
            out = np.nanmean(top, axis=1)
        return np.nan_to_num(out)


# ---------- 检验 ----------

@dataclass
class PickRow:
    order: int
    rank: int              # 实际选的英雄在候选里排第几（1 = 推荐第一）
    candidates: int
    regret: float          # 最佳候选指数 - 实际选的指数（百分点）
    won: bool


def replay_picks(t: Tables, matches: list[dict], **weights) -> list[PickRow]:
    rows = []
    for m in matches:
        pos_of = {(p["heroId"]): int(p["position"][-1]) for p in m["players"]}
        bans = {b["bannedHeroId"] for b in m["pickBans"] if not b["isPick"] and b.get("wasBannedSuccessfully")}
        picks = sorted((p for p in m["pickBans"] if p["isPick"]), key=lambda p: p["order"])
        for k, p in enumerate(picks):
            hero, rad = p["heroId"], p["isRadiant"]
            if hero not in t.idx or hero not in pos_of:
                continue
            before = picks[:k]
            allies = [q["heroId"] for q in before if q["isRadiant"] == rad and q["heroId"] in t.idx]
            enemies = [q["heroId"] for q in before if q["isRadiant"] != rad and q["heroId"] in t.idx]
            pos = pos_of[hero]
            seen = [*allies, *enemies, *bans]
            s = t.scores(pos, allies, enemies, seen, **weights)
            taken = {t.idx[h] for h in seen if h in t.idx}
            hi = t.idx[hero]
            cand = [i for i in np.flatnonzero(t.valid & t.fits[:, pos]) if i not in taken]
            if hi not in cand:
                cand.append(hi)
            cs = s[cand]
            rank = int((cs > s[hi]).sum()) + 1
            regret = (sigmoid(cs.max()) - sigmoid(s[hi])) * 100
            rows.append(PickRow(k, rank, len(cand), regret, rad == m["didRadiantWin"]))
    return rows


def predict_drafts(t: Tables, matches: list[dict]) -> dict[str, list[tuple[float, bool]]]:
    """三个模型对天辉胜率的预测（logit），和真实结果"""
    out = defaultdict(list)
    for m in matches:
        pos_of = {p["heroId"]: int(p["position"][-1]) for p in m["players"]}
        r = [t.idx[p["heroId"]] for p in m["players"] if p["isRadiant"] and p["heroId"] in t.idx]
        d = [t.idx[p["heroId"]] for p in m["players"] if not p["isRadiant"] and p["heroId"] in t.idx]
        if len(r) != 5 or len(d) != 5:
            continue
        base = (sum(t.base[i, pos_of[t.ids[i]]] for i in r) - sum(t.base[i, pos_of[t.ids[i]]] for i in d))
        counter = sum((t.adv[i, j] - t.adv[j, i]) / 2 for i in r for j in d)
        syn = W_SYN * (sum(t.syn[a, b] for a in r for b in r if a < b) - sum(t.syn[a, b] for a in d for b in d if a < b))
        win = m["didRadiantWin"]
        out["只看英雄强度"].append((base, win))
        out["强度 + 克制"].append((base + counter, win))
        out["强度 + 克制 + 配合（现在的引擎）"].append((base + counter + syn, win))
    return out


def separation(rows: list[PickRow]) -> float:
    """推荐区分度：实际胜负 与 「排名越靠前」的相关系数（越大越好）"""
    if len(rows) < 10:
        return 0.0
    x = np.array([1 - (r.rank - 1) / max(r.candidates - 1, 1) for r in rows])
    y = np.array([r.won for r in rows], float)
    return float(np.corrcoef(x, y)[0, 1])


def tune(t: Tables, matches: list[dict], log=print) -> tuple[float, float, list[str]]:
    """在一半对局上网格搜 W_LANE / W_RISK，另一半上报告，避免调参调到噪声上"""
    a = [m for m in matches if m["id"] % 2 == 0]
    b = [m for m in matches if m["id"] % 2 == 1]
    lines, best = [], None
    for wl in (0.0, 0.5, 1.0):
        for wr in (0.0, 0.05, 0.1, 0.2, 0.3):
            ra = replay_picks(t, a, w_lane=wl, w_risk=wr)
            sa, se = separation(ra), separation([r for r in ra if r.order < 4])
            lines.append(f"  W_LANE={wl:<4} W_RISK={wr:<5} 调参集：全部 {sa:.4f}  前 4 手 {se:.4f}")
            if best is None or sa > best[0]:
                best = (sa, wl, wr)
    _, wl, wr = best
    for name, kw in (("改前（0, 0）", dict(w_lane=0.0, w_risk=0.0)), (f"最优（{wl}, {wr}）", dict(w_lane=wl, w_risk=wr))):
        rb = replay_picks(t, b, **kw)
        lines.append(f"  验证集 {name}：全部 {separation(rb):.4f}  前 4 手 {separation([r for r in rb if r.order < 4]):.4f}"
                     f"  后 4 手 {separation([r for r in rb if r.order >= 6]):.4f}")
    return wl, wr, lines


# ---------- 报告 ----------

def _wr(rows) -> str:
    if not rows:
        return "   -"
    w = sum(r.won for r in rows) / len(rows)
    se = math.sqrt(w * (1 - w) / len(rows)) * 100
    return f"{w * 100:5.1f}% ±{se * 1.96:.1f}（{len(rows)} 手）"


def report(rows: list[PickRow], drafts: dict, n_matches: int, train: dict) -> str:
    L = []
    L.append(f"参数：W_LANE={E.W_LANE} W_RISK={E.W_RISK}；推荐区分度（相关系数）全部 {separation(rows):.4f}，"
             f"前 4 手 {separation([r for r in rows if r.order < 4]):.4f}，后 4 手 {separation([r for r in rows if r.order >= 6]):.4f}")
    L.append(f"样本：{n_matches} 场（统计数据截止 match_id {train.get('hi_id')} 之后的新对局，样本外），"
             f"共 {len(rows)} 手选人")
    L.append("胜率后面的 ± 是 95% 置信区间；两组区间不重叠才算真有差别。\n")

    L.append("【一、实际选的英雄在推荐里排第几 → 这一手所在队伍的胜率】")
    for lo, hi, name in ((1, 1, "排第 1"), (2, 3, "第 2~3"), (4, 10, "第 4~10"), (11, 20, "第 11~20"), (21, 999, "第 21 以后")):
        L.append(f"  {name:8} {_wr([r for r in rows if lo <= r.rank <= hi])}")

    L.append("\n【二、实际选的比推荐第一差多少（指数百分点）→ 胜率】")
    for lo, hi, name in ((0, 0.001, "就是第一"), (0.001, 2, "差 0~2"), (2, 5, "差 2~5"), (5, 10, "差 5~10"), (10, 999, "差 10 以上")):
        L.append(f"  {name:8} {_wr([r for r in rows if lo <= r.regret < hi])}")

    L.append("\n【三、按选人顺序拆开：前四手 vs 最后四手（后选能看到更多对面英雄）】")
    for name, keep in (("第 1~4 手", lambda r: r.order < 4), ("第 7~10 手", lambda r: r.order >= 6)):
        sub = [r for r in rows if keep(r)]
        good = [r for r in sub if r.rank <= 3]
        bad = [r for r in sub if r.rank > 20]
        L.append(f"  {name}：推荐前 3 {_wr(good)}   |   排 20 名以后 {_wr(bad)}")

    L.append("\n【四、只看最终阵容，预测哪边赢】")
    for name, preds in drafts.items():
        acc = sum((x > 0) == w for x, w in preds) / len(preds)
        ll = -sum(math.log(sigmoid(x) if w else 1 - sigmoid(x)) for x, w in preds) / len(preds)
        L.append(f"  {name:22} 准确率 {acc * 100:5.1f}%   对数损失 {ll:.4f}（瞎猜 = 0.6931，越低越好）")
    full = list(drafts.values())[-1]
    L.append("  校准（现在的引擎）：预测天辉胜率 → 实际天辉胜率")
    for lo, hi in ((0, 0.4), (0.4, 0.45), (0.45, 0.5), (0.5, 0.55), (0.55, 0.6), (0.6, 1.01)):
        sub = [w for x, w in full if lo <= sigmoid(x) < hi]
        if sub:
            L.append(f"    预测 {lo * 100:3.0f}%~{min(hi, 1) * 100:3.0f}%：实际 {sum(sub) / len(sub) * 100:5.1f}%（{len(sub)} 场）")
    return "\n".join(L)


def run(cfg: Config, n: int = 3000, log=print, do_tune: bool = False) -> str:
    stats = stats_mod.load_stats(cfg)
    after = stats.get("hi_id")
    if not after:
        raise RuntimeError("统计数据里没有 match_id 范围，先重新 update 一次")
    if not cfg.stratz_token:
        raise RuntimeError("回测需要 STRATZ token（取选人顺序和位置）")
    eng = Engine(assets.load_heroes(), stats, stats_mod.load_positions(cfg))
    log(f"取样本外对局（match_id > {after}）…")
    matches = fetch_matches(cfg, after, n, log)
    random.Random(0).shuffle(matches)
    log(f"  可用 {len(matches)} 场，开始重放 …")
    t = Tables(eng)
    if do_tune:
        wl, wr, lines = tune(t, matches, log)
        return "\n".join(["调参（相关系数：排名越靠前 ↔ 越赢，越大越好）", *lines])
    text = report(replay_picks(t, matches), predict_drafts(t, matches), len(matches), stats)
    (DATA_DIR / "backtest_report.txt").write_text(text, encoding="utf-8")
    return text
