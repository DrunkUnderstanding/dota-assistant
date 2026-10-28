# 分层冲突分析（重构计划 P0.7）

> 2026-10-28 生成，基线 `b1113e8`。表格部分由 `python tools/refactor/export_names.py --layering` / `--live-groups` 算出来
> （只用 ast 读源码：每个顶层定义 / `Live` 的每个方法用到的本模块其它名字、别的模块的名字，含函数里的延迟 import，
> 再按 `docs/refactor/moves.json` 的去处映射到新模块）。改了放法重新跑脚本、替换表格。

分层（PLAN 2.2，从低到高）：`core` → `sources` → `kb` → `ai` → `domain` → `vision` / `game`（平级）→ `research` → `app` → `cli`。

## 结论

**按现在的 moves.json 放法，没有往上层的 import，`test_layers.py` 暂时不需要白名单。**

为了做到这一点，对 PLAN 2.1 表做了一处改放法（其余是表里没写到的名字补去处，见 moves.json 的 `notes`）：

| 冲突（按原表放） | 谁 import 谁 | 处理 |
|---|---|---|
| `kb/cd_ops.update_ops` 要用 `ultcd.pick` 挑「对面会给小条的大招」，`pick` 又用 `cooldowns`、`MIN_CD` | `kb.cd_ops` → `domain.ultcd`（kb 往上到 domain） | **改放法**：`MIN_CD`、`cooldowns`、`pick` 一起放 `kb/cd_ops`（只读知识库条目的纯函数）。`domain/ultcd.cooldown` 用 `cooldowns`、`app/update.fetch_icons` 和 `live` 用 `pick`，都是往下引用 `cd_ops`。测试里 `ultcd.pick` / `ultcd.cooldowns` 跟着改成 `cd_ops.xxx`（只改模块路径） |

PLAN 第 1 节列的其它往上 / 互相导入，按表放已经消掉，逐条核对：

| 现在的延迟 import | 新位置 | 结果 |
|---|---|---|
| `data.fetch_items_const` / `fetch_item_icons` → `coach.ITEMS_CONST` / `items_const` | 两个名字都搬进 `sources/assets`（PLAN 表已写） | 同模块，消掉。`coach` 改成往下引用 `assets` |
| `data.fetch_icons` → `kb`、`ultcd.pick`；`data.update` → `kb`；`data.positions_from_lane_roles` → `engine.Engine.fits_by_roles` | 这几个（加上调 `positions_from_lane_roles` 的 `fetch_positions_opendota`、只有它用的 `ROLE_SPLIT`）放 `app/update` | app 往下引用，消掉 |
| `kb._update_cd_ops` → `ultcd.update_ops`；`ultcd` 顶层 `from . import kb` | `kb/__init__` ↔ `kb/cd_ops` | 同层（kb 包内）互相引用，**`kb/__init__` 里对 `cd_ops` 的延迟 import 必须保留**（`cd_ops` 顶层就要 `kb.KB_DIR`，两边都在顶层 import 会循环） |
| `ultcd._reader` → `gsi.find_dota`、`gamefiles.VPK` | `core/dota_install`、`core/vpk` | 往下，消掉 |
| `death` → `gsi.buyback_left` | `core/gsi_fields` | 往下，消掉 |
| `lanestate`（live 记对线）→ `backtest` | live 只用 `LOG_MINUTES`、`log_moment` → `domain/lanelog`；`fetch` 里的 `backtest` 留在 `research/lanestate` | `domain/lanelog` 只依赖 `core.config`，消掉 |
| `gsi.find_dota` → `config.load` | `core/dota_install` → `core/config` | 同层 |

同层互相引用（允许，列出来方便 P2/P3 时注意 import 顺序）：

- `domain.coach` ↔ `domain.itembuild`：原来就是函数内延迟 import，保留。
- `kb` ↔ `kb.cd_ops`：见上。
- `sources.player` → `sources.stats`：`_POSITIONS`（`fetch_positions` 和 `_player_matches_stratz` / `update_player` 都用）放 `stats`，`player` 用 `stats._POSITIONS`。
- `sources.assets` / `stats` / `player` → `sources.http`：一律 `http._get(...)` / `http._stratz(...)` / `http._session` 形式（测试只 patch `http` 一处）。
- `app.live.*` 的 mixin 之间互相调方法（都在 `self` 上，不需要 import 彼此）；`app.start` → `app.update`、`app.webui`；`app.demo` / `app.webui` → `app.live`。
- `vision` 和 `game` 之间：现在没有互相引用。

## 搬家时要跟着改的 patch 目标（会在测试里体现）

| 测试现在的写法 | 搬家后 | 前提（调用方怎么取名字） |
|---|---|---|
| `patch.object(data, "_get")`（test_mates） | `http._get` | `mates`、`sources/*` 用 `http._get(...)` |
| `patch.object(data, "_stratz")`（test_nokey）、`patch.object(ls.data, "_stratz")`（test_lanestate） | `http._stratz` | `brief`、`coach`、`sources/player`、`research/lanestate` 用 `http._stratz(...)` |
| `patch.object(data, "_player_matches_stratz")`（test_mates） | `player._player_matches_stratz` | `mates` 用 `player._player_matches_stratz(...)` |
| `patch.object(data, "positions_source" / "load_positions")`（test_nokey） | `stats.xxx` | 调用方用 `stats.xxx(...)` |
| `patch.object(data, "DATA_DIR" / "OPENDOTA_POSITIONS")`（test_nokey） | `stats.DATA_DIR` / `stats.OPENDOTA_POSITIONS` | `positions_path` / `positions_source` / `load_positions` 都在 `stats`，`stats` 要 `from ..core.config import DATA_DIR`（这是 import 进来又被 patch 的名字，清单里标了 `imported`） |
| `patch.object(data, "ICON_DIR" / "ICONS_DONE" / "load_heroes" / "fetch_icon")` 后调 `data.fetch_icons`（test_ultcd） | 四个都 patch `assets.xxx`，调 `update.fetch_icons` | `app/update.fetch_icons` 里必须写 `assets.ICON_DIR`、`assets.ICONS_DONE`、`assets.load_heroes()`、`assets.fetch_icon`（不能 `from ..sources.assets import ...`，否则 patch 打空） |
| `patch.object(data, "ICON_DIR" / "_download_icon")`（test_webui） | `assets.xxx` | `fetch_icon` 和它们同在 `assets` |
| `ultcd.OPS_PATH = ...`（test_ultcd） | `cd_ops.OPS_PATH` | `update_ops` / `_read_ops_file` 同在 `cd_ops` |
| `lanestate.LOG_FILE = ...`（test_webui，live 写对线记录）、`patch.object(ls, "LOG_FILE")`（test_lanestate，`review` 读） | `lanelog.LOG_FILE` | `research/lanestate.review` 要写 `lanelog.LOG_FILE`（读的是 domain 里那份）；`CACHE` 仍在 `research/lanestate` |
| `live_mod.FIX_FILE` / `live_mod.DEATH_MIN_SHOW = ...`（test_webui） | `consts.FIX_FILE` / `consts.DEATH_MIN_SHOW` | `Live.__init__` 里 `self.fix_file = consts.FIX_FILE`，`coach_tick` 里 `consts.DEATH_MIN_SHOW` |
| `patch.object(webui, "HTTPServer")`（test_webui） | `app.webui.HTTPServer` | `webui` 整体搬，照旧 `from ..core.log import HTTPServer`（清单标了 `imported`） |
| `coach.items_const()`（test_itembuild 直接调） | `assets.items_const()` | 只改模块路径 |

`tests/test_moves.py` 会拦下这些写法（ast 检查，不 import）：

- 名字搬走以后，旧文件里还以任何形式绑定它（自己定义、`from 新模块 import 名字`、`名字 = 新模块.名字`）；已经有名字搬走的旧文件里出现 `import *` 或模块级 `__getattr__`。
- 同一个旧模块拆出来的兄弟文件里多定义一份（例如 `app/update.py` 里又写 `ICON_DIR = ...`）；测试会 patch / 赋值的名字，兄弟文件连 `import` 都不许绑定（例如 `app/update.py` 里 `from ..sources.assets import ICON_DIR`）。
- `app/live/` 下除了 `consts.py`，任何文件绑定 `consts` 的名字（`from .consts import FIX_FILE`）或 `import *`。
- `Live.方法名` 只认定义在 `Live` 或 `app/live/core.py` 里 `class Live(...)` 基类中的方法；同一个方法在两个 mixin 里各有一份也会报。
- `imported` 只许 `data.DATA_DIR`、`webui.HTTPServer` 两条（基线白名单写死在测试里）。
- `moves.json` 只追加不删：前 770 条的（旧模块, 名字）摘要固定。`--write-moves` 在现有表上合并：已有条目保留，新名字追加到末尾，没搬的名字跟着规则改去处。

## 各新模块的依赖（按当前代码）

| 新模块 | 层 | import 的新模块（按当前代码，含函数内延迟 import） |
|---|---|---|
| `ai.gameplan` | ai | `ai.llm`、`core.config`、`kb` |
| `app.demo` | app | `app.live.core`、`app.webui`、`core.config` |
| `app.live.cards` | app | `ai.llm`、`app.live.consts`、`app.live.core`、`app.live.draft`、`domain.brief`、`domain.engine` |
| `app.live.consts` | app | `ai.llm`、`core.config`、`core.dota_install`、`domain.brief`、`game.hotkeys` |
| `app.live.core` | app | `ai.gameplan`、`app.live.cards`、`app.live.consts`、`app.live.draft`、`app.live.ingame`、`app.live.mates`、`app.live.overlay`、`app.live.settings`、`app.live.timers`、`core.config`、`core.dota_install`、`core.heroes`、`core.log`、`domain.advisor`、`domain.brief`、`domain.buyback`、`domain.coach`、`domain.engine`、`domain.jungle`、`domain.mates`、`domain.ultcd`、`game.gsi`、`game.speech`、`vision.buybackscan`、`vision.capture`、`vision.grid`、`vision.recognize`、`vision.roles` |
| `app.live.draft` | app | `app.live.consts`、`app.live.core`、`app.live.ingame`、`app.live.timers`、`core.gsi_fields`、`core.heroes`、`domain.advisor`、`domain.engine`、`vision.grid`、`vision.recognize` |
| `app.live.ingame` | app | `app.live.cards`、`app.live.consts`、`app.live.core`、`app.live.draft`、`app.live.timers`、`core.gsi_fields`、`domain.brief`、`domain.coach`、`domain.death`、`domain.itembuild`、`domain.jungle`、`domain.lanelog`、`kb`、`vision.recognize` |
| `app.live.mates` | app | `app.live.core`、`domain.mates`、`sources.http` |
| `app.live.overlay` | app | `app.live.cards`、`app.live.consts`、`app.live.draft`、`app.live.ingame`、`core.config` |
| `app.live.settings` | app | `ai.llm`、`app.live.cards`、`app.live.consts`、`app.live.core`、`app.live.draft`、`app.live.overlay`、`core.config`、`core.dota_install`、`game.gsi`、`game.hotkeys` |
| `app.live.timers` | app | `app.live.consts`、`app.live.core`、`app.live.draft`、`domain.buyback`、`domain.coach`、`domain.ultcd`、`kb`、`kb.cd_ops`、`sources.assets`、`vision.buybackscan` |
| `app.start` | app | `ai.llm`、`app.update`、`app.webui`、`core.config`、`core.dota_install`、`game.gsi`、`sources.assets`、`sources.player`、`sources.stats`、`vision.gamefiles` |
| `app.update` | app | `core.config`、`core.heroes`、`domain.engine`、`kb`、`kb.cd_ops`、`sources.assets`、`sources.http`、`sources.player`、`sources.stats` |
| `app.webui` | app | `app.live.core`、`core.config`、`core.log`、`sources.assets` |
| `cli` | cli | `app.demo`、`app.start`、`app.update`、`app.webui`、`core.config`、`core.log`、`domain.advisor`、`domain.brief`、`domain.itembuild`、`domain.lanes`、`game.gsi`、`game.speech`、`kb`、`research.aibacktest`、`research.backtest`、`research.lanestate`、`sources.assets`、`sources.player`、`sources.stats`、`vision.calibrate`、`vision.recognize` |
| `core.dota_install` | core | `core.config` |
| `core.log` | core | `core.config` |
| `domain.advisor` | domain | `core.config`、`core.heroes`、`domain.engine`、`domain.explain`、`domain.lanes`、`kb`、`sources.assets`、`sources.player`、`sources.stats` |
| `domain.brief` | domain | `ai.gameplan`、`core.config`、`domain.coach`、`domain.engine`、`domain.itembuild`、`domain.lanes`、`domain.teamfight`、`kb`、`sources.http`、`sources.stats` |
| `domain.coach` | domain | `core.config`、`domain.itembuild`、`kb`、`sources.assets`、`sources.http` |
| `domain.death` | domain | `core.config`、`core.gsi_fields` |
| `domain.engine` | domain | `core.heroes` |
| `domain.explain` | domain | `kb` |
| `domain.itembuild` | domain | `domain.coach`、`domain.death`、`kb`、`sources.assets`、`sources.stats` |
| `domain.lanelog` | domain | `core.config` |
| `domain.lanes` | domain | `domain.death`、`domain.engine`、`domain.explain`、`kb` |
| `domain.mates` | domain | `core.config`、`sources.http`、`sources.player` |
| `domain.teamfight` | domain | `domain.death`、`domain.explain`、`domain.lanes`、`kb` |
| `domain.ultcd` | domain | `kb`、`kb.cd_ops` |
| `game.gsi` | game | `core.config`、`core.dota_install`、`core.gsi_fields`、`core.log` |
| `kb` | kb | `core.config`、`kb.cd_ops` |
| `kb.cd_ops` | kb | `core.dota_install`、`core.vpk`、`kb` |
| `research.aibacktest` | research | `ai.llm`、`core.config`、`domain.engine`、`research.backtest`、`sources.assets`、`sources.stats` |
| `research.backtest` | research | `core.config`、`domain.engine`、`sources.assets`、`sources.http`、`sources.stats` |
| `research.lanestate` | research | `core.config`、`domain.lanelog`、`research.backtest`、`sources.http`、`sources.stats` |
| `sources.assets` | sources | `core.config`、`core.heroes`、`sources.http` |
| `sources.http` | sources | `core.config` |
| `sources.player` | sources | `core.config`、`sources.http`、`sources.stats` |
| `sources.stats` | sources | `core.config`、`sources.http` |
| `vision.buybackscan` | vision | `core.config`、`vision.recognize` |
| `vision.calibrate` | vision | `vision.recognize` |
| `vision.gamefiles` | vision | `core.config`、`core.vpk` |
| `vision.grid` | vision | `core.config`、`vision.gamefiles` |
| `vision.recognize` | vision | `core.config` |
| `vision.roles` | vision | `vision.recognize` |

### 往上层的 import（逐条）

（无）

## live.py 方法分组

`Live` 实际有 105 个方法（PLAN 第 1 节写的 127 个不准，以 ast 数的为准），模块级名字 31 个：`Live`、`log` 在 `core`，其余 29 个（常量 + `_side_cn` `_names` `_slot_text` `_check_hotkeys` `_check_dota_dir` `_colorful` `_view_text` `plan_error` `_next_stage` `_mask`）在 `consts`。

和 PLAN P4 表不一样 / 表里没写的：

- `_check_hotkeys`、`_check_dota_dir`：PLAN 表写在 `settings.py`，但它们是**模块级函数**不是方法，按「模块级小函数一律 consts」放 `consts`（`save_settings` 用 `consts._check_hotkeys(...)`）。
- `side`、`_hero`、`heroes` → `core`：`side` 被 draft / cards / timers 用，`_hero` 被 draft / timers / core（`action`、`snapshot`）用，是各组共用的小工具；`heroes` 是给 webui 的英雄列表。
- `save_shot` → `core`（拿不准，放 core）：表里没写；只被分发入口调（`action` 的 `shot`、截图热键），哪个阶段都能用，不属于选人；它调的 `grab` 在 draft（mixin 之间走 `self`，不影响）。
- `_pick_view` → `draft`：顶栏帮认的候选（调 `pick_candidates`），`slots_view`（draft）和 `buyback_view`（timers）都用，按含义放帮认那边。
- `show_notice` → `draft`：没人内部调（`demo.py` 调），盖住选人推荐、下一次 `refresh` 换回，放 draft。
- `_close_card` → `overlay`：被 `toggle_overlay` / `close_overlay` / `action` 调，关叠加层上的卡片。
- `_knowledge`、`_my_slot`、`_game_clock`、`_slot_name` → `timers`：主要调用方都在 timers（`_my_slot` 另被 `slots_view` 调一次）。

拿不准、但按 PLAN 表保留的（调用图上「主要调用方在别组」或「多组共用」）：

- `current_draft`（draft）：被 ingame（`_lane_log`、`_start_coach`）、cards（`_start_brief`、`explain_pick`）调得比 draft 多；但它就是「格子 → 局面」，PLAN 明确放 draft，保留。
- `grab`（draft）：截一帧，draft / timers（`_scan_buyback`）/ ingame（`_take_death_shots`）/ core（`save_shot`）都用；PLAN 放 draft，保留。若执行时觉得别扭可以挪 core（不影响行为）。
- `mute_cue`（ingame）：被 `close_overlay` / `toggle_overlay`（overlay）和 `action` 调；但它关的是野区提醒（`jungle_cue`），PLAN 放 ingame，保留。
- `_showing_brief`（cards）：cards 3 次、ingame 2 次、overlay 1 次，本局要点的状态判断，放 cards。
- `tick`（draft）、`coach_tick`（ingame）：每秒的分发入口（`_loop` → `tick` → `coach_tick` → 买活 / 要点 / 死亡…），按 PLAN 放。

统计（分发入口 = `__init__` `start` `_loop` `action` `snapshot` `publish` `_start_hotkeys` `tick` `coach_tick` `overlay_view`，被它们调不算「属于哪组」）：

| 分组 | 方法数 |
|---|---|
| core | 17 |
| draft | 26 |
| ingame | 10 |
| timers | 22 |
| cards | 12 |
| overlay | 12 |
| settings | 4 |
| mates | 2 |
| consts（模块级常量 / 函数） | 29 |

| 方法 | 放在 | 被谁调（组: 次数） | 调谁（组: 次数） | 备注 |
|---|---|---|---|---|
| `__init__` | core | - | settings:1、core:1、overlay:1 | 分发入口 |
| `start` | core | - | draft:1、core:1 | 分发入口 |
| `submit` | core | cards:4、draft:2、timers:2、settings:1、core:1、ingame:1、mates:1 | - |  |
| `_loop` | core | core:1 | draft:1、core:1 | 分发入口 |
| `_run` | core | core:1 | core:1 |  |
| `subscribe` | core | - | - |  |
| `unsubscribe` | core | - | - |  |
| `publish` | core | core:1 | core:1 | 分发入口 |
| `current_snapshot` | core | - | - |  |
| `action` | core | - | draft:7、overlay:7、timers:3、cards:3、core:2、ingame:1、mates:1、settings:1 | 分发入口 |
| `snapshot` | core | core:1 | overlay:3、core:2、settings:1、timers:1 | 分发入口 |
| `status` | core | core:1 | - |  |
| `close` | core | - | - |  |
| `heroes` | core | - | - |  |
| `side` | core | draft:4、cards:2、timers:1 | - |  |
| `_hero` | core | draft:7、timers:5、core:2 | - |  |
| `save_shot` | core | settings:1、core:1 | draft:1 |  |
| `current_draft` | draft | ingame:2、cards:2、draft:1 | core:2 | 主要调用方在别组（不算分发入口） |
| `refresh` | draft | draft:6、core:2 | core:1、draft:1 |  |
| `draft_view` | draft | overlay:1 | draft:1 |  |
| `slots_view` | draft | draft:1 | draft:2、core:2、timers:1 |  |
| `_looks_picked` | draft | draft:1 | - |  |
| `clear` | draft | core:1 | draft:1 |  |
| `show_notice` | draft | - | - |  |
| `should_scan` | draft | draft:1 | - |  |
| `should_scan_bans` | draft | draft:1 | - |  |
| `tick` | draft | core:1 | draft:7、core:1、ingame:1 | 分发入口 |
| `scan_bans` | draft | draft:1 | draft:3、core:1 |  |
| `scan_role` | draft | draft:1 | draft:1、core:1 |  |
| `_role_ready` | draft | draft:1 | draft:1 |  |
| `_grid_ready` | draft | draft:1 | - |  |
| `_ids` | draft | draft:1 | - |  |
| `grab` | draft | draft:2、timers:1、ingame:1、core:1 | - | 多组共用 |
| `scan` | draft | settings:1、core:1、draft:1 | draft:6、core:1 |  |
| `_keep_crops` | draft | draft:1 | - |  |
| `learn_slot` | draft | draft:2、core:1 | - |  |
| `name_slot` | draft | core:1 | draft:3、core:1 |  |
| `_record_fix` | draft | core:1、draft:1 | - |  |
| `pick_candidates` | draft | draft:1 | core:1 |  |
| `_pick_view` | draft | draft:1、timers:1 | draft:1 |  |
| `_confirm_with_grid` | draft | draft:1 | - |  |
| `fill_slots` | draft | draft:1 | core:1 |  |
| `learn` | draft | core:1 | core:1 |  |
| `coach_tick` | ingame | draft:1 | ingame:7、cards:2、timers:1 | 分发入口 |
| `_stage_tick` | ingame | ingame:1 | cards:2 |  |
| `_jungle_tick` | ingame | ingame:1 | - |  |
| `_lane_log` | ingame | ingame:1 | draft:1 |  |
| `_death_recap` | ingame | ingame:1 | ingame:1 |  |
| `_take_death_shots` | ingame | ingame:2 | draft:1 |  |
| `_start_coach` | ingame | ingame:1 | draft:1、core:1、ingame:1 |  |
| `_coach_ready` | ingame | ingame:1 | ingame:1 |  |
| `mute_cue` | ingame | overlay:2、core:1 | - | 主要调用方在别组（不算分发入口） |
| `_show_coach` | ingame | ingame:2 | - |  |
| `_buyback_tick` | timers | ingame:1 | timers:3 |  |
| `_scan_buyback` | timers | timers:1 | timers:4、core:1、draft:1 |  |
| `_keep_game_crops` | timers | timers:1 | - |  |
| `_timer_read` | timers | timers:1 | timers:1 |  |
| `_slot_name` | timers | timers:3 | - |  |
| `_revived` | timers | timers:1 | timers:1、core:1 |  |
| `_hero_icon` | timers | timers:1 | - |  |
| `_check_feed` | timers | timers:1 | timers:2、core:2 |  |
| `_feed_read` | timers | timers:1 | timers:2 |  |
| `_feed_orphans` | timers | timers:1 | core:1 |  |
| `_feed_done` | timers | timers:1 | timers:1 |  |
| `_game_clock` | timers | timers:3 | - |  |
| `_my_slot` | timers | timers:2、draft:1 | core:1 |  |
| `mark_buyback` | timers | core:1 | timers:1 |  |
| `buyback_view` | timers | core:1 | timers:3、draft:1 |  |
| `_knowledge` | timers | timers:2 | - |  |
| `_ult_of` | timers | timers:2 | core:2、timers:1 |  |
| `_ult_cd` | timers | timers:3 | - |  |
| `_ult_check` | timers | timers:2 | timers:2 |  |
| `mark_ult` | timers | core:1 | timers:2 |  |
| `set_ult` | timers | core:1 | timers:2 |  |
| `_ult_view` | timers | timers:1 | timers:2 |  |
| `_start_brief` | cards | ingame:1 | core:2、cards:1、draft:1 |  |
| `_brief_ready` | cards | cards:1 | cards:2 |  |
| `_start_plan` | cards | cards:1 | cards:1、core:1 |  |
| `_plan_ready` | cards | cards:1 | cards:2 |  |
| `_showing_brief` | cards | cards:3、ingame:2、overlay:1 | - | 多组共用 |
| `_brief_view` | cards | cards:3、ingame:1 | - |  |
| `show_brief` | cards | settings:1、core:1、cards:1 | cards:2 |  |
| `explain_pick` | cards | core:1 | core:2、cards:1、draft:1 |  |
| `_pick_ready` | cards | cards:1 | cards:1、core:1 |  |
| `_pick_plan_ready` | cards | cards:1 | - |  |
| `set_brief_page` | cards | core:1 | cards:2 |  |
| `pick_view` | cards | overlay:1 | - |  |
| `toggle_overlay` | overlay | settings:1、core:1 | overlay:1、ingame:1 |  |
| `_close_card` | overlay | overlay:2、core:1 | cards:1 |  |
| `close_overlay` | overlay | core:1 | overlay:2、ingame:1 |  |
| `toggle_lock` | overlay | settings:1、core:1 | overlay:1 |  |
| `set_locked` | overlay | overlay:2、core:1 | overlay:1 |  |
| `overlay_moved` | overlay | core:1 | overlay:1 |  |
| `_load_overlay_state` | overlay | core:1 | - |  |
| `_save_overlay_state` | overlay | overlay:2 | - |  |
| `overlay_visible` | overlay | core:1 | - |  |
| `overlay_view` | overlay | core:1 | cards:1、draft:1 | 分发入口 |
| `set_overlay_style` | overlay | core:1 | - |  |
| `overlay_style` | overlay | core:1 | - |  |
| `save_settings` | settings | core:1 | settings:2 |  |
| `_apply_dota_dir` | settings | settings:1 | - |  |
| `settings_view` | settings | core:1 | - |  |
| `_start_hotkeys` | settings | core:1、settings:1 | overlay:2、core:2、draft:1、cards:1 | 分发入口 |
| `query_mate` | mates | core:1 | mates:1、core:1 |  |
| `_mate_ready` | mates | mates:1 | - |  |

## 产品代码里看到、但没动的（只记录）

- `gsi.dump` 函数里又 `from pathlib import Path` 了一次（模块顶层已经 import），无害。
- `webui.py` 自己又定义了一份 `PORTRAIT_DIR` / `ITEM_ICON_DIR`（和 `data.py` 的同值），搬家时两份各跟各的模块走，别合并（合并算改行为）。
- `coach.items_const()` 在文件不存在时直接 `requests.get` 下载（不走 `data._get`），搬到 `sources/assets` 后照旧。
- `ultcd.log`（`logging.getLogger`）定义了但模块里没用到（`update_ops` 的 `log` 是参数）；照搬到 `domain/ultcd`。
