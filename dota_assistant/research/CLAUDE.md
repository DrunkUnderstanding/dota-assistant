[根目录](../../CLAUDE.md) > dota_assistant > **research**

# research：回测与研究脚本

## 职责

用「统计数据截止点之后」的新对局（样本外）检验推荐、分路、对线判断到底有没有用；以及 AI 选人盲测。产出的是报告和缓存，**不参与实时助手**。打分常数的取舍（关掉对线加权、冷门打折几点、配合不打折等）都是这里的回测定的。

层号 6，能 import `core` ~ `domain` 和 `vision` / `game`。

## 关键文件

| 文件 | 管什么 |
|---|---|
| `backtest.py` | 样本外回测 / 调参（`backtest` 命令）：`fetch_matches`（STRATZ 取选人顺序和每人位置，缓存 `data/backtest_matches.json`）、`Tables`、`replay_picks`（每一手只用轮到他时看得到的信息重放）、`predict_drafts`（只看最终阵容预测胜负）、`separation`（区分度）、`tune`（`--tune` 网格搜，一半调参一半验证）、`logistic_fit` / `additivity`（「逐项相加」有没有算多）、`run` |
| `aibacktest.py` | AI 选人盲测（`backtest-ai`）：取第 3~6 手的选人时刻，候选 = 实际选的 + 随机 11 个，让大模型和统计引擎同题排序比区分度；结果逐条追加到 `data/ai_backtest.jsonl`，可分几次跑 |
| `lanestate.py` | 对线优劣：`lane-test` 回测（只用 3:00 / 6:00 的击杀 / 死亡 / 等级猜 STRATZ 对线结果，屯野 / 莲花和对线结果的关系）；`lane-review`（`review`）把实战记录 `data/lane_log.jsonl` 对照 STRATZ 赛后对线结果 |

## 命令

- `backtest [--matches 3000] [--tune]`：约 1~2 分钟（STRATZ 取数据），结果缓存；完整报告在 `data/backtest_report.txt`。
- `lanes-test [--matches N]`：对面分路猜得准不准；cli 里用 `backtest.fetch_matches` 取样本、`domain.lanes.evaluate` 评估，要先 `update` 过统计数据且填了 STRATZ token。
- `lane-test [--matches N]`、`lane-review`。
- `backtest-ai [--moments 300] [--model ...] [--workers 3]`：走 `ai/llm.py`（默认 Claude Code 订阅）。

## 依赖与分层

- `backtest` → `domain.engine`（常数 `LANE_OPPONENTS` `MIN_GAMES` `RISK_TOP` `W_SYN`、`Engine`、`sigmoid`）、`sources`；`aibacktest` → `backtest`、`ai.llm`、`domain.engine`；`lanestate` → `domain.lanelog`、`sources`。
- `domain/engine.py` 的「阵容」胜率和 `backtest.predict_drafts` 是同一个式子（engine 文件头注释）。

## 对应测试

- `tests/test_backtest.py`：回测重放逻辑，人造数据，不联网。
- `tests/test_lanestate.py`：STRATZ 数据压缩、特征、模型、实战记录对照，不联网。
- 行为快照 slow 组（`tools/golden/run.py check --slow`）跑 `backtest / lanes-test / lane-test --matches 300`。

## 注意事项 / 坑

- **相关不是因果**：选得好的人可能也打得好，排名靠前组的胜率差里有一部分是玩家水平（`backtest.py` 文件头、HANDOFF 第 2.1 节）。
- 小样本会骗人：第一次回测（2449 场）说「早选几乎没用」，换一批样本就不成立（HANDOFF 第 2.1 节「更正」）。区分度噪声约 ±0.01，变化在这以内就当没区别。
- 样本里天辉胜率约 54.6%；选人推荐不受影响（同一手的候选都在同一边），「阵容」胜率里用 `RADIANT_EDGE` 补（HANDOFF 第 45 节）。
- `update` 拉了新统计后截止点变了，`backtest` 会自动重新取样本；改推荐逻辑后重跑对比报告数字（HANDOFF 第 9 节「常用命令」）。
- 大模型凭自身理解排选人不如统计引擎（HANDOFF 第 10 节）：这是「选人不用大模型」的依据。
- `lane-review` 依赖 GSI 的 kills / assists / level，这些字段还没在真实推送里见过（HANDOFF 第 2 节当前状态）。

## 相关 HANDOFF 节

- HANDOFF 第 2.1 节（回测结果）
- HANDOFF 第 10 节（大模型选人盲测）
- HANDOFF 第 22 节（对面分路回测）
- HANDOFF 第 28 节（对线优劣：能不能猜、屯野 / 莲花怎么分）
- HANDOFF 第 43.1 / 43.2 节（冷门打折、配合不打折的回测）、第 45 节（逐项相加）

## 变更记录 (Changelog)

- 2026-11-02 11:18:41：初建，内容来自 HANDOFF 第 2.1、4、9 节 + 源码模块说明。
