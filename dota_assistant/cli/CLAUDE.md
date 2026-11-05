[根目录](../../CLAUDE.md) > dota_assistant > **cli**

# cli：命令行入口（最上层）

## 职责

`python -m dota_assistant <命令>` 的全部命令（`__main__.py` 只调 `cli.main`）；打包版 exe（`packaging/launcher.py`）和 `pyproject.toml` 的命令入口 `dota-assistant` 也走 `dota_assistant.cli.main`（原 `cli.py` 搬成 `cli/__init__.py`，路径不变）。

层号 8（最高），能 import 所有层；**按命令延迟 import**，跑 `suggest` 不会把 Electron / OCR 那一套拉进来。

## 关键文件

| 文件 | 管什么 |
|---|---|
| `__init__.py` | `main(argv=None) -> int`：argparse 子命令、`config.load()`、`log.setup(...)`、分发；`cmd_suggest` / `cmd_recognize` / `cmd_label` / `cmd_kb` / `cmd_logs` / `cmd_upgrade`；`FILE_LOG_COMMANDS` |

## 命令表

| 命令 | 做什么 | 落到哪 |
|---|---|---|
| `update [--player]` | 拉取 / 刷新数据（`--player` 只刷个人数据） | `app.update.update` / `sources.player.update_player` |
| `suggest --enemy … --ally … --ban … [--pos N] [--top N] [--all-roles] [--me X --side radiant/dire]` | 命令行里手动推荐；`--me` 已选好英雄时改看对面分路 + 我这路对线提示 | `domain.advisor` |
| `start` | 一键启动（「启动助手.bat」调它；打包版 exe 不带参数也是它） | `app.start.main` |
| `run [--browser]` | 实时助手 | `app.webui.run` |
| `demo [--step 秒] [--speed 倍] [--browser]` | 不开 Dota 演示 | `app.demo.run` |
| `calibrate <截图>` / `recognize <截图>` / `label <截图…>` | 校准 / 识别一张（输出标注图）/ 逐格纠正存模板（自己展开通配符） | `vision.calibrate` / `vision.recognize` |
| `gsi-install <Dota 目录>` | 写 GSI 配置，并把 `dota_dir` 记进 `config.toml` | `game.gsi.install_cfg` + `core.config.save` |
| `uncensor [on\|off\|status]` | 反和谐开关：on / off 记进 `config.toml` 并马上改名 `game\dota_lv`；status 看现在的状态 | `game.uncensor` |
| `gsi-dump` | 只收 GSI 推送存到 `shots/gsi/` | `game.gsi.dump` |
| `say <文字>` | 测试语音 | `game.speech.Speaker` |
| `backtest [--matches N] [--tune]` / `lanes-test` / `lane-test` / `lane-review` / `backtest-ai` | 回测与研究 | `research.*`、`domain.lanes.evaluate` |
| `coach-sim <比赛编号…>` / `brief-sim <比赛编号…> [--ai]` / `death-sim [文件…]` | 录像 / 死亡推送重放 | `domain.coach` / `domain.brief` / `domain.itembuild.death_sim` |
| `kb hero` / `kb item` / `kb find` / `kb update` / `kb mistake` | 知识库查询 / 更新 / 记误判 | `cmd_kb` → `kb` |
| `logs [-n 行数] [--open]` | 看最新日志末尾 / 打开日志文件夹 | `core.log` |
| `upgrade [--check] [--yes] [--zip 压缩包]` | 更新助手本身 | `app.upgrade` |

## 依赖与分层

- 顶层只 import `app.update`、`core.config`、`core.log`、`sources.assets` / `player` / `stats`，其余都在分支里延迟 import。
- `FILE_LOG_COMMANDS`（start / run / demo / update / calibrate / label / gsi-dump / backtest / backtest-ai / coach-sim / brief-sim / lanes-test / lane-test / upgrade，以及 `kb update`）写日志文件；查询类命令只在控制台显示警告，免得把有用的日志挤掉。

## 对应测试

- `tests/test_smoke.py`：每个子命令 `--help` 退出码 0（命令列表从 argparse 动态取，新命令自动覆盖）、顺带跑几组 `suggest` 且不写运行目录；静态核对函数里的延迟 import 目标存在。
- 行为快照 cli 组（`tools/golden/g_cases.py`）：顶层和每个子命令的 `--help`、13 组 `suggest`、`kb`、`recognize`、`brief-sim` / `coach-sim` / `death-sim`、`lane-review`、`logs`。**加了命令 / 改了输出，快照会变。**

## 注意事项 / 坑

- `main()` 开头把 stdout / stderr 重设成 UTF-8：被重定向到管道 / 文件时默认是 GBK。
- `upgrade` 的 `--finish --pid --target --src --no-restart` 是隐藏参数，给 `app/upgrade.py` 起的收尾进程用，**是新旧版本之间的约定，别改**。
- 模块说明（`__doc__`）就是 `--help` 的描述；里面没列 `brief-sim`，但命令存在。
- 动了推荐逻辑：用 `suggest` 看几组结果是否符合常识（根 CLAUDE.md 规则）。

## 设计记录（原「相关 HANDOFF 节」）

本模块的设计 / 验证 / 待验证记录在 [NOTES.md](NOTES.md)（2026-11-04 从 HANDOFF 拆过来，节号不变）。下面列的节号不在本目录 NOTES 里的，按 [HANDOFF 节号索引](../../docs/HANDOFF.md#节号索引) 找。

- HANDOFF 第 9 节（常用命令）
- HANDOFF 第 9 节（知识库）里的 `kb` 用法
- HANDOFF 第 41 节（更新助手本身，`upgrade`）

## 变更记录 (Changelog)

- 2026-11-02 11:18:41：初建，内容来自源码（argparse 子命令和分发）+ HANDOFF 第 9 节。
- 2026-11-04：HANDOFF 按模块拆分，过程记录搬进各模块 `NOTES.md`；「相关 HANDOFF 节」改成「设计记录」，指向 NOTES 和 HANDOFF 节号索引。
