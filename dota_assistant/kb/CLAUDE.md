[根目录](../../CLAUDE.md) > dota_assistant > **kb**

# kb：当前版本知识库

## 职责

把 Valve 官方 datafeed（和游戏内说明同源）的英雄技能 / 天赋 / 神杖魔晶 / 物品说明整理成可查的知识库，作为一切「某技能 / 物品能做什么、克不克制」判断的唯一依据。为什么要有它：AI 凭记忆说机制会记错旧版本（例如不知道射手天赋带克敌机先，HANDOFF 第 9 节「知识库」起因）。

层号 2，只能 import `core` / `sources`。命令入口 `python -m dota_assistant kb ...`（原 `kb.py` 整体搬进 `kb/__init__.py`，用法不变）。

## 四层内容

1. **官方原文**：`data/kb/kb.json`，`update` 时看版本号（`patchnoteslist` 最新一项），变了才重拉；说明里的 `%占位符%` 换成各级数值。
2. **机制标签**：`TAGS` 里按官方措辞的关键词 + 排除词从原文抽（不是人工标注），每个标签附命中的原句。
3. **玩家规则**：`knowledge/rules.toml`（`Rule`、`load_rules`）。
4. **常见误判**：`knowledge/mistakes.toml`（`Mistake`、`load_mistakes`、`add_mistake`）；`kb hero / item` 输出末尾显示，打法页 AI 把相关的放进资料。

## 关键文件

| 文件 | 管什么 |
|---|---|
| `__init__.py` | 拉取（`current_patch` / `fetch` / `update`）、文本整理（`fill`、天赋数值）、`Entry`（技能 / 物品条目，`full_text` / `main_text` / `tags`）、`HeroKB`（`tags` / `with_tag`）、`KnowledgeBase`（`load` / `item` / `search`，`heroes` 按 id）、`TAGS`、`ITEM_ALIASES`（BKB、MKB、跳刀…）、规则 / 误判读写、展示（`render_hero` / `render_item` / `hero_brief`） |
| `cd_ops.py` | 大招冷却用到的知识库条目：`pick`（每个英雄要计时的大招）、`cooldowns`（各级冷却）、`MIN_CD = 40`；神杖 / 魔晶改冷却的原样写法 `update_ops` / `load_ops`（存 `data/kb/cd_ops.json`） |

## 对外接口

- `KnowledgeBase.load()`：没有 `data/kb/kb.json` 返回 `None`（调用方要处理「还没 update」）。
- `kb.update(hero_ids, log=..., force=False)`：`kb update` 时顺带调 `cd_ops.update_ops`。
- CLI：`kb hero <英雄>`、`kb item <物品>`、`kb find <关键词或正则>`、`kb update [--force]`、`kb mistake [--wrong … --right … --hero … --item … --who … --evidence … --has-tag …]`（不带 `--wrong` 就列出全部）。
- `cd_ops.pick` / `cd_ops.cooldowns` 给 `domain/ultcd.py`；那边写成 `from ..kb import cd_ops as kbcd`。

## 依赖与分层

- 依赖 `core.config`（`DATA_DIR`、`ROOT`）；`cd_ops` 读游戏文件时延迟 import `core.dota_install.find_dota` / `core.vpk.VPK`，取不到就读 SteamDatabase 在 GitHub 上同步的同一份文件（`MIRROR`）。
- 使用者：`ai/gameplan.py`；`domain/` 的 advisor、explain、lanes、teamfight、coach、itembuild、brief、ultcd、mates；`app/update.py`、`app/live`（timers / ingame 里延迟 import）、`cli`。

## 对应测试

- `tests/test_kb.py`：文本整理、标签抽取、规则加载；有本地知识库时抽查真实数据。`RealDataTest.test_mistakes_still_hold` 逐版本核对每条误判的 `has_tag`。
- `tests/test_ultcd.py`：含 `cd_ops`（挑大招、各级冷却、游戏文件写法解析）。
- 依赖知识库的还有 `test_coach` `test_explain` `test_lanes` `test_teamfight` `test_itembuild` `test_gameplan`。

## 注意事项 / 坑

- **标签是关键词匹配，会有少量误判**（例如「召唤」会命中描述性用词）：引用时带原句，别只看标签名。区分「施加破坏」（对敌人施加）和「怕破坏」。
- 官方 `immunity` 字段：3 = 对敌方无视技能免疫、4 / 5 = 不无视、1 / 2 = 只作用于友方；2026-10-26 以前读反过（HANDOFF 第 30 节）。
- datafeed 给神杖 / 魔晶冷却**丢了符号**（「-10」「=30」只剩数字），所以 `cd_ops` 从游戏文件 `scripts/npc/heroes/npc_dota_hero_*.txt` 取原样写法（HANDOFF 第 32 节）。
- 已知：修补匠、娜迦海妖各有 1 个天赋数值取不到（显示 `?`）。
- 新记误判会让相关阵容的 AI 打法缓存失效（`ai/gameplan.py`）。
- 规则 vs 误判：规则是「应该怎么判断」（经验），误判是「这个说法事实上不对」（机制）。
- 网络不稳时 dota2.com 会断连接，`kb._get` 已加重试（HANDOFF 第 8 节）。

## 设计记录（原「相关 HANDOFF 节」）

本模块的设计 / 验证 / 待验证记录在 [NOTES.md](NOTES.md)（2026-11-04 从 HANDOFF 拆过来，节号不变）。下面列的节号不在本目录 NOTES 里的，按 [HANDOFF 节号索引](../../docs/HANDOFF.md#节号索引) 找。

- HANDOFF 第 9 节（知识库，2026-10-21）
- HANDOFF 第 30 节（后期团战，`immunity` 读反的更正）
- HANDOFF 第 32 节（对面大招冷却，神杖 / 魔晶写法）
- HANDOFF 第 15.1 节（AI 打法，「司夜刺客没有破坏」的更正）

## 变更记录 (Changelog)

- 2026-11-02 11:18:41：初建，内容来自 HANDOFF 第 4、9 节 + 源码模块说明。
- 2026-11-04：HANDOFF 按模块拆分，过程记录搬进各模块 `NOTES.md`；「相关 HANDOFF 节」改成「设计记录」，指向 NOTES 和 HANDOFF 节号索引。
