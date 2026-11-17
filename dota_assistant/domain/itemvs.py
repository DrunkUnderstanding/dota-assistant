"""对面阵容 → 同分段的人多买 / 少买哪些装备（knowledge/item_vs.json，NOTES 第 79 节研究整理的表，第 80 节接进产品）。

表是 7.41f 超凡～冠绝 17.3 万局按「英雄 + 位置」统计的：对面有某个英雄时，每件装备的购买比例和平时比变了多少
（按对局时长标准化、Bonferroni 校正、两半复核过）。**只说明「同分段的人这么出」，不说明「这么出更容易赢」**
（同一研究里胜率那部分测不出来），所以界面上只写购买比例，不写胜率。

- shifts()：这局对面 5 个英雄 → 每件变了的装备一条 Shift（几个敌人推同一件时取变得最多的那个，头像都列上）
- strong：这局 ≥ STRONG_SHARE 的人买、比平时多 ≥ STRONG_LIFT —— 够格进出装顺序（itembuild.make）；
  少买的同样按 STRONG_LIFT 算 strong（顺序里那件灰掉 + 红字，玩家 2026-11-16 要画出来）
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from ..core.config import ROOT

log = logging.getLogger(__name__)

TABLE = ROOT / "knowledge" / "item_vs.json"
STRONG_SHARE = 0.40       # 这局至少多少比例的人买，才插进出装顺序（和「核心装」coach.CORE_SHARE 一样）
STRONG_LIFT = 0.15        # 比平时至少多 / 少几个百分点（2.3 万人次估过：强的这档 74% 的局一件都没有，不刷屏）

# rows 每列的位置（表里 fields.vs 写着）
R_ITEM, R_SHARE, R_EXP, R_LIFT, R_OK, R_MIN = 0, 1, 2, 3, 5, 10


@dataclass
class Shift:
    key: str                  # 装备 key
    share: float              # 对面有 enemies[0] 时的购买比例
    usual: float              # 按时长标准化的「平时」比例
    minute: float | None      # 对面有它时的中位购买分钟（没有就 None，用平时的）
    enemies: list[int] = field(default_factory=list)   # 推这件的对面英雄，变得最多的在前
    n: int = 0                # enemies[0] 那组的人次

    @property
    def lift(self) -> float:
        return self.share - self.usual

    @property
    def strong(self) -> bool:
        if self.lift > 0:
            return self.share >= STRONG_SHARE and self.lift >= STRONG_LIFT
        return -self.lift >= STRONG_LIFT


@lru_cache(maxsize=4)
def load(path: Path = TABLE) -> dict | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("对面阵容出装表读不到（%s），出装只按同分段核心装", e)
        return None
    if d.get("v", 0) < 2:
        log.warning("对面阵容出装表是旧版（v%s），出装只按同分段核心装", d.get("v"))
        return None
    return d


def base_rate(table: dict | None, hero_id: int, pos: int, key: str) -> tuple[float, float | None] | None:
    """平时（不分对面）的 (购买比例, 中位购买分钟)；表里没有返回 None"""
    it = ((table or {}).get("base", {}).get(f"{hero_id}_{pos}") or {}).get("items", {}).get(key)
    return (it[0], it[1]) if it else None


def shifts(hero_id: int, pos: int, enemy_ids: list[int], table: dict | None = None) -> list[Shift]:
    """这局对面阵容下，购买比例明显变了的装备（只要「变化过校正且两半一致」的），按变化从大到小。
    同一件几个敌人都推：多买的取多得最多的那个敌人的数，头像都列上；有人推多、有人推少，按变化大的那边算"""
    table = table if table is not None else load()
    vs = ((table or {}).get("vs") or {}).get(f"{hero_id}_{pos}") or {}
    by_item: dict[str, list[tuple[int, int, list]]] = {}
    for e in enemy_ids:
        g = vs.get(str(e))
        for r in (g or {}).get("rows", []):
            if r[R_OK]:
                by_item.setdefault(r[R_ITEM], []).append((e, g["n"], r))
    out = []
    for k, hits in by_item.items():
        hits.sort(key=lambda x: -abs(x[2][R_LIFT]))
        e, n, r = hits[0]
        up = r[R_LIFT] > 0
        same = [x[0] for x in hits if (x[2][R_LIFT] > 0) == up]
        out.append(Shift(k, r[R_SHARE], r[R_EXP], r[R_MIN] if len(r) > R_MIN else None, same, n))
    return sorted(out, key=lambda s: -abs(s.lift))
