"""第 1 步：定出 7.42 超凡以上胜率前 20 的英雄（用胜率监控攒的数据、同一套估计算法），
再从 OpenDota explorer 取 7.42 上线后超凡～冠绝天梯全英雄选择的对局编号，每个英雄最多抽 PER_HERO 局。
输出 data/research/hero-builds/top.json、matches.json。

    python -X utf8 docs/research/hero-builds/scripts/a_ids.py
"""
import json
import random
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "docs" / "research" / "patch-watch"))
import watch  # noqa: E402

from dota_assistant.core.config import DATA_DIR  # noqa: E402
from dota_assistant.sources import http  # noqa: E402

OUT = DATA_DIR / "research" / "hero-builds"
OUT.mkdir(parents=True, exist_ok=True)
TOP = 30                  # 2026-11-20 玩家要前 30（想看兽王这类）
GROUP = "hi"              # 超凡以上（玩家 2026-11-20 明确要超凡以上；对局编号也只取平均段位超凡～冠绝）
PER_HERO = 3000          # 2026-11-20 第二版：要算每条路线的胜率，样本从 900 加到 3000
FIRST, SPAN, BLOCKS = 9_095_000_000, 500_000, 10  # 7.42 上线时 match_id 约 9 095 935 000


def sql(q: str) -> list[dict]:
    for _ in range(30):
        try:
            return http._sql(q)
        except (http.APIError, requests.RequestException) as e:
            print(f"  explorer 出错（{str(e)[:60]}），60 秒后重试", flush=True)
        time.sleep(60)
    raise RuntimeError("OpenDota 一直连不上")


d = watch.compute(watch.load_hist(), time.time())
rows = [h for h in d["heroes"] if h["b"][GROUP]["est"][-1]]
rows.sort(key=lambda h: -h["b"][GROUP]["est"][-1])
top = [{"id": h["id"], "name": h["name"], "rank": i + 1, "est": h["b"][GROUP]["est"][-1],
        "sd": h["b"][GROUP]["sd"][-1], "act": h["b"][GROUP]["act"][-1], "n": h["b"][GROUP]["n"][-1],
        "pre": h["b"][GROUP]["pre"], "pos": h["pos"]} for i, h in enumerate(rows[:TOP])]
asof = d["times"][-1]
(OUT / "top.json").write_text(json.dumps({"asof": asof, "cut": watch.CUT, "group": GROUP, "heroes": top},
                                         ensure_ascii=False, indent=1), encoding="utf-8")
print("截至", time.strftime("%m-%d %H:%M", time.localtime(asof)), f"；前 {TOP}：", "、".join(h["name"] for h in top))

ids = {h["id"] for h in top}
arr = "ARRAY[" + ",".join(map(str, sorted(ids))) + "]::int[]"
allm = []
for k in range(BLOCKS):
    s = FIRST + k * SPAN
    q = (f"SELECT match_id, radiant_team, dire_team FROM public_matches "
         f"WHERE match_id > {s} AND match_id <= {s + SPAN} AND start_time >= {watch.CUT} "
         f"AND game_mode = 22 AND lobby_type = 7 AND duration >= 900 AND avg_rank_tier BETWEEN 70 AND 89 "
         f"AND (radiant_team && {arr} OR dire_team && {arr})")
    r = sql(q)
    allm += r
    print(s, len(r), "累计", len(allm), flush=True)

random.Random(42).shuffle(allm)
by = {h: [] for h in ids}
for m in allm:
    for h in set(m["radiant_team"]) | set(m["dire_team"]):
        if h in by:
            by[h].append(m["match_id"])
pick = set()
for h in sorted(ids, key=lambda h: len(by[h])):          # 冷门的先拿
    have = sum(1 for x in by[h] if x in pick)
    for x in by[h]:
        if have >= PER_HERO:
            break
        if x not in pick:
            pick.add(x)
            have += 1
for h in top:
    print(f"{h['name']}：explorer 里 {len(by[h['id']])} 局")
(OUT / "matches.json").write_text(json.dumps(sorted(pick)), encoding="utf-8")
print("抽了", len(pick), "局")
