[根目录](../../CLAUDE.md) > dota_assistant > **sources**

# sources：联网拉数据

## 职责

所有「从网上拉数据、缓存到 `data/`、再读回来」的代码（原 `data.py` 拆出来的四块）。数据源：OpenDota（explorer SQL、heroStats、玩家 / 比赛）、STRATZ GraphQL（要 token）、Valve datafeed / CDN。**怎么拉**在这里，**按什么顺序拉一遍**在 `app/update.py`。

层号 1，只能 import `core`。

## 关键文件

| 文件 | 管什么 |
|---|---|
| `http.py` | 联网基础：`OPENDOTA` / `STRATZ` 地址、带连接层重试的 `_session`、`_get` / `_stratz` / `_sql` / `_stratz_brackets`、出错类型 `APIError`、`NETWORK_ERRORS` |
| `assets.py` | 死数据：英雄表（`fetch_heroes` / `load_heroes`）、头像（`fetch_portraits`）、顶栏 / 技能图标（`fetch_icon`、`ICON_DIR`、`ICONS_DONE`）、装备常量（`items_const` / `fetch_items_const`，原 `coach.ITEMS_CONST`）和装备图标（`fetch_item_icons`） |
| `stats.py` | 克制 / 配合统计（OpenDota explorer，`fetch_stats` / `stats_path` / `load_stats`）；英雄分位置场次 / 胜率（STRATZ `fetch_positions`，或没 token 时 OpenDota 分路凑的 `OPENDOTA_POSITIONS`）；`positions_source` / `load_positions` |
| `player.py` | 个人数据：段位（`MEDALS` / `rank_name`）、最近天梯每个英雄在每个位置的局数 / 胜场（`fetch_player` / `update_player` / `load_player`）、`player_history()` 按位置归类、生涯局数（`fetch_career`）、双排队友（`update_duo` / `load_duo`，`data/duo.json`）、`needs_update` |

## 对外接口 / 调用约定

- **一律 `http._get(...)` / `http._stratz(...)` 这种「模块.名字」形式调用**，不要 `from .http import _get`：测试只 patch `sources.http` 这一处（`tests/test_patch_targets.py` 查，打空了会报）。
- `http.NETWORK_ERRORS = (requests.RequestException, APIError)`：断网 / 503 / 数据源自己报错都算网络问题，日志只记一行、不记堆栈。
- `stats.load_positions(cfg)`：优先 STRATZ 的 `data/positions_r<min>-<max>.json`，没有再用 `data/positions_opendota.json`，都没有返回 `None`（推荐退回按定位标签粗筛）。
- 缓存文件：`data/stats_*.json`、`data/positions_*.json`、`data/player.json`、`data/duo.json`、`data/heroes.json`、`data/portraits/`、`data/items/`、`data/icons/`。

## 依赖与分层

- 依赖 `core.config`（`Config`、`DATA_DIR`）、`core.heroes`；第三方 `requests` / `urllib3`。
- 上层使用者：`app/update.py`（编排）、`domain/advisor.py` `coach.py` `mates.py`、`app/webui.py`（`/icons/` 现下现给）、`cli`。

## 对应测试

- `tests/test_nokey.py`：没 STRATZ token 时用 OpenDota 凑分位置数据（patch `sources.stats.DATA_DIR`，见 `test_patch_targets.IMPORTED_OK`）。
- `tests/test_familiar.py`：生涯局数 / 双排队友（patch `sources.http`、`sources.player`）。
- `tests/test_mates.py`、`tests/test_lanestate.py`：假接口替换 `http`。
- `tests/test_itembuild.py`、`tests/test_vision.py`、`tests/test_ultcd.py`：用到 `assets`。

## 注意事项 / 坑

- 克制 / 配合**自己用 explorer 对 `public_matches` 聚合**（天梯全英雄选择，时长 ≥15 分钟），不用 `/heroes/{id}/matchups`（那是职业比赛）。explorer 单次会超时，按 `match_id` 分块（`CHUNK_IDS` 100 万一块），超时自动对半拆；时间窗口用写死的 `IDS_PER_DAY` 估算（HANDOFF 第 5 节第 2 条、第 8 节）。
- 分位置数据只算打到 `POSITION_MIN_MINUTES = 30` 分钟的局：STRATZ 不写 `maxTime` 默认只给 35 分钟（HANDOFF 第 29 节）。
- STRATZ 请求要带 `Authorization: Bearer <token>` 和 `User-Agent: STRATZ_API`，不带 UA 会被拒（HANDOFF 第 9 节「常用命令」末尾）；行为快照会核对这两个请求头没丢（`tools/golden/README.md`「请求头核对」）。
- STRATZ 个人对局接口会整体超时，`fetch_player` 自动退回 OpenDota（这次不带位置）；OpenDota 免费额度约每分钟 60 次，429 自动退避（HANDOFF 第 8 节）。
- 没 token 也要能用：分位置改用 OpenDota 分路凑（HANDOFF 第 35 节）。
- token 不能出现在日志 / 报错里。

## 相关 HANDOFF 节

- HANDOFF 第 3 节（架构，数据流图）、第 4 节（模块地图，原 `data.py` 拆分对照）
- HANDOFF 第 5 节（关键设计决策 2、3）、第 8 节（已知问题与风险）
- HANDOFF 第 29 节（STRATZ 默认只给 35 分钟）、第 35 节（不填 key 也能用）、第 43 节（生涯局数 / 双排队友）

## 变更记录 (Changelog)

- 2026-11-02 11:18:41：初建，内容来自 HANDOFF 第 3~5、8 节 + 源码模块说明。
