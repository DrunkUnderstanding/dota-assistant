[根目录](../CLAUDE.md) > **tests**

# tests：单元测试 + 守门测试

## 职责

`python -m unittest` 跑全部（unittest 自动发现 `tests/test_*.py`）。两类：**功能测试**（各模块行为，多数不联网、用人造数据或假接口）和**守门测试**（分层、patch 目标、搬家清单、冒烟、编码 / 换行、工程配置），后者是 2026-10-28 工程化重构加的门禁，平时也一直开着。

## 守门测试

| 文件 | 查什么 |
|---|---|
| `test_layers.py` | 用 ast 扫 `dota_assistant` 下每个 `.py` 的 import（含函数里的延迟 import），不许往上层引用；层号表 `LAYERS`；`__main__` 只能 import `cli`；例外 `ALLOWED` 现在为空 |
| `test_patch_targets.py` | 防止 `mock.patch` 打空：patch 的名字必须在目标模块**自己的源码顶层**定义（import 进来的不算，`IMPORTED_OK` 白名单每条带理由） |
| `test_moves.py` | `docs/refactor/moves.json` 搬家清单：搬好的名字旧位置不许再绑定（不留转发）；`app/live/` 下除 `consts.py` 外不许绑定 consts 的名字；表只追加不删 |
| `test_smoke.py` | 每个模块能 import 且 import 不起线程、不写运行目录；延迟 import / `模块.名字` / 全局名静态核对；每个子命令 `--help`；`Live` 接口和 `data/live_api.json` 一字不差；前端动作名和 `/api/xxx` 都有着落 |
| `test_hygiene.py` | git 跟踪的文本文件 UTF-8 无 BOM；`.bat` / `.cmd` 是 CRLF；Python 的 `open` / `read_text` / `write_text` / `subprocess(text=True)` 显式写 `encoding` |
| `test_project.py` | `pyproject.toml` 依赖和 `requirements.txt` 一致、只打包 `dota_assistant`、命令入口能 import、`requirements.lock` 覆盖每个依赖 |
| `test_paths.py` | 运行目录 `ROOT`：默认仓库根 / `DOTA_ASSISTANT_HOME` / 打包版 exe 目录 |

## 功能测试（文件 → 模块）

| 文件 | 测的模块 |
|---|---|
| `test_engine.py` `test_familiar.py` `test_skip.py` | `domain/engine`、`domain/advisor`（打分回归、熟练度分组 + 胜率拆解、这局不想玩） |
| `test_lanes.py` `test_explain.py` `test_teamfight.py` | `domain/lanes` `explain` `teamfight`（有本地知识库时抽查真实阵容） |
| `test_itembuild.py` `test_coach.py` `test_brief.py` `test_death.py` `test_jungle.py` `test_camps.py` | 出装、缺口提醒、本局要点、死亡复盘、野区、野点标记（位置 / 秒数） |
| `test_camera.py` | 游戏画面里的镜头：投影、反推镜头、认自己的血条（用 `tests/data/healthbars_own_and_ally.png`） |
| `test_buyback.py` `test_ultcd.py` | 买活计时（含 `vision/buybackscan`、`gsi_fields.buyback_left`）、对面大招（含 `kb/cd_ops`） |
| `test_mates.py` | `domain/mates` + webui 的 `/api/mates` |
| `test_kb.py` `test_gameplan.py` `test_llm.py` | 知识库、AI 打法（不调大模型）、AI 来源 + `config.toml` 写回 |
| `test_vision.py` `test_grid.py` `test_roles.py` `test_capture.py` | 顶栏识别（合成截图）、网格认禁用 + 资源解码、认位置（假 OCR）、截屏 |
| `test_live.py` | GSI 服务（模拟 POST）、播报文案、找 Dota 目录 |
| `test_webui.py` | 结构化视图、`Live` 主控各流程、本地服务（含路径穿越）、SSE |
| `test_log.py` `test_upgrade.py` `test_nokey.py` | 日志、更新助手本身、不填 key 也能用 |
| `test_backtest.py` `test_lanestate.py` | `research/backtest`、`research/lanestate` + `domain/lanelog` |

## 辅助文件

- `tests/tools/sentinel.py`：记运行目录里玩家数据（`data` `shots` `logs` `templates` `config.toml` `calibration.json`）的文件清单，前后比；`tools/check_isolation.py` 和 `tools/golden/run.py` 都用。`KNOWN` 里登记了已知的 `shots/gsi/`（`test_live` 的 GSI 测试会写真实目录，报出来但不算 G4 失败）。
- `tests/data/live_api.json`：`Live` 全部方法 / 属性的名字 + 签名基线。确实有意改了接口才更新：`set UPDATE_LIVE_API=1 && python -m unittest tests.test_smoke`。

## 怎么跑

- 全部：`python -m unittest`；单个：`python -m unittest tests.test_engine`。
- 连同「不碰真实目录」一起查：`python tools/check_isolation.py`（`--` 后面的参数传给 unittest）。
- 其他门禁（编译、ruff、行为快照）见根 CLAUDE.md。

## 注意事项 / 坑

- **测试 patch 定义名字的那个模块**（例如 `sources.http`，不是 import 它的模块），否则 `test_patch_targets` 报打空。
- 单测**不许写真实 `data/` `shots/` `logs/` 等目录**：用临时目录 / 替换模块常量（`app/live` 的常量只替换 `consts`）。
- 有些测试依赖本地数据，没有就跳过（例如 `test_vision` 要 `data/portraits/`，`test_kb` 的真实数据抽查要本地知识库）：干净检出时跳过不等于通过。
- 新加的测试文件同样受 `test_hygiene`（编码）和 `test_patch_targets`（扫 `tests/` 下所有 `.py`）约束。

## 设计记录（原「相关 HANDOFF 节」）

本模块没有单独的 NOTES；下面的节号（2026-11-04 起已从 HANDOFF 拆到各模块的 NOTES）按 [HANDOFF 节号索引](../docs/HANDOFF.md#节号索引) 找。

- HANDOFF 第 3 节（分层）、第 4 节（「仓库里其它目录 / 文件」表的 `tests/` 一行）
- HANDOFF 第 2 节当前状态里「工程化重构」一行（门禁清单）
- 重构计划 `docs/refactor/PLAN.md`

## 变更记录 (Changelog)

- 2026-11-02 11:18:41：初建，内容来自各测试文件的模块说明 + HANDOFF 第 4 节。
- 2026-11-04：HANDOFF 按模块拆分，过程记录搬进各模块 `NOTES.md`；「相关 HANDOFF 节」改成「设计记录」，指向 NOTES 和 HANDOFF 节号索引。
