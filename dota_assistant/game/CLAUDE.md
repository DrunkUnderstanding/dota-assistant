[根目录](../../CLAUDE.md) > dota_assistant > **game**

# game：和游戏 / 系统打交道

## 职责

Valve 官方 Game State Integration（GSI）本地服务、Windows 全局热键、语音播报。全部走官方 / 系统接口：GSI 是游戏自己 POST 过来，热键是 `RegisterHotKey` 系统注册（不装键盘钩子、不读按键流），语音是 Windows 自带 SAPI。

和 `vision` 平级（层号 5），只能 import `core` / `sources` / `kb` / `ai` / `domain` 和同层。

## 关键文件

| 文件 | 管什么 |
|---|---|
| `gsi.py` | `GameState`（最新推送 + 最近 `HISTORY_SECONDS = 60` 秒历史；`in_draft` / `in_match` / `team` / `team_slot` / `player_name` / `my_hero_key` / `clock_minutes` / `deaths` / `level` / `last_hits` / `net_worth` / `gold` / `items` / `inventory` / `buyback` / `alive` / `match_id`）、`GSIServer`（127.0.0.1 HTTP 服务）、cfg 安装 `cfg_path` / `install_cfg` / `ensure_cfg`、`dump`（`gsi-dump` 命令） |
| `hotkeys.py` | `Hotkeys({"F8": 回调, ...})`，`start` / `stop`；`vk_code` 支持 F1~F24 和单个字母 / 数字 |
| `speech.py` | `Speaker(rate, enabled)`：常驻一个 PowerShell 进程调 System.Speech，`say()` 新句子打断旧句子，`close()` |

## 对外接口

- `install_cfg(dota_dir, port)`：写 `game/dota/cfg/gamestate_integration/gamestate_integration_dota_assistant.cfg`；目录不像 Dota（没有 `pak01_dir.vpk`）就抛 `FileNotFoundError`。`ensure_cfg` 只在文件不在或内容变了才重写。**Dota 启动时才读 cfg，开着的要重开**；启动项 `-gamestateintegration` 只能提醒玩家手动加。
- 选人阶段的推送自动存 `shots/gsi/`（`GameState.log_draft`，内容变了才存，最多 300 份，HANDOFF 第 13 节）。
- 热键回调在热键线程里执行，界面 / 状态相关的事要转回 `Live` 的工作线程。默认键位在 `Config`：F8 显隐、F7 解锁 / 固定、F9 立即重认、F10 截图、F6 再看本局要点。
- CLI：`gsi-install <Dota 安装目录>`、`gsi-dump`、`say <文字>`。

## 依赖与分层

- `gsi.py` 依赖 `core.dota_install`、`core.gsi_fields`（阶段常量、`buyback_left`）、`core.log.HTTPServer`。
- import 它的：`app/live`（`core` 用 `GSIServer` / `Speaker`，`consts` 用 `vk_code`，`settings` 用 `ensure_cfg` / `Hotkeys`）、`app/start.py`（`ensure_cfg`）、`cli`。`app/demo.py` 不 import，而是按 GSI 格式把录像 POST 到本地端口（当「假 Dota」）。

## 对应测试

- `tests/test_live.py`：GSI 服务（模拟 POST）与播报文案、找 Dota 目录。
- `tests/test_death.py`、`tests/test_itembuild.py`、`tests/test_buyback.py`：用 `GameState` 喂手写推送。
- `tests/test_webui.py`：Live 里不开 GSI 端口，换成 `GameState` 直接改推送。

## 注意事项 / 坑

- **玩家身份下 GSI 拿不到敌方信息**，这是 Valve 有意的限制，别想绕过（HANDOFF 第 5 节第 5 条）。买活 / 大招计时里别人的数据只能靠截图或玩家点。
- `hero.buyback_cooldown` 是冷却结束时的 `game_time`，不是剩余秒数（`core/gsi_fields.py`、HANDOFF 第 31 节）。
- 局内字段（`kills` / `assists` / `level`、死亡复盘用的状态和技能冷却）**还没在真实推送里全部核对过**（HANDOFF 第 2 节当前状态）。
- 热键用 `RegisterHotKey` 而不是 `keyboard` 之类的钩子，降低被反作弊误判的可能（HANDOFF 第 5 节第 7 条）；Dota 里占用了的键在「设置」里改。
- 语音不用 Python TTS 库（PySide6 的 QtTextToSpeech 装不上），改常驻 PowerShell，零依赖（HANDOFF 第 5 节第 6 条）。语音默认关（`voice = false`），面板开关也藏了（HANDOFF 第 2 节当前状态）。
- 子进程输出按 UTF-8 解码前确认对方真输出 UTF-8：`speech.py` 的脚本开头把 PowerShell 控制台输入 / 输出都设成 UTF-8。

## 相关 HANDOFF 节

- HANDOFF 第 5 节（关键设计决策 5~7）
- HANDOFF 第 13 节（一键启动，GSI 配置自动写）
- HANDOFF 第 17 节（日志，游戏电脑上第一次接上 GSI / 热键）
- HANDOFF 第 36 节（找 Dota 目录认了空壳，GSI 连不上）

## 变更记录 (Changelog)

- 2026-11-02 11:18:41：初建，内容来自 HANDOFF 第 4、5 节 + 源码模块说明。
