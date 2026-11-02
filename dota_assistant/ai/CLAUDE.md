[根目录](../../CLAUDE.md) > dota_assistant > **ai**

# ai：调大模型

## 职责

调大模型、拿回按 JSON schema 约束的结构化结果；以及「本局要点」的第二页 **AI 打法**。选人推荐本身**不用**大模型：盲测显示大模型凭自身理解排选人不如统计引擎（HANDOFF 第 10 节），所以 AI 只用在「给齐资料、让它写打法、程序核对出处」这一处。

层号 3，只能 import `core` / `sources` / `kb`。

## 关键文件

| 文件 | 管什么 |
|---|---|
| `llm.py` | 三种 AI 来源统一成 `ask_json(prompt, system, schema, model, timeout, provider, api_key, base_url) -> (结果, 折合价格, 错误信息)`；`PROVIDERS`、`Unavailable`、`options(cfg)`、`unavailable(cfg)`（不发请求先看缺什么）、`claude_exe()` |
| `gameplan.py` | AI 打法：`facts()` 拼资料（本局数据 + 自己 / 对面五人 / 同路队友的官方技能全文 + 玩家规则 + 常见误判）、`parse()` 核对出处、`generate(cfg, ...) -> Plan`；`Tip` / `Plan`、`PHASES`（五段）、`SYSTEM` / `SCHEMA` 提示词、缓存 `data/plans/` |

## 三种 AI 来源（`config.toml` 的 `ai_provider`，控制面板「设置」也能改）

- `claude_code`：无头调本机 Claude Code（玩家订阅，不另外花 API 钱）；填了 `ai_api_key` / `ai_base_url` 就走中转站。
- `anthropic`：玩家自己的 Anthropic API Key（官方 `anthropic` SDK）；填了 `ai_base_url` 就走中转站（Bearer 认证）。
- `openai`：OpenAI 兼容接口（DeepSeek、通义、Kimi 等），大多只支持 `json_object`，schema 写进提示词约束；**打法质量没验证过**。

## 对外接口

- `llm.unavailable(cfg) -> str`：缺什么返回原因（没装 Claude Code / 没填 Key / 没填地址），齐了返回空串。`Unavailable` 不算出错：AI 打法跳过，别的功能照常（HANDOFF 第 35 节）。
- `gameplan.generate(...)`：几十秒，**放后台线程调**；同一份资料读缓存（缓存键含来源、模型、提示词、资料；换来源不串缓存）。没有知识库时抛 `RuntimeError`。
- 使用者：`domain/brief.py`、`app/live`（cards / consts）、`research/aibacktest.py`（选人盲测）、`cli brief-sim --ai`。

## 依赖与分层

- `gameplan` 依赖 `kb`（原文、规则、误判）和 `core.config`；`llm` 只用标准库 + 可选的 `anthropic`。
- `domain/brief.py` 向下 import `ai.gameplan`（ai 层号低于 domain，合法）。

## 对应测试

- `tests/test_llm.py`：几种来源（接口用假的）+ `config.toml` 写回。
- `tests/test_gameplan.py`：出处核对、整理顺序、缓存格式，不调大模型。
- `tests/test_nokey.py`：AI 来源选 API Key 却没填时，打法页写明用不了，其余照常。

## 注意事项 / 坑

- **每条建议必须带 `quote`（从资料里原样复制 6~40 字），程序核对确实在资料里，对不上的整条丢掉**（`Plan.dropped`），卡片标「AI 生成，未验证」。改提示词别破坏这个约束。
- 常见误判只放进提示词，不做事后过滤（玩家选的，HANDOFF 第 9 节「知识库」）。
- 直接调 `claude.exe` 而不是 npm 的 `claude.cmd`：经过 cmd.exe 时空参数和多行参数会丢 / 截断（`claude_exe` 注释）。
- 只有 Claude Code 会报价格，另外两种记 0。
- API Key 和 STRATZ token 一样不进 git、不外传、不进日志、不发给页面（`app/live/consts.SECRET_FIELDS`）。
- 状态：打法页只核对过出处、质量未验证；Anthropic API / OpenAI 兼容接口没真实调用过（HANDOFF 第 2 节当前状态、第 21 节）。

## 相关 HANDOFF 节

- HANDOFF 第 10 节（大模型选人盲测）
- HANDOFF 第 15.1 节（AI 打法）
- HANDOFF 第 21 节（设置窗口 + AI 来源可选）
- HANDOFF 第 30 节（「后期团战」一段）
- HANDOFF 第 35 节（不填 key 也能用）

## 变更记录 (Changelog)

- 2026-11-02 11:18:41：初建，内容来自 HANDOFF 第 4 节 + 源码模块说明。
