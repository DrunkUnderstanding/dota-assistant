[根目录](../../CLAUDE.md) > app > **NOTES**

# app：设计与验证记录

> 内容：一键启动、不填 key、更新助手、新版本弹窗。2026-11-04 从 `docs/HANDOFF.md` 原样拆过来，节号沿用原 HANDOFF 编号（代码注释 / 其它文档里的「HANDOFF 第 N 节」按 [HANDOFF 的节号索引](../../docs/HANDOFF.md#节号索引) 找到在哪个文件）。
> 职责、文件表、接口、坑见同目录的 `CLAUDE.md`；这里只放「为什么这么做、怎么验证的、还差什么」，按时间追加。

## 当前状态（验证情况）

| 模块 | 状态 | 验证情况 |
|---|---|---|
| 不填 key 也能用（没 STRATZ token / 调不了 AI，第 35 节） | ✅ 完成 | 单元测试（`tests/test_nokey.py`）；全新目录、没 token、AI 来源选 API Key 却没填，从零 `update` 再跑 `suggest` 和 `demo`（无头浏览器看叠加层）：推荐、分路、本局要点、局内提醒都出来，AI 打法那页写明用不了，日志没有警告；分位置数据改用 OpenDota 分路凑，1523 场回测见第 35 节 |
| 更新助手本身（`app/upgrade.py`，控制面板顶栏「有新版本」+ 设置里「版本」，`upgrade` 命令，第 41 节） | 🟡 代码完成 | 单元测试（假 GitHub 接口、临时裸仓库当 GitHub、替换保留个人文件、失败挪回、面板状态）；真 GitHub：打包版按 VERSION.txt 判新旧四种情况都对、真下 254 MB Release 校验 sha256 + 解压；源码版临时克隆在 Electron 里点「更新并重启」→ git pull → 自己重开到新提交；打包版用新打的 zip 走完「等旧进程退出 → 换文件 → 重开新版本」，个人文件都在。**2026-10-29 真实发布走通**：从 GitHub 下的 v2026.10.29 点「有新版本」→「更新并重启」，自己下 v2026.10.29.2（253 MB，25 秒）、换文件、重开，个人文件都在（第 41 节末）；行为快照这台机器没有 `D:\dota-golden`，没跑。2026-11-02 查到新版本**自己弹出更新窗口**，「以后再说」后同一版本不再弹、选人 / 比赛中不弹（第 51 节；单测 + Electron 发鼠标消息点过，**没真实发布验证**） |

## 待实战验证 / 下一步

-0.4 **更新助手**（第 41 节，2026-10-29）：① 有 `D:\dota-golden` 的机器上跑 `python tools/golden/run.py check`：cli 组多了 `upgrade --help`、顶层 help 多一行，replay 组快照多了 `upgrade` 键和最后两个动作（冻结目录不是 git 克隆，检查不联网），确认只有这些差异再 `update-snapshot`；② ~~发布带这个功能的版本、再拿它真实自动升一次~~（2026-10-29 已做，v2026.10.29 → v2026.10.29.2）；v2026.10.27.2 及更早的版本没有更新功能，用的人要手动下一次。

-0.39 **新版本自己弹窗**（第 51 节，2026-11-02）：下次发 Release 后，开着旧版的助手（在主菜单）看是不是自己弹出更新窗口；点「以后再说」后重开不再弹、顶栏按钮还在；在选人 / 比赛里查到的，打完回到主菜单才弹。注意：弹窗逻辑在旧版本里，**这个提交之后打的包**才会弹，之前的版本还是只有顶栏按钮。

0.5 **不填 key**（第 35 节）：没 token 的人用打包版会一直用打包时带的 STRATZ 分位置数据（不更新）；要不要过一阵在打包时刷新一次、或改成「STRATZ 旧了超过 N 天就换 OpenDota 凑的」，等有人反馈推荐位置不准再定。

## 13. 一键启动（2026-10-22）

玩家反馈（原话）：「现在这个运行怎么看起来这么麻烦，能否简单一点」。
新增 `启动助手.bat` → `python -m dota_assistant start`（`start.py`），依次：
- 没有 `config.toml` → 问好友代码和 token，其余用 `config.example.toml` 默认值；
- 界面组件没装且有 npm → 自动在 `electron/` 下 `npm install`；没有 Node.js 就提示去装；
- 从注册表 `HKCU\Software\Valve\Steam` + `libraryfolders.vdf` 找 Dota 目录，自动写 GSI 配置（启动项 `-gamestateintegration` 只能提醒玩家手动加，只提醒一次）；
- 数据超过 3 天全量更新、个人数据超过 12 小时刷新，没网时用旧数据；
- 最后 `webui.run`。

选人阶段的 GSI 推送现在自动存到 `shots/gsi/`（`GameState.log_draft`，内容变了才存，最多 300 份），不用再单独跑 `gsi-dump`。

**实现假设（非玩家原话）**：更新频率 3 天 / 12 小时；首次提示只显示一次（`data/.gsi_hint_shown`）。
**验证情况**：单元测试全过；配置生成的替换逻辑验证过。开发机没装 Dota（找目录返回「没找到」），
自动安装界面组件和完整启动没在开发机跑，要在游戏电脑第一次双击时验证。

## 35. 不填 key 也能用（2026-10-27）

玩家要求：把不需要 API Key 的功能隔离出来，不填 key 也能用上。两种「key」：STRATZ token（免费领，可选）、AI 打法的 API Key。

**不填 STRATZ token 时**
- 照常：选人推荐（克制 / 配合来自 OpenDota explorer）、个人数据（只填好友代码，走 OpenDota，不带位置）、识别、GSI、叠加层、热键、
  知识库、对线 / 团战机制提示、针对装、野区 / 买活 / 大招计时。
- 没有：对线胜负、起手装、强势期、同分段出装节奏（这几样只有 STRATZ 有，本局要点「说明」里写明，不编）。
- **分位置数据**（推荐的位置筛选、猜对面分路、队友占了哪个位置都靠它）：以前没 token 就完全没有，推荐退回定位标签粗筛（一号位和中单推荐一模一样），
  猜分路直接不显示。现在 `update` 没 token、又没有 STRATZ 的分位置文件时，改拉 OpenDota `/scenarios/laneRoles`
  （每个英雄一次，127 次约 2 分钟），存 `data/positions_opendota.json`（`data.fetch_positions_opendota` / `positions_from_lane_roles`）：
  OpenDota 只分路（优势 / 中 / 劣势 / 打野）不分位置，优势路按定位标签拆成一 / 五号位、劣势路拆成三 / 四号位（只有一边对得上标签的给 90%，否则对半），
  胜场一律记一半（只定「打不打这个位置」，不按位置修正胜率）。`load_positions` 先用 STRATZ 的（填过 token 或打包版自带），没有再用这份。
  拆分规则、90% 都是实现假设。
- 1523 场样本外对局回测（`data/backtest_matches.json`，同一份统计数据）：

| 分位置数据 | 猜对面单人位置 | 走哪条路猜对 | 实际选的英雄过位置筛选 | 推荐区分度 |
|---|---|---|---|---|
| STRATZ | 77.8% | 79.9% | 94.3% | 0.0666 |
| OpenDota 分路 + 定位标签（采用） | 68.9% | 77.2% | 87.7% | 0.0627 |
| 只看定位标签（试过，不用） | 35.2% | 45.6% | 73.7% | 0.0644 |

  区分度三种都在噪声（约 ±0.01）内。只看定位标签猜分路「八成把握」那档实际只对 61%，会误导，所以没拿它兜底。
  OpenDota 的分路数据是已解析对局、不分段位、每个英雄一两千局，帕吉这类「什么路都走」的拆得不准（帕吉一号位 13%，STRATZ 3%）。
- OpenDota explorer 的 `player_matches`（有 lane_role）14 天里只有 469 场（大多是比赛局），拿不来算分位置。

**调不了 AI 时**（`llm.unavailable`：Claude Code 没装 / 选了 Anthropic 没填 Key / OpenAI 兼容接口没填地址）
- `gameplan.generate` 先看缓存（同一阵容以前做过的照样给），没缓存就抛 `llm.Unavailable`，不发请求；
  打法页写「AI 打法用不了：<缺什么>。数据页不受影响；…换 AI 来源，或者关掉 AI 打法」，日志一行 INFO，不记堆栈。
- Claude Code 没装时以前报「调不起 Claude Code：[WinError 2]」，现在直接说没找到 Claude Code。
- 启动窗口（`start.no_key_note`）说一句哪些照常、少了哪些、分位置数据从哪来。

**顺手修的两处**（测试时撞上的）
- `webui`：随机端口分到 Chromium 拒绝的端口（6669 这类，`UNSAFE_PORTS`）界面会打不开（ERR_UNSAFE_PORT），现在换一个。
  这台机器的动态端口从 1024 起，实测分到过 6669。
- `demo`：往本机 GSI 端口推数据不再走系统代理（`HTTP_PROXY` 时会被代理吞掉，助手收不到），推失败直接报错。
  另外注意：`gsi-dump` 开着时也占 3000 端口，`demo` 会推到它那里；同时跑要把其中一个的 `gsi_port` 改掉。

**验证**：`tests/test_nokey.py`（12 个）+ 全部单测；全新目录（不带 data / config.toml，`electron/node_modules` 用目录联接）从零跑：
`update`（85 万局，无 token）→ OpenDota 分位置 → `update --player` → `suggest` 几个位置 → `demo`（AI 来源选 Anthropic 不填 Key，无头浏览器看叠加层）：
推荐按位置分开了、对面分路和「你这路怎么打」有、本局要点「说明 / 对线 / 对面要防」+ AI 页写明用不了、25:30 / 31:30 局内提醒弹出，日志没有警告。
打包版（`packaging/build.py`，密钥检查通过）解压到新目录、不填任何 key：exe 跑 `suggest`、`kb hero` 正常，`run --browser` 起来后第一次自动弹「设置」，推荐照常出（用的是打包时带的 STRATZ 分位置数据）。没在没装 Python / Node 的电脑上试。

## 41. 更新助手本身（2026-10-29，`app/upgrade.py`）

玩家需求（原话）：「能否帮我弄一个更新功能，这个功能是针对可执行包的，但是应该github的也能用上」。
做法：**打包版**走 GitHub Release，**源码版**（git clone、用「启动助手.bat」跑）走 git pull；都从控制面板点、点完自己重启。下载源码 zip 解压的没法自动更新，只提示去 GitHub 下。

**什么时候查**：`run` / `start`（`webui.run`）起来 5 秒后在后台查一次（`Updater.check_async`；等的这 5 秒状态就是 `checking`，「检查更新」按钮变灰，这时手动点的并进那次、结果按手动的算——以前会报「正在检查 / 下载」的 ERROR，2026-11-02 修）；设置里「版本」那节有「检查更新」；命令行 `upgrade [--check] [--yes] [--zip 压缩包]`。
有新版本时顶栏设置按钮左边出绿色「有新版本」，点开是更新内容（Release 说明 / 新提交标题）+「更新并重启」，下载时按钮和窗口里有进度。

**打包版怎么判新旧**：`VERSION.txt` 是 `<打包日期>-<提交>`，用 GitHub compare 接口比 `本地提交...最新 Release 的 tag`：ahead / diverged = 有新版本，identical = 最新，behind = 本地更新；
本地提交不在 GitHub 上（没推送的提交打的包，接口 404）就退回比日期（Release 发布日期 > 打包日期才算新）。不登录每小时 60 次，限流了说一句「过一会儿再查」。
**所以以后发 Release 要从已经推送的提交打包、tag 要推上去**，不然只能按日期比。

**打包版怎么换**（新旧版本之间的约定，**别改**）：旧版下载 zip（核对 Release 登记的 sha256）→ 解压到 `_update/new/` →
用**新包自己的 exe** 起 `upgrade --finish --pid <旧进程> --target <运行目录> --src <新包>`（新控制台窗口，`DOTA_ASSISTANT_HOME` 指向运行目录，日志写在运行目录的 `logs/`）→ 旧版关面板退出 →
收尾进程等旧进程退出 → 程序文件（exe、`_internal`、`ui`、`web`、`knowledge`、README、VERSION…）整个换掉，旧的挪进 `_update/old/`，中途失败全挪回去 → 起新版本 `start`。
保留：`config.toml`、`logs/`、`shots/`、`templates/` 不在包里不碰；`data/` 只补本地没有的文件；`calibration.json` 本地有就留着。`_update/` 下次 `start` 时在后台删。
替换由新包做（它最清楚自己的目录结构），以后改目录结构只要改新版本的 `replace`。

**源码版**：`git fetch` 看上游分支，落后且本地没有自己的提交才给更新；`git pull --ff-only`，requirements.txt 变了跑 `pip install -r`、electron/package.json 变了跑 `npm install`，
再用同一个 `upgrade --finish`（不带 `--src`）等旧进程退出后重开 `start`。本地有没推送的提交 / 分叉了 / 分支没跟踪远端：只说原因，不动。

**退出**：`Live.quit_requested` 一设，面板自己 `window.close()`，Electron 8 秒没退就 `terminate`；`serve` 这时返回 0（不然打包版启动窗口会停在「按回车」，旧进程不退，收尾进程就一直等）。

**验证**：
- 单测 `tests/test_upgrade.py`：判新旧（compare 各种状态、404 按日期、没 zip / 没发布、不是 git 也不是打包版）；git 用临时裸仓库当 GitHub（最新 / 有新提交再 pull / 本地有提交 / 分叉 / 没跟踪远端）；
  下载 + sha256 + 解压（含 `../` 越界路径、不是打包版的 zip）；替换（个人文件都在、旧的多余文件没了、失败挪回、文件被占用给明白的报错、`_update` 清掉）；`Updater` 状态和 Live 两个动作。
- 真 GitHub：假的打包目录把 VERSION 写成 v2026.10.26 / v2026.10.27.2 / 当前 main / 不存在的提交，结果分别是有新版本 / 最新 / 本地更新 / 看不出；真下 v2026.10.27.2 的 254 MB zip，21 秒，sha256 对上，解压出包根。
- 源码版端到端：临时目录里两个克隆 + 裸仓库，玩家那份起 `start`（Electron 带调试端口只读状态），5 秒后顶栏出「有新版本」、设置里写「源码版」和两个提交号；点开窗口、点「更新并重启」（CDP 发的鼠标事件；开发机这时玩家在用，没用 SendInput 抢前台）：
  git pull → 收尾进程 → 面板关、旧进程退出 → 新 `start` 起来是新提交，日志无警告，再查一次是「已经是最新版」。
- 打包版端到端：用这次改动打的包当新版、一份拷贝把 VERSION 改旧并放上个人文件（config.toml、player.json、改过的 calibration、logs、shots、多余的旧文件）当旧版。
  `upgrade --zip`：收尾进程等命令行退出后 0.7 秒换完（exe 挪过去、`_internal` 因为收尾进程自己在用改成复制），个人文件都在、旧的多余文件没了、`_internal` / `web` 和新包逐字节一样；
  旧版开着界面时起收尾进程、关面板：旧进程 0.3 秒内退出、换完、新版本自己起来（三个窗口、日志「代码版本」是新的）、`_update` 被删。
- 面板在 GitHub 限流时：设置「版本」里红字「检查更新失败：GitHub 接口限流了…」，顶栏不出按钮（无头 Edge 截图看过）；`tools/browser_check.py` 6 个页面无报错。
**没验证**：没写权限的目录（Program Files）会在换文件时报错并挪回；行为快照这台机器没有 `D:\dota-golden`，没跑。
**顺手修的**：`calibration.json` 坏了（不是 JSON / 结构不对）时打包版双击启动直接停在「出错了…按回车」，日志里只有「启动助手…」。现在 `Calibration.load` 读不了就记一条 WARN、当没校准（控制面板显示「未校准」），单测 `test_vision.CalibrationFileTest`。这个改动也拿来实测了一次真实的自动更新（见下）。

**真实发布实测（2026-10-29）**：提交 e5a7637 打包发 **v2026.10.29**（第一个带更新功能的版本）；再用一个小改动（上面「顺手修的」校准文件）提交 925aa03 打包发 **v2026.10.29.2**。
从 GitHub 下 v2026.10.29 的 zip 解压到临时目录，放上个人文件（config.toml、player.json、改过的 calibration.json、shots），双击 exe：
- 第一次启动先弹「设置」（新装的正常行为）；关掉后 10 秒顶栏出「有新版本」，窗口里是 `20261029-e5a7637 → Dota 选人助手 2026-10-29（2）（下载 253 MB）` 和 Release 说明；
- 点「更新并重启」：顶栏「下载中 29%」、窗口进度条，25 秒下完 → 解压 4 秒 → 收尾进程 → 旧版面板关、退出 → 3.5 秒换完 → 新版自己起来（日志「代码版本 20261029-925aa03」）→ `_update` 删掉 → 再查一次「已经是最新版」。从点按钮到新版起来约 35 秒；
- 个人文件都在（config / player.json / 自己的校准 / 截图），新版里坏的 calibration.json 也能启动（当没校准）。
- 点击方式：玩家主屏正开着全屏程序，没用 SendInput 抢光标，改成给面板的渲染窗口 PostMessage 发鼠标消息（不动光标、不抢前台），截图用 PrintWindow。
- 发现两处小毛病，已改（下一个 Release 才带上）：Release 说明的 Markdown `**` / 反引号原样显示 → 显示前去掉；下载中「更新并重启」按钮是灰的但看不出来 → 控制面板里禁用的按钮半透明。

## 51. 查到新版本自己弹窗（2026-11-02，`Updater.view` 的 `remind` + `panel.js` 的 `remindUpgrade`）

玩家（原话）：「帮我做一个提醒更新的功能」；问了想补哪块，选「有新版本时主动弹窗」（没选定时再查、叠加层提示、数据过期提醒）。
原来只在控制面板顶栏出一个绿色「有新版本」小按钮，不点开不知道更新了什么。

- **什么时候弹**：启动 5 秒后的自动检查查到新版本 → 控制面板自己打开「更新助手」窗口（就是点顶栏按钮打开的那个：版本、更新内容、「更新并重启」）。
- **「以后再说」**（或 Esc）：这个版本记进 `data/.upgrade_later`（一行版本号：打包版是 Release 名，源码版是 `提交号（日期）`），以后自动检查到同一个版本不再弹，顶栏按钮照旧在；
  GitHub 上又出了更新的版本会再弹。`data/` 打包版换文件时只补不覆盖，所以记录升级后也在。下载 / 安装中关窗口不算以后再说。
- **手动「检查更新」**（设置里「版本」）查到了照样弹，盖在设置窗口上面，不管以前说没说过以后再说。
- **不打扰**：GSI 连着且在一局里（`game_state` 有值且不是赛后）时 Python 不给 `remind`，打完回主菜单 / 赛后再弹（选人时控制面板要用来改格子，模态窗口会挡住）；
  自动检查的等别的窗口（第一次启动弹的「设置」）关了再弹——设置窗口关时页面自己再看一眼（状态没变时 Python 不推新快照）。
- 不弹 Windows 通知、不抢前台：控制面板在后面就等切过去时看到。
- 接口：`Updater.check_async(asked=)`、`Updater.dismiss()`；网页动作 `upgrade_later`；视图多 `remind` / `asked`。

**验证**：`test_upgrade.UpdaterTest.test_remind_until_later`（弹 → 以后再说 → 文件 → 新 Updater 同版本不弹 → 手动查照样弹 → 新版本再弹 → 没查过不记）、
`LiveTest.test_actions`（选人时不弹、赛后弹、`upgrade_later`）；`test_smoke` 动作表加 `upgrade_later`，`live_api.json` 重新生成（只多 `_upgrade_view`）。全部单测、编译、ruff、`node --check`、`check_isolation`、`browser_check` 通过。
**Electron 实测**（隔离运行目录 + 单独 profile，不开 GSI / 热键，假的 `upgrade.check`、`prepare` 直接报错不真装）：玩家正在用 VS Code，没用 SendInput 抢前台，
和第 41 节一样给控制面板渲染窗口 PostMessage 发鼠标消息，CDP 读按钮位置、核对 mousedown 落在哪个元素：自动检查 → 自己弹出 → 点「以后再说」关掉、3 秒不再弹、文件记下版本、顶栏按钮在；
点顶栏按钮能打开；设置里点「检查更新」→ 盖在设置上弹出，关掉后设置还开着；关设置；再来一个新版本 → 又弹；点「更新并重启」→ 窗口里红字「更新失败：…」不关；关掉记下新版本。页面无报错。
另用无头 Edge 多跑了：刷新页面后同一版本不弹、设置开着时自动检查到的新版本等设置关了才弹。
行为快照：replay 组照旧因 `WEB_ACTIONS` 报错（这次多了 `upgrade_later`），cli / update 组差异是前几节留下的，没更新快照。
**没验证**：没真实发布验证（要下一个 Release 之后、用带这个改动的版本才看得到）；GSI 连着时「一局里不弹」只有单测。
