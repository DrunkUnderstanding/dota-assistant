[根目录](../../CLAUDE.md) > dota_assistant > **domain**

# domain：领域逻辑

## 职责

选人推荐打分、推荐分组与显示、对面分路、机制对位、出装、局内缺口提醒、本局要点、死亡复盘、野区 / 买活 / 大招计时、队友战绩。**不依赖界面**，输入是数据（阵容、GSI 推送、知识库），输出是结构化结果或文字，方便单测和回放。

层号 4，只能 import `core` / `sources` / `kb` / `ai`。被 `vision` / `game` 以上的层使用（主要是 `app/live`、`research`、`cli`）。

## 关键文件

| 文件 | 管什么 |
|---|---|
| `engine.py` | 打分：自身强度、克制、配合、位置筛选（个人数据项系数已置 0）。`Suggestion`（`winrate` / `breakdown` / `calc_text` / `reason_parts` / `reason`）、`Engine`（`suggest` / `draft_rest` / `threats` / `base_at` / `advantage` / `synergy` / `fits` / `fits_by_roles`）；常数都在文件头，带推导注释 |
| `advisor.py` | 局面 `Draft` → 建议 `Advice`（`Advisor.advise`）；分「推荐 + 针对这局」（`split_targeted`）；剩余位置 `TeamSlot` / `team_slots`；「阵容」胜率 `draft_rate`；播报 `speech()`；显示 `render_lines()`（命令行）/ `render_view()`（网页快照） |
| `lanes.py` | 对面分路：`guess` 猜谁打几号位（带把握，`SURE = 0.6` 以下标「?」）、`assign` 三路排法、`tips` 「你这路怎么打」硬规则（每条带官方原文）、`evaluate()` 回测 |
| `explain.py` | 推荐理由里的机制对位（克敌机先 ↔ 闪避、显形 ↔ 隐身、破坏 ↔ 被动、无视免疫 ↔ 技能免疫），`Why` 带官方原句，不影响排名 |
| `teamfight.py` | 后期团战提示：要等的大招、留打断、控制留给对面核心、谁能保人、指向技能别往哪丢 |
| `itembuild.py` | 出装：`make()` 核心装 + 针对装排顺序、「下一件」和还差多少钱（`remaining_cost` / `next_line` / `next_parts`）、后期出装 / 卖哪件（`late_lines` / `sell_pick`）、按死法补装（`death_pick` / `death_sim`）；装备一行写成片段列表（`chip` / `group` / `text`） |
| `coach.py` | 局内缺口提醒：`plan()` 开局定缺口（对抗装、不需要的装备、节奏），`Coach.tick()` 去重、`Coach.on_death()`；`prepare()` 给实时版，`simulate()` 录像重放 |
| `brief.py` | 本局要点：`build()` 出一张按阶段分页的卡片（`STAGES` 对线期 / 中期 / 后期，`STAGE_START` 10 / 30 分钟弹）：对线、起手装、强势期、出装、对面要防、团战；`simulate()` 录像回放；缓存 `data/brief/` |
| `death.py` | 死亡复盘：死前 `WINDOW = 20` 秒的 GSI 推送 → `analyze()` 出「怎么死的」几句话（`Recap`）；`dump()` 把原始推送存 `shots/deaths/` |
| `jungle.py` | 野区计时：封野、拉野（双拉）、屯野，按位置 / 英雄到点前几秒弹一行（`make()` → `Plan.cue()`）；顶栏倒数用的整局动作表 `Plan.timeline()`，第二行抢莲花 `Plan.lotus_timeline()`（不进 `kinds`，卡片 / 野点框没有，第 61 节）；倒数前面的物品图标 `Plan.bar_icon()`（封野眼、莲花治疗莲花，第 62 节） |
| `camps.py` | 野点标记：野点 / 刷新框坐标表（`CAMPS`，本机游戏文件读的）、优势路拉野点 `PULL`、每个野点几秒屯 / 拉（`load_timings` 读游戏文件 `creep_pull_timings.txt`，Dota 按住 Alt 显示的就是它），`marks()` 按野区计划 + 阵营 + 时钟 + 英雄坐标给出要圈的野点（世界坐标 + 刷新框 + 小地图上 0~1 的位置 + 字 + 到点时刻）；HANDOFF 第 53、54 节 |
| `buyback.py` | 买活计时：双方 10 人的买活冷却 `Tracker`（`mark` / `start` / `sync_self` / `reset`），`COOLDOWN = 480` |
| `ultcd.py` | 对面大招冷却：按等级 + 勾的减冷却（天赋 / 神杖 / 魔晶 / 玲珑心）算 `cooldown`，`Tracker`（`mark` / `recool` / `reset`）；挑大招 / 各级冷却在 `kb/cd_ops.py` |
| `lanelog.py` | 对线实战记录：`log_moment()` 在 3:00 / 6:00 / 10:00 把自己的 GSI 数据追加进 `data/lane_log.jsonl`（`LOG_MINUTES`），赛后 `lane-review` 对照 |
| `mates.py` | 队友战绩：`parse_code`（好友代码 / Steam ID / 个人页链接）、`fetch` / `fetch_match`、表现分 `score`（同英雄分位）、可选 STRATZ 评分；`Service`（`list` / `query` / `remove` / `match`）给 webui 的 `/api/mates`、`/api/match` 用；查过的人存 `data/mates.json` |

## 模块内依赖（改一处会波及）

- `advisor` → `engine`、`explain`、`lanes`；`brief` → `coach`、`itembuild`、`lanes`、`teamfight`、`ai.gameplan`；`itembuild` → `coach`。
- `lanes` 用 `death.SAVE_EXCLUDE`；`teamfight` 用 `death.SAVE_RE` / `SAVE_EXCLUDE`、`lanes` 的 `ESCAPE_EXCLUDE` / `INTERRUPT_TAGS` / `UNTOUCHABLE`：改这些正则会同时影响对线提示和团战提示。

## 打分要点（`engine.py` 文件头）

- 所有量在 **logit 空间相加**再换回胜率；克制 / 配合 = 实际对位胜率 − 按双方强度推算的期望，再按样本贝叶斯收缩（`K_VS = 400`、`K_SYN = 1100`，`W_SYN = 1.0`）。
- 个人数据不参与排名：`W_ME`、`FAMILIAR_BONUS`、`POOL_BONUS` 都是 0（玩家 2026-10-24 / 25 决定）；对线加权 `W_LANE`、反制扣分 `W_RISK` 回测无效，默认 0，只保留「易被反制」文字提示（HANDOFF 第 2.1 节）。
- 冷门英雄 / 冷门位置打折：`COLD_RANK = 40`、最多扣 `COLD_MAX` ≈ 2 个百分点，只影响自身强度项（HANDOFF 第 43.1、43.2 节）。
- 「阵容」胜率用 `Engine.draft_rest` + 阵营 `RADIANT_EDGE = 0.13`，只进叠加层标题栏，不影响排名（HANDOFF 第 45 节）。
- 「推荐」只放生涯打过 `familiar_games`（默认 20）局以上的英雄，不熟的进「针对这局」并标出来；含双排队友（HANDOFF 第 43 节）。`Draft.skip`（这局不想玩）只从给我的推荐里去掉、不算禁用、不进 `Draft.key()`（HANDOFF 第 44 节）。

## 对应测试

`test_engine`（人造数据回归）、`test_familiar`（熟练度分组 + 胜率拆解）、`test_skip`、`test_lanes`、`test_explain`、`test_teamfight`、`test_itembuild`、`test_coach`、`test_brief`、`test_death`、`test_jungle`、`test_camps`、`test_buyback`、`test_ultcd`、`test_mates`、`test_lanestate`（含 `lanelog`）、`test_live`（播报文案）、`test_webui`（结构化视图）、`test_nokey`。

## 注意事项 / 坑

- **动了推荐逻辑**：跑 `python -m dota_assistant suggest ...` 看几组结果是否符合常识；`tools/golden/run.py check` 会把 13 组 `suggest` 的输出逐字节比对，行为本该变时先看 diff 再 `update-snapshot`；大改打分再跑 `backtest` 对比区分度。
- **机制判断全部来自知识库原文**（`explain` / `lanes` / `teamfight` / `coach`），每条带原句；不用大模型（大模型凭自身理解排选人不如统计引擎，HANDOFF 第 10 节）。规则和措辞多是「实现假设」，玩家没确认过。
- `death.py` 的字段名按 Valve GSI 写法，**还没在真实对局推送里核对过**；`buyback.py` 的自己冷却、`ultcd` 的 `hero.level` 也没在真实推送里见过（HANDOFF 第 2 节当前状态）。
- 出装**不按装备胜率排**（买得晚的大件胜率天然高）；没「出了更容易赢」大数据支撑的针对装只作提示、不进顺序（`itembuild.py` 文件头、HANDOFF 第 33 节；玩家要求出装建议要「大数据 + 本局」双重依据）。
- 买活 / 大招计时只记玩家看得到的事（买活全场公开；对面大招要玩家看到后点一下），不显示玩家本来看不到的信息。
- `mates` 只用公开接口（OpenDota / 可选 STRATZ），对方没公开比赛数据就查不到；`data/mates.json` `matches/` `benchmarks/` 是个人数据，不打包。

## 设计记录（原「相关 HANDOFF 节」）

本模块的设计 / 验证 / 待验证记录在 [NOTES.md](NOTES.md)（选人推荐 + 队友战绩）、[NOTES-ingame.md](NOTES-ingame.md)（局内）（2026-11-04 从 HANDOFF 拆过来，节号不变）。下面列的节号不在本目录 NOTES 里的，按 [HANDOFF 节号索引](../../docs/HANDOFF.md#节号索引) 找。

- 打分 / 推荐：第 2.1 节、第 5 节、第 24 / 24.1 节（剩余位置）、第 26 节（推荐 + 针对这局）、第 43 / 43.1 / 43.2 节、第 44 节、第 45 节
- 机制对位 / 分路：第 16 节（选人时的「为什么选」）、第 18 节（推荐卡片上的「对位」克制）、第 22 节（对面分路 + 对线打法）、第 30 节（后期团战）
- 局内：第 11 / 11.1 节（缺口提醒）、第 15 节（本局要点）、第 18 节（死亡复盘）、第 25 / 25.1 节（出装、按死法补装）、第 27 节（野区）、第 28 节（本局要点按阶段弹）、第 29 节（后期内容）、第 31 / 32 节（买活、大招）、第 33 节（出装双重依据）、第 34 节（装备图标）
- 队友战绩：第 38 / 38.1 / 38.2 节

## 变更记录 (Changelog)

- 2026-11-02 11:18:41：初建，内容来自 HANDOFF 第 2~5 节 + 各节标题 + 源码模块说明。
- 2026-11-04：HANDOFF 按模块拆分，过程记录搬进各模块 `NOTES.md`；「相关 HANDOFF 节」改成「设计记录」，指向 NOTES 和 HANDOFF 节号索引。
