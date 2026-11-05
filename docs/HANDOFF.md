# 开发交接文档

> 最后更新：2026-11-04。写给接手开发的人（包括 AI 助手）。使用说明见 [README](../README.md)。
>
> **这份文档只回答三件事：做什么、现在到哪了、下一步干什么。** 每个功能「为什么这么做、怎么验证的、还差什么」的详细记录在各模块目录的 `NOTES.md`（2026-11-04 从本文拆出去的，原文没删改）；职责 / 文件表 / 接口 / 坑在各模块的 `CLAUDE.md`。代码注释里的「HANDOFF 第 N 节」用文末的[节号索引](#节号索引)找。

## 1. 目标与范围

玩家原始需求：做一款**实时帮助玩 Dota 2** 的 AI 软件，包含 ① 英雄克制选取 ② 当局出装推荐 ③ 当前态势下的最佳打法；
**根据屏幕里的英雄阵容实时给建议**。

已确定的决定（来自和玩家的讨论）：

| 项 | 决定 |
|---|---|
| 用途 | 先自用 |
| 先做什么 | 选人推荐（①），出装和打法以后再说 |
| 模式 | 主要打**天梯全英雄选择** |
| 展示 | **叠加层 + 语音** 混合 |
| 项目 | 独立目录，与其他项目分开 |
| 玩家 | 好友代码（不公开，见本机 config.toml 的 account_id），超凡入圣 4 星，主打一号位，也打三 / 四号位 |
| 开发 / 游戏 | 开发在另一台电脑完成，**游戏电脑是另一台**（本文档就是为此写的） |

安全底线（不要越过）：只读屏幕截图 + Valve 官方 GSI；**不读游戏内存、不注入、不模拟键鼠输入、不显示玩家本来看不到的信息**。

## 2. 当前状态

✅ 完成并实测过　🟡 代码完成、还没在真实对局里验证　⏸ 暂时关着　❌ 未开始。逐项的验证细节在对应 NOTES 的「当前状态」小节。

**选人**

| 功能 | 状态 | 还差什么 | 记录 |
|---|---|---|---|
| 数据层（OpenDota 克制 / 配合、STRATZ 分位置、个人数据、头像） | ✅ | — | [sources](../dota_assistant/sources/NOTES.md) |
| 打分引擎 + 推荐四小组（熟练 / 不熟练 × 胜率最高 / 克制最强） | ✅ 回测过 | 四小组、熟练度分组没实战看过 | [domain](../dota_assistant/domain/NOTES.md) |
| 屏幕识别（校准、模板、学习模板、去噪点、防误记） | 🟡 | 「全是风行者」「齐天大圣垃圾模板」修完，下一局天梯看日志 | [vision](../dota_assistant/vision/NOTES.md)、[app/live](../dota_assistant/app/live/NOTES.md) |
| 禁用识别（选人网格红斜线） | 🟡 | 没在有禁用的真实天梯里实时跑过 | [vision](../dota_assistant/vision/NOTES.md) |
| 对面分路 + 对线打法、剩余位置、机制说明 / 「如果选 X」 | 🟡 | 没在真实选人 / Electron 里看过 | [domain](../dota_assistant/domain/NOTES.md) |
| 这局不想玩、对面最后一手（猜）、叠加层上改位置 | 🟡 | 真实选人里没用过 | [domain](../dota_assistant/domain/NOTES.md) |
| 叠加层 + 控制面板（网页 + Electron）、上下滚 / 收起 | ✅ / 🟡 | 没在 Dota 上方实测；副屏拖动、真鼠标拉宽高没跑完 | [web](../web/NOTES.md)、[electron](../electron/NOTES.md) |

**局内**

| 功能 | 状态 | 还差什么 | 记录 |
|---|---|---|---|
| GSI | ✅ | 局内字段没核对；朋友电脑「找 Dota 目录认了空壳」修完没验证 | [game](../dota_assistant/game/NOTES.md)、[core](../dota_assistant/core/NOTES.md) |
| 热键 / 日志 | ✅ | — | [game](../dota_assistant/game/NOTES.md)、[core](../dota_assistant/core/NOTES.md) |
| 语音 | ⏸ 默认关 | `config.toml` 写 `voice = true` 能开 | [game](../dota_assistant/game/NOTES.md) |
| 缺口提醒 / 本局要点 / 死亡复盘 / 出装 | 🟡 **默认不弹**（`ingame_tips = false`，只记日志） | 没接过真实游戏，GSI 字段没在真实推送里核对 | [domain 局内](../dota_assistant/domain/NOTES-ingame.md) |
| 野区提醒（Dota 时钟正下方的倒数；第二行抢莲花） | 🟡 | 实战看位置挡不挡、秒数和按住 Alt 的一样不 | [domain 局内](../dota_assistant/domain/NOTES-ingame.md) |
| 游戏画面野点框 / 小地图野点 | 🟡 **默认关** | 玩家说「触发 VAC 了」，先弄清提示原话和有没有封禁记录再决定开不开 | [vision](../dota_assistant/vision/NOTES.md)、[domain 局内](../dota_assistant/domain/NOTES-ingame.md) |
| 买活计时（含截图自动记）/ 对面大招冷却 | 🟡 | 补漏改完没在真实对局跑过；`hero.level` 没见过 | [domain 局内](../dota_assistant/domain/NOTES-ingame.md) |
| 局内打法 | ❌ | 本局要点 + 缺口提醒算雏形；敌方装备识别、热键提问待做 | [domain 局内](../dota_assistant/domain/NOTES-ingame.md) |

**其它**

| 功能 | 状态 | 还差什么 | 记录 |
|---|---|---|---|
| 知识库（7.41f：127 英雄、257 件物品） | ✅ | — | [kb](../dota_assistant/kb/NOTES.md) |
| 设置窗口 + AI 来源可选 | 🟡 | Anthropic API / OpenAI 兼容接口没真实调过 | [ai](../dota_assistant/ai/NOTES.md) |
| 免安装打包 | 🟡 | 没在没装 Python / Node 的干净电脑上试过 | [packaging](../packaging/NOTES.md) |
| 不填 key 也能用 | ✅ | — | [app](../dota_assistant/app/NOTES.md) |
| 更新助手 + 查到新版本自己弹窗 | 🟡 | 更新真实发布走通过；自己弹窗下次发 Release 时看 | [app](../dota_assistant/app/NOTES.md) |
| 队友战绩（独立页） | 🟡 | 没公开比赛数据的人显示成什么样没看过 | [domain](../dota_assistant/domain/NOTES.md) |
| 对线优劣回测 + 实战记录 | 🟡 | GSI 的 kills / assists / level 没在真实推送见过 | [research](../dota_assistant/research/NOTES.md) |
| 工程化重构（分层子包 + 门禁） | 🟡 已合并 main | 玩家用重构后的版本打一局天梯 | [docs/refactor](refactor/NOTES.md) |

## 3. 下一步

**一句话：大部分功能代码都写完了，最缺的是真实天梯里跑一遍。** 每条要看什么在对应 NOTES 的「待实战验证 / 下一步」小节里写全了。

1. **下一局天梯，选人阶段**看日志：识别有没有漏认 / 乱认 / 同一英雄进好几格、Dota 没开时不再识别（[vision](../dota_assistant/vision/NOTES.md)、[app/live](../dota_assistant/app/live/NOTES.md)）；推荐四小组、对面最后一手、这局不想玩、分路 / 剩余位置合不合常识（[domain](../dota_assistant/domain/NOTES.md)）。
2. **下一局天梯，局内**：野区倒数位置和秒数、买活 / 大招小条、局内卡片确实不弹只记日志（[domain 局内](../dota_assistant/domain/NOTES-ingame.md)、[app/live](../dota_assistant/app/live/NOTES.md)）。
3. **弄清「触发 VAC」那回事**，再决定游戏画面野点框开不开回来（[vision](../dota_assistant/vision/NOTES.md)）。
4. **发版后**：开着旧版看会不会自己弹更新窗口（[app](../dota_assistant/app/NOTES.md)）；新打包版发给朋友，看 Dota 目录 / 阵营对不对（[core](../dota_assistant/core/NOTES.md)）；干净电脑上试免安装版（[packaging](../packaging/NOTES.md)）。
5. **有 `D:\dota-golden` 的机器**上跑 `python tools/golden/run.py check`，确认第 41 / 43 / 45 / 50 节带来的快照差异都是该有的再更新。
6. **远期**：队友预选识别（避免推荐撞车）；局内打法（需求③，做成事件提醒：肉山时间、对面核心没 BKB 等，不做持续解说）；可选：大模型生成一句话推荐理由（现在模板拼的已够用）。

## 4. 关键设计决策（为什么这么做）

1. **统计在 logit 空间相加 + 贝叶斯收缩**。克制收缩强度 `K_VS=400`、配合 `K_SYN=1100`，由「单局方差 / 真实效应方差」估出来（注释里有推导）。
   目的是不让小样本的极端胜率霸榜。个人胜率 `K_ME=40`，收缩很狠，因为混着队友影响。
2. **克制 / 配合数据自己聚合，不用现成接口**。OpenDota 的 `/heroes/{id}/matchups` 是职业比赛、样本只有几十局；
   改为用 explorer 直接对 `public_matches`（天梯全英雄选择 `game_mode=22, lobby_type=7`，时长 ≥15 分钟）做 SQL 聚合。
   explorer 单次查询会超时，所以按 `match_id` 分块（100 万一块，约 30 万局，6~9 秒），超时自动对半拆。
   时间窗口用 `IDS_PER_DAY = 155 万` 从最新 match_id 往回推（2026-10 实测值，以后可能漂移）。
3. **位置用 STRATZ**。公开对局没有位置信息，OpenDota 的定位标签太宽（斧王也挂 Carry）。STRATZ 有分段 × 位置的场次胜率，
   也能给出玩家每局的位置（约 28% 的局没解析，按英雄主位置 ≥60% 归类，否则不计）。没有 token 时退回标签粗筛 `fits_by_roles()`。
4. **识别用「校准 + 模板」而不是猜布局或上大模型**。开发时手上没有真实截图，不敢写死坐标；
   让玩家在自己的截图上框首尾两个格子、中间等距补齐，坐标存比例，同宽高比换分辨率不用重标。
   相似度是缩到 32×18、逐通道标准化后的余弦相似度，几毫秒一次。初始模板是官方头像，真实画面靠「学习模板」补。
5. **GSI 只用来知道阵营 / 阶段 / 我选的谁**。玩家身份下 GSI 拿不到敌方信息，这是 Valve 有意的限制，别想绕过。
6. **语音不用 Python TTS 库**。PySide6 的 QtTextToSpeech 在 Addons 包里，开发机装 Addons 撞了 Windows 长路径限制装不上；
   改成常驻 PowerShell + System.Speech，零依赖。
7. **热键用 RegisterHotKey**，不用 `keyboard` 之类的全局键盘钩子，降低被反作弊误判的可能。

## 5. 已知问题与风险

- `IDS_PER_DAY` 是写死的估计值；如果天梯量变化，「最近 14 天」会偏差几天，影响不大。
- STRATZ token 写在 `config.toml`，有效期到 **2027-10**；过期后到 stratz.com/api 重新领。token 曾在聊天里出现过，介意就重新生成。
- 网络不稳时 dota2.com / STRATZ / OpenDota 会直接断连接（SSL EOF / 10054），`kb._get` 和 `data._session` 已加连接层重试。
- STRATZ 个人对局接口会整体超时（2026-10-22 实测 `player.matches` 连 `matchCount` 都报 `NPGSQL TIMEOUT`）：
  `fetch_player` 自动退回 OpenDota，这次个人数据不带位置（全记「未解析」，按英雄主位置归类）；STRATZ 恢复后 `update --player` 重拉即可。
- OpenDota 免费额度：每分钟 60 次左右，`update` 用约 60 次 explorer 查询，正常够用；429 会自动退避重试。
- 克制 / 配合数据不分位置（公开对局没有位置），分位置只修正了英雄自身强度。
- 叠加层在独占全屏下不可见，这是 Windows 的限制，只能用无边框窗口。
- 高 DPI：截图按物理像素、校准存比例，理论上不受缩放影响；叠加层按逻辑像素放置。多屏 + 不同缩放没测过。
  `monitor` 编号对截屏（mss）和叠加层（Electron `screen.getAllDisplays()` 的顺序）各自解释，多屏时两边顺序可能不一致。
- Valve 对第三方叠加层的政策会变，定期留意；本项目只做建议、不读内存，风险较低但不是零。

## 6. 文档放哪、怎么维护

| 想知道 | 看哪 |
|---|---|
| 项目规则、门禁、分层、模块索引 | 根目录 [CLAUDE.md](../CLAUDE.md) |
| 某个模块管什么、文件表、接口、坑、对应测试 | 模块目录的 `CLAUDE.md` |
| 某个功能为什么这么做、怎么验证的、还差什么 | 模块目录的 `NOTES.md`（`domain` 分两份：选人 `NOTES.md`、局内 `NOTES-ingame.md`） |
| 常用命令 | [cli/NOTES.md](../dota_assistant/cli/NOTES.md)、[cli/CLAUDE.md](../dota_assistant/cli/CLAUDE.md) |
| 重构计划、旧文件 → 新位置 | [refactor/PLAN.md](refactor/PLAN.md)、[refactor/NOTES.md](refactor/NOTES.md) |
| 组合研究（精灵老鹿等） | [research/](research/) |

**以后怎么写**：
- 做完一个功能 / 修完一个问题：在**主要涉及的那个模块**的 `NOTES.md` 末尾追加一节，节号接着全局往下编（下一个是 61），标题带日期和关键函数；同时把节号加进下面的索引。
- 状态变了：改本文第 2 节那一行（一句话），细节写进模块 NOTES 的「当前状态」。
- 新的待验证项：写进模块 NOTES 的「待实战验证 / 下一步」；要紧的在本文第 3 节加一句。
- 本文保持在一两屏内，**不要再往这里堆过程记录**。

## 节号索引

原 HANDOFF 的节号（代码注释、提交说明、各模块 CLAUDE.md 里引用的「第 N 节」）现在在哪个文件。原第 1 / 5 / 8 节还在本文（现第 1 / 4 / 5 节）；原第 2 节每行的验证细节、原第 7 节每一条，分到了各模块 NOTES 的「当前状态」「待实战验证 / 下一步」小节。原文有几处节号重复（两个 9、两个 18、两个 28，22.1 / 22.2 在第 23 节下面），表里都列了。

| 节 | 标题 | 在哪 |
|---|---|---|
| 2.1 | 回测结果 | [research/NOTES](../dota_assistant/research/NOTES.md) |
| 3 | 架构 | [app/live/NOTES](../dota_assistant/app/live/NOTES.md) |
| 4 | 模块地图 | [docs/refactor/NOTES](refactor/NOTES.md) |
| 6 | 在游戏电脑上接手：检查清单 | [vision/NOTES](../dota_assistant/vision/NOTES.md) |
| 9 | 常用命令 | [cli/NOTES](../dota_assistant/cli/NOTES.md) |
| 9 | 知识库 | [kb/NOTES](../dota_assistant/kb/NOTES.md) |
| 10 | 大模型选人盲测 | [research/NOTES](../dota_assistant/research/NOTES.md) |
| 11 | 局内缺口提醒 | [domain/NOTES-ingame](../dota_assistant/domain/NOTES-ingame.md) |
| 11.1 | 实时版 | [domain/NOTES-ingame](../dota_assistant/domain/NOTES-ingame.md) |
| 12 | 界面改版：网页 + Electron | [web/NOTES](../web/NOTES.md) |
| 13 | 一键启动 | [app/NOTES](../dota_assistant/app/NOTES.md) |
| 14 | 叠加层拖动 / 固定 | [electron/NOTES](../electron/NOTES.md) |
| 15 | 本局要点卡片 | [domain/NOTES-ingame](../dota_assistant/domain/NOTES-ingame.md) |
| 15.1 | AI 打法 | [domain/NOTES-ingame](../dota_assistant/domain/NOTES-ingame.md) |
| 16 | 选人时的「为什么选 / 选了怎么打」 | [domain/NOTES](../dota_assistant/domain/NOTES.md) |
| 17 | 日志 | [core/NOTES](../dota_assistant/core/NOTES.md) |
| 18 | 推荐卡片上的「对位」克制 | [domain/NOTES](../dota_assistant/domain/NOTES.md) |
| 18 | 死亡复盘 | [domain/NOTES-ingame](../dota_assistant/domain/NOTES-ingame.md) |
| 19 | 禁用识别：选人网格的红斜线 | [vision/NOTES](../dota_assistant/vision/NOTES.md) |
| 20 | 自动认位置 | [vision/NOTES](../dota_assistant/vision/NOTES.md) |
| 21 | 免安装打包 + 设置窗口 | [packaging/NOTES](../packaging/NOTES.md) |
| 22 | 对面分路 + 对线打法 | [domain/NOTES](../dota_assistant/domain/NOTES.md) |
| 22.1 | 叠加层适配屏幕 + 可拉宽 | [electron/NOTES](../electron/NOTES.md) |
| 22.2 | 拖到副屏 + 自由改宽高 | [electron/NOTES](../electron/NOTES.md) |
| 23 | 叠加层底色 / 不透明度可调 | [electron/NOTES](../electron/NOTES.md) |
| 24 | 剩余位置推荐 | [domain/NOTES](../dota_assistant/domain/NOTES.md) |
| 24.1 | 每个空位置 3 + 3、展开收起 | [domain/NOTES](../dota_assistant/domain/NOTES.md) |
| 25 | 出装推荐 | [domain/NOTES-ingame](../dota_assistant/domain/NOTES-ingame.md) |
| 25.1 | 按死法补装 | [domain/NOTES-ingame](../dota_assistant/domain/NOTES-ingame.md) |
| 26 | 推荐分两组：推荐 + 针对这局 | [domain/NOTES](../dota_assistant/domain/NOTES.md) |
| 27 | 野区计时提醒：封野 / 拉野 / 双拉 / 屯野 | [domain/NOTES-ingame](../dota_assistant/domain/NOTES-ingame.md) |
| 28 | 对线优劣：能不能猜、屯野 / 莲花怎么分 | [research/NOTES](../dota_assistant/research/NOTES.md) |
| 28 | 本局要点按阶段弹 | [domain/NOTES-ingame](../dota_assistant/domain/NOTES-ingame.md) |
| 29 | 后期内容补齐：STRATZ 默认只给 35 分钟 | [sources/NOTES](../dota_assistant/sources/NOTES.md) |
| 30 | 后期团战：要等谁的大招、控制留给谁、躲谁 | [domain/NOTES-ingame](../dota_assistant/domain/NOTES-ingame.md) |
| 31 | 买活计时：顶栏头像下面 | [domain/NOTES-ingame](../dota_assistant/domain/NOTES-ingame.md) |
| 31.1 | 买活自动记：截图认顶栏 | [domain/NOTES-ingame](../dota_assistant/domain/NOTES-ingame.md) |
| 31.2 | 自动记改成认消息栏「买活了」 | [domain/NOTES-ingame](../dota_assistant/domain/NOTES-ingame.md) |
| 31.3 | 漏记：格子没填的英雄、顶栏没看出死过的 | [domain/NOTES-ingame](../dota_assistant/domain/NOTES-ingame.md) |
| 32 | 对面大招冷却：顶栏对面头像下面第二条 | [domain/NOTES-ingame](../dota_assistant/domain/NOTES-ingame.md) |
| 32.1 | 顶栏小条加大、换成图标 | [domain/NOTES-ingame](../dota_assistant/domain/NOTES-ingame.md) |
| 33 | 出装建议要「大数据 + 本局」两种依据 | [domain/NOTES-ingame](../dota_assistant/domain/NOTES-ingame.md) |
| 34 | 出装推荐显示装备图标 | [web/NOTES](../web/NOTES.md) |
| 35 | 不填 key 也能用 | [app/NOTES](../dota_assistant/app/NOTES.md) |
| 36 | 找 Dota 目录认了空壳，GSI 连不上、阵营全按天辉算 | [core/NOTES](../dota_assistant/core/NOTES.md) |
| 37 | 对面大招小条认错 / 缺：局内重识别把阵容冲掉了 | [app/live/NOTES](../dota_assistant/app/live/NOTES.md) |
| 37.1 | 认不出来玩家帮认，记成模板 | [app/live/NOTES](../dota_assistant/app/live/NOTES.md) |
| 37.2 | 选人界面：看得出哪格没认出，在叠加层上帮认 | [app/live/NOTES](../dota_assistant/app/live/NOTES.md) |
| 38 | 队友战绩：手动填好友代码查 | [domain/NOTES](../dota_assistant/domain/NOTES.md) |
| 38.1 | 改成独立页：点一局看整局，列表里有每局 KDA 和表现 | [domain/NOTES](../dota_assistant/domain/NOTES.md) |
| 38.2 | 加上 STRATZ 评分（可选） | [domain/NOTES](../dota_assistant/domain/NOTES.md) |
| 39 | 选人顶栏去噪点：徽章 / 段位条 / 颜色条 | [vision/NOTES](../dota_assistant/vision/NOTES.md) |
| 40 | 「全是风行者」：帮认把空位记成了英雄 | [vision/NOTES](../dota_assistant/vision/NOTES.md) |
| 41 | 更新助手本身 | [app/NOTES](../dota_assistant/app/NOTES.md) |
| 42 | 顶栏计时开关 | [web/NOTES](../web/NOTES.md) |
| 43 | 推荐按熟练度分组 + 每个推荐的胜率怎么来的 | [domain/NOTES](../dota_assistant/domain/NOTES.md) |
| 43.1 | 打分改了两处：冷门打折按选取率、配合不再打七折 | [domain/NOTES](../dota_assistant/domain/NOTES.md) |
| 43.2 | 冷门打折改成最多扣 2 点、门槛倒数第 40 个英雄 | [domain/NOTES](../dota_assistant/domain/NOTES.md) |
| 44 | 选人时「这局不想玩」：点 × 去掉、后面的顺位补上来 | [domain/NOTES](../dota_assistant/domain/NOTES.md) |
| 45 | 胜率是不是「对位直接相加」+ 叠加层顶上的「阵容」胜率 | [domain/NOTES](../dota_assistant/domain/NOTES.md) |
| 46 | 选人叠加层放不下就上下滚 + 「推荐」「针对这局」能收起 | [web/NOTES](../web/NOTES.md) |
| 47 | 龙骑士认成齐天大圣：赛后帮认的格子在不选人时一直被记成模板 | [app/live/NOTES](../dota_assistant/app/live/NOTES.md) |
| 48 | Dota 没开也在识别 | [app/live/NOTES](../dota_assistant/app/live/NOTES.md) |
| 49 | 给我的推荐分四小组：熟的 / 不熟的 × 胜率最高 / 克制最强 | [domain/NOTES](../dota_assistant/domain/NOTES.md) |
| 50 | 对面最后一手（猜）+ 假设对面拿了它给我的推荐 | [domain/NOTES](../dota_assistant/domain/NOTES.md) |
| 50.1 | 猜的头像上点 × 排除 + 上面四组改成各 3 个 | [domain/NOTES](../dota_assistant/domain/NOTES.md) |
| 50.2 | 四组标题改成「熟练 / 不熟练」+ 叠加层上点英雄改位置 | [domain/NOTES](../dota_assistant/domain/NOTES.md) |
| 51 | 查到新版本自己弹窗 | [app/NOTES](../dota_assistant/app/NOTES.md) |
| 52 | 局内打法 / 出装卡片先不弹 | [app/live/NOTES](../dota_assistant/app/live/NOTES.md) |
| 53 | 小地图野点标记：拉野 / 屯野 / 封野 / 莲花在小地图的野点上画圈 | [domain/NOTES-ingame](../dota_assistant/domain/NOTES-ingame.md) |
| 54 | 游戏画面里的野点框：在真实游戏画面的野点上描出刷新框、写几秒屯 / 拉 | [vision/NOTES](../dota_assistant/vision/NOTES.md) |
| 55 | 拉野按时间段 + 劣势路也提醒拉野 | [domain/NOTES-ingame](../dota_assistant/domain/NOTES-ingame.md) |
| 56 | 游戏画面野点框刷新加快 + 试玩模式空格子不算死 + 野点标记日志不重复 | [vision/NOTES](../dota_assistant/vision/NOTES.md) |
| 57 | 野区提醒只画框、不弹叠加层卡片；去掉抢莲花 | [app/live/NOTES](../dota_assistant/app/live/NOTES.md) |
| 58 | 游戏画面野点框默认关 | [vision/NOTES](../dota_assistant/vision/NOTES.md) |
| 59 | 野区提醒改成 Dota 时钟正下方的倒数 | [domain/NOTES-ingame](../dota_assistant/domain/NOTES-ingame.md) |
| 60 | 组合研究：精灵老鹿（艾欧 + 拉席克） | [research/NOTES](../dota_assistant/research/NOTES.md) |
| 61 | 抢莲花回来：Dota 时钟下方倒数的第二行 | [domain/NOTES-ingame](../dota_assistant/domain/NOTES-ingame.md) |
| 62 | 野区倒数加游戏物品图标：封野放眼、莲花放治疗莲花 | [domain/NOTES-ingame](../dota_assistant/domain/NOTES-ingame.md) |
| 63 | 屯野 / 拉野也有图标了：黄色野点、自家小兵 | [domain/NOTES-ingame](../dota_assistant/domain/NOTES-ingame.md) |
