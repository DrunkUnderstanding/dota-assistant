"""GSI 推送里的字段：游戏阶段常量和从推送内容算出来的值（纯函数，不带 IO）。

GSI 服务本身（收推送、GameState）在 gsi.py；死亡复盘（death）、选人（live）只要这些常量 / 函数。
"""
from __future__ import annotations

STRATEGY_STATE = "DOTA_GAMERULES_STATE_STRATEGY_TIME"   # 选完人到出门之间
SELECTION_STATE = "DOTA_GAMERULES_STATE_HERO_SELECTION"
DRAFT_STATES = {SELECTION_STATE, STRATEGY_STATE}
MATCH_STATES = {"DOTA_GAMERULES_STATE_PRE_GAME", "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"}


def buyback_left(raw: dict) -> float:
    """买活冷却还剩几秒。hero.buyback_cooldown **不是「还剩几秒」**，是冷却结束那一刻的 map.game_time（不是 clock_time）：
    2026-10-26 实战，34:20 死、约 34:51 买活，之后每次推送都是 2817 不变 = 买活时 game_time 2337 + 480；没买过活是 0。
    冷却过了这个数还留着，所以要和当前 game_time 比；没有 game_time 就当它是 0（不在冷却）"""
    def num(v) -> float:
        return float(v) if isinstance(v, (int, float)) else 0.0
    end, now = num((raw.get("hero") or {}).get("buyback_cooldown")), (raw.get("map") or {}).get("game_time")
    return max(0.0, end - now) if end and isinstance(now, (int, float)) else 0.0
