import json, sys
sys.path.insert(0, r"D:\xqs\dota-assistant")
from concurrent.futures import ThreadPoolExecutor
from dota_assistant.core import config
from dota_assistant.sources import http
cfg = config.load()
ids = [m["id"] for m in json.load(open("pos.json", encoding="utf-8"))]
q = "query($id: Long!) { match(id: $id) { id players { heroId isRadiant stats { towerDamageReport { npcId damage damageCreeps damageFromAbility } towerDamagePerMinute } } } }"
def one(mid):
    try: return http._stratz(cfg, q, id=mid)["match"]
    except Exception as e: print("err", mid, str(e)[:60], flush=True)
with ThreadPoolExecutor(2) as ex:
    out = [m for m in ex.map(one, ids) if m]
json.dump(out, open("towerdmg.json", "w", encoding="utf-8")); print(len(out))
print(json.dumps(out[0]["players"][0])[:600])
