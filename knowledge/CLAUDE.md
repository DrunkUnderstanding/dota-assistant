[根目录](../CLAUDE.md) > **knowledge**

# knowledge：玩家规则 + AI 常见误判（进 git）

## 职责

知识库（`dota_assistant/kb/`）里**人写的那两层**，跟着 git 走、换机器也在。官方原文本身在 `data/kb/`（`update` 拉的，不进 git），这里只放官方数据里看不出来的「判断」和「纠错」。

## 关键文件

| 文件 | 管什么 | 读写 |
|---|---|---|
| `rules.toml` | 玩家确认过的经验规则，一条一个 `[[rule]]`：`hero`（空 = 通用）、`item`、`text`、`source`（「玩家原话」/「实现假设」）、`date`、`evidence`（在 kb 里能查到的依据） | `kb.load_rules()`；手工编辑 |
| `mistakes.toml` | AI（打法页 AI / 开发助手）说错过、查原文纠正的说法，一条一个 `[[mistake]]`：`wrong`、`right`（要能在 kb 里查到）、`hero` / `item`、`who`、`date`、`evidence`（官方原文）、可选 `has_tag` | `kb.load_mistakes()`；用命令追加 |

## 怎么用

- **玩家纠正了某个判断** → 写进 `rules.toml`（`source = "玩家原话"`）。开发时补的、玩家没确认的写 `source = "实现假设"`，可以推翻。
- **机制事实说错了**（打法页 AI 或你自己）→ 用命令记，别手写：
  `python -m dota_assistant kb mistake --wrong 错误说法 --right 正确说法 --hero X [--item Y] --who 开发助手 --evidence 原文 [--has-tag 标签]`
  英雄 / 物品名自动换成正式名；带 `--has-tag` 时先核对标签存在，不存在就不记（HANDOFF 第 9 节「知识库」）。
- 列出全部误判：`python -m dota_assistant kb mistake`（不带 `--wrong`）。
- 规则 vs 误判：规则是「应该怎么判断」（经验），误判是「这个说法事实上不对」（机制）。

## 谁在用

- `kb hero` / `kb item` 输出末尾显示「常见误判」（回答机制问题前先看）。
- `ai/gameplan.py`：本局十个英雄相关的误判跟着各自原文放进 AI 打法的资料，不针对英雄的单列一段；新记一条误判会让相关阵容的打法缓存失效。
- `domain/coach.py`：「这个英雄 + 这件装备」的玩家规则参与「不需要的装备」判断。

## 对应测试

- `tests/test_kb.py`：规则加载；`RealDataTest.test_mistakes_still_hold` 每次跑测试都核对每条误判的 `has_tag`，版本改了机制会报出来（要本地有知识库）。

## 注意事项 / 坑

- 写进来的依据要能在 kb 里查到（`kb hero` / `kb item` / `kb find`）；**文档里的机制结论也要回原文核对**——第一条误判就是开发助手照搬文档里「司夜刺客没有破坏」没查原文（HANDOFF 第 15.1 节更正）。
- TOML 文件 UTF-8 无 BOM（`tests/test_hygiene.py` 查）；`add_mistake` 用 JSON 字符串转义写 TOML 基本字符串。
- 打包时整个目录复制进产物、更新助手时整个替换（`packaging/build.py`、`app/upgrade.py`）。

## 相关 HANDOFF 节

- HANDOFF 第 9 节（知识库，2026-10-21）
- HANDOFF 第 15.1 节（AI 打法）

## 变更记录 (Changelog)

- 2026-11-02 11:18:41：初建，内容来自 HANDOFF 第 4、9 节 + 两个 toml 的文件头注释 + `kb/__init__.py`。
