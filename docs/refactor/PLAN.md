# 工程化重构计划（交给执行的 agent）

> 写于 2026-10-28，基线提交 `b1113e8`（main，已推送）。写给执行重构的 AI agent。
> 先读完本文件、[CLAUDE.md](../../CLAUDE.md)、[HANDOFF.md](../HANDOFF.md) 第 2～4 节再动手。

## 0. 先读这一节

**目标**：把平铺的 40 个模块按层整理成子包，拆开 1874 行的 `live.py`，补齐工程配置（pyproject、锁版本、lint、分层检查）。

**硬要求（玩家原话）：「要保证重构以后当前版本下的所有功能都正常」。** 所以：

1. **只搬家，不改行为。** 不改算法、阈值、文案、快照 JSON 字段、网页（`web/`）、Electron（`electron/`）。
   唯一允许的可见差异：日志文件里的模块名前缀（`%(mod)s`，例如 `live` 变成 `app.live.draft`），要在 HANDOFF 记一笔。
   发现原有 bug 也不修，记到报告里，重构完再说。
2. **每一步都要过门禁（第 3 节）**，任何一关失败就停下，报告失败输出，不要「顺手改测试让它过」。
3. **已有测试的断言不许改。** 只允许改 import 路径和 `mock.patch` 的目标模块（搬家导致的）。
   如果某个断言非改不可，停下问用户。
4. **提交 / 推送**：遵守 CLAUDE.md，用户没明确说就不 commit / push。开工前先问用户：能不能在 `refactor/structure` 分支上每一步 commit（不 push）；合并回 main、推送都要用户另外点头。
   这台机器没配 git 用户：沿用历史提交的作者（`git log -1 --format='%an <%ae>'`），用 `git -c user.name=… -c user.email=… commit`，不要改全局配置。
5. **不要在 `D:\dota` 里切分支**：玩家平时在那里跑「启动助手.bat」打游戏。用 worktree（第 4 节 P0.1）。
6. 安全底线、UTF-8 / CRLF 规范、不把 `config.toml` 的 token 往外发：同 CLAUDE.md。

**不在范围内**（要做得先问用户）：拆 `kb.py` 成多个文件、把各领域模块里的联网函数抽到 sources、把 `Live` 从 mixin 改成组合、`src/` 布局、换 pytest、前端框架化。

## 1. 现状（2026-10-28 实查）

- `dota_assistant/` 下 40 个 `.py` 平铺；测试 27 个文件、**266 个用例、约 10 秒**；`node --check` 覆盖 `web/*.js`、`electron/*.js`。
- 大文件：`live.py` 1874 行（`Live` 一个类 127 个方法），`data.py` 558、`kb.py` 557、`coach.py` 548，`tests/test_webui.py` 1250。
- 往上层导入、互相导入（现在靠函数内延迟 import 绕开）：
  - `data`（联网层）→ `coach.ITEMS_CONST / items_const`、`kb`、`ultcd.pick`、`engine.Engine.fits_by_roles`（都在 `update()`、`fetch_icons()`、`positions_from_lane_roles()` 里）
  - `kb` ↔ `ultcd`（`kb._update_cd_ops` 调 `ultcd.update_ops`；`ultcd` 又 import `kb`）
  - `ultcd` → `gsi.find_dota`、`gamefiles.VPK`；`death` → `gsi.buyback_left`
  - `coach` ↔ `itembuild`（同层，保留）
  - `lanestate`（`live` 用它记对线）→ `backtest`（研究脚本）
- 路径：`config.ROOT = Path(__file__).resolve().parent.parent`（打包版是 exe 所在目录），`DATA_DIR = ROOT / "data"`，约 20 处 `from .config import DATA_DIR, ROOT`，导入时就定死。
- 测试替换模块里的东西主要有两种（**搬家时最容易出错的地方**）：
  - `mock.patch.object(data, "_get")` 等（`data` 24 处、`llm` 4、`kb` 2、`gameplan` 2、`ls`=lanestate 4、`webui` 2、`logs` 2）
  - 直接赋值：`live_mod.FIX_FILE`、`live_mod.DEATH_MIN_SHOW`、`death.DUMP_DIR`、`lanestate.LOG_FILE`、`ultcd.OPS_PATH`、`buybackscan.SAMPLES_DIR`、`mates_mod.MATES_FILE`
  - 规律：patch 只对**运行时去那个模块里查名字**的调用生效。函数搬到新模块后，patch 目标要跟着搬到「调用方查名字的那个模块」。
- `tests/test_hygiene.py` 的 `python_files()` 只 glob `dota_assistant/*.py`，**不递归**：建了子包以后子包里的文件就不查了，必须先改成递归。
- `packaging/build.py` 用 `--collect-submodules dota_assistant`（子包会自动带上）；`packaging/launcher.py` 用 `from dota_assistant.cli import main`（这个路径要保住）。
- 网页调用的 Live 动作（`Live.action` 的 `t`）：`set_slot set_bans set refresh clear rescan learn shot dismiss_coach show_brief explain_pick brief_page buyback ult name_slot ult_set toggle_overlay toggle_lock close_overlay overlay_moved save_settings mate_query mate_remove open_logs`。

## 2. 目标结构

包仍然在仓库根目录的 `dota_assistant/`，**不改成 src 布局**（不然 `ROOT`、打包、`.bat` 都要变）。

```
dota_assistant/
  __main__.py
  core/       config.py  log.py  heroes.py  dota_install.py  vpk.py  gsi_fields.py
  sources/    http.py  assets.py  stats.py  player.py
  kb/         __init__.py（原 kb.py 整体搬进来）  cd_ops.py
  ai/         llm.py  gameplan.py
  domain/     engine.py advisor.py lanes.py explain.py teamfight.py itembuild.py coach.py brief.py
              death.py jungle.py buyback.py ultcd.py lanelog.py mates.py
  vision/     capture.py  recognize.py（原 vision.py）  grid.py  gamefiles.py  roles.py  buybackscan.py  calibrate.py
  game/       gsi.py  hotkeys.py  speech.py
  research/   backtest.py  aibacktest.py  lanestate.py
  app/        live/（__init__.py  core.py  consts.py  draft.py  ingame.py  timers.py  cards.py  overlay.py  settings.py  mates.py）
              webui.py  start.py  demo.py  update.py
  cli/        __init__.py（原 cli.py，保留 main）
```

### 2.1 搬家对照表（P0 生成 `docs/refactor/moves.json`，测试按它核对）

| 原位置 | 新位置 | 说明 |
|---|---|---|
| `config.py` | `core/config.py` | `ROOT` 改成 `parents[2]`（开发版）；P0 加的 `DOTA_ASSISTANT_HOME` 保留 |
| `log.py` | `core/log.py` | `PKG = "dota_assistant"` 不变 |
| `heroes.py` | `core/heroes.py` | 纯数据模型，sources 要用 |
| `gsi.py` 里的 `find_dota` `is_dota_dir` 及 Steam 库查找 | `core/dota_install.py` | `ultcd` / `start` / `live` 都要用 |
| `gsi.py` 里的 `buyback_left`、`*_STATE` 常量等纯函数 / 常量 | `core/gsi_fields.py` | `death` 要用；不带 IO |
| `gsi.py` 其余（`GSIServer` `GameState` `ensure_cfg` `install_cfg` `dump`） | `game/gsi.py` | |
| `gamefiles.py` 里的 `VPK` 读取 | `core/vpk.py` | `kb/cd_ops` 要用 |
| `gamefiles.py` 其余（DXT 解码、导出网格头像） | `vision/gamefiles.py` | |
| `data.py`：`OPENDOTA` / `STRATZ` 常量、`APIError` `NETWORK_ERRORS` `_session` `_get` `_stratz` `_sql` `_stratz_brackets` `_say` | `sources/http.py` | **别的模块一律用 `http._get(...)` 形式调用**（模块属性），这样测试只 patch 一处 |
| `data.py`：英雄表、头像、图标、装备常量 / 图标（`fetch_heroes` `fetch_portraits` `fetch_icon` `_download_icon` `ICON_DIR` `ICONS_DONE` `fetch_items_const` `fetch_item_icons` `load_heroes`）+ `coach.ITEMS_CONST` `coach.items_const` | `sources/assets.py` | `coach` 改成从这里取 |
| `data.py`：克制统计 / 分位置（`fetch_stats` `stats_path` `load_stats` `fetch_positions` `positions_path` `positions_source` `load_positions` `OPENDOTA_POSITIONS` `POSITION_MIN_MINUTES` 等） | `sources/stats.py` | |
| `data.py`：个人数据（`fetch_player` `update_player` `load_player` `player_history` `_player_matches_*` `_main_position` `rank_name`） | `sources/player.py` | |
| `data.py`：`update()` `fetch_icons()` `positions_from_lane_roles()` `fetch_positions_opendota()` | `app/update.py` | 编排：要用 kb / ultcd / engine，只能放上层 |
| `kb.py` | `kb/__init__.py` | 整体搬，不拆 |
| `ultcd.py`：游戏文件 / 镜像读冷却符号（`OPS_PATH` `HERO_FILE` `MIRROR` `parse_kv` `_find` `cd_ops` `_reader` `update_ops` `_read_ops_file` `load_ops`） | `kb/cd_ops.py` | 断开 kb ↔ ultcd |
| `ultcd.py` 其余（`pick` `cooldown` `mods` `Mod` `parse_op` `Tracker` …） | `domain/ultcd.py` | |
| `llm.py` `gameplan.py` | `ai/` | |
| `engine advisor lanes explain teamfight itembuild coach brief death jungle buyback mates` | `domain/` | 原样搬 |
| `lanestate.py`：`LOG_FILE` `LOG_MINUTES` `log_moment` 及 `live` 用到的函数 | `domain/lanelog.py` | `live` 只依赖这部分 |
| `lanestate.py` 其余（回测、`run`、`review`） | `research/lanestate.py` | |
| `backtest.py` `aibacktest.py` | `research/` | |
| `capture vision grid roles buybackscan calibrate` | `vision/`（`vision.py` → `vision/recognize.py`） | |
| `hotkeys.py` `speech.py` | `game/` | |
| `live.py` | `app/live/`（见 P4） | |
| `webui.py` `start.py` `demo.py` | `app/` | |
| `cli.py` | `cli/__init__.py` | `dota_assistant.cli.main` 路径不变 |

表是按今天读代码定的。**执行时以实际依赖为准**：某个名字按表放会造成往上层导入，就改放法，同时改表、在报告里说明。

### 2.2 分层规则（P2 起由 `tests/test_layers.py` 强制）

从低到高：`core` → `sources` → `kb` → `ai` → `domain` → `vision` / `game`（两者平级）→ `research` → `app` → `cli`。

- 只能 import 同层或更低层。`__main__` 只 import `cli`。
- 同层互相 import 允许（例如 `coach` ↔ `itembuild` 的延迟 import 保留）。
- 例外写在 `test_layers.py` 的白名单里，每条带理由；新增例外要在报告里说明。
- 不留「转发旧路径」的空壳模块（`data.py` 里写 `from .sources.http import *` 那种）：空壳会让 `mock.patch` 打到错的地方，测试看着通过、其实没替换。

## 3. 门禁：每一步都要过

「一步」= 搬一个模块（或一组紧耦合的模块）+ 改完所有 import / patch 目标。每步跑 G1～G5，阶段结束再跑 G6。

| 编号 | 内容 | 通过标准 |
|---|---|---|
| G1 编译 | `python -X utf8 -m compileall -q dota_assistant tests packaging`；`node --check` 每个 `web/*.js`、`electron/*.js` | 无输出、退出码 0 |
| G2 单测 | `python -m unittest` | 全过；用例数 ≥ 上一步（基线 266 + P0 新加的）；`git diff` 里已有测试只有 import / patch 目标变化 |
| G3 行为快照 | `python tools/golden/run.py check`（P0.4 建） | CLI 快照 + Live 回放快照**逐字节一致** |
| G4 不碰真实目录 | 单测和 G3 跑前后对比 `data/` `shots/` `logs/` `templates/` `config.toml` `calibration.json` 的文件列表 + 修改时间（P0.3 建） | 完全没变 |
| G5 冒烟 | `tests/test_smoke.py`（P0.5 建）：import 每个模块；每个子命令 `--help`；`suggest` 一组 | 全过 |
| G6 阶段验收 | 慢快照（`run.py check --slow`：`backtest` `lanes-test` `lane-test` 用缓存的对局）+ `run --browser` 无头打开 panel / overlay / topbar 无控制台报错 + 阶段要求的真鼠标项 | 全过 |

**失败处理**：停下，`git diff` 回到上一个通过的点，报告哪一关、完整输出、你的判断。不要连着试多种改法。

## 4. 阶段

### P0 安全网（不改结构，只加测试 / 工具；做完要用户看一眼再往下）

**P0.1 工作区**
- `git worktree add D:\dota-refactor -b refactor/structure b1113e8`。
- 拷进 worktree（都不进 git）：`config.toml`、`data/`（只读用的副本）。`electron/node_modules` 用 `mklink /J` 连到 `D:\dota\electron\node_modules`。
- 在 worktree 里跑 G1、G2，记下基线用例数（应为 266；有因为缺数据跳过的，记下名字）。

**P0.2 可指定运行目录**（P0 里唯一的产品代码改动，单独一步）
- `config.py`：环境变量 `DOTA_ASSISTANT_HOME` 有值时，`ROOT` 用它，否则和现在完全一样（含打包版 `sys.frozen`）。
- 新测试 `tests/test_paths.py`：没设变量时 `ROOT` 是仓库根（里面有 `config.example.toml` 和 `dota_assistant/`）；设了以后 `ROOT` / `DATA_DIR` 跟着变（子进程里验，避免污染当前进程）；模拟 `sys.frozen` 时是 exe 所在目录。
- 这个测试在 P1 搬 `config.py` 时还要过（防 `parents[2]` 写错）。

**P0.3 测试不碰真实目录**：`tests/tools/sentinel.py` 记录第 3 节 G4 列的路径的（相对路径, 大小, mtime）；`tools/golden/run.py` 和一个包装命令 `python tools/check_isolation.py` 在单测前后各记一次并比较。今天就要跑一次：有测试会写真实目录的话，**先报告，不要自己改测试**（那是现有问题）。

**P0.4 行为快照（核心）** `tools/golden/`：
- **冻结的运行目录**：`D:\dota-golden\home\`（不进 git，里面有个人数据）：`config.toml`（`stratz_token`、`ai_api_key` 清空，`ai_plan = false`、`voice = false`）、`config.example.toml`、`knowledge/`、`data/`、`calibration.json`、选用的截图（`shots/` 里 `20261023-014333/014335/014347`、`20261022-215058/220708` 等 14 张选人截图、`shots/deaths/` 全部 json、`shots/buyback/feed/` 全部）。打包成 `home.zip` 留底；每次检查先解压到临时目录，再把所有文件 mtime 设成当前时间（缓存按 mtime 判断新旧，这样不会过期去联网）。
- **网络录放**：runner 在进程里包住 `requests.Session.request`、`requests.get` / `post`：`record` 模式真联网并把（方法, URL, 参数, 请求体）→ 响应存到 `D:\dota-golden\cassettes\`；`check` 模式只从这里取，取不到直接抛错（会体现在快照里）。录完 `grep` 一遍确认 cassette 里没有 token。
- **CLI 快照**：进程内调 `dota_assistant.cli.main(argv)`，记 stdout / stderr / 退出码。至少：
  - `suggest`：`--enemy PA,莱恩,风行者 --ally 敌法师,冰女 --pos 3`；`--enemy 末日,沉默 --pos 1 --me 卓尔 --side radiant`；`--pos 1`（空阵容）；`--pos 5 --ban 小小,斧王`；`--enemy 敌法师,军团 --pos 2 --top 6`；`--all-roles --pos 4`；`--ally 敌法师,冰女 --enemy PA,莱恩 --pos 1`；`--me 莱恩 --pos 5 --enemy 半人马,风行者 --side dire`；`--me 斧王 --pos 3 --enemy 敌法师,水晶室女`；共 ≥10 组
  - `kb hero 小黑` `kb hero 末日` `kb item BKB` `kb item 跳刀` `kb find 克敌机先` `kb mistake`
  - `recognize <每张选人截图>`
  - `brief-sim 9055590694`、`coach-sim 9054096051`、`death-sim`（全部存下的死亡推送）、`lane-review`（走 cassette）
  - 每个子命令 `--help`
- **Live 回放快照**：`tools/golden/replay_live.py`，参照 `tests/test_webui.py` 的做法造 `Live(use_gsi=False, use_hotkeys=False, screen_factory=假屏幕, coach_prepare=…)`，在工作线程里按顺序推：选人阶段（截图帧 → 识别、网格禁用、自动认位置）→ 帮认 `name_slot` / `pick_candidates` → 策略时间本局要点 → 号角 → 10:00 / 30:00 分阶段弹 → 局内提醒 → 死亡推送序列（`shots/deaths/*.json`）→ 复活 → 买活（自己 GSI、`mark_buyback`、消息栏帧）→ 大招 `ult` / `ult_set` → 叠加层 `toggle_overlay` / `toggle_lock` / `close_overlay` / `overlay_moved` / `set` 样式 → `save_settings`（写临时 config）→ `mate_query`（cassette）。**第 1 节列的每个网页动作至少走一次**。每步后记 `snapshot()`，`json.dumps(sort_keys=True)`。
  - 时间相关字段（本机时间锚点、「几分钟前」、日志时间）用正则归一化；后台线程等它完成再记，保证顺序固定。
- **确定性检查**（P0 的退出标准之一）：`record` 一次，`check` 连跑两次都一致；隔一天再 `check` 一次也一致。
- 慢快照（`--slow`）：`backtest --matches 300`、`lanes-test --matches 300`、`lane-test --matches 300`（用 `data/backtest_matches.json` 缓存；`--matches` 小了会不会重新抽样要先看代码，会的话用默认值）。

**P0.5 冒烟 + 接口清单** `tests/test_smoke.py`：
- import 每个模块（按 `pkgutil.walk_packages` 递归）。
- 每个子命令 `--help` 退出码 0（子进程跑 `python -m dota_assistant <cmd> --help`）。
- `Live` 公开方法清单快照：方法名 + 参数签名存成 `tests/data/live_api.json`，P4 拆完必须一样。
- 网页动作清单：从 `web/*.js` `web/*.html` 里正则抽 `act('…')` / `run('…')` / `send('…')` 的动作名，断言每个都在 `Live.action` 能处理的集合里（处理不了的会返回错误 / 记日志，按代码实际行为判断）。

**P0.6 先改 hygiene**：`tests/test_hygiene.py` 的 `python_files()` 改成递归（`rglob`），排除 `__pycache__`，目录加上 `tools`（P0 新建的脚本也要守 `encoding="utf-8"` 规矩）。除了多查 `tools/` 以外现在等价，单独一步。

**P0.7 搬家清单**：脚本导出每个现有模块的公开名字（不以 `_` 开头的 + 第 1 节里测试 patch 的私有名）到 `docs/refactor/moves.json`，按 2.1 节填好新位置。`tests/test_moves.py`：表里每个（新模块, 名字）都存在；搬完以后（P4 结束）旧模块文件都不存在了。

**P0 退出标准**：G1～G5 全过；用例数 = 266 + 新加的；快照确定性检查通过；G4 基线报告给用户（有没有测试写真实目录）。

### P1 core 层

步骤（每条一步，每步 G1～G5）：
1. 建 `core/`，搬 `config.py`（`ROOT` 改 `parents[2]`），全仓改 import。`test_paths` 必须过。
2. 搬 `log.py`、`heroes.py`。
3. 从 `gsi.py` 拆出 `core/dota_install.py`、`core/gsi_fields.py`；从 `gamefiles.py` 拆出 `core/vpk.py`。

新测试：`tests/test_patch_targets.py`：用 `ast` 扫测试里所有 `mock.patch.object(模块, "名字")` 和 `模块.大写名 = …`，断言名字定义在这个模块自己的源码里（不是 import 进来的），函数的 `__module__` 就是这个模块。从这一步起一直开着，防止 patch 打空。

### P2 sources + kb

1. 建 `sources/http.py`，搬网络基础函数；`data.py` 里剩下的先留着，调用改成 `http.xxx`。测试 `patch.object(data, "_get")` 改成 `patch.object(http, "_get")`。
2. 依次拆 `sources/assets.py`、`stats.py`、`player.py`、`app/update.py`，最后删 `data.py`。
3. `kb.py` → `kb/__init__.py`；`ultcd` 的游戏文件部分 → `kb/cd_ops.py`（`ultcd.OPS_PATH` 的测试赋值改到 `cd_ops`）。

新测试：`tests/test_layers.py` 上线（2.2 节规则，先只检查已经搬好的子包，之后每阶段放开一层）。
阶段末加跑：`update` 命令用 cassette 跑一次（`record` 时录过），输出和 P0 快照一致。

### P3 ai / domain / vision / game / research

纯搬家，按 2.1 节。顺序：`ai` → `domain`（一次一两个模块）→ `vision` → `game` → `research`（含 `lanestate` 拆成 `domain/lanelog.py` + `research/lanestate.py`）。
阶段末：`test_layers.py` 覆盖到除 `app` / `cli` 以外所有层；跑 G6 的慢快照。

### P4 app + cli（风险最高）

1. `webui.py` `start.py` `demo.py` 搬进 `app/`。
2. `live.py` → `app/live/`，**用 mixin 拆，不改成组合**（属性还都在 `self` 上，测试里 `live.slots`、`live._crops`、`live.hints` 这类访问不用改）：

   | 文件 | 放什么（按 `live.py` 现在的方法分组） |
   |---|---|
   | `core.py` | `Live` 类本体：`__init__`、工作线程（`start` `submit` `_loop` `_run`）、订阅 / 推送（`subscribe` `publish` `current_snapshot` `snapshot` `status`）、`action` 分发、`close`；`class Live(DraftMixin, InGameMixin, TimersMixin, CardsMixin, OverlayMixin, SettingsMixin, MatesMixin)` |
   | `consts.py` | 模块级常量（`OVERLAY_FILE` `FIX_FILE` `DEATH_MIN_SHOW` `HOTKEY_ACTIONS` …）和模块级小函数（`_side_cn` `_names` `_mask` `plan_error` …）。各 mixin 用 `consts.NAME` 访问，**不要 `from .consts import NAME`**，测试统一 patch `consts` |
   | `draft.py` | 选人：`current_draft` `refresh` `draft_view` `slots_view` `scan*` `tick` `grab` `fill_slots` `learn*` `name_slot` `pick_candidates` `_confirm_with_grid` `_role_ready` `_grid_ready` … |
   | `ingame.py` | `coach_tick` `_stage_tick` `_jungle_tick` `_lane_log` `_death_recap` `_take_death_shots` `_start_coach` `_coach_ready` `mute_cue` `_show_coach` |
   | `timers.py` | 买活 / 大招：`_buyback_tick` `_scan_buyback` `_check_feed` `_feed_*` `mark_buyback` `buyback_view` `_ult_*` `mark_ult` `set_ult` `_revived` `_hero_icon` … |
   | `cards.py` | 本局要点 / 如果选它：`_start_brief` `_brief_ready` `_start_plan` `_plan_ready` `show_brief` `explain_pick` `_pick_*` `set_brief_page` `pick_view` |
   | `overlay.py` | `toggle_overlay` `close_overlay` `toggle_lock` `set_locked` `overlay_moved` `_load/_save_overlay_state` `overlay_visible` `overlay_view` `set_overlay_style` `overlay_style` |
   | `settings.py` | `save_settings` `settings_view` `_apply_dota_dir` `_start_hotkeys` `_check_hotkeys` `_check_dota_dir` |
   | `mates.py` | `query_mate` `_mate_ready` |

   方法体一个字不改，只是挪文件；`__init__.py` 只 `from .core import Live`（外加测试要用的名字）。分组以实际调用关系为准，挪不顺就留在 `core.py`。
3. `cli.py` → `cli/__init__.py`，延迟 import 的路径全部改新路径。按命令拆文件可选，拆了每个子命令都要在快照里。
4. 删所有旧模块文件，`test_moves.py` 的「旧文件都不存在」打开。

新 / 加强的测试：`live_api.json` 必须一字不差；网页动作清单测试；`test_layers.py` 覆盖全部层。

**阶段末真鼠标（必做，见第 5 节清单）**：叠加层、顶栏、控制面板各项。

### P5 工程配置 + 文档

1. `pyproject.toml`：项目元数据、Python ≥ 3.11、依赖（和 `requirements.txt` 一致）、`[project.scripts] dota-assistant = "dota_assistant.cli:main"`、ruff 配置。`requirements.txt` 保留（`.bat` 和打包在用），另加锁版本文件 `requirements.lock`（`pip freeze` 当前能跑的版本）。
2. ruff：只开不改行为的规则（未使用 import、未定义名字、语法类）。`ruff check` 进门禁 G1。**不要跑 `ruff format`**（大面积改格式，审不过来）。
3. 可选（问用户）：GitHub Actions 用 `windows-latest` 跑 G1 / G2（G3 依赖本机个人数据，CI 跑不了）。
4. 更新 `CLAUDE.md`（门禁命令加上 G3～G5、模块路径）、`README.md`、`CONTRIBUTING.md`、`HANDOFF.md` 第 3 / 4 节（架构、模块地图换新路径）。
   HANDOFF 拆分（`docs/design/`、`docs/validation/`）是可选项，要做就保证内容不丢：脚本核对原文每一段都出现在某个新文件里。

验证：`pip install -e .` 后 `dota-assistant --help`；`.bat` 用 cmd 真跑一次（CLAUDE.md 要求）。

### P6 打包 + 全量验收

1. `python packaging/build.py`，拿产物 exe 跑：`suggest`、`kb hero 小黑`、`start`（Electron 三个窗口起来、日志无报错）。
2. 第 5 节真鼠标清单再走一遍。
3. `demo`（Electron）完整跑一遍：选人 → 策略时间 → 局内提醒 → 结束。
4. 报告给用户，请玩家打一局天梯验收（游戏里的 GSI、截图识别、买活自动记只能这样验）。合并 main、推送等用户点头。

## 5. 真鼠标验收清单（P4 末、P6 各一次）

工具：[uitest/](uitest/)（2026-10-28 提交前真跑过的脚本，基本可用，有几处写死要改：`serve.py` 里的 `D:/dota` 路径、它 import 的是**重构前**的模块路径，P4 以后要跟着改；开发机单屏 1920×1080、缩放 1）。
- `serve.py <端口> <临时目录>`：起一个 Live（选人阶段假 GSI + 10-23 截图裁图，帮认 / 队友战绩写临时目录），往 `<临时目录>/ctl.txt` 追加 `game` 切到局内并空出夜魇第 3 格。
- 起 Electron：`electron/node_modules/electron/dist/electron.exe electron --server=<地址> --remote-debugging-port=9334`。**VS Code 终端里有 `ELECTRON_RUN_AS_NODE=1`，要先去掉**（否则主进程报 `ipcMain` 是 undefined）。
- `cdp.mjs <页面关键字> <表达式>`：只读页面状态 / 元素位置。`mouse.py click X Y`：SendInput 真鼠标，按下前确认那一点是 `electron.exe` 的窗口（不是就不点）。`mouse.py type`：用 KEYEVENTF_UNICODE 打字（普通虚拟键码会被中文输入法吃掉）。
- `flow.mjs`：顶栏帮认整条（读位置和点击在一个进程里做：菜单 10 秒没人点会自己收起，分开跑太慢会碰上）。
- `front.py 控制面板`：把面板还原并提到最前（面板常被最小化或压在 VS Code 后面）；`panelclick.mjs` 现读位置马上点。**面板客户区原点 398,157 是写死的，要改成用 `front.py` 打印的值。** 用脚本强拉窗口后立刻点，按下和抬起之间窗口位置会跳（按下时 clientY 偏 26），要等 1 秒再点。
- `close.mjs`：用调试端口让 Electron 退出（2026-10-28 在 Claude Code 里 `Stop-Process` 被权限拦过，这个不用杀进程）。后台起的服务用任务管理工具停掉。
- 真鼠标会动玩家的光标，**跑之前告诉用户别碰鼠标键盘**；打字只在确认前台是 Electron 以后再打，不然字会打进别的窗口。

| 项 | 怎么点 | 通过标准 |
|---|---|---|
| 叠加层空白处 | 光标移到卡片外 | 光标下不是 Electron（点穿） |
| 叠加层帮认 | 点「?」→ 属性 → 英雄 | 格子填上、菜单收起、`fixes.jsonl` 多一行、原菜单位置恢复点穿 |
| 叠加层锁 / × / 拖动 / 拉宽 | 照 HANDOFF 第 14、22.1 节 | 解锁 → 拖动后位置保存 → 固定后卡片中间点穿；× 收起、F8 打开 |
| 剩余位置展开 / 要点翻页 / 展开另 N 条 | 点按钮 | 有反应（这几样都在 `reportHitRects` 列表里） |
| 顶栏买活小条 | 左键开始 / 再点取消，右键「认错了」菜单 | 倒计时 / 取消 / 菜单都对 |
| 顶栏大招小条 | 左键计时、右键等级 / 减冷却菜单 | 同上 |
| 顶栏帮认 | 「?」→ 力量 → 最后一行英雄 | 菜单底边 < 窗口高 410，填上、「?」消失、收起后点穿 |
| 控制面板队友战绩 | 输入框打字 → 查询 → 刷新 → × | 卡片出现 → 「查询中…」→「刚刚查的」→ 删掉 |
| 控制面板格子 / 位置 / 清空 / 设置保存 | 点 | 和改之前一样（设置保存先把 `config.toml` 备份，测完还原） |

每一项写进报告：点了什么、看到什么；做不到的写「没验证」和原因。

## 6. 风险和对策

| 风险 | 对策 |
|---|---|
| `mock.patch` 打到旧位置或转发模块，测试假通过 | 不留转发模块；`test_patch_targets.py`；跨模块共享的函数一律用 `模块.函数` 形式调用 |
| `config.py` 搬进 `core/` 后 `ROOT` 少一层 parent | `test_paths.py`（P0.2 建，P1 必过）；G3 快照里任何「找不到 data」都会体现 |
| 延迟 import（`cli` 按命令、`data` / `kb` 函数里）路径没改，平时不报错 | `test_smoke.py` 跑每个子命令的 `--help` + 快照覆盖每个命令；`grep -rn "from \.\w* import" dota_assistant` 逐个核对 |
| 子包里的文件逃过 hygiene 检查 | P0.6 先改成递归 |
| 打包版漏模块 | `--collect-submodules` 会带子包；P6 真跑 exe |
| 拆 `Live` 时行为变了 | 只用 mixin、方法体不动；`live_api.json` + 网页动作清单 + Live 回放快照 |
| 玩家在重构期间打游戏用的是半成品 | worktree，`D:\dota` 不切分支 |
| 快照里有个人数据 / token | 快照和 cassette 放 `D:\dota-golden\`，不进 git；录完 grep token |
| 快照不稳定（时间、缓存过期、线程顺序） | P0 确定性检查（连跑两次 + 隔天）不过就不往下做 |

## 7. 报告格式（每阶段结束、遇到失败时）

1. 做了哪几步（对应本文件编号），每步的 G1～G5 结果（用例数、快照是否一致）。
2. 搬家对照表有没有改、为什么。
3. 新增的分层例外及理由。
4. 发现但没修的原有问题。
5. 真鼠标项：点了什么、结果；没验证的写明。
6. 下一阶段要用户决定的事。
