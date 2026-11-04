import json, sys
sys.path.insert(0, r"D:\xqs\dota-assistant")
from concurrent.futures import ThreadPoolExecutor
from dota_assistant.core import config
from dota_assistant.sources import http
cfg = config.load()
M = json.load(open("matches_full.json", encoding="utf-8"))
q = "query($id: Long!) { match(id: $id) { id bottomLaneOutcome midLaneOutcome topLaneOutcome } }"
def one(mid):
    try: return http._stratz(cfg, q, id=mid)["match"]
    except Exception as e: print("err", mid, str(e)[:60], flush=True)
with ThreadPoolExecutor(2) as ex:
    out = [m for m in ex.map(one, [m["id"] for m in M]) if m]
json.dump(out, open("lane.json", "w", encoding="utf-8")); print(len(out))
