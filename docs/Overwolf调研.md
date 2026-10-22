# Overwolf 官方开源示例调研（2026-10-22）

玩家要求（原话）：「那你就学习一下overwolf的github开源示例应用看看」。
范围：GitHub `overwolf` 组织的公开仓库、dev.overwolf.com 文档。没有安装 Overwolf，没有解包任何商业插件。
本文是调研记录，**下面的建议都还没被玩家确认，也没有实现**。

## 看了哪些仓库

| 仓库 | 内容 |
|---|---|
| [overwolf/sample-app](https://github.com/overwolf/sample-app) | 原生平台官方示例，有 `native/`（纯 JS）和 `ts/`（TS + webpack）两套 |
| [overwolf/front-app](https://github.com/overwolf/front-app) | 官方的 UI/UX 最佳实践示例（TS + React） |
| [overwolf/events-sample-apps](https://github.com/overwolf/events-sample-apps) | 各游戏的游戏事件（GEP）示例，含 `dota-events-sample-app-master` |
| [overwolf/ow-electron-packages-sample](https://github.com/overwolf/ow-electron-packages-sample) | ow-electron 的 overlay / GEP / recorder 示例 |
| [overwolf/ow-electron-packages-types](https://github.com/overwolf/ow-electron-packages-types) | ow-electron 各包的类型定义，`modules/overlay.d.ts` 注释最全 |

## 关键结论

**1. 它们的游戏内叠加层全部依赖注入，我们不能用。**
ow-electron 的 overlay 是离屏渲染后合成进游戏画面，代码里直接写着 `event.inject(); // Inject overlay immediately`。
所以只学它的交互设计，技术上继续用普通 Electron 窗口（第 1 节安全底线）。

**2. 窗口分工和我们的结构基本一致。**
Overwolf 应用都有一个后台控制器：检测游戏启停、注册热键、接游戏事件、缓存事件再转发给各窗口；
界面窗口打开时**先补读已缓存的数据，再订阅后续事件**；**新一局开始时清空缓存**。
我们的 Python 就是后台控制器，SSE 推快照就是事件总线。SSE 连上时已经会先推一份完整快照；「新一局清空」我们还没做。

**3. 游戏内窗口的交互做法**（普通 Electron 都能实现，不需要注入）：
- 默认穿透，按热键进入「可交互」模式，再按一次或点到窗口外面时恢复穿透（Overwolf 叫 `ReleaseOnHidden` / `ReleaseOnLostFocus`）。
  Dota 属于「标准模式」游戏（有鼠标指针），官方说不需要独占键鼠。
- 窗口可拖动，并记住位置；按「显示器组合」分别记，换显示器配置时才回默认位置；拖动时限制在游戏所在屏幕内。
- 有多块屏幕时，控制面板放到游戏不在的那块屏上。
- 热键可在设置里改，冲突时提示；界面上常驻热键提示。

**4. 设计规范里的具体建议：**
- 原文 "Don't create full-screen transparent windows"、"Keep your windows small"：不要做全屏透明窗口，窗口尽量小。
  **我们现在的叠加层窗口高度一直拉到屏幕底部（第 12 节的实现假设），正好是它不建议的做法。**
- 原文 "avoid using semi-transparent widgets or overlays that reduce clarity"：不建议半透明。我们现在的卡片底色是 88% 不透明。
- 原文 "avoid covering key game HUD areas"、"Allow users to adjust overlay positioning"：别挡游戏 UI，位置要让玩家能调。
- 显示数据来源状态：游戏中途启动时数据可能不全，应该给玩家提示。
- 字体、字号、对比度：官方文档里**没有**具体规范。

**5. Dota 2 选人数据（GEP 的 draft / bans）的来源：文档没说清楚。**
- 文档只说 Overwolf 的 Dota 功能需要 `-gamestateintegration` 启动项，**没说明 draft / bans 来自 GSI 还是其他渠道**。
- 示例数据里有两队英雄，但没说明是玩家视角还是观战视角。**玩家视角能不能实时拿到对面选人，查不到定论**，所以继续靠截屏识别。
- 文档写明的限制：选人阶段其他玩家的 steamId / 名字为空，到策略时间才有。
- 来源：[Dota 2 GEP 文档](https://dev.overwolf.com/ow-native/live-game-data-gep/supported-games/dota-2)、[Dota 2 合规页](https://dev.overwolf.com/ow-native/guides/game-compliance/dota-2)。

**6. 工程结构：** 示例用 TS + webpack，每个窗口一个入口。我们的页面由 Python 直接提供，引入打包工具收益很低。
值得借鉴的只有：消息类型集中成常量表；`main.js` 按窗口拆成「创建 / 显隐 / 交互切换 / 记位置」几个函数；
以后要加 preload 时用 `contextBridge`，不开 `nodeIntegration`。

## 候选改进（待玩家确认，未实现）

1. **叠加层改成按内容定尺寸的小窗**，不再一直拉到屏幕底部。改 `electron/main.js` 和 `web/overlay.html`（网页把内容高度报给 main.js）。
2. **临时可交互模式**：热键切换「穿透 / 可交互」，可交互时显示拖动条，点到窗口外自动恢复穿透；另加一个「强制恢复穿透」热键。改 `main.js`、网页、Python 热键。
3. **拖动并记住位置**：按显示器组合分别保存；控制面板默认放在游戏不在的屏幕上。改 `main.js`。
4. **新一局自动清空局面**：GSI 显示进入新一局的选人阶段时清空 10 个格子和禁用；面板上加数据来源状态灯。改 `live.py`、网页。
5. **启动时检查 `-gamestateintegration`**：读 dota2.exe 的进程启动参数（只读参数，不读游戏内存），缺了就在控制面板提示怎么加。改 Python。
