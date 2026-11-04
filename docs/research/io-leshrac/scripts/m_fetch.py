import json, sys
sys.path.insert(0, r"D:\xqs\dota-assistant")
from concurrent.futures import ThreadPoolExecutor
from dota_assistant.core import config
from dota_assistant.sources import http
cfg = config.load()
M = json.load(open("matches_full.json", encoding="utf-8"))
ids = []
for m in M:
    io = next(p for p in m["players"] if p["heroId"] == 91); le = next(p for p in m["players"] if p["heroId"] == 52)
    if io["position"] == "POSITION_5" and le["position"] == "POSITION_1" and le["stats"] and le["stats"]["itemPurchases"]:
        ids.append(m["id"])
q = """query($id: Long!) { match(id: $id) { id playbackData { roshanEvents { time hp } }
  players { heroId isRadiant position playbackData { playerUpdatePositionEvents { time x y } }
    stats { killEvents { time target positionX positionY isSmoke isGank isTpRecently isSolo }
            deathEvents { time positionX positionY attacker } assistEvents { time target positionX positionY } lastHitsPerMinute } } } }"""
def one(mid):
    try:
        m = http._stratz(cfg, q, id=mid)["match"]
    except Exception as e:
        print("err", mid, str(e)[:80], flush=True); return None
    for p in m["players"]:
        ev = (p.pop("playbackData") or {}).get("playerUpdatePositionEvents") or []
        # 每 15 秒一个点：取该时刻之前最后一个位置
        pts, j, last = [], 0, None
        for t in range(-90, 6000, 15):
            while j < len(ev) and ev[j]["time"] <= t: last = (ev[j]["x"], ev[j]["y"]); j += 1
            if j >= len(ev) and ev and t > ev[-1]["time"] + 30: break
            pts.append(last)
        p["pos15"] = pts
    rs = (m.get("playbackData") or {}).get("roshanEvents") or []
    m["rosh_deaths"] = [rs[i]["time"] for i in range(1, len(rs)) if rs[i]["hp"] is None and rs[i-1]["hp"] is not None]
    m.pop("playbackData", None)
    return m
out = []
with ThreadPoolExecutor(2) as ex:
    for i, m in enumerate(ex.map(one, ids)):
        if m: out.append(m)
        if i % 40 == 0: print(i, flush=True)
json.dump(out, open("pos.json", "w", encoding="utf-8"))
print("done", len(out))
q2 = "query { constants { npcs { id name } } }"
try:
    json.dump(http._stratz(cfg, q2), open("npcs.json", "w", encoding="utf-8"))
except Exception as e: print("npc err", e)
