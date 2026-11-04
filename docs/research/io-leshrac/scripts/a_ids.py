import json, sys
sys.path.insert(0, r"D:\xqs\dota-assistant")
from dota_assistant.sources import http
lo, hi = 9044932453, 9066632453
rows = []
step = 1_000_000
for s in range(lo, hi, step):
    e = min(s + step, hi)
    q = (f"SELECT match_id, start_time, duration, radiant_win, avg_rank_tier, "
         f"(91 = ANY(radiant_team)) io_rad FROM public_matches "
         f"WHERE match_id > {s} AND match_id <= {e} AND game_mode = 22 AND lobby_type = 7 AND duration >= 900 "
         f"AND avg_rank_tier BETWEEN 70 AND 89 AND "
         f"((91 = ANY(radiant_team) AND 52 = ANY(radiant_team)) OR (91 = ANY(dire_team) AND 52 = ANY(dire_team)))")
    try:
        r = http._sql(q)
    except Exception as ex:
        print("err", s, ex); continue
    rows += r
    print(s, len(r), flush=True)
json.dump(rows, open("pair_matches.json", "w", encoding="utf-8"))
print("total", len(rows))
