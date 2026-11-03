[根目录](../CLAUDE.md) > **tools**

# tools：门禁与开发工具

## 职责

开发用的脚本，不进产品：行为快照（门禁 G3）、单测不碰真实目录（G4）、无头浏览器检查（阶段验收 G6）、锁依赖、重构核对。门禁编号来自 2026-10-28 工程化重构计划（`docs/refactor/PLAN.md`）。`tools/` 也在编译和 ruff 门禁范围里（`compileall ... tools`、`ruff check ... tools`），`test_hygiene` 也查这里的 Python 编码写法。

## 关键文件

| 文件 | 管什么 |
|---|---|
| `golden/run.py` | 行为快照入口（G3）：`check` / `check --slow` / `check --group G [--keep]` / `check --shift-days N` / `record [--reuse-home]` / `update-snapshot` / `scan-token`。详细用法见 [golden/README.md](golden/README.md) |
| `golden/g_worker.py` | 跑一组快照的子进程：先装假时钟，再装网络 / OCR 录放和各道闸，最后才 import 产品代码 |
| `golden/g_cases.py` | 快照用例：cli 组的命令清单（`CLI`）、选人截图清单（`DRAFT_SHOTS`） |
| `golden/replay_live.py` | replay 组：造一个 `Live` 按剧本推完整一局（选人截图 → 帮认 / 设置 / 叠加层操作 → 本局要点 → 真实推送 → 买活 / 大招 → 结束 → 队友战绩），每个网页动作都走到 |
| `golden/g_common.py` | 路径和小工具；环境变量 `DOTA_GOLDEN_DIR`（快照放哪）、`DOTA_GOLDEN_SOURCE`（冻结目录从哪复制，默认 `D:\dota`） |
| `golden/g_cassette.py` `g_clock.py` `g_files.py` `g_home.py` `g_normalize.py` | 网络 / OCR 录放、假时钟、文件变动记录、冻结目录、归一化（路径 / 堆栈 / 模块名 / 时间 / 内存地址） |
| `check_isolation.py` | G4：跑单测前后比对 `data/` `shots/` `logs/` `templates/` `config.toml` `calibration.json`；`--root` 可多给几个目录，`--` 后面传给 unittest |
| `browser_check.py` + `browser_console.mjs` | G6：临时运行目录起界面服务（同 `run --browser` 的 WebServer + Live，不开 GSI / 热键 / 截屏 / 语音 / AI），无头 Edge / Chrome 打开控制面板、叠加层、顶栏、队友战绩页，`browser_console.mjs`（node）收控制台报错 |
| `extract_camps.py` | 从本机游戏文件读野点 / 刷新框（触发器模型 PHYS 块）/ 莲花池 / 小地图范围（`maps/dota.vpk` 的地图实体，二进制 KV3 v5，照 ValveResourceFormat 的读法），和 `domain/camps.py` 的表对一遍；要另装 `zstandard lz4`（不进 requirements） |
| `lock_deps.py` | 重新生成 `requirements.lock`（只锁 `requirements.txt` 里的包及传递依赖） |
| `refactor/export_names.py` | 搬家清单：导出旧模块名字、合并 `docs/refactor/moves.json`、分层冲突分析（`test_layers` / `test_moves` 引用它） |
| `refactor/check_live_split.py` | 核对 `live.py` 拆成 `app/live/` 后每个方法源码和拆之前一字不差（只允许 `consts.NAME` 和延迟 import 多两个点） |

## 行为快照要点（G3，`python tools/golden/run.py check`，约 25 秒）

- 三组：cli（`--help`、13 组 `suggest`、`kb`、`recognize`、各种 `-sim`、`lane-review`、`logs`）、replay（一整局 `Live` 回放）、update；`--slow` 再加 backtest / lanes-test / lane-test。逐字节比对，退出码 0 = 一致且没碰真实目录。
- 快照、cassette、冻结目录都在**仓库外 `D:\dota-golden\`**（含个人数据，不进 git、不外传）；冻结目录里 STRATZ token 换成占位符 `golden-dummy-stratz-token`，`scan-token` 查一遍。
- check 时网络只读 cassette、不起 PowerShell / claude / node / electron 子进程、不碰注册表。
- **行为本来就该变**：先看 diff（完整的在 `D:\dota-golden\last-diff\`）确认差异都是该有的，再 `update-snapshot`；**重构「只搬家」时不许用 `update-snapshot` / `record` 让 check 通过**。
- 加用例：在 `g_cases.py` 对应组**末尾**加（前面的命令会写缓存，插中间会改后面的结果），要联网的 `record --reuse-home`。

## 对应测试

- `tools/` 本身没有单测；`tests/test_layers.py`、`tests/test_moves.py` 调 `refactor/export_names.py`；`check_isolation.py` 和 golden 共用 `tests/tools/sentinel.py`。

## 注意事项 / 坑

- 机器上没有 `D:\dota-golden` 时快照跑不了（HANDOFF 第 2 节当前状态里记过一次「这台机器没有 `D:\dota-golden`，没跑」）：报告里要说明没跑。
- golden 的 G4 比单测更严：`sentinel.KNOWN` 的 `shots/gsi/` 豁免不适用，任何真实目录变化都算失败。
- `browser_check.py` 从仓库只读复制 `data/` `knowledge/` `web/` 等到临时目录，不带 `config.toml`（token / AI Key 清空），可以在玩家开着助手打游戏时跑。
- `docs/refactor/uitest/` 下还有真鼠标 / CDP 的 UI 测试脚本（不在 `tools/` 里）。

## 相关 HANDOFF 节

- HANDOFF 第 2 节当前状态「工程化重构」一行、第 4 节（`tools/` 几行）、第 9 节（常用命令）
- `tools/golden/README.md`、`docs/refactor/PLAN.md`

## 变更记录 (Changelog)

- 2026-11-02 11:18:41：初建，内容来自 `tools/golden/README.md`、各脚本模块说明、HANDOFF 第 4 节。
