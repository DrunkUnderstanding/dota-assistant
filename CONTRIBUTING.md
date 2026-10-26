# 参与开发

欢迎来加功能、修 bug、补英雄机制规则。动手前先看 [docs/HANDOFF.md](docs/HANDOFF.md)：需求、当前状态、架构、下一步都在里面。
想法拿不准的，先到 QQ 群（群号见 [README](README.md#交流--参与开发)）或 Issues 里聊一下，免得白做。

## 安全底线（不接受越线的改动）

- 只读**屏幕截图** + Valve 官方 **Game State Integration**
- **不读游戏内存、不注入、不模拟键鼠**、不显示玩家本来看不到的信息

这条是为了不违反 Valve 的规则、不让用户被封号，越线的 PR 会直接关掉。

## 开发环境

```
pip install -r requirements.txt
cd electron && npm install
python -m dota_assistant run --browser     # 不开游戏也能看界面
```

`config.toml` 从 `config.example.toml` 复制，里面的 STRATZ token / AI Key 是你自己的，**不要提交**（已在 `.gitignore` 里）。

## 提 PR 前过三关

1. `python -X utf8 -m compileall -q dota_assistant tests packaging`（改了 JS 再 `node --check 文件`）
2. `python -m unittest` 全过
3. 实际跑一遍改动涉及的入口：推荐逻辑用 `python -m dota_assistant suggest ...` 看几组结果符不符合常识；改了界面用 `run --browser` 看；改了 `.bat` 用 cmd 真跑

PR 里写清楚改了什么、怎么验证的。新功能顺手更新 `docs/HANDOFF.md` 的「当前状态」。

## 约定

- 文档、注释、提交信息用简体中文。
- **Dota 机制以知识库为准**：`python -m dota_assistant kb hero|item|find ...` 查官方原文，不凭记忆写规则。
  玩家纠正过的判断写进 `knowledge/rules.toml`，机制事实的错误用 `kb mistake` 记进 `knowledge/mistakes.toml`。
- Windows 规范（`tests/test_hygiene.py` 会查）：文本文件 UTF-8 无 BOM；`.bat` / `.cmd` 用 CRLF；
  Python 里 `open` / `read_text` / `write_text` / `subprocess(text=True)` 都显式写 `encoding="utf-8"`。

## 反馈 bug

控制面板点「打开日志」，把最新的 `.log` 发到 Issues 或群里（日志里不会有 token）。识别不准的话附上截图；
死亡复盘说得不对的话附上 `shots/deaths/` 里那次的文件。
