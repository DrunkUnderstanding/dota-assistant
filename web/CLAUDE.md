[根目录](../CLAUDE.md) > **web**

# web：界面页面

## 职责

界面全部是原生 HTML / CSS / JS（ES module），**不用前端框架、不打包**，改完刷新页面就生效（HANDOFF 第 12 节）。页面由 `app/webui.py` 的本机服务提供，Electron（`electron/main.js`）开窗口加载；状态以 Python 那边为准，页面只负责「把快照画出来、把操作发回去」，不存局面。

设计（从 Overwolf 学的）：深色底配金色强调色、到处都有英雄头像、推荐做成卡片（头像、胜率、胜率条、理由标签）。

## 关键文件

| 文件 | 管什么 |
|---|---|
| `overlay.html` | 叠加层窗口（透明、置顶、穿透）：选人推荐卡片 / 本局要点 / 局内提醒 / 死亡复盘；脚本内联（`<script type="module">`）；`?preview=1` 在浏览器里垫一张像游戏的背景预览 |
| `world.html` | 游戏画面里的野点框：盖满 Dota 那块屏，每 0.1 秒拉 `/api/world`，SVG 描出刷新框（和按住 Alt 时 Dota 的黄框同一个）/ 莲花池的圈 + 框上边一块牌子写几秒；整个窗口点穿；`?preview=1&bg=图片` 塞假数据（HANDOFF 第 54 节） |
| `minimap.html` | 小地图野点（config `minimap_marks`，默认关）：盖在 Dota 小地图上，野区提醒到点时在野点 / 莲花池上画圈 + 一行字（倒数页面自己走）；整个窗口点穿；`?scale=&left=&top=` 是窗口在 1080 下的左上角，`?preview=1&bg=图片` 塞假数据（HANDOFF 第 53 节） |
| `topbar.html` | 买活 / 大招计时条：盖在游戏顶栏，每个英雄头像下面一个买活小条、对面的再加一个大招小条（右键菜单改等级 / 勾减冷却）；脚本内联；`?scale=` 按屏幕高度换算、`?preview=1` 塞假数据 |
| `panel.html` + `panel.js` + `panel.css` | 控制面板（普通窗口，关掉 = 退出助手）：10 个格子、位置 / 阵营 / 识别模式、叠加层设置、顶栏计时开关、设置窗口、右侧叠加层预览 |
| `mates.html` + `mates.js` + `mates.css` | 队友战绩独立页（控制面板顶栏「队友战绩」`window.open` 开）：左边查过的人，右边简况 + 最近 20 局，点一局看整局；地址栏 `#p=好友代码[&m=比赛编号]` |
| `view.js` | 叠加层内容的渲染（`renderOverlay` / `applyOverlayStyle` / `bindOverlayClicks` / `portrait` / `esc`），叠加层窗口和控制面板预览共用；装备图标片段（`★` 针对这局、`·` 之后常见、`✚` 按死法补，和 `domain/itembuild.py` 同一套记号） |
| `api.js` | 和 Python 通信：`connect`（SSE `/api/events`，自动重连）、`act(type, payload)`（POST `/api/action`）、`getJSON` |
| `base.css` | 配色等公共样式 |

## 对外接口（页面 ↔ 后端）

- 收：SSE `/api/events` 推完整快照（`Live.snapshot()`）；`/api/heroes`（帮认 / 补全）、`/api/config`（叠加层配置）、`/api/mates`、`/api/match/<编号>?p=`。
- 发：`act('动作名', {...})` → `Live.action`；`panel.js` 的 `run`、`topbar.html` 的 `send` 是它的包装。动作清单见 [app/live/CLAUDE.md](../dota_assistant/app/live/CLAUDE.md)。
- 和 Electron：通过 `window.overlayShell`（`electron/preload.js` 暴露）报固定 / 解锁、可点区域、开始 / 结束拖动、改大小。

## 对应测试 / 检查

- `tests/test_smoke.py`：前端每一处 `act` / `run` / `send` 调用都必须被归类、动作名都在 `Live.action` 分发表里；前端请求的每个 `/api/xxx` webui 都有；`mates.js` 发的 op webui 都认（扫 `web/*.js`、`web/*.html`、`electron/*.js`）。
- `tools/browser_check.py`（阶段验收 G6）：临时运行目录起服务，无头 Edge / Chrome 打开控制面板、叠加层、顶栏、队友战绩页，收控制台报错。
- `tests/test_webui.py` 覆盖后端视图；页面本身没有 JS 单测。

## 注意事项 / 坑

- 改了 JS 跑 `node --check`（根 CLAUDE.md 规则）；`overlay.html` / `topbar.html` 的脚本是内联的，`node --check` 管不到，用 `tools/browser_check.py` 看控制台报错。**改了能点的界面要起 Electron 用真鼠标点一遍**。
- 穿透切换不能靠页面 `mouseover` / `mouseout`：窗口一从穿透切到接鼠标 Chromium 就补发一次 `mouseout`，按钮永远点不到。现在是页面报「可点区域」、Electron 主进程按系统光标位置切（HANDOFF 第 14 节）。布局变了要重报区域。
- 选人推荐放不下时上下滚、不缩小字，「推荐」「针对这局」能收起成小头像；叠加层最窄 400（HANDOFF 第 46 节，和 `electron/main.js` `MIN_WIDTH`、`app/live/consts.OVERLAY_WIDTH` 一致）。
- 推荐卡片头像上的 × 是「这局不想玩」（`skip_hero`），点名字恢复（HANDOFF 第 44 节）。
- 控制面板「语音」开关现在 `hidden`（玩家决定默认关，HANDOFF 第 2 节当前状态）。
- 拼 HTML 的动态文字用 `view.js` 导出的 `esc()` 转义。
- 打包时 `web/` 整个复制进产物（`packaging/build.py`），本目录的文件都会被带上。

## 相关 HANDOFF 节

- HANDOFF 第 12 节（界面改版：网页 + Electron）、第 14 节（叠加层拖动 / 固定）、第 23 节（底色 / 不透明度）
- HANDOFF 第 22.1 / 22.2 节（适配屏幕、拉宽、拖到副屏）、第 24.1 节（剩余位置展开收起）
- HANDOFF 第 31 / 32.1 节（顶栏小条）、第 34 节（装备图标）、第 37.2 节（叠加层帮认）
- HANDOFF 第 38.1 节（队友战绩独立页）、第 42 节（顶栏计时开关）、第 44 节、第 46 节

## 变更记录 (Changelog)

- 2026-11-02 11:18:41：初建，内容来自 HANDOFF 第 4、12、14 节 + 页面文件头注释。
