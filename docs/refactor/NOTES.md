[根目录](../../CLAUDE.md) > 工程化重构 > **NOTES**

# 工程化重构：设计与验证记录

> 内容：重构状态、旧文件 → 新位置对照。2026-11-04 从 `docs/HANDOFF.md` 原样拆过来，节号沿用原 HANDOFF 编号（代码注释 / 其它文档里的「HANDOFF 第 N 节」按 [HANDOFF 的节号索引](../HANDOFF.md#节号索引) 找到在哪个文件）。
> 职责、文件表、接口、坑见同目录的 `CLAUDE.md`；这里只放「为什么这么做、怎么验证的、还差什么」，按时间追加。

## 当前状态（验证情况）

| 模块 | 状态 | 验证情况 |
|---|---|---|
| 工程化重构（2026-10-28，分支 `refactor/structure`，计划 [refactor/PLAN.md](PLAN.md)） | 🟡 已合并 main（2026-10-28），实战待验 | 平铺的 40 个模块按层分进 `core / sources / kb / ai / domain / vision / game / research / app / cli` 子包，`live.py` 用 mixin 拆成 `app/live/`（新路径见第 4 节）；加了 `pyproject.toml`、`requirements.lock`、ruff。只搬家不改行为：门禁全过（编译 + ruff、单测、行为快照逐字节一致、单测不碰真实目录、冒烟 + 搬家清单、慢快照、无头浏览器打开三个页面无控制台报错）；临时 venv 里 `pip install -e .` 后 `dota-assistant --help` 正常。重构期间 main 多出的 d1bfef6（队友战绩独立页）已合并进分支、按新结构移植（`domain/mates.py`、`app/webui.py`；`Live` 里的队友战绩方法删了，没有 `app/live/mates.py`），行为快照按 d1bfef6 重建后逐字节一致。2026-10-28 晚 P6：真鼠标清单在重构后的代码上点过（叠加层帮认 / 锁 / 拖动 / 拉宽 / × / 翻页 / 展开、顶栏买活 / 大招 / 帮认、队友战绩独立页查询 / 进整局 / 点名字查人 / 刷新 / 删人、面板位置 / 清空 / 设置保存都对；F8 热键没测，测试服务关了热键）；打包版 exe 跑过 `suggest`、`kb hero 小黑`、`start`（三个窗口、日志无警告、关面板正常退出）、`demo`（选人 → 本局要点 → 局内提醒 → 结束，日志无警告）。**还差玩家用重构后的版本打一局天梯**（第 7 节） |

## 待实战验证 / 下一步

-1. **工程化重构收尾**（第 2 节、[refactor/PLAN.md](PLAN.md) P6）：代码、门禁、真鼠标清单、打包版都过了（第 2 节），2026-10-28 已合并 main 并推送，**只差 ④ 玩家实战**。原清单：① PLAN 第 5 节真鼠标清单（叠加层 / 顶栏 / 控制面板）在重构后的代码上点一遍（「控制面板队友战绩」那项按 d1bfef6 改成独立页 `mates.html`：开窗口、查询、进整局、返回、点名字查人、删人、刷新，同第 38.2 节末）；
   ② `python packaging/build.py` 打包，拿 exe 跑 `suggest`、`kb hero 小黑`、`start`；③ `demo` 完整跑一遍；④ 玩家用重构后的版本打一局天梯（GSI、截图识别、买活自动记只能这样验）。都过了再合并 main、推送（要玩家点头）。
   真鼠标时发现的**原有问题（重构前就有，`D:\dota` 的 main 上一样，没修）**：控制面板「当前局面」10 个格子点不进输入框——叠加层用的 `web/base.css` 里 `.slot { width: 52px; height: 29px … }` 也套在了面板的格子上，面板格子被压成 52×29、输入框被挤没了；手动填阵容只能靠叠加层「?」帮认。
   **唯一可见差异：日志里模块名前缀变了**（例如 `live` → `app.live.draft`、`data` → `sources.http`），按模块名搜日志的习惯要跟着改。
   以后每次改完多跑三个门禁：`python tools/golden/run.py check`（行为快照；行为本来就该变时，先确认差异是该有的再 `update-snapshot`）、`python tools/check_isolation.py`、`ruff check dota_assistant tests tools packaging`。
   可选（问玩家）：GitHub Actions 在 `windows-latest` 上跑编译 + ruff + 单测（行为快照依赖本机个人数据，CI 跑不了）。

## 4. 模块地图

2026-10-28 工程化重构把平铺的模块按层分进子包（只搬家，不改行为）。**本文第 5 节以后的历史记录里的旧文件名对照见此表**（历史记录不逐处改）：

| 旧文件 | 新位置 |
|---|---|
| `config.py` `log.py` `heroes.py` | `core/config.py` `core/log.py` `core/heroes.py` |
| `gsi.py` | 找 Dota 目录 → `core/dota_install.py`；`buyback_left`、阶段常量等纯函数 → `core/gsi_fields.py`；GSI 服务 / cfg 安装 → `game/gsi.py` |
| `gamefiles.py` | VPK 读取 → `core/vpk.py`；DXT 解码、导出网格头像 → `vision/gamefiles.py` |
| `data.py` | 联网基础 → `sources/http.py`；英雄表 / 头像 / 图标 / 装备常量 → `sources/assets.py`；克制 / 分位置 → `sources/stats.py`；个人数据 → `sources/player.py`；`update()` `fetch_icons()` 等编排 → `app/update.py` |
| `coach.py` 的 `ITEMS_CONST` `items_const` | `sources/assets.py`（`coach.py` 其余 → `domain/coach.py`） |
| `kb.py` | `kb/__init__.py`（整体搬，`python -m dota_assistant kb ...` 不变） |
| `ultcd.py` | 游戏文件 / 镜像读减冷却写法、`pick` `cooldowns` `MIN_CD` → `kb/cd_ops.py`；其余（`cooldown` `mods` `Tracker` …）→ `domain/ultcd.py` |
| `llm.py` `gameplan.py` | `ai/` |
| `engine.py` `advisor.py` `lanes.py` `explain.py` `teamfight.py` `itembuild.py` `coach.py` `brief.py` `death.py` `jungle.py` `buyback.py` `mates.py` | `domain/`（同名） |
| `lanestate.py` | `live` 记对线用的 `LOG_FILE` `LOG_MINUTES` `log_moment` → `domain/lanelog.py`；回测、`lane-test` / `lane-review` → `research/lanestate.py` |
| `vision.py` | `vision/recognize.py` |
| `capture.py` `grid.py` `roles.py` `buybackscan.py` `calibrate.py` | `vision/`（同名） |
| `hotkeys.py` `speech.py` | `game/` |
| `backtest.py` `aibacktest.py` | `research/` |
| `live.py` | `app/live/`：类本体 `core.py`，模块级常量 / 小函数 `consts.py`，方法按功能分进 `draft` `ingame` `timers` `cards` `overlay` `settings` 六个 mixin（原计划还有 `mates`，main 的 d1bfef6 把队友战绩改成独立页走 webui，`Live` 里那两个方法删了，这个 mixin 也就没了） |
| `webui.py` `start.py` `demo.py` | `app/` |
| `cli.py` | `cli/__init__.py`（`dota_assistant.cli.main` 路径不变） |

下表路径都相对 `dota_assistant/`，按层从低到高排：

| 文件 | 职责 |
|---|---|
| `__main__.py` | `python -m dota_assistant` 入口，只调 `cli.main` |
| **core/** | 最底层，不依赖别的层 |
| `core/config.py` | `config.toml` → `Config` 数据类（字段说明在 `config.example.toml`）；运行目录 `ROOT`（环境变量 `DOTA_ASSISTANT_HOME` 可指定，打包版是 exe 所在目录） |
| `core/log.py` | 日志：每次启动一个 `logs/<时间>-<命令>.log`，留最近 30 个；token 打码；未捕获异常带堆栈 |
| `core/heroes.py` | 英雄表 + 名字查找（中文名 / 英文名 / 简称 `ALIASES` / 唯一子串） |
| `core/dota_install.py` | 找 Dota 2 安装目录：`config.toml` 的 `dota_dir`，或者到本机各个 Steam 库里找（只读注册表 / 磁盘，不碰游戏进程）（原 `gsi.py` 的一部分） |
| `core/gsi_fields.py` | GSI 推送里的字段：游戏阶段常量和从推送内容算出来的值（`buyback_left` 等纯函数，不带 IO）（原 `gsi.py` 的一部分） |
| `core/vpk.py` | 最小的 VPK v2 读取：Dota 资源包 `pak01_dir.vpk`，只读磁盘上的静态文件（原 `gamefiles.py` 的一部分） |
| **sources/** | 联网拉数据（原 `data.py`：所有联网：OpenDota / STRATZ / Valve；缓存到 `data/`；`player_history()` 把个人记录按位置归类） |
| `sources/http.py` | 联网基础：OpenDota / STRATZ 地址、带重试的会话、`_get` / `_stratz` / `_sql`、出错类型。别的模块一律 `http._get(...)` 形式调用 |
| `sources/assets.py` | 英雄表、头像、顶栏图标、装备常量 / 图标（含原 `coach.ITEMS_CONST` / `items_const`）：下载并缓存在 `data/` 里的死数据 |
| `sources/stats.py` | 克制 / 配合统计（OpenDota explorer）、英雄分位置场次 / 胜率（STRATZ，或没 token 时 OpenDota 分路凑的）：拉取、缓存路径、读取 |
| `sources/player.py` | 个人数据：段位、最近天梯每个英雄在每个位置的局数 / 胜场；`player_history()` 把个人记录按位置归类；每个英雄生涯局数（`fetch_career`，我 → `player.json` 的 `career`，双排队友 → `data/duo.json`，第 43 节） |
| **kb/** | 知识库 |
| `kb/__init__.py` | 当前版本知识库：Valve 官方技能 / 天赋 / 物品原文、机制标签、玩家规则 |
| `kb/cd_ops.py` | 大招冷却用到的知识库条目：游戏文件 / 镜像里的减冷却写法（`update_ops` / `load_ops`）、每个英雄要计时的大招（`pick`）、各级冷却（`cooldowns`） |
| **ai/** | 调大模型 |
| `ai/llm.py` | 无头调用 Claude Code（订阅额度），选人盲测和 AI 打法共用 |
| `ai/gameplan.py` | AI 打法（本局要点第二页）：资料 = 本局数据 + 自己和对面五人的官方技能全文；每条带原文出处，程序核对出处；缓存 `data/plans/` |
| **domain/** | 领域逻辑，不依赖界面 |
| `domain/engine.py` | 打分：自身强度、克制、配合、熟练度、个人表现、位置筛选。常数都在文件头，带推导注释 |
| `domain/advisor.py` | 局面 `Draft` → 建议 `Advice`；播报文案 `speech()`；显示行 `render_lines()`。不依赖界面 |
| `domain/nextpick.py` | 猜对面最后一手：空着的位置 × 这个位置谁选得多 × 克我方 4 人 / 配对面 4 人（第 50 节） |
| `domain/lanes.py` | 对面分路：猜对面谁打几号位（带把握）、三路排法、同路队友；「你这路怎么打」的硬规则提示（每条带官方原文）；`evaluate()` 回测（第 22 节） |
| `domain/explain.py` | 推荐理由里的机制对位（克敌机先 ↔ 闪避、显形 ↔ 隐身…），每条带官方原句 |
| `domain/teamfight.py` | 后期团战：对面哪几个大招要等、谁的持续施法要留打断、控制留给对面谁、谁能保人、指向技能别往哪丢（第 30 节） |
| `domain/itembuild.py` | 出装推荐：核心装 + 针对装排成顺序，「下一件」和还差多少钱（第 25 节） |
| `domain/coach.py` | 局内缺口提醒：`plan()` 开局定缺口，`Coach.tick()` 去重；`prepare()` 给实时版，`simulate()` 录像重放 |
| `domain/brief.py` | 本局要点：对线（laneOutcome）、起手装、强势期（按比赛时长的胜率）、出装、对面要防；`simulate()` 录像回放 |
| `domain/death.py` | 死亡复盘：死前 20 秒的 GSI 推送 → 「怎么死的」几句话；原始推送存 `shots/deaths/` |
| `domain/jungle.py` | 野区计时提醒：封野、拉野（优势路小野 / 大野、劣势路对面大野，按时间段，`Pull`）、屯野，到点前几秒在叠加层顶上弹一行（开着 `world_marks` 时不弹，只画框），过了自己收起（第 27、55、57 节） |
| `domain/camps.py` | 野点标记：28 个野点 / 刷新框坐标（游戏文件读的）、拉野点，每个野点几秒屯 / 拉（游戏文件 `creep_pull_timings.txt`，`load_timings`），野区提醒到点时圈哪几个（`marks()`，第 53、54 节） |
| `domain/buyback.py` | 买活计时：双方 10 人的买活冷却（自己按 GSI，别人截图自动记 / 玩家点），界面是 `web/topbar.html`（第 31 节） |
| `domain/ultcd.py` | 对面大招冷却：每个对面英雄挑一个大招、按等级 + 勾的减冷却（天赋 / 神杖 / 魔晶 / 玲珑心）算冷却，玩家看到放了点一下计时（第 32 节）；挑大招 / 各级冷却在 `kb/cd_ops.py` |
| `domain/lanelog.py` | 对线实战记录：`live` 在 3:00 / 6:00 / 10:00 把自己的 GSI 数据记进 `data/lane_log.jsonl`，赛后 `lane-review` 对照（原 `lanestate.py` 的一部分） |
| `domain/mates.py` | 队友战绩：好友代码 → OpenDota 资料 / 最近 100 局 / 同队记录 + STRATZ 天梯位置；每局表现分（同英雄分位）；整局详情；`Service` 给 webui 的 `/api/mates`、`/api/match` 用；查过的人记在 `data/mates.json`（第 38 / 38.1 节） |
| **vision/** | 看屏幕（和 game 平级） |
| `vision/capture.py` | 截屏：优先 PrintWindow 只截 Dota 窗口（叠加层、直播弹幕等盖在上面的窗口不会截进去），找不到窗口 / 全黑再退回 mss 整屏 |
| `vision/recognize.py` | 校准数据 `Calibration`、模板库 `TemplateBank`、`recognize()`（原 `vision.py`） |
| `vision/grid.py` | 选人网格认禁用：找格子、认格子是谁（`GridMap`，按布局存 `data/grid_map.json`）、红斜线 = 禁用；`BanWatcher` 选人时一直看（第 19 节） |
| `vision/gamefiles.py` | 从 Dota 安装目录的资源包（`pak01_dir.vpk`）导出选人网格用的英雄图到 `data/portraits_grid/`（PNG / YCoCg-DXT5 解码） |
| `vision/roles.py` | 自动认位置：Windows OCR 认顶栏名字下面的「优势路 / 中路 / 劣势路 / 辅助 / 纯辅助」，按玩家名（GSI）找自己那格（第 20 节） |
| `vision/buybackscan.py` | 买活自动记：每秒截图看顶栏头像变灰 / 变回彩色，OCR 认复活倒计时，提前复活 = 买活（第 31.1 节） |
| `vision/calibrate.py` | 校准窗口 |
| `vision/camera.py` | 游戏画面里的镜头：认自己英雄的血条（`find_bars`）、血条 + GSI 英雄坐标反推镜头对着哪（`target_from_bar` / `Tracker`）、世界坐标 → 屏幕像素（`project`）；镜头参数是在游戏里量的（第 54 节） |
| **game/** | 和游戏 / 系统打交道（和 vision 平级） |
| `game/gsi.py` | GSI 本地 HTTP 服务 + cfg 安装；对局中的时间 / 装备 / 正补 / 死亡次数 |
| `game/hotkeys.py` | RegisterHotKey 全局热键（不装键盘钩子） |
| `game/speech.py` | 常驻 PowerShell 进程调 System.Speech，新句子打断旧句子 |
| **research/** | 研究 / 回测脚本 |
| `research/backtest.py` | 样本外回测、调参 |
| `research/nextpick.py` | 对面最后一手猜得准不准（`next-test`，第 50 节） |
| `research/aibacktest.py` | AI 选人盲测（`backtest-ai`）：拿统计数据之后的新对局，在选人进行到一半时让大模型推荐，再和真实胜负对答案（第 10 节） |
| `research/lanestate.py` | 对线优劣回测（`lane-test`）和实战记录复盘（`lane-review`）（第 28 节） |
| **app/** | 把下面各层串起来的编排 |
| `app/live/__init__.py` | 只导出 `Live` |
| `app/live/core.py` | 实时主控 `Live`（不依赖界面库）：局面格子、识别、推荐、语音、GSI、热键；状态整理成 JSON 快照。这里是类本体（`__init__`、工作线程、订阅 / 推送、`action` 分发），其余方法在下面七个 mixin 里 |
| `app/live/consts.py` | `Live` 用的模块级常量和小函数（`FIX_FILE` `DEATH_MIN_SHOW` …），各 mixin 一律写 `consts.NAME`，测试只替换这里 |
| `app/live/draft.py` | 选人：格子 / 局面 / 推荐、定时识别（顶栏、网格禁用、自动认位置）、帮认和记模板 |
| `app/live/ingame.py` | 局内：缺口提醒、分阶段弹本局要点、野区提醒、对线记录、死亡复盘和死亡截图 |
| `app/live/timers.py` | 买活 / 大招计时：顶栏小条、买活自动记（顶栏头像 + 复活倒计时 + 消息栏）、对面大招冷却 |
| `app/live/cards.py` | 本局要点 / 如果选它：做要点卡片（数据页 + AI 打法页）、翻页 |
| `app/live/overlay.py` | 叠加层：显隐、固定 / 解锁、拖动后的位置、底色样式 |
| `app/live/settings.py` | 设置：账号设置保存、Dota 目录、热键注册 |
| `app/live/world.py` | 游戏画面里的野点标记：有标记时单独一个线程每 0.04 秒截 Dota 窗口认镜头（`start_world` / `_world_loop`），`world_view` 给 `/api/world`（第 54 节） |
| `app/webui.py` | 本机 HTTP 服务（静态页面、头像、SSE 推快照、POST 操作）+ 启动 Electron，Electron 的输出转进日志 |
| `app/start.py` | 一键启动：双击「启动助手.bat」走这里，能自动做的准备工作都自动做，只在缺东西时问一句（第 13 节） |
| `app/demo.py` | 演示：按真实对局逐个选人，再当「假 Dota」把录像按 GSI 格式推给本地端口 |
| `app/update.py` | 全量更新数据（`update` 命令 / 启动时数据过期）：英雄表、头像、装备、知识库、顶栏图标、分位置数据、个人数据、克制矩阵，按顺序拉一遍（原 `data.py` 的编排部分） |
| `app/upgrade.py` | 更新助手本身（第 41 节）：打包版看 GitHub Release、下 zip、交给新包的 exe 替换文件再重启；源码版 git pull 再重启；`Updater` 给控制面板用 |
| **cli/** | 最上层 |
| `cli/__init__.py` | 所有命令入口（`main`；按命令延迟 import） |

仓库里其它目录 / 文件：

| 文件 | 职责 |
|---|---|
| `web/` | 界面页面：`overlay.html` 叠加层、`topbar.html` 买活 / 大招计时条、`world.html` 游戏画面里的野点框、`minimap.html` 小地图野点（默认关）、`panel.html` 控制面板、`view.js` 推荐卡片（两边共用）、`base.css` 配色 |
| `electron/` | 界面外壳：只开窗口（叠加层透明 / 置顶 / 穿透，控制面板普通窗口），无逻辑。`npm install` 装 Electron |
| `knowledge/rules.toml` | 玩家确认过的经验规则（进 git） |
| `knowledge/mistakes.toml` | AI 常见误判：说错过、查原文纠正的说法（进 git，`kb mistake` 记录） |
| `tests/` | `python -m unittest`：引擎、识别（合成截图）、GSI、播报文案；另有守门测试 `test_layers`（分层）、`test_patch_targets`（patch 不打空）、`test_moves`（搬家清单）、`test_smoke`（每个模块能 import、每个子命令 `--help`）、`test_hygiene`（编码 / 换行）、`test_project`（pyproject 和 requirements 一致） |
| `tools/golden/` | 行为快照（门禁 G3）：CLI 输出 + 一整局 Live 回放逐字节比对；快照和个人数据在仓库外 `D:\dota-golden\`，用法见 [tools/golden/README.md](../../tools/golden/README.md) |
| `tools/extract_camps.py` | 从本机游戏文件（`maps/dota.vpk` 的地图实体 + 触发器模型，KV3 v5）读野点 / 刷新框 / 莲花池 / 小地图范围，和 `camps.CAMPS` 对一遍；要另装 `zstandard lz4`（第 53、54 节） |
| `tools/check_isolation.py` | 门禁 G4：跑单测前后比对 `data/` `shots/` `logs/` `templates/` `config.toml` `calibration.json` 有没有被改 |
| `tools/browser_check.py` | 阶段验收 G6：在临时运行目录起界面服务（同 `run --browser` 的 WebServer + Live），无头 Edge / Chrome 打开控制面板、叠加层、顶栏收控制台报错 |
| `tools/lock_deps.py` | 重新生成 `requirements.lock`（只锁 `requirements.txt` 里的包及传递依赖） |
| `packaging/` | 免安装打包：`build.py`（PyInstaller + Electron），`launcher.py`（exe 入口，调 `dota_assistant.cli.main`） |
| `pyproject.toml` | 项目元数据、命令入口 `dota-assistant`、ruff 配置；依赖和 `requirements.txt` 一致 |

运行时生成、和机器相关的文件：`calibration.json`（校准）、`templates/`（学习模板，值得保留和拷走）、`shots/`（截图，不进 git）、`data/`（可重新拉，不进 git）。
