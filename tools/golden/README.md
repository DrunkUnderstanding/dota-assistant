# 行为快照（重构门禁 G3）

重构「只搬家不改行为」的安全网：把一批 CLI 命令的输出和一整局 Live 回放的状态变化存成快照，
搬完每一步跑 `check`，**逐字节一致**才算过。计划见 [docs/refactor/PLAN.md](../../docs/refactor/PLAN.md) P0.4。

进 git 的只有这个目录里的脚本；快照、cassette、冻结目录都在仓库外的 `D:\dota-golden\`（里面有个人数据，**不进 git、不外传**）。

## 常用命令

```bat
python tools/golden/run.py check                 :: 门禁 G3：cli + replay + update 三组，约 25 秒
python tools/golden/run.py check --slow          :: 阶段验收 G6：再加 backtest / lanes-test / lane-test，约 26 秒
python tools/golden/run.py check --group replay  :: 只跑一组（排查用；--keep 保留解压出来的临时目录）
python tools/golden/run.py check --shift-days 1  :: 模拟「隔一天再跑」（见下面「时间」）
python tools/golden/run.py record --reuse-home   :: 重录 cassette 并重写快照（会真联网，update 组要 6 分钟左右；--group G 只重录一组）
python tools/golden/run.py record                :: 从 D:\dota 重建冻结目录再录（冻结时刻也换新）
python tools/golden/run.py update-snapshot       :: 只按当前代码重写快照，不联网（确认差异是该有的才用）
python tools/golden/run.py scan-token            :: 确认 D:\dota-golden 下没有真 STRATZ token
```

`check` 退出码：0 = 全部一致且没碰真实目录；1 = 有不一致（打印前 300 行 unified diff，完整的在 `D:\dota-golden\last-diff\`，每次 check 开头先清空这个目录）
或 G4 发现仓库根的 `data/ shots/ logs/ templates/ config.toml calibration.json` 被改动；其他 = 子进程出错（看输出的堆栈）。

**重构时不要用 `update-snapshot` / `record` 让 check 通过**：快照不一致就是行为变了，停下来查。
唯一允许的可见差异（日志模块名）已经归一化掉了。

## 目录

| 位置 | 是什么 |
|---|---|
| `D:\dota-golden\home.zip` | 冻结的运行目录（`home\` 是解压后的样子，方便看）。每组快照跑之前解压到系统临时目录，所有文件 mtime 设成冻结时刻，`DOTA_ASSISTANT_HOME` 指向它 |
| `D:\dota-golden\meta.json` | 冻结时刻 `epoch`（假时钟用）、建立 / 录制时间 |
| `D:\dota-golden\cassettes\<组>\http\` | 网络录放（每组一份）：一个请求（方法 + URL + 参数 + 请求体）一个 JSON，里面按顺序存每次的响应；record 时还存「发出去的样子」`sent`（请求头 / timeout / 重试，token 是占位符） |
| `D:\dota-golden\cassettes\<组>\ocr.json` | Windows OCR 录放（图片像素 sha1 → 认到的字） |
| `D:\dota-golden\snapshots\{cli,replay,update,slow}.txt` | 快照 |
| `D:\dota-golden\record-output\` | 最近一次 record 时（真联网那遍）的输出，排查用 |
| `D:\dota-golden\last-diff\` | 最近一次 check 不一致的完整 diff 和新输出 |

冻结目录内容（`g_home.py`）：从 `D:\dota` 只读复制 `config.toml`（处理过，见下）、`config.example.toml`、`knowledge/`、`data/`、
`calibration.json`、`shots/` 里 14 张选人截图（`g_cases.DRAFT_SHOTS`）、`shots/deaths/*.json` 全部、`shots/buyback/feed/` 全部、
回放用的一张局内截图。

`config.toml` 的处理：`stratz_token` 换成占位符 `golden-dummy-stratz-token`、`ai_api_key` 清空、`ai_plan = false`、
`voice = false`、`dota_dir = ""`。**token 用占位符而不是清空**：清空的话本局要点 / 局内提醒 / 队友战绩 / lane-review / update
里走 STRATZ 的分支全都跳过，快照就测不到了。record 时请求发出去之前（只在内存里）把占位符换成 `D:\dota\config.toml` 里的真 token；
cassette 里存的请求头是占位符（真 token 在存盘前一律换回占位符），`scan-token` 会查一遍。

## 快照里有什么

**cli 组**（`g_cases.CLI`，一个进程里依次调 `dota_assistant.cli.main(argv)`）：每条记退出码、stdout、stderr、
这条命令新写的日志文件内容、**这条命令写过的冻结目录文件**（见下面「文件变动」）。顶层和每个子命令的 `--help`；13 组 `suggest`；`kb hero/item/find/mistake`（`kb mistake` 不带
`--wrong` 只列出、不写文件；另外两条走「参数不全 / 标签不存在」的分支，也不写）；14 张截图的 `recognize`；`brief-sim 9055590694`、
`coach-sim 9054096051`、`death-sim`（全部 / 单个文件）、`lane-review`、`logs`。

**update 组**：`update`、`update --player`（会改冻结目录的 `data/`，所以单独一组、一份新解压的目录）。

**slow 组**（`--slow`）：`backtest / lanes-test / lane-test --matches 300`。`data/backtest_matches.json` 是旧统计数据那批的
（`after_id` 对不上现在 `stats` 的 `hi_id`），所以 backtest 会重新抽样——抽样 SQL 和取对局都走 cassette，结果固定。

**replay 组**（`replay_live.py`）：造一个 `Live`（同 `tests/test_webui.py`：不开 GSI 端口、不注册热键，GSI 换成一个 `GameState`
直接改推送内容），推荐 / 识别 / 网格 / 知识库 / 本局要点 / 局内提醒 / 队友战绩都用真的，按剧本推完整一局：
选人截图帧（识别、网格禁用、认位置）→ 帮认 / 手填 / 各种设置和叠加层操作 → 「如果选它」→ 策略时间本局要点 → 比赛 9064593746
的真实推送（号角、0:04 / 4:59 / 10:13 三次死亡）→ 野区提醒、3/6/10 分钟对线记录、10:00 中期页 → 买活（顶栏变灰 → 复活 →
消息栏「买活了」帧，自己的按 GSI，手动点）→ 对面大招 → 15~30 分钟检查点 → 比赛结束 → 设置保存 → 下一局选人 → 队友战绩 → 打开日志。
第 1 节列的 24 个网页动作（`Live.action` 的 type）每个都走到，没走到会直接报错。
认位置：手头的截图顶栏都没有位置字样（真 OCR 认不出），所以另有一步把 `roles.ocr` 临时换成合成的「中路 劣势路 辅助 优势路 纯辅助」，
走通「认出位置 → 改位置 → 播报」，下一局选人再走「自动认的位置不带到下一局」。
每步记：结果（`action` 的返回值或异常）、这一步的日志（级别 + 消息）、要念的话、`snapshot()` 里变了的顶层键（完整内容），
再加三项 `snapshot()` 里没有、网页 / Electron 会用的：`额外.overlay_pos`（拖过的位置）、`额外.overlay_config`（网页 `/overlay`
接口回的 `webui.overlay_config(cfg, overlay_pos)`）、`额外.overlay_file`（叠加层状态文件）——叠加层动作之后和回放结束时不管变没变都记。
开头记一次 `live.heroes()`（网页 `/api/heroes`，帮认菜单用），结尾列出回放写过的冻结目录文件（见「文件变动」，文本附全文）。

确定性：不起 Live 的工作线程，主线程按顺序跑 `submit` 进来的任务（`Live._run`）；Live 开的后台线程（target 是
`dota_assistant` 里的函数）就地同步跑完；时钟是假的，只在剧本里拨。

## 文件变动（`g_files.py`）

命令 / 回放写进冻结目录的东西也是行为（`update` 写的统计数据、`save_settings` 写的 `config.toml`、帮认流水 `data/fixes.jsonl`、
队友战绩、对线记录、死亡推送存档、学习模板、截图…）。每条 CLI 命令前后、回放前后各扫一遍冻结目录（`logs/` 不算，日志另外记），
按 mtime / 大小找出新增 / 改动 / 删除的文件：
- 二进制：原始字节的大小 + sha1；
- 文本（.json .jsonl .toml .txt …）：先归一化（同下面「归一化」），JSON 按键排序重排，再算大小 + sha1；回放结尾附全文
  （超过 64KB 只记 sha1；`config.toml`、`data/fixes.jsonl` 不管多长都贴全文）；
- cli / update / slow 组的假时钟是「冻结时刻 + 真实流逝」，写进文件的 `time.time()`（`fetched_at` 之类）每次差零点几秒：
  文本里落在 [冻结时刻, 冻结时刻 + 6 小时] 的 10~13 位数字换成 `<运行时刻>`。回放组时钟完全是假的，不换。

## 请求头核对

cassette 的键不含请求头，所以另外核对：每个请求「发出去的样子」（产品代码给的 headers、Session 自带的非默认 headers、timeout、
适配器的 `max_retries`）按主机归类计数写在快照末尾；cassette 里存了 `sent` 的，还逐条比对，对不上的列在末尾。
`data._stratz` 的 `Authorization` / `User-Agent: STRATZ_API` 或 `_session` 的重试设置搬家时丢了，都会比出来。

## 加新快照

- CLI：在 `g_cases.py` 对应组的**末尾**加一条（前面的命令会往冻结目录写缓存，插在中间会改后面的结果）。
  `{HOME}` 会换成冻结目录。然后 `record --reuse-home`（要联网录新请求；不联网的命令用 `update-snapshot` 就行）。
- Live：在 `replay_live.Replay.run()` 里加步骤（`self.act({...})` 走网页动作，`self.call(label, fn)` 走别的方法，
  `self.game_push(...)` / `self.tick(...)` 推 GSI）。产品模块一律从 `_modules()` 取：那里按「新路径优先、旧路径兜底」import，
  重构搬家以后 runner 不用改。
- 冻结目录要多带文件：改 `g_home.py`，然后 `record`（重建目录，冻结时刻换新，所有快照会跟着重写）。

## 归一化了什么（`g_normalize.py`）

只抹掉搬家 / 换时间 / 换临时目录会变、但不算行为的东西，其余原样比：

1. 路径：临时冻结目录 → `<HOME>`，仓库根 → `<REPO>`，`D:\dota-golden` → `<GOLDEN>`，系统临时目录 → `<TMP>`
2. 堆栈：`Traceback` 的每一帧 → `  <堆栈>`（只留异常类型和消息）；别处的 `xxx.py:行号` → `<py>:<N>`
3. 模块名：日志行里的 `%(mod)s` → `<mod>`（`live` → `app.live.draft` 是计划允许的唯一可见差异）；`dota_assistant.xxx.yyy` → `dota_assistant.<mod>`；
   回放的日志只记级别和消息，本来就不带模块名
4. 时间：日志行开头的时间戳、控制台警告开头的 `HH:MM:SS`、`isoformat` 时间、文件名里的 `YYYYMMDD-HHMMSS`；
   日志开头那行「Python 版本 | 系统 | 代码版本」
5. 内存地址 `0x…`；线程编号（`ThreadPoolExecutor-0_3` → `pool`，`Thread-12` → `Thread-N`），线程池打的连续几行日志按内容排序

## 时间（为什么隔天跑也一样）

`g_clock.py`：`time.time()` 冻在 `meta.json` 的冻结时刻（cli 组再加上进程里真实过去的秒数），冻结目录的文件 mtime 也是这个时刻，
所以按 mtime 判断的缓存（本局要点 3 天、局内提醒对照数据 3 天）永远新鲜、不会去联网；回放组 `time.time / monotonic / sleep`
全是假的，`datetime.now()` / 不带参数的 `strftime` / `localtime` 也跟着假时钟走（死亡推送、截图的文件名固定，不会因为真实秒数撞名 / 不撞名而变）。
cli 组的 `datetime.now()` 走真实时间，用到它的地方（日志文件名、帮认记录和对线记录里的时间…）在快照里被第 4 条归一化。
`check --shift-days N` 把这些「真实时间」往后拨 N 天跑一遍（`time.time` 仍是冻结时刻，和真隔天跑一样）。

## 闸（check 模式不碰外面）

- 网络：`requests.Session.request` 只读 cassette，取不到抛 `CassetteMiss`（`requests.ConnectionError` 的子类：产品代码当断网处理，
  错误出现在快照里，输出末尾也会列出缺了哪些请求）；`socket.connect` 连非本机地址直接报错。
- 子进程：PowerShell（OCR / 语音）、claude、node、npm、electron 一律不起（record 时只放行 OCR 的 PowerShell；AI 本来就因为 `ai_plan = false`
  和回放里的假 `plan_build` 不会调）。
- `os.startfile`（「打开日志」会弹资源管理器）、`webbrowser.open` 换成只记一笔；`winreg.OpenKey` 报错（不去注册表找 Steam / Dota，
  和这台机器装没装 Dota 无关）。
- G4：`check` 前后用 `tests/tools/sentinel.py` 记一次仓库根的玩家目录，**任何**变化都算失败（不用 `sentinel.KNOWN` 给单测开的 `shots/gsi/` 豁免：golden 只该写临时冻结目录）。
