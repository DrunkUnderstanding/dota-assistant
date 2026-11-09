"""第 1 步：OpenDota explorer 取 7.41f 超凡～冠绝天梯全英雄选择的对局（不限英雄），在版本里均匀取几块 match_id，
随机抽 N 局（默认 20000）。输出 data/research/bkb/matches.json。"""
import json
import random
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from dota_assistant.core.config import DATA_DIR  # noqa: E402
from dota_assistant.sources import http  # noqa: E402

OUT = DATA_DIR / "research" / "bkb"
OUT.mkdir(parents=True, exist_ok=True)
N = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
PATCH_START = 1789430400                    # 7.41f
# 7.41f 在 match_id 约 8 999 200 000 ~ 9 078 000 000 之间；取 6 块、每块 25 万个编号（每块约 9 千局超凡～冠绝）
STARTS = [9_003_000_000, 9_018_000_000, 9_033_000_000, 9_048_000_000, 9_063_000_000, 9_075_000_000]
SPAN = 250_000


def sql(q: str) -> list[dict]:
    for _ in range(30):
        try:
            return http._sql(q)
        except http.APIError:
            raise
        except requests.RequestException as e:
            print(f"  网络 / 服务器问题（{str(e)[:60]}），60 秒后重试", flush=True)
            time.sleep(60)
    raise RuntimeError("OpenDota 一直连不上")


rows = []
for s in STARTS:
    q = (f"SELECT match_id, radiant_win, radiant_team, dire_team FROM public_matches "
         f"WHERE match_id > {s} AND match_id <= {s + SPAN} AND start_time >= {PATCH_START} "
         f"AND game_mode = 22 AND lobby_type = 7 AND duration >= 900 AND avg_rank_tier BETWEEN 70 AND 89 "
         f"AND NOT (0 = ANY(radiant_team)) AND NOT (0 = ANY(dire_team))")
    r = sql(q)
    rows += r
    print(s, len(r), "累计", len(rows), flush=True)
random.Random(11).shuffle(rows)
rows = rows[:N]
(OUT / "matches.json").write_text(json.dumps(rows), encoding="utf-8")
print("total", len(rows))
