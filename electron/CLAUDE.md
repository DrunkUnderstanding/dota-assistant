[根目录](../CLAUDE.md) > **electron**

# electron：界面外壳

## 职责

只开窗口、加载 Python 本机服务的页面，**不含任何业务逻辑**。由 `python -m dota_assistant run` / `start` 启动（`app/webui.py` 的 `electron_command`），参数 `--server=http://127.0.0.1:端口`。叠加层是普通桌面窗口，盖在「无边框窗口」模式的 Dota 上面：**不注入游戏、不读游戏内存**（独占全屏下看不到，这是 Windows 的限制）。

为什么不用 Overwolf / ow-electron：它的游戏内叠加层通常要注入游戏进程，违反安全底线（HANDOFF 第 12 节、`docs/Overwolf调研.md`）。

## 关键文件

| 文件 | 管什么 |
|---|---|
| `main.js` | 主进程：开窗口、穿透切换、拖动 / 改大小、多屏定位、日志转发 |
| `preload.js` | 预加载脚本：只暴露 `window.overlayShell` 几个口子（`setLocked` / `setHitRects` / `dragStart` / `dragEnd` / `resizeStart` / `resizeEnd`），叠加层和顶栏计时条共用 |
| `package.json` | 只有 devDependency `electron` 44.2.0；`npm install` 装 Electron 本体（`node_modules/` 不进 git） |
| `package-lock.json` | npm 锁文件 |

## 窗口

| 窗口 | 页面 | 特性 |
|---|---|---|
| 叠加层 | `/overlay.html` | 透明、置顶、鼠标穿透、不抢焦点；点锁（或 F7）解锁后可拖动（能拖到别的屏）、拉右边 / 下边 / 右下角改宽高；固定状态下只有锁和 × 接点击 |
| 游戏画面野点 | `/world.html` | 透明置顶不抢焦点、**一直点穿**，盖满 Dota 所在的屏（`createWorld`）；`setContentProtection(true)` 不进截图 |
| 小地图野点 | `/minimap.html?scale=…&left=…&top=…` | 透明置顶不抢焦点、**一直点穿**（没有可点的东西），按 config 的 `minimap_x / y / size` 盖住 Dota 小地图 + 四周留边（`createMinimap`），放在 Dota 所在的屏；`setContentProtection(true)` 不进截图 |
| 买活 / 大招计时条 | `/topbar.html?scale=…` | 同样透明置顶不抢焦点，盖在屏幕最上面一条（游戏顶栏）；只有小条和打开的菜单接鼠标；不能拖，放在 Dota 所在的屏（config 的 `monitor`），不跟着叠加层走 |
| 控制面板 | `/panel.html` | 普通窗口，**关掉它 = 退出助手** |
| 队友战绩 | `/mates.html` | 控制面板 `window.open` 开的普通窗口，可以单独关；`setWindowOpenHandler` 只放行本机服务地址 |

## 对外接口

- IPC（页面 → 主进程，经 `preload.js`）：`overlay:locked`（固定 / 解锁）、`overlay:hit-rects`（可点区域，窗口内 CSS 像素）、`overlay:drag`（开始 / 结束拖动）、`overlay:resize`（`'w'` / `'h'` / `'wh'` / 结束）。
- 主进程 → Python：拖完 / 改完大小后 `main.js` 直接 POST `/api/action` `{type: 'overlay_moved', ...}`（位置按所在屏的比例 + 宽高 + 屏），Python 存进 `data/overlay.json`。
- 日志：往 stdout 写「级别 内容」一行，Python（`webui._pipe_electron`）收进 `logs/` 的日志文件；页面脚本报错也转过去。

## 依赖

- 只依赖 Electron 本身；页面都从 `SERVER` 加载。打包版：Electron 本体放 `ui/`，`main.js` / `preload.js` / `package.json` 复制进 `ui/resources/app/`（`packaging/build.py`）。
- 没装 Node.js 时 `start` 提示去装；npm 装完有时没下载 Electron 本体，`start.py` 补跑 `node node_modules/electron/install.js`（HANDOFF 第 12 节）。

## 对应测试 / 检查

- 没有 JS 单测。`tests/test_smoke.py` 也扫 `electron/*.js` 里的动作名（必须在 `Live.action` 分发表里）。
- 改了 JS 跑 `node --check electron/main.js electron/preload.js`；改了能点的部分要起 Electron 用真鼠标点一遍（根 CLAUDE.md 规则）。真鼠标测试要发真实输入（`mouse_event` / SendInput，`SetCursorPos` 不经过系统鼠标输入、不算数），并先 `SetProcessDpiAwareness(2)`（HANDOFF 第 14 节）；`docs/refactor/uitest/` 下有现成的脚本（如 `mouse.py`：SendInput 真鼠标，按下前确认那一点是 Electron 窗口）。

## 注意事项 / 坑

- **穿透切换靠主进程每 30ms 看系统光标在不在页面报的可点区域里**，不靠页面 `mouseover` / `mouseout`（会循环补发 `mouseout`，按钮永远点不到，2026-10-22 实测出的问题，HANDOFF 第 14 节）。
- `MIN_WIDTH = 400` 要和 Python 的 `app/live/consts.OVERLAY_WIDTH` 下限一致；`MIN_HEIGHT = 160`；`TOPBAR_HEIGHT = 410`（按 1080 高屏幕上顶栏、小条、右键菜单的位置算的）。
- 窗口始终收在它所在的那块屏里面（松手时光标在哪块屏就算哪块）；4K 200% 缩放下曾伸出屏幕被切，已收回屏内（HANDOFF 第 12 节）。
- 多屏 + 不同缩放没测过；`monitor` 编号 mss 和 Electron `screen.getAllDisplays()` 各自解释，顺序可能不一致（HANDOFF 第 8 节）。
- 关闭叠加层后 `user_hidden` 一直保留，下一局也不自动弹，要按 F8（HANDOFF 第 14 节）。
- `ELECTRON_DISABLE_SECURITY_WARNINGS = 'true'`：页面是本机服务的固定文件，免得每次启动刷警告。
- 状态：还没在 Dota 上方实测（HANDOFF 第 2 节当前状态）。

## 设计记录（原「相关 HANDOFF 节」）

本模块的设计 / 验证 / 待验证记录在 [NOTES.md](NOTES.md)（2026-11-04 从 HANDOFF 拆过来，节号不变）。下面列的节号不在本目录 NOTES 里的，按 [HANDOFF 节号索引](../docs/HANDOFF.md#节号索引) 找。

- HANDOFF 第 12 节（界面改版：网页 + Electron）
- HANDOFF 第 14 节（叠加层拖动 / 固定）
- HANDOFF 第 22.1 / 22.2 节（适配屏幕、拉宽、拖到副屏 + 自由改宽高）
- HANDOFF 第 31 节（顶栏计时窗口）、第 46 节（最窄 400）

## 变更记录 (Changelog)

- 2026-11-02 11:18:41：初建，内容来自 HANDOFF 第 4、12、14 节 + `main.js` / `preload.js` 文件头注释。
- 2026-11-04：HANDOFF 按模块拆分，过程记录搬进各模块 `NOTES.md`；「相关 HANDOFF 节」改成「设计记录」，指向 NOTES 和 HANDOFF 节号索引。
