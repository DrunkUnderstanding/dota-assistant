"""CLI 快照的命令清单（顺序固定：前面的命令写进冻结目录的缓存会影响后面的，加新命令请加在组的末尾）。

{HOME} 在运行时换成这次解压出来的冻结目录。
"""
from __future__ import annotations

# 冻结目录里放的 14 张选人截图（原图，不要 *_识别.png）
DRAFT_SHOTS = [
    "20261022-215058", "20261022-215059", "20261022-220708", "20261022-220710", "20261022-220919",
    "20261022-231827", "20261023-004917", "20261023-013637", "20261023-013638", "20261023-013639",
    "20261023-014333", "20261023-014335", "20261023-014347", "20261023-190213",
]

SUBCOMMANDS = ["update", "suggest", "start", "run", "demo", "calibrate", "recognize", "label", "gsi-install",
               "backtest", "lanes-test", "lane-test", "lane-review", "backtest-ai", "coach-sim", "death-sim",
               "brief-sim", "kb", "gsi-dump", "say", "logs"]

SUGGEST = [
    ["--enemy", "PA,莱恩,风行者", "--ally", "敌法师,冰女", "--pos", "3"],
    ["--enemy", "末日,沉默", "--pos", "1", "--me", "卓尔", "--side", "radiant"],
    ["--pos", "1"],
    ["--pos", "5", "--ban", "小小,斧王"],
    ["--enemy", "敌法师,军团", "--pos", "2", "--top", "6"],
    ["--all-roles", "--pos", "4"],
    ["--ally", "敌法师,冰女", "--enemy", "PA,莱恩", "--pos", "1"],
    ["--me", "莱恩", "--pos", "5", "--enemy", "半人马,风行者", "--side", "dire"],
    ["--me", "斧王", "--pos", "3", "--enemy", "敌法师,水晶室女"],
    ["--enemy", "PA，斧王", "--ally", "冰女", "--ban", "敌法师", "--pos", "1"],     # 全角逗号
    ["--enemy", "潮汐猎人,裂魂人,莱恩,森海飞霞,水晶室女", "--ally", "敌法师,痛苦女王,撼地者,巫医", "--pos", "4"],
    ["--enemy", "不存在的英雄", "--pos", "1"],                                      # 名字错：退出码 1
    ["--pos", "2"],                                                                  # 没给 --side：按 config
]

CLI = (
    [["--help"]]
    + [[c, "--help"] for c in SUBCOMMANDS]
    + [["suggest", *a] for a in SUGGEST]
    + [
        ["kb", "hero", "小黑"], ["kb", "hero", "末日"], ["kb", "item", "BKB"], ["kb", "item", "跳刀"],
        ["kb", "find", "克敌机先"], ["kb", "find", "眩晕.*秒"],
        # 不带 --wrong 只列出、不写文件（cli._kb_mistake）；下面两条走「参数不全 / 标签不存在」的分支，也不写文件
        ["kb", "mistake"],
        ["kb", "mistake", "--wrong", "golden 测试"],
        ["kb", "mistake", "--wrong", "golden 测试", "--right", "golden 测试", "--hero", "小黑", "--has-tag", "不存在的标签"],
        ["kb", "hero", "不存在的英雄"], ["kb", "item", "不存在的物品"],
    ]
    + [["recognize", f"{{HOME}}/shots/{s}.png"] for s in DRAFT_SHOTS]
    + [
        ["brief-sim", "9055590694"],
        ["coach-sim", "9054096051"],
        ["death-sim"],
        ["death-sim", "{HOME}/shots/deaths/20261028-011337.json"],
        ["lane-review"],
        ["logs", "-n", "40"],
    ]
)

# 会改冻结目录里 data/ 的：单独一组、一份新解压的目录
# logs 紧跟在 update 后面：两条 update 落在同一秒会写进同一个日志文件，「共 N 行」就不固定了
UPDATE = [
    ["update"],
    ["logs", "-n", "15"],
    ["update", "--player"],
]

# 慢快照（check --slow）：backtest 的缓存（data/backtest_matches.json）是旧统计数据那批的（after_id 对不上现在的 hi_id），
# 所以会重新抽样，抽样和取对局都走 cassette；lanes-test / lane-test 接着用 backtest 存下的那批
SLOW = [
    ["backtest", "--matches", "300"],
    ["lanes-test", "--matches", "300"],
    ["lane-test", "--matches", "300"],
]

CASES = {"cli": CLI, "update": UPDATE, "slow": SLOW}
