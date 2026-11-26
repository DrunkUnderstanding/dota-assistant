"""对局节奏（第 88 节）重拉第 1 步：7.42 上线到现在（含 7.42a）的对局编号 + 开始时间，随机抽 N 局。

条件和 a_ids.py 一样（超凡～冠绝、天梯全英雄选择、≥15 分钟、10 人都在），但一直往后取到 explorer 没有新局为止，
并且留下 start_time，方便按「上线头两天 / 之后 / 7.42a 以后」分段比。
输出 data/research/role-play/rhythm_ids.json：[[match_id, start_time], ...]

    python -X utf8 docs/research/role-play/scripts/i_rhythm_ids.py [N]
"""
import json
import random
import sys
import time

import requests
from common import CUT_742, D

from dota_assistant.sources import http

N = int(sys.argv[1]) if len(sys.argv) > 1 else 30000
FIRST, SPAN = 9_095_900_000, 500_000     # 7.42 上线时 match_id 约 9 095 935 000


def sql(q: str) -> list[dict]:
    for _ in range(30):
        try:
            return http._sql(q)
        except (http.APIError, requests.RequestException) as e:
            print(f"  explorer 出错（{str(e)[:60]}），60 秒后重试", flush=True)
        time.sleep(60)
    raise RuntimeError("OpenDota 一直连不上")


def main() -> None:
    rows: list[list[int]] = []
    empty = 0
    k = 0
    while empty < 2:
        s = FIRST + k * SPAN
        r = sql(f"SELECT match_id, start_time FROM public_matches WHERE match_id > {s} AND match_id <= {s + SPAN} "
                f"AND start_time >= {CUT_742} AND game_mode = 22 AND lobby_type = 7 AND duration >= 900 "
                f"AND avg_rank_tier BETWEEN 70 AND 89 AND NOT (0 = ANY(radiant_team)) AND NOT (0 = ANY(dire_team))")
        rows += [[x["match_id"], x["start_time"]] for x in r]
        empty = empty + 1 if not r else 0
        last = time.strftime("%m-%d %H:%M", time.gmtime(max(x["start_time"] for x in r))) if r else "-"
        print(s, len(r), "累计", len(rows), "本段最晚", last, "UTC", flush=True)
        k += 1
    rng = random.Random(42)
    rng.shuffle(rows)
    out = sorted(rows[:N])
    (D / "rhythm_ids.json").write_text(json.dumps(out), encoding="utf-8")
    print("7.42 一共", len(rows), "抽", len(out))


if __name__ == "__main__":
    main()
