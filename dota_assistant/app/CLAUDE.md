[根目录](../../CLAUDE.md) > dota_assistant > **app**

# app：把下面各层串起来的编排

## 职责

把 `core` ~ `research` 串成能跑的程序：本机网页服务 + 启动 Electron、一键启动、演示、全量更新数据、更新助手本身。实时主控 `Live` 在子包 [`app/live/`](live/CLAUDE.md)，单独一份文档。

层号 7，能 import 除 `cli` 以外的所有层。

## 关键文件

| 文件 | 管什么 |
|---|---|
| `live/` | 实时主控 `Live`（类本体 + 六个 mixin + consts），见 [live/CLAUDE.md](live/CLAUDE.md) |
| `webui.py` | 本机 HTTP 服务 `WebServer`（只听 127.0.0.1 随机端口）+ 启动 Electron（`find_electron` / `electron_command`），Electron 输出转进日志（`_pipe_electron`）；`overlay_config`；`serve` / `run` |
| `start.py` | 一键启动（双击「启动助手.bat」→ `start` 命令）：清 `_update/` → 补 `config.toml` → 装 Electron → 写 GSI 配置 → 导出网格头像 → 数据过期就更新 → 启动；`no_key_note` 提示缺 token / AI |
| `demo.py` | 演示：按真实对局（`DEMO_MATCH` 9054096051）逐个选人，再当「假 Dota」把录像按 GSI 格式推给本地端口（`gsi_frames` / `run`） |
| `update.py` | 全量更新数据（`update` 命令 / 启动时过期）：英雄表、头像、装备、知识库、顶栏图标、分位置数据、个人数据、克制矩阵按顺序拉一遍；`fetch_icons`；没 token 时 `fetch_positions_opendota` / `positions_from_lane_roles` 用 OpenDota 分路凑分位置数据 |
| `upgrade.py` | 更新助手本身（数据更新是 `update.py`）：打包版看 GitHub Release、下 zip、核对 sha256、交给新包的 exe `upgrade --finish` 替换文件再重启；源码版 `git pull --ff-only` 再重启；`Updater` 给控制面板用 |

## webui 路由

| 路径 | 内容 |
|---|---|
| `/` | 302 跳到 `/panel.html` |
| `/overlay.html` `/panel.html` `/topbar.html` `/minimap.html` `/mates.html` … | `web/` 下的静态文件 |
| `/portraits/<id>.png`、`/items/<key>.png` | `data/portraits/`、`data/items/` |
| `/icons/...` | 技能 / 金币图标，第一次从 Valve CDN 下（`sources.assets.fetch_icon`），存 `data/icons/` |
| `/api/events` | SSE：状态一变就推一份完整快照 |
| `/api/state` `/api/heroes` `/api/config` | GET 当前快照 / 英雄表（帮认菜单用）/ 叠加层配置（`overlay_config`） |
| `/api/action` | POST：交给 `Live.action`（在工作线程里执行） |
| `/api/mates`、`/api/match/<比赛编号>?p=<好友代码>` | 队友战绩页，直接调 `domain.mates.Service`，不走 `Live` |

## 依赖与分层

- `webui` → `core`、`domain.mates`、`sources.http`；`start` → `update`、`upgrade`、`core`、`sources`、`game.gsi`；`update` → `sources`、`core.heroes`，并延迟 import `kb`。
- 只被 `cli` 和 `packaging/launcher.py`（经 `cli.main`）调用。

## 对应测试

- `tests/test_webui.py`：结构化视图、Live 主控（含局内提醒）、本地服务（含路径穿越）、SSE；不联网、不出声、不开窗口。
- `tests/test_upgrade.py`：假 GitHub 接口、临时裸仓库当 GitHub、替换保留个人文件、失败挪回、面板状态。
- `tests/test_nokey.py`：没 token / 调不了 AI 时 `update` / `start` 照常。
- `tests/test_log.py`：Electron 输出转发；`tests/test_mates.py`：`/api/mates` 接口。
- 冒烟：`test_smoke` 核对前端请求的每个 `/api/xxx` 接口 webui 都有。

## 注意事项 / 坑

- **`upgrade --finish --pid --target --src [--no-restart]` 是新旧版本之间的约定，改了旧版本就升不上来**（`upgrade.py` 文件头；`cli` 里这几个参数是隐藏的）。
- 替换时保留个人东西：`config.toml`、`logs/`、`shots/`、`templates/` 不碰；`data/` 只补本地没有的；`calibration.json` 本地有就留着；其余（exe、`_internal`、`ui`、`web`、`knowledge`、README …）整个换掉，旧的先挪进 `_update/old/`，失败挪回（HANDOFF 第 41 节）。
- 随机端口要避开 Chromium 拒绝的端口（`UNSAFE_PORTS`，2026-10-27 实测分到过 6669 打不开界面）。
- 没装 Electron 时 `run --browser` 只是预览，叠加层盖不到游戏上；旧版 Qt 界面已删除，不要再加回来当兜底（HANDOFF 第 12 节）。
- npm 装完有时没下载 Electron 本体，`start.py` 会补跑 `node node_modules/electron/install.js`（HANDOFF 第 12 节）。
- 更新频率：全量 `FULL_UPDATE_DAYS = 3` 天、个人数据 `PLAYER_UPDATE_HOURS = 12` 小时，没网用旧数据；GSI 启动项提醒只显示一次（`data/.gsi_hint_shown`，HANDOFF 第 13 节）。
- 改了 `启动助手.bat` 要用 cmd 真跑一遍（根 CLAUDE.md 规则），`.bat` 必须 CRLF。

## 设计记录（原「相关 HANDOFF 节」）

本模块的设计 / 验证 / 待验证记录在 [NOTES.md](NOTES.md)（2026-11-04 从 HANDOFF 拆过来，节号不变）。下面列的节号不在本目录 NOTES 里的，按 [HANDOFF 节号索引](../../docs/HANDOFF.md#节号索引) 找。

- HANDOFF 第 3 节（架构）、第 12 节（界面改版：网页 + Electron）、第 13 节（一键启动）
- HANDOFF 第 35 节（不填 key 也能用）、第 38.1 节（队友战绩独立页）、第 41 节（更新助手本身）

## 变更记录 (Changelog)

- 2026-11-02 11:18:41：初建，内容来自 HANDOFF 第 3、4、12、13、41 节 + 源码模块说明。
- 2026-11-04：HANDOFF 按模块拆分，过程记录搬进各模块 `NOTES.md`；「相关 HANDOFF 节」改成「设计记录」，指向 NOTES 和 HANDOFF 节号索引。
