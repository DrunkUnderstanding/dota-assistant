[根目录](../../CLAUDE.md) > dota_assistant > **core**

# core：最底层公共件

## 职责

分层里的最底层（`tests/test_layers.py` 里层号 0），不 import 任何别的层。放配置、日志、英雄表、找 Dota 安装目录、GSI 字段常量、VPK 读取这类「谁都要用、又不依赖业务」的东西。全部只读磁盘 / 注册表，不碰游戏进程。

## 关键文件

| 文件 | 管什么 |
|---|---|
| `config.py` | `config.toml` → `Config` 数据类（字段说明在仓库根 `config.example.toml`）；运行目录 `ROOT`、`DATA_DIR`；`FROZEN`（打包版） |
| `log.py` | 日志：每次启动一个 `logs/<时间>-<命令>.log`，留最近 `KEEP = 30` 个；token / API Key 打码；未捕获异常（含后台线程）带堆栈；`HTTPServer` 也在这里 |
| `heroes.py` | `Hero`（frozen 数据类）、`HeroDB`（按中文名 / 英文名 / 简称 `ALIASES` / 唯一子串查找） |
| `dota_install.py` | 找 Dota 2 安装目录：`config.toml` 的 `dota_dir`，或到本机各个 Steam 库里找（注册表 + `libraryfolders.vdf`） |
| `gsi_fields.py` | GSI 推送里的字段：游戏阶段常量（`SELECTION_STATE` `STRATEGY_STATE` `DRAFT_STATES` `MATCH_STATES`）和纯函数 `buyback_left` |
| `vpk.py` | 最小的 VPK v2 读取（`VPK` 类）：Dota 资源包 `pak01_dir.vpk` + 分卷，按路径取文件 |

## 对外接口

- `config.load(path=None) -> Config`：没有 `config.toml` 就用默认值；只认 `Config` 里有的键。
- `config.save(updates, path=None)`：把几项设置写回 `config.toml`，**只改对应那一行的值，注释和其他行原样保留**；没有这一项就加在末尾；没有 `config.toml` 时先复制 `config.example.toml`；写之前 `tomllib.loads` 校验，写坏了宁可报错（控制面板「设置」保存走这里，HANDOFF 第 21 节）。
- `config.ROOT`：环境变量 `DOTA_ASSISTANT_HOME` 有值时用它（行为快照 / 测试用冻结目录）；打包版是 exe 所在目录；否则是仓库根。
- `log.setup(cfg, 命令名, to_file=...)`、`log.latest()`、`log.open_dir()`；各模块照常 `logging.getLogger(__name__)`，没调 `setup()` 时什么都不输出。
- `HeroDB.find(text)` 找不到 / 多个匹配时抛 `KeyError`（消息是给人看的中文）；`find_many(list)`。
- `dota_install.is_dota_dir(d)`：要有 `game/dota/pak01_dir.vpk` 才算；`steam_libraries()`；`find_dota(...)`。
- `gsi_fields.buyback_left(raw)`：买活冷却还剩几秒。
- `vpk.VPK(dir_path)`：建目录、按路径取文件。

## 依赖与分层

- 只依赖标准库（`log.py` 引 `config`）。被所有层使用。
- 配置字段新增要同时改 `Config`（带注释）和 `config.example.toml`；`config.toml` 里有个人 token，**不要读出来贴到别处**。

## 对应测试

- `tests/test_paths.py`：`ROOT` 的三种取法（默认仓库根 / `DOTA_ASSISTANT_HOME` / 打包版）。
- `tests/test_log.py`：写文件、token 不落盘、旧日志清理、后台线程异常、Electron 输出转发。
- `tests/test_llm.py`：含 `config.toml` 写回（`config.save`）。
- `tests/test_live.py`：含 `dota_install`（找 Dota 目录）。
- `tests/test_buyback.py`：含 `gsi_fields.buyback_left`。
- `tests/test_engine.py` 等用 `Hero` / `HeroDB` 造数据。

## 注意事项 / 坑

- **`hero.buyback_cooldown` 不是「还剩几秒」**，是冷却结束那一刻的 `map.game_time`；2026-10-26 实战里按「还剩几秒」读成了 46 分钟（HANDOFF 第 31 节）。`buyback_left` 要和当前 `game_time` 比。
- **找 Dota 目录要认 `pak01_dir.vpk`**：只看 `game\dota` 文件夹会把挪过库 / 卸载后剩下的空壳当成 Dota，GSI 配置写进去游戏不读、阵营一直按天辉算（2026-10-27 朋友电脑，HANDOFF 第 36 节）。
- 日志里 STRATZ token、AI API Key 一律替换成 `***`（含异常堆栈）；加新的密钥字段要一起处理。
- `log_level` 可设 DEBUG（多记每秒识别分数和每次 GSI 推送）。
- 运行目录相关路径都从 `ROOT` / `DATA_DIR` 派生；单测不能写真实 `data/` `shots/` `logs/`（`tools/check_isolation.py` 查）。

## 相关 HANDOFF 节

- HANDOFF 第 4 节（模块地图，core 部分）
- HANDOFF 第 17 节（日志）
- HANDOFF 第 21 节（免安装打包 + 设置窗口）
- HANDOFF 第 31 节（买活计时）
- HANDOFF 第 36 节（找 Dota 目录认了空壳）

## 变更记录 (Changelog)

- 2026-11-02 11:18:41：初建，内容来自 HANDOFF 第 4 节 + 源码模块说明。
