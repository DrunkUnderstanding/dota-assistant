[根目录](../../CLAUDE.md) > cli > **NOTES**

# cli：设计与验证记录

> 内容：常用命令。2026-11-04 从 `docs/HANDOFF.md` 原样拆过来，节号沿用原 HANDOFF 编号（代码注释 / 其它文档里的「HANDOFF 第 N 节」按 [HANDOFF 的节号索引](../../docs/HANDOFF.md#节号索引) 找到在哪个文件）。
> 职责、文件表、接口、坑见同目录的 `CLAUDE.md`；这里只放「为什么这么做、怎么验证的、还差什么」，按时间追加。

## 9. 常用命令

```
python -m unittest                                  # 回归
python -X utf8 -m compileall -q dota_assistant tests packaging tools   # 门禁 G1：编译（再加下一行 ruff）
ruff check dota_assistant tests tools packaging     # 门禁 G1：未使用 import / 未定义名字 / 语法类（不要 ruff format）
python tools/golden/run.py check                    # 门禁 G3：行为快照逐字节一致，约 25 秒（--slow 再加回测类，阶段验收 G6）
python tools/check_isolation.py                     # 门禁 G4：单测不碰 data/ shots/ logs/ 等真实目录
python tools/browser_check.py                       # 阶段验收 G6：无头浏览器开三个页面，看控制台报错
python -m dota_assistant update [--player]          # 拉数据
python -m dota_assistant suggest --enemy ... --pos 3
python -m dota_assistant run
python -m dota_assistant calibrate <截图>
python -m dota_assistant recognize <截图>
python -m dota_assistant label <截图...>
python -m dota_assistant gsi-install "<dota 2 beta 目录>"
python -m dota_assistant say <文字>
python -m dota_assistant logs [-n 200] [--open]     # 看最新一份日志的末尾 / 打开 logs 文件夹
python -m dota_assistant backtest [--matches 3000]   # 样本外回测，约 1~2 分钟（STRATZ 取数据），结果缓存
python -m dota_assistant backtest --tune            # 网格搜 W_LANE / W_RISK，一半调参一半验证
python -m dota_assistant next-test [--matches 4000]  # 对面最后一手猜得准不准（和 backtest 共用缓存的对局），约 20 秒
```

改推荐逻辑后重跑 `backtest`（会复用缓存的对局），对比报告数字是否变好；`update` 拉了新统计后，缓存会因为截止点变化自动重新取样本。

调试 STRATZ 接口：POST `https://api.stratz.com/graphql`，头 `Authorization: Bearer <token>`、`User-Agent: STRATZ_API`（不带 UA 会被拒）。
可以用 `{ __type(name: "HeroStatsQuery") { fields { name } } }` 这类自省查询找字段。
