"""第 1 步：OpenDota explorer 取 7.41f 超凡～冠绝天梯全英雄选择的对局编号（不限英雄），在版本里均匀取 BLOCKS 块，
随机抽 N 局（默认 200000），去掉黑皇杖研究已经抓过的（那 2 万局直接合进来用）。输出 data/research/item-vs/matches.json。

    python docs/research/item-vs/scripts/a_ids.py [N]
"""
import json
import random
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from dota_assistant.core.config import DATA_DIR  # noqa: E402
from dota_assistant.sources import http  # noqa: E402

OUT = DATA_DIR / "research" / "item-vs"
OUT.mkdir(parents=True, exist_ok=True)
N = int(sys.argv[1]) if len(sys.argv) > 1 else 200000
PATCH_START = 1789430400                    # 7.41f
# 7.41f 在 match_id 约 8 999 200 000 之后（2026-11-16 最新约 9 092 300 000）；每块 25 万个编号约 9 千局超凡～冠绝
FIRST, LAST = 9_000_000_000, 9_092_000_000
BLOCKS = 32
SPAN = 250_000
STARTS = [FIRST + (LAST - SPAN - FIRST) * i // (BLOCKS - 1) for i in range(BLOCKS)]


def sql(q: str) -> list[dict]:
    for _ in range(30):
        try:
            return http._sql(q)
        except http.APIError as e:
            print(f"  explorer 报错（{str(e)[:60]}），60 秒后重试", flush=True)
        except requests.RequestException as e:
            print(f"  网络 / 服务器问题（{str(e)[:60]}），60 秒后重试", flush=True)
        time.sleep(60)
    raise RuntimeError("OpenDota 一直连不上")


old = set()
bkb = DATA_DIR / "research" / "bkb" / "matches.json"
if bkb.exists():
    old = {r["match_id"] for r in json.loads(bkb.read_text(encoding="utf-8"))}

rows = []
for s in STARTS:
    q = (f"SELECT match_id, radiant_win, radiant_team, dire_team, avg_rank_tier FROM public_matches "
         f"WHERE match_id > {s} AND match_id <= {s + SPAN} AND start_time >= {PATCH_START} "
         f"AND game_mode = 22 AND lobby_type = 7 AND duration >= 900 AND avg_rank_tier BETWEEN 70 AND 89 "
         f"AND NOT (0 = ANY(radiant_team)) AND NOT (0 = ANY(dire_team))")
    r = [x for x in sql(q) if x["match_id"] not in old]
    rows += r
    print(s, len(r), "累计", len(rows), flush=True)
random.Random(23).shuffle(rows)
rows = rows[:N]
(OUT / "matches.json").write_text(json.dumps(rows), encoding="utf-8")
print("total", len(rows), "（另有黑皇杖研究的", len(old), "局）")
