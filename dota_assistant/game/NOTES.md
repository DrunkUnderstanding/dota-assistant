[根目录](../../CLAUDE.md) > game > **NOTES**

# game：设计与验证记录

> 内容：GSI、热键、语音。2026-11-04 从 `docs/HANDOFF.md` 原样拆过来，节号沿用原 HANDOFF 编号（代码注释 / 其它文档里的「HANDOFF 第 N 节」按 [HANDOFF 的节号索引](../../docs/HANDOFF.md#节号索引) 找到在哪个文件）。
> 职责、文件表、接口、坑见同目录的 `CLAUDE.md`；这里只放「为什么这么做、怎么验证的、还差什么」，按时间追加。

## 当前状态（验证情况）

| 模块 | 状态 | 验证情况 |
|---|---|---|
| 语音（Windows SAPI，慧慧中文） | ⏸ 暂时隐藏 | 功能还在；2026-10-22 玩家决定默认关（`voice = false`），面板「语音」开关先藏起来（`panel.html` 里 `hidden`）。`config.toml` 写 `voice = true` 能打开 |
| GSI | ✅ 完成 | 单元测试模拟 POST；2026-10-22 游戏电脑上第一次接上真实游戏，日志里阶段 / 阵营 / 英雄都对（第 17 节）；局内字段还没核对。**2026-10-27 朋友的打包版 GSI 一直没连上、阵营全按天辉算**：找 Dota 目录认了空壳，已改（第 36 节），**改完没在朋友电脑上验证** |
| 热键（RegisterHotKey） | ✅ 完成 | 2026-10-22 游戏电脑上 5 个热键都注册成功，F10 实际按过（第 17 节） |

## 64. 反和谐开关（2026-11-05，`game/uncensor.py`、`config.uncensor`、设置窗口「反和谐」）

**要什么**：玩家的游戏是国服低暴力模式（血、骷髅等被和谐），想开着助手时自动反和谐，而且要**能开能关**。

**原理（核对过原文）**：SteamDatabase 同步的 `game/dota/gameinfo.gi` 里：

```
// These are optional low-violence paths. They will only get mounted if you're in a low-violence mode.
Game_LowViolence	dota_lv
```

低暴力模式下游戏启动时额外挂上 `game\dota_lv`；文件夹不在就挂不上，用 `game\dota` 的正常资源。所以反和谐 = 把 `dota_lv` 改名挪开。

**怎么做的**：
- `uncensor.apply(dota, on)`：开 = `game\dota_lv` 改名成 `game\dota_lv_assistant_off`（助手专用名字，不碰别的文件夹）；关 = 改回原名。**只改名不删**，关掉开关就和没动过一样。
- Steam 更新 / 验证完整性会把 `dota_lv` 补回来（两个文件夹同时在）：开着就删掉旧的挪开份再挪一次；关着就只删旧的挪开份。所以每次启动助手都检查。
- **Dota 开着不挪**：游戏可能已经挂上了 `dota_lv`，中途挪走怕读资源出错。看 Dota 开没开只用 `CreateToolhelp32Snapshot` 列进程名（不打开游戏进程）；看不出来时当开着。开着就每 15 秒（`consts.UNCENSOR_RETRY`）再看，关掉游戏后自动改好，下次开游戏生效。放回去（关反和谐）不怕游戏开着。
- `Live._uncensor_tick`（在 `tick` 里，到点才查）：真跑（`use_gsi=True`）时启动后查一次；测试 / 回放（`use_gsi=False`）不查，不碰真实 Dota 目录。设置里改了开关（或开着时改了 Dota 目录）马上查一次，结果写状态栏和设置窗口「最近一次」。
- 开关：控制面板「设置」→「反和谐」；`config.toml` 的 `uncensor`（默认 `false`）；命令行 `python -m dota_assistant uncensor on|off|status`。

**安全边界**：这是助手第一次**改游戏资源文件夹**（之前只往 `game\dota\cfg` 写 GSI 配置）。只改名、不改文件内容、不碰游戏进程 / 内存；默认关，玩家自己打开。会不会因此被 Valve 处理：社区里删 / 改名 `dota_lv` 是常见做法，没查到因此被封的记录，但不能打包票。

**验证**：
- `tests/test_uncensor.py`：临时目录里的假 Dota 目录，开 / 关来回、Dota 开着不动、Steam 补回后再挪、没有 `dota_lv`、设置窗口开关、Dota 关掉后自动重试成功、测试不碰真实目录。
- 无头浏览器 `tools/browser_check.py` 全过；真浏览器（Playwright）点设置里的开关 → 保存：假 Dota 目录里 `dota_lv` 变成 `dota_lv_assistant_off`、`config.toml` 写进 `uncensor = true`、状态栏「已反和谐，下次开 Dota 生效」；再点关 → 改回 `dota_lv`。
- **开发机没装 Dota，没在真游戏里看过效果**：待玩家在装了 Dota 的电脑上开开关、重开 Dota，看血 / 骷髅回来没有，以及游戏开着时会不会等关了再改。
