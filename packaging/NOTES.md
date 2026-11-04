[根目录](../CLAUDE.md) > packaging > **NOTES**

# packaging：设计与验证记录

> 内容：免安装打包。2026-11-04 从 `docs/HANDOFF.md` 原样拆过来，节号沿用原 HANDOFF 编号（代码注释 / 其它文档里的「HANDOFF 第 N 节」按 [HANDOFF 的节号索引](../docs/HANDOFF.md#节号索引) 找到在哪个文件）。
> 职责、文件表、接口、坑见同目录的 `CLAUDE.md`；这里只放「为什么这么做、怎么验证的、还差什么」，按时间追加。

## 当前状态（验证情况）

| 模块 | 状态 | 验证情况 |
|---|---|---|
| 免安装打包（`packaging/build.py`） | 🟡 能打出来 | 2026-10-23 开发机打包 + 启动通过（Electron 窗口、识别、推荐都起来了）；**没在一台没装 Python / Node 的干净电脑上试过** |

## 待实战验证 / 下一步

0. **免安装版**（第 21 节）：拿 zip 到一台没装 Python / Node 的电脑上解压试跑；有人用 API Key / DeepSeek 的话，看一局 AI 打法出处核对丢掉了多少条（日志「已丢掉」）。

## 21. 免安装打包 + 设置窗口（2026-10-23）

玩家需求（原话）：「帮我打包当前的内容」→ 选了「打成独立 exe」、带上 data/；
「里面的 APIKey 是不是应该让使用者自己提供，需要一个能够给他填入这个的位置」；「使用的 AI 应该也允许他自填对吧」→ 选了「Claude Code + Anthropic API Key + OpenAI 兼容接口」。

**设置窗口**（控制面板右上角「设置」；第一次打开且好友代码、token 都没填时自动弹一次，用 localStorage 记）：
- 账号：好友代码、STRATZ token。改了要重启才会重新拉个人数据 / 分位置数据。
- AI 打法：开关、来源（`ai_provider`：claude_code / anthropic / openai）、API Key（`ai_api_key`）、接口地址（`ai_base_url`）、模型。下一局就生效（`gameplan.generate` 每次读 `live.cfg`）。
- 保存走 `save_settings` 操作 → `config.save()` 只改 config.toml 里对应那一行的值，注释保留，没有的项加到末尾，写之前先用 tomllib 解析一遍。
- 密钥不发给页面（快照里只有「设没设 + 末 4 位」）、网页操作日志里打成 `***`。框留空 = 不改，只输空格 = 清掉。
- `start` 不再在命令行里问好友代码 / token，改成直接从 config.example.toml 生成 config.toml，提示去「设置」里填。
- 热键（2026-10-23 加，玩家原话「在设置里应该允许自由配置这个按钮的功能」）：5 个功能各一个下拉框，只给 F1~F12（单个字母虽然能注册，但会把 Dota 里那个键整个吃掉）；
  撞键页面标红不让存，`live._check_hotkeys` 也会拦；保存后 `Live._start_hotkeys` 整组重新注册，马上生效（`Hotkeys.stop` 等旧线程注销完再注册，实测同一个键能重新注册上）。
  点顶栏的快捷键也会打开设置并跳到热键。功能名和顶栏短说明都在 `live.HOTKEY_ACTIONS`。
- 控制面板顶栏自适应（同日，玩家说「有点挤」）：「设置」改成齿轮图标；窗口宽 ≤1320px 时快捷键换短说明（显隐 / 解锁 / 重识别 / 截图 / 要点），≤1020px 再收掉「上次识别」字样，完整说明在悬停提示。

**AI 来源**（`llm.py`）：
- claude_code：原来的无头 Claude Code。**填了 ai_api_key / ai_base_url 就让它走中转站**（2026-10-23 加）：
  玩家用 DuckCoding（`https://api.duckcoding.ai`），它的 Claude Code 分组直接用 SDK 调会 403「only accepts requests from the official Claude Code CLI」，只能让官方 CLI 去调。
  中转站参数用 `--settings <临时 json>` 的 env 传，不能只设进程环境变量：`~/.claude/settings.json` 里的 env 会盖过它（实测设错 key 照样成功，说明没生效）。
  验证：真实调用，错 key → 失败（Claude Code 一直重试，表现为超时），玩家的 key → 成功；单元测试 `ClaudeCodeRelayTest`。
- 玩家本机 config.toml 现在是 `ai_provider = "claude_code"` + DuckCoding 的 key 和地址。
- anthropic：官方 `anthropic` SDK（填了地址就当中转站，Bearer 认证），`output_config.format = json_schema` 硬约束输出；按 Key 无效 / 没模型 / 限速 / 网络分别报错。价格记 0（看 Anthropic 后台）。
- openai：`requests` 调 `{base_url}/chat/completions`，`response_format = json_object`（不认就去掉再试一次），schema 写进系统提示词；会剥 \`\`\`json 代码块。没有硬约束，`gameplan.parse` 加了类型判断，格式不对的条目跳过。出处核对照旧，对不上的整条丢。
- 打法缓存键加了来源（claude_code 不加，沿用旧缓存），换来源不会读到别家的结果。

**打包**（`python packaging/build.py`，产物 `build/Dota选人助手/` + `build/Dota选人助手-<日期>-<提交>.zip`）：
- PyInstaller onedir + 控制台窗口（第一次拉数据要看进度），入口 `packaging/launcher.py`：不带参数 = `start`，带参数和 `python -m dota_assistant` 一样；双击出错时停住等回车。
- `config.ROOT` 在打包版里是 exe 所在目录（`sys.frozen`），config.toml / data / logs / shots 都在旁边。
- Electron 本体拷到 `ui/`，main.js / preload.js 放 `ui/resources/app`，`webui.electron_command` 打包版不传目录参数。只留 zh-CN / en-US 语言包。
- data/ 带上，但去掉 player.json、overlay.json、.gsi_hint_shown、bench/；带上 calibration.json；写 VERSION.txt（日志里的「代码版本」读它）。
  2026-10-27 又去掉 `lane_log.jsonl`（对线实战记录，里面有自己的 Steam ID / 名字）和 `backtest_matches.json`（12 MB 回测缓存）。
- 2026-10-27 起打包第一步 `prefetch()`：补全头像、重下装备常量、补装备图标、顶栏图标（金币 + 英雄小图标 + 大招），只下缺的；
  没有英雄表 / 知识库、或顶栏图标没下到（断网）就停。包里带着 `data/icons/prefetched.txt`，用的人启动时不会再补下。
  实跑：打包通过（目录 609 MB、zip 264 MB），包里 98 张大招 + 127 张英雄小图标 + 金币 + 416 张装备图标，没有 lane_log / 回测数据；
  拷到临时目录跑 exe 的 `suggest`、`kb hero` 正常。
- 打包最后扫一遍产物，本机 config.toml 里的 stratz_token / ai_api_key 出现在任何文本文件里就中止。
- 大小：目录约 590 MB（Electron 320、OpenCV 110、PySide6 75），zip 约 250 MB。PySide6 只有 `calibrate` 用，以后可以考虑去掉。
- 验证：开发机上解压目录里双击跑 `start`：生成 config.toml、GSI 服务、Electron 两个窗口、识别和推荐都起来了；用接口点了一次保存设置，config.toml 写对、日志里没有 key。
  当时开发版助手也开着，所以热键注册失败、Electron 缓存目录被占（报 Unable to move the cache），都是两份同时开导致的。
- 注意：没有 config.toml 时直接跑命令行子命令（如 `suggest`），Config 默认值是段位 0-0、7 天，和打包带的数据文件（7-8、14 天）对不上会报找不到数据；走 `start` 会先生成 config.toml，没问题。
