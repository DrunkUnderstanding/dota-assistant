import json, sys, random, time
sys.path.insert(0, r"D:\xqs\dota-assistant")
from dota_assistant.core import config
from dota_assistant.sources import http
cfg = config.load()
rows = json.load(open("pair_matches.json", encoding="utf-8"))
random.seed(1)
ids = [r["match_id"] for r in random.sample(rows, min(300, len(rows)))]
q = """query($id: Long!) { match(id: $id) { id didRadiantWin durationSeconds
  towerDeaths { time npcId isRadiant }
  players { heroId isRadiant position lane role level kills deaths assists networth isVictory
    item0Id item1Id item2Id item3Id item4Id item5Id backpack0Id neutral0Id
    abilities { abilityId time level }
    stats { level itemPurchases { time itemId } killEvents { time target } deathEvents { time } assistEvents { time target } networthPerMinute campStack }
    playbackData { abilityUsedEvents { time abilityId target } }
  } } }"""
out = []
from concurrent.futures import ThreadPoolExecutor
def one(mid):
    try:
        return http._stratz(cfg, q, id=mid)["match"]
    except Exception as e:
        print("err", mid, str(e)[:80], flush=True)
with ThreadPoolExecutor(4) as ex:
    for i, m in enumerate(ex.map(one, ids)):
        if m: out.append(m)
        if i % 25 == 0: print(i, len(out), flush=True)
json.dump(out, open("matches_full.json", "w", encoding="utf-8"))
print("done", len(out))
