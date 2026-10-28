# 项目指令

Dota 2 选人助手（自用）。**接手先读 [docs/HANDOFF.md](docs/HANDOFF.md)**：需求、当前状态、架构、下一步都在里面。

- 用简体中文交流和写文档、注释。
- 不要在用户没明确要求时 `git commit` / `git push`。用户让提交时，先过三关再 commit + push：① `python -X utf8 -m compileall -q dota_assistant tests packaging`（改了 JS 再 `node --check`）② `python -m unittest` 全过 ③ 实际跑一遍改动涉及的入口（`suggest` 冒烟；改了 .bat 用 cmd 真跑；改了界面起 `run --browser` 看）。任一关失败就停下报告。
- 安全底线：只读屏幕截图 + Valve 官方 GSI；不读游戏内存、不注入、不模拟键鼠、不显示玩家本来看不到的信息。
- `config.toml` 里有个人 STRATZ token，不进 git、不要贴到任何外部服务。
- **回答 Dota 机制问题（技能 / 物品能做什么、克不克制）先查知识库**：`python -m dota_assistant kb hero|item|find ...`，引用官方原句，查不到就说查不到，不凭记忆；文档里的机制结论也要回原文核对。`kb hero/item` 输出末尾的「常见误判」先看。玩家纠正的判断写进 `knowledge/rules.toml`；机制事实说错了（打法页 AI 或你自己），用 `python -m dota_assistant kb mistake --wrong … --right … --hero … --who …` 记进 `knowledge/mistakes.toml`。
- Windows 基础规范（`tests/test_hygiene.py` 会查）：文本文件一律 UTF-8 无 BOM；`.bat` / `.cmd` 必须 CRLF（`.gitattributes` 已配）；Python 里 `open` / `read_text` / `write_text` / `subprocess(text=True)` 都要显式写 `encoding="utf-8"`；子进程输出按 UTF-8 解码前先确认对方真输出 UTF-8。
- 改完跑 `python -m unittest`；动了推荐逻辑，再用 `python -m dota_assistant suggest ...` 看几组结果是否符合常识。
- 其余门禁（2026-10-28 工程化重构加的，改完一起跑）：编译也带上 `tools`（`python -X utf8 -m compileall -q dota_assistant tests packaging tools`）；`ruff check dota_assistant tests tools packaging`（没装先 `pip install ruff`；只查未使用 import / 未定义名字 / 语法类，**不要跑 `ruff format`、不要大面积 `--fix`**）；`python tools/golden/run.py check`（行为快照逐字节比对，约 25 秒；行为本来就该变时先看 diff 确认差异都是该有的，再 `update-snapshot`，用法见 `tools/golden/README.md`）；`python tools/check_isolation.py`（单测不碰 `data/` `shots/` `logs/` 等真实目录）。`tests/test_smoke.py`、`test_moves.py`、`test_layers.py`、`test_patch_targets.py` 含在 unittest 里。
- 代码按层分在 `dota_assistant/` 的子包里：`core` → `sources` → `kb` → `ai` → `domain` → `vision` / `game` → `research` → `app`（实时主控在 `app/live/`）→ `cli`，只能 import 同层或更低层（`tests/test_layers.py` 查），每个文件管什么见 HANDOFF 第 4 节。不留转发旧路径的空壳模块；跨模块共享的函数用 `模块.函数` 形式调用（例如 `http._get(...)`），测试 patch 定义它的那个模块（`tests/test_patch_targets.py` 查）。
- 新确定的需求、做完的模块、验证结论，同步更新 `docs/HANDOFF.md` 的「当前状态」和「下一步」。
