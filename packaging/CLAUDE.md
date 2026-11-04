[根目录](../CLAUDE.md) > **packaging**

# packaging：免安装打包

## 职责

把助手打成「解压即用」的 Windows 目录 + zip，给没装 Python / Node 的电脑用（`python packaging/build.py`）。打包版的 exe 入口也在这里。

## 关键文件

| 文件 | 管什么 |
|---|---|
| `build.py` | 打包：`prefetch()` 先补全死数据（头像、装备常量 / 图标、顶栏图标，要联网）→ `pyinstaller()`（`--onedir --console`，`--collect-submodules dota_assistant`、`--hidden-import anthropic`）→ `electron()`（复制 Electron 本体到 `ui/`，只留 zh-CN / en-US 语言包，`main.js` `preload.js` `package.json` 放进 `ui/resources/app/`）→ `resources()`（`web/` `knowledge/` `data/`、`config.example.toml` `README.md` `calibration.json`，写 `VERSION.txt`）→ `check_secrets()` → `make_zip()` |
| `launcher.py` | 打包版 exe 入口：不带参数 = `start`（一键启动），带参数和 `python -m dota_assistant` 一样（调 `dota_assistant.cli.main`）；双击出错时等回车再关窗口 |

## 产物

- `build/Dota选人助手/`：`Dota选人助手.exe` + `_internal/`（PyInstaller）、`ui/`（Electron）、`web/` `knowledge/` `data/`、`config.example.toml`（第一次启动复制成 `config.toml`）。
- `build/Dota选人助手-<版本>.zip`；版本号 = 日期 + git 短哈希（有未提交改动会带「-改动未提交」）。`build/` 不进 git。

## 依赖

- `pip install pyinstaller`；`electron/` 下装过一次 `npm install`（要 `node_modules/electron/dist/electron.exe`）。
- 先跑过 `python -m dota_assistant update`（要有英雄表和知识库，否则 `prefetch` 直接退出）。
- `launcher.py` 只 import `dota_assistant.cli`；`tests/test_smoke.py` 的 `EXTRA_SCRIPTS` 会核对 `packaging/*.py` 里对 `dota_assistant` 的 import（打包入口不在单测里跑）。

## 注意事项 / 坑

- **不打进去的**：`config.toml`（个人 token / key）、`logs/`、`shots/`，以及 `data/` 里的个人数据（`DATA_SKIP`：`player.json` `overlay.json` `.gsi_hint_shown` `lane_log.jsonl` `mates.json` `matches/` `benchmarks/`）和开发用的回测数据（`bench` `backtest_matches.json`）。新增个人数据文件要加进 `DATA_SKIP`。
- **`check_secrets()` 会扫一遍产物**，本机 `config.toml` 里的 `stratz_token` / `ai_api_key` 出现在任何文本文件里就停止；产物里有 `config.toml` 也停止。
- `web/` `knowledge/` 是整个目录复制，目录里的所有文件都会进包。
- 打包版的运行目录是 exe 所在目录（`core/config.py` 的 `FROZEN`）；更新助手时由新包的 exe 替换文件，按 `VERSION.txt` 判新旧（`app/upgrade.py`，HANDOFF 第 41 节）。GitHub Release 的 tag 形如 `v2026.10.29`，同一天再发一版是 `v2026.10.29.2`（HANDOFF 第 41 节末的真实发布记录）。
- 状态：开发机打包 + 启动通过，**没在一台没装 Python / Node 的干净电脑上试过**（HANDOFF 第 2 节当前状态）。

## 设计记录（原「相关 HANDOFF 节」）

本模块的设计 / 验证 / 待验证记录在 [NOTES.md](NOTES.md)（2026-11-04 从 HANDOFF 拆过来，节号不变）。下面列的节号不在本目录 NOTES 里的，按 [HANDOFF 节号索引](../docs/HANDOFF.md#节号索引) 找。

- HANDOFF 第 21 节（免安装打包 + 设置窗口）
- HANDOFF 第 41 节（更新助手本身、真实发布实测）

## 变更记录 (Changelog)

- 2026-11-02 11:18:41：初建，内容来自 `build.py` / `launcher.py` 模块说明 + HANDOFF 第 2、4、21、41 节。
- 2026-11-04：HANDOFF 按模块拆分，过程记录搬进各模块 `NOTES.md`；「相关 HANDOFF 节」改成「设计记录」，指向 NOTES 和 HANDOFF 节号索引。
