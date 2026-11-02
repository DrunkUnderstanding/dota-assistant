[根目录](../../../CLAUDE.md) > dota_assistant > [app](../CLAUDE.md) > **live**

# app/live：实时主控 `Live`

## 职责

实时助手的大脑（不依赖任何界面库）：控制面板 10 个格子（`self.slots`）、定时识别、推荐、语音、GSI、热键、局内各种提醒和计时，状态整理成 JSON 快照推给网页（SSE）。原来的 `live.py` 在 2026-10-28 工程化重构时按功能拆成 mixin（只搬家不改行为）。

**控制面板的 10 个格子是唯一的「当前局面」**：识别和网页输入都只往里写，推荐只从格子读（HANDOFF 第 3 节）。

## 关键文件

| 文件 | 管什么 |
|---|---|
| `__init__.py` | 只导出 `Live` |
| `core.py` | 类本体 `class Live(DraftMixin, InGameMixin, TimersMixin, CardsMixin, OverlayMixin, SettingsMixin)`：`__init__`（各依赖可注入，测试 / 回放用）、工作线程（`start` / `submit` / `_loop` / `_run`）、订阅推送（`subscribe` / `unsubscribe` / `publish` / `current_snapshot`）、`action` 分发、`snapshot` / `status` / `heroes` / `side` / `save_shot` / `close` |
| `consts.py` | 模块级常量和小函数：`SHOTS_DIR` `OVERLAY_FILE`（`data/overlay.json`）`FIX_FILE`（`data/fixes.jsonl` 帮认流水）`OVERLAY_WIDTH`（下限 400）`EMPTY_LIKE` `PICKED_COLOR_GAIN` `DEATH_SHOT_DELAYS` `SIDE_MODES` `SCAN_MODES` `SECRET_FIELDS` `TIMER_SWITCHES` `HOTKEY_ACTIONS`、`plan_error` 等 |
| `draft.py` | 选人：`current_draft` / `refresh` / `draft_view` / `slots_view`、`should_scan` / `tick` / `scan` / `scan_bans` / `scan_role`、帮认和记模板（`learn_slot` / `name_slot` / `learn` / `pick_candidates` / `fill_slots`）、`skip_hero` / `unskip_hero`、`clear` |
| `ingame.py` | 局内：`coach_tick`（缺口提醒）、`_stage_tick`（按阶段弹本局要点）、`_jungle_tick` / `mute_cue`（野区）、`_lane_log`（对线记录）、`_death_recap` / `_take_death_shots`（死亡复盘和死亡截图） |
| `timers.py` | 顶栏计时：买活（`mark_buyback`、`_buyback_tick`、截图 / 消息栏自动记 `_scan_buyback` / `_check_feed`）、对面大招（`mark_ult` / `set_ult` / `_ult_check`）、`set_timer`（顶栏计时开关）、`buyback_view` |
| `cards.py` | 本局要点 / 「如果选它」卡片：`show_brief`、`explain_pick`、`set_brief_page`、`pick_view`；后台出数据页 / AI 打法页（`_start_brief` / `_start_plan` …） |
| `overlay.py` | 叠加层：显隐（`toggle_overlay` / `close_overlay` / `overlay_visible`）、固定 / 解锁（`toggle_lock` / `set_locked`）、拖动后的位置（`overlay_moved`）、底色样式（`set_overlay_style` / `overlay_style`）、`overlay_view` |
| `settings.py` | 设置：`save_settings`（写回 `config.toml`）、`settings_view`、Dota 目录、热键注册 |

## 网页动作（`Live.action`）

`{"type": ...}`，在工作线程里执行，返回状态栏提示；不认识的抛 `ValueError`。现有 26 种：`set_slot` `set_bans` `set`（`position` / `side_mode` / `mode` / `voice` / `scan_without_gsi` / `overlay_locked` / 顶栏计时开关 / `overlay_opacity` / `overlay_color`）`refresh` `clear` `rescan` `learn` `shot` `dismiss_coach` `show_brief` `explain_pick` `skip_hero` `unskip_hero` `brief_page` `buyback` `ult` `name_slot` `ult_set` `toggle_overlay` `toggle_lock` `close_overlay` `overlay_moved` `save_settings` `upgrade_check` `upgrade_install` `open_logs`。队友战绩不走这里（webui 直接调 `domain.mates.Service`）。

## 依赖与分层

- 属于 `app` 层：用 `domain`（advisor、brief、buyback、coach、jungle、ultcd、engine）、`vision`（recognize、grid、roles、buybackscan、capture）、`game`（gsi、speech、hotkeys）、`ai`（gameplan、llm）、`core`、`app.upgrade`。
- 被 `app/webui.py` 持有（`WebServer.live`），`tools/golden/replay_live.py`、`tools/browser_check.py` 也会造一个。

## 对应测试 / 守门

- `tests/test_webui.py`：Live 主控各流程（选人、帮认、局内提醒、死亡、买活、大招、叠加层、设置）。`tests/test_nokey.py`、`tests/test_skip.py` 也用到。
- `tests/test_smoke.py`：**`Live` 全部方法 / 属性（含私有）的名字 + 签名必须和 `tests/data/live_api.json` 一字不差**；确实有意改接口时 `set UPDATE_LIVE_API=1 && python -m unittest tests.test_smoke` 重写。前端每处 `act` / `run` / `send` 的动作名都要在 `Live.action` 分发表里。
- `tests/test_moves.py`：`app/live/` 下除 `consts.py` 外**任何文件都不许绑定 consts 的名字**，mixin 一律写 `consts.NAME`（测试只替换 consts 这一处）。
- 行为快照 replay 组（`tools/golden/replay_live.py`）按剧本推完整一局，逐字节比对。

## 注意事项 / 坑

- **所有改状态的操作都排进同一个工作线程**（网页请求、热键、定时识别都只是往队列里丢任务）：mss 截屏句柄只能在创建它的线程里用，也省了加锁。耗时的（OCR、AI 打法、拉数据）放后台线程，完成后再 `submit` 回来。
- 快照和上次不同才推；`SECRET_FIELDS`（STRATZ token、API Key）不发给页面、不进日志（`action` 记日志时打码）。
- 叠加层关掉后 `user_hidden` 一直保留，下一局也不会自动弹，要按 F8（HANDOFF 第 14 节）。
- 宽度下限 `OVERLAY_WIDTH[0] = 400` 要和 `electron/main.js` 的 `MIN_WIDTH` 一致（HANDOFF 第 46 节）。
- 帮认会学坏模板：像空位的裁图（`EMPTY_LIKE`）不记、同一英雄认进好几格不算（HANDOFF 第 40 节）。出了选人不再认顶栏，策略时间只补空格（HANDOFF 第 37 节）。
- 没 GSI 时阵营按 `SIDE_FALLBACK = "radiant"` 先当，卡片和面板上要标出来。
- HANDOFF 第 4 节的表里写「其余方法在下面七个 mixin 里」，实际是六个（原计划的 `mates` mixin 没有了，同一格里有说明）。
- 2026-10-28 拆分的核对脚本：`tools/refactor/check_live_split.py`。

## 相关 HANDOFF 节

- HANDOFF 第 3 节（架构）、第 11.1 节（缺口提醒实时版）、第 14 节（叠加层拖动 / 固定）、第 16 节（「如果选 X」卡片）
- HANDOFF 第 22.2 节（拖到副屏 + 自由改宽高）、第 23 节（底色 / 不透明度）、第 28 节（本局要点按阶段弹）
- HANDOFF 第 31 / 32 节（买活、大招计时）、第 37 / 37.2 节（帮认）、第 40 节、第 42 节（顶栏计时开关）、第 44 节（这局不想玩）、第 46 节

## 变更记录 (Changelog)

- 2026-11-02 11:18:41：初建，内容来自 HANDOFF 第 3、4 节 + 源码（`core.py` 的 `action` 分发表、各 mixin 方法）。
