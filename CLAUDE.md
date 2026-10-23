# 项目指令

Dota 2 选人助手（自用）。**接手先读 [docs/HANDOFF.md](docs/HANDOFF.md)**：需求、当前状态、架构、下一步都在里面。

- 用简体中文交流和写文档、注释。
- 不要在用户没明确要求时 `git commit` / `git push`。
- 安全底线：只读屏幕截图 + Valve 官方 GSI；不读游戏内存、不注入、不模拟键鼠、不显示玩家本来看不到的信息。
- `config.toml` 里有个人 STRATZ token，不进 git、不要贴到任何外部服务。
- **回答 Dota 机制问题（技能 / 物品能做什么、克不克制）先查知识库**：`python -m dota_assistant kb hero|item|find ...`，引用官方原句，查不到就说查不到，不凭记忆；文档里的机制结论也要回原文核对。`kb hero/item` 输出末尾的「常见误判」先看。玩家纠正的判断写进 `knowledge/rules.toml`；机制事实说错了（打法页 AI 或你自己），用 `python -m dota_assistant kb mistake --wrong … --right … --hero … --who …` 记进 `knowledge/mistakes.toml`。
- 改完跑 `python -m unittest`；动了推荐逻辑，再用 `python -m dota_assistant suggest ...` 看几组结果是否符合常识。
- 新确定的需求、做完的模块、验证结论，同步更新 `docs/HANDOFF.md` 的「当前状态」和「下一步」。
