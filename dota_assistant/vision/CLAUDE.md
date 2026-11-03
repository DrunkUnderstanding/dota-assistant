[根目录](../../CLAUDE.md) > dota_assistant > **vision**

# vision：看屏幕

## 职责

只读屏幕截图，认出选人阵容、禁用、自己的位置，以及比赛中谁买活了。**不碰游戏进程 / 内存**。和 `game` 平级（层号 5），只能 import `core` / `sources` / `kb` / `ai` / `domain` 和同层。

## 关键文件

| 文件 | 管什么 |
|---|---|
| `capture.py` | 截屏：`WindowGrabber` 优先 PrintWindow 只截 Dota 窗口（盖在上面的叠加层、弹幕窗口不会截进去）；找不到窗口 / 全黑再退回 `Screen` 用 mss 截整屏 |
| `recognize.py` | 顶栏识别（原 `vision.py`）：校准 `Calibration`（`calibration.json`，格子存占屏幕比例）、模板库 `TemplateBank`（官方头像 `data/portraits/` + 学到的 `templates/<英雄 id>/`，`templates/0/` 是空位）、`recognize()`、`similarity` / `feature` / `confident`、`DraftView` / `Slot` |
| `grid.py` | 选人网格认禁用：`find_cells` 找格子、`GridMap` 认格子是谁（按布局存 `data/grid_map.json`）、`banned` 看红斜线；`BanWatcher` 选人时一直看 |
| `gamefiles.py` | 从 Dota 资源包导出选人网格用的英雄图到 `data/portraits_grid/`（`GRID_PORTRAIT_DIR`；VTEX 里的 YCoCg-DXT5 解码 `dxt5` / `decode_vtex` / `extract_selection`） |
| `roles.py` | 自动认位置：Windows OCR（PowerShell 调 `Windows.Media.Ocr`）认顶栏名字下面的「优势路 / 中路 / 劣势路 / 辅助 / 纯辅助」，按 GSI 玩家名找自己那格（`ocr` / `find_roles` / `find_name` / `read_roles`） |
| `buybackscan.py` | 买活自动记：`Watcher` 每秒看顶栏头像变灰 / 变回彩色 + OCR 复活倒计时；`FeedCheck` / `read_feed` 认左侧消息栏「买活了」+ 行首英雄小图；样本存 `shots/buyback/` |
| `camera.py` | 游戏画面里的镜头：`find_bars` 认自己英雄的血条（白外框，队友 / 小兵的不认）、`target_from_bar` / `Tracker` 用血条 + GSI 英雄坐标反推镜头对着哪、`project` 世界坐标 → 屏幕像素；镜头常数是 2026-11-03 在游戏里量的（HANDOFF 第 54 节） |
| `calibrate.py` | 校准窗口（PySide6）：在选人截图上框每排首尾两个格子，中间等距补齐，存 `calibration.json` |

## 对外接口

- `recognize(img, cal, bank, threshold, ...) -> DraftView`；判「认出」用 `confident(score, gap, threshold)`：第一名比第二名高出 `SURE_GAP = 0.15` 且分数过 `min(threshold, SURE_SCORE)`。
- `TemplateBank` 学模板：控制面板「记住这次识别」、叠加层 / 顶栏「?」帮认（`app/live/draft` 的 `learn_slot` / `name_slot`）、`label` 命令。
- CLI：`calibrate <截图>`、`recognize <截图>`（输出标注图）、`label <截图...>`。
- `BanWatcher`、`roles.read_roles`、`buybackscan.Watcher` / `FeedCheck` 由 `app/live`（draft / timers）在工作线程或后台线程调。

## 依赖与分层

- 依赖 `core.config`、`core.vpk`（`gamefiles`）；第三方 `cv2`、`numpy`、`mss`，`calibrate` 用 PySide6（`requirements.txt` 里的 PySide6 现在只有它用，HANDOFF 第 12 节）。
- OCR 走 PowerShell 子进程，一次约 0.3~0.5 秒，放后台线程。

## 对应测试

- `tests/test_vision.py`：拿官方头像拼假选人截图（缩放 + 模糊 + 变暗 + JPEG）认回来；没头像时跳过（先 `update`）。
- `tests/test_grid.py`：合成网格画面认禁用 + 游戏资源解码。
- `tests/test_roles.py`：假 OCR 结果 → 每格位置、哪格是我。
- `tests/test_capture.py`：`Screen` / `WindowGrabber`。
- `tests/test_camera.py`：投影 / 反投影、按游戏里量的点反推镜头、真实截图里只认自己的血条（`tests/data/healthbars_own_and_ally.png`）。
- `tests/test_buyback.py`：含 `buybackscan`；`tests/test_webui.py` 里有帮认 / 记模板 / 买活自动记的 Live 流程。

## 注意事项 / 坑

- **Dota 要开「无边框窗口」**：独占全屏下截到的可能是黑屏，叠加层也盖不上去（HANDOFF 第 8 节）。
- 顶栏头像上盖着颜色条、天梯段位条、英雄徽章，比的时候按 `VIEWS` 三种挖法都比一次，取第一名和第二名拉得最开的（HANDOFF 第 39 节）。**只看分数靠不住**，要看拉不拉得开。
- 学模板会学坏：2026-10-29 帮认把空位裁图记成风行者，下一局空格全认成风行者。现在同一英雄认进好几格不算、像空位的裁图不记（`app/live/consts.EMPTY_LIKE`，HANDOFF 第 40 节）；改完没在真实选人里跑过。
- 出了选人就不再认顶栏，策略时间只补空格子：选完人以后顶栏会认乱，曾把对面阵容冲掉（HANDOFF 第 37 节）。
- 买活自动记的阈值 / 判据大多是实现假设，只有两次真买活样本、都是 1080p；不灵先看 `shots/buyback/`（HANDOFF 第 31.1~31.3 节）。灰不灰看头像饱和度**中位数**，不看平均（小图标会把平均抬到阈值附近）。
- 网格认禁用：玩家可自定义布局，不写死坐标；布局不变就沿用 `data/grid_map.json`（HANDOFF 第 19 节）。
- 截图按物理像素、校准存比例；多屏 + 不同缩放没测过，`monitor` 编号 mss 和 Electron 各自解释（HANDOFF 第 8 节）。

## 相关 HANDOFF 节

- HANDOFF 第 5 节（关键设计决策 4：校准 + 模板）
- HANDOFF 第 19 节（禁用识别）、第 20 节（自动认位置）
- HANDOFF 第 31.1 / 31.2 / 31.3 节（买活自动记）
- HANDOFF 第 37 / 37.1 / 37.2 节（局内重识别冲掉阵容、帮认）
- HANDOFF 第 39 节（顶栏去噪点）、第 40 节（「全是风行者」）

## 变更记录 (Changelog)

- 2026-11-02 11:18:41：初建，内容来自 HANDOFF 第 4、5、8 节 + 各节标题 + 源码模块说明。
