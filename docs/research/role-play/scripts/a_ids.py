"""第 1 步：抽对局编号（不限英雄，均匀抽）。
- 7.42：OpenDota explorer 取上线后超凡～冠绝、天梯全英雄选择、≥15 分钟的全部对局，随机抽 N 局；
- 7.41f：从出装对位研究（docs/research/item-vs）均匀抽好的 7.41f 编号里再随机抽 N 局（条件一样），做对照。
输出 data/research/role-play/ids.json：{"742": [...], "741": [...]}

    python -X utf8 docs/research/role-play/scripts/a_ids.py [N]
"""
import json
import random
import sys
import time

import requests
from common import CUT_742, D

from dota_assistant.core.config import DATA_DIR
from dota_assistant.sources import http

N = int(sys.argv[1]) if len(sys.argv) > 1 else 15000
FIRST, SPAN, BLOCKS = 9_095_900_000, 500_000, 14     # 7.42 上线时 match_id 约 9 095 935 000


def sql(q: str) -> list[dict]:
    for _ in range(30):
        try:
            return http._sql(q)
        except (http.APIError, requests.RequestException) as e:
            print(f"  explorer 出错（{str(e)[:60]}），60 秒后重试", flush=True)
        time.sleep(60)
    raise RuntimeError("OpenDota 一直连不上")


new = []
for k in range(BLOCKS):
    s = FIRST + k * SPAN
    r = sql(f"SELECT match_id FROM public_matches WHERE match_id > {s} AND match_id <= {s + SPAN} "
            f"AND start_time >= {CUT_742} AND game_mode = 22 AND lobby_type = 7 AND duration >= 900 "
            f"AND avg_rank_tier BETWEEN 70 AND 89 AND NOT (0 = ANY(radiant_team)) AND NOT (0 = ANY(dire_team))")
    new += [x["match_id"] for x in r]
    print(s, len(r), "累计", len(new), flush=True)
    if not r and k > 3:
        break
rng = random.Random(7)
rng.shuffle(new)
old = [r["match_id"] for r in json.loads((DATA_DIR / "research" / "item-vs" / "matches.json").read_text(encoding="utf-8"))]
rng.shuffle(old)
out = {"742": sorted(new[:N]), "741": sorted(old[:N])}
(D / "ids.json").write_text(json.dumps(out), encoding="utf-8")
print("7.42 一共", len(new), "抽", len(out["742"]), "；7.41f 抽", len(out["741"]))
