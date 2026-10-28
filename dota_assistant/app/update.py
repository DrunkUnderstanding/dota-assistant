"""全量更新数据（update 命令 / 启动时数据过期）：英雄表、头像、装备、知识库、顶栏图标、分位置数据、个人数据、克制矩阵，按顺序拉一遍。

要用知识库挑大招（kb、ultcd）、按定位标签拆分路（engine），所以放在应用层，具体怎么拉在 sources/ 里。

数据源：
- 中文英雄名：Valve 官方 datafeed
- 英雄定位标签：OpenDota /heroStats
- 克制 / 配合：OpenDota explorer 直接聚合 public_matches（天梯全英雄选择，可按段位过滤）
- 英雄分位置场次 / 胜率、你每局打的位置：STRATZ（需要 token）
"""
from __future__ import annotations

import json
import time
from collections import defaultdict

from ..core.config import DATA_DIR, Config
from ..core.heroes import HeroDB
from ..sources import assets, http, player
from ..sources import stats as stats_mod


def fetch_icons(log=http._say) -> None:
    """顶栏 / 买活自动记要的图标一次下全，局内不用临时下：金币、每个英雄的小图标、每个对面会给小条的大招图标。
    已有的跳过；第一张就下不到（断网）就不试剩下的、不写标记，下次再补；个别 CDN 没有的照样写标记（运行时按需再试）"""
    from concurrent.futures import ThreadPoolExecutor
    from .. import kb
    from ..kb import cd_ops
    base = kb.KnowledgeBase.load()
    rels = ["icons/gold.png"] + [f"heroes/icons/{h.key}.png" for h in assets.load_heroes().by_id.values()]
    rels += [f"abilities/{ab.key}.png" for h in (base.heroes.values() if base else ()) if (ab := cd_ops.pick(h))]
    todo = [r for r in rels if not (assets.ICON_DIR / r).is_file()]
    if todo and assets.fetch_icon(todo[0]) is None:
        log("  顶栏图标没下到（断网？），局内用到时再下")
        return
    with ThreadPoolExecutor(8) as pool:
        missing = [r for r, p in zip(todo[1:], pool.map(assets.fetch_icon, todo[1:])) if p is None]
    assets.ICONS_DONE.write_text(base.patch if base else "", encoding="utf-8")
    log(f"  顶栏图标：新下载 {len(todo) - len(missing)} 张，共 {len(rels) - len(missing)}/{len(rels)} 张"
        + (f"（CDN 没有：{'、'.join(missing)}）" if missing else ""))


# ---------- 没填 STRATZ token 时的分位置数据：OpenDota 分路 + 定位标签 ----------

# OpenDota /scenarios/laneRoles：每个英雄走优势路 / 中路 / 劣势路 / 打野的局数（已解析的对局，不分段位，每个英雄约 1000~2000 局）。
# 只分路不分位置：优势路按定位标签（Engine.fits_by_roles）拆成一号位 / 五号位，劣势路拆成三号位 / 四号位，
# 只有一边对得上标签的给它 ROLE_SPLIT，两边都对得上或都对不上就对半；打野对半算四 / 五号位。
# 胜场一律记一半：只用来定「这个英雄打不打这个位置」（位置筛选、猜分路），不按位置修正胜率。
# 1523 场样本外对局回测（HANDOFF 第 35 节）：猜对面单人位置 68.9%（STRATZ 77.8%，只看定位标签 35.2%），
# 实际选的英雄过位置筛选 87.7%（STRATZ 94.3%，只看定位标签 73.7%），推荐区分度在噪声内没变
ROLE_SPLIT = 0.9


def positions_from_lane_roles(db: HeroDB, lane_roles: dict[int, list[dict]]) -> dict[str, dict[str, list[int]]]:
    from ..engine import Engine
    out = {}
    for h in db:
        lane = defaultdict(int)
        for r in lane_roles.get(h.id) or []:
            lane[int(r["lane_role"])] += int(r["games"])
        if not sum(lane.values()):
            continue
        fit = {p: Engine.fits_by_roles(h, p) for p in range(1, 6)}
        core = lambda a, b: ROLE_SPLIT if fit[a] and not fit[b] else 1 - ROLE_SPLIT if fit[b] and not fit[a] else 0.5
        s1, s3 = core(1, 5), core(3, 4)
        games = {1: lane[1] * s1, 2: lane[2], 3: lane[3] * s3,
                 4: lane[3] * (1 - s3) + lane[4] / 2, 5: lane[1] * (1 - s1) + lane[4] / 2}
        out[str(h.id)] = {str(p): [round(g), round(g) // 2] for p, g in games.items()}
    return out


def fetch_positions_opendota(db: HeroDB, log=http._say) -> dict:
    lane_roles = {}
    for i, h in enumerate(db, 1):
        lane_roles[h.id] = http._get(f"{http.OPENDOTA}/scenarios/laneRoles", hero_id=h.id)
        if i % 30 == 0:
            log(f"  {i} 个英雄 …")
        time.sleep(0.5)                    # OpenDota 免费接口每分钟 60 次
    return {"fetched_at": int(time.time()), "source": "opendota_lane_roles",
            "heroes": positions_from_lane_roles(db, lane_roles)}


# ---------- 汇总 ----------

def update(cfg: Config, log=http._say) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    log("拉取英雄表 …")
    heroes = assets.fetch_heroes()
    (DATA_DIR / "heroes.json").write_text(json.dumps(heroes, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"  {len(heroes)} 个英雄")
    assets.fetch_portraits(log)
    assets.fetch_items_const(log)
    assets.fetch_item_icons(log)
    log("知识库（Valve 官方技能 / 物品说明）…")
    from .. import kb
    kb.update([h["id"] for h in heroes], log)
    fetch_icons(log)                       # 要知识库挑大招，放在它后面
    if cfg.stratz_token:
        log("拉取英雄分位置数据（STRATZ）…")
        pos = stats_mod.fetch_positions(cfg)
        if pos["heroes"]:
            stats_mod.positions_path(cfg).write_text(json.dumps(pos), encoding="utf-8")
            log(f"  {len(pos['heroes'])} 个英雄")
        else:   # 2026-10-26 STRATZ 偶尔返回空表，覆盖掉以后推荐退回按定位标签筛位置（赏金猎人进中单），所以保留旧的
            log("  STRATZ 返回了空数据，保留原来的分位置数据")
    elif not stats_mod.positions_path(cfg).exists():      # 打包版自带 STRATZ 的分位置数据，有就不用凑
        log("没填 STRATZ token：用 OpenDota 的分路数据凑分位置数据（约 2 分钟）…")
        try:
            pos = fetch_positions_opendota(assets.load_heroes(), log)
        except http.NETWORK_ERRORS as e:
            log(f"  没拉到（{e}），{'沿用上次的' if stats_mod.OPENDOTA_POSITIONS.exists() else '推荐先按定位标签粗筛位置'}")
        else:
            stats_mod.OPENDOTA_POSITIONS.write_text(json.dumps(pos), encoding="utf-8")
            log(f"  {len(pos['heroes'])} 个英雄")
    player.update_player(cfg, log)
    log(f"拉取最近 {cfg.days} 天天梯对局的克制 / 配合数据 …")
    stats = stats_mod.fetch_stats(cfg, log)
    stats_mod.stats_path(cfg).write_text(json.dumps(stats), encoding="utf-8")
    log(f"完成：{stats['matches']} 局 → {stats_mod.stats_path(cfg).name}")
