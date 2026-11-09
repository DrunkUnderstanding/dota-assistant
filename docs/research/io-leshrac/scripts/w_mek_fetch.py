"""艾欧 + 拉席克同队的局重新抓一遍全场 10 人的出装（2026-11-09，第 71 节）。
第 68 节那两批只存了目标英雄自己的出装，看不出队友谁出了梅肯斯姆，所以把两人同队的局再抓一次。
可续：已取过的编号记在 data/research/io-items/pair_full.jsonl。"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from dota_assistant.core import config  # noqa: E402
from dota_assistant.core.config import DATA_DIR  # noqa: E402
from dota_assistant.sources import http  # noqa: E402

IO, LESH = 91, 52
cfg = config.load()


def pair_ids(path):
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        m = json.loads(line)
        if m.get("missing"):
            continue
        side = {p["h"]: p["rad"] for p in m["players"]}
        if IO in side and LESH in side and side[IO] == side[LESH]:
            out.add(m["id"])
    return out


ids = sorted(pair_ids(DATA_DIR / "research" / "io-items" / "fetched.jsonl")
             | pair_ids(DATA_DIR / "research" / "hero-items-52" / "fetched.jsonl"))
OUT = DATA_DIR / "research" / "io-items" / "pair_full.jsonl"
done = set()
if OUT.exists():
    done = {json.loads(line)["id"] for line in OUT.read_text(encoding="utf-8").splitlines()}
todo = [i for i in ids if i not in done]
print(f"两人同队 {len(ids)} 局，已取 {len(done)}，还要 {len(todo)}", flush=True)

FIELDS = ("id didRadiantWin durationSeconds "
          "players { heroId isRadiant position isVictory kills deaths "
          "stats { networthPerMinute itemPurchases { time itemId } deathEvents { time attacker } "
          "heroDamageReport { dealtTotal { physicalDamage magicalDamage pureDamage } } } }")


def compact(m):
    ps = []
    for p in m["players"]:
        s = p.get("stats") or {}
        dmg = ((s.get("heroDamageReport") or {}).get("dealtTotal")) or {}
        ps.append({"h": p["heroId"], "rad": p["isRadiant"], "pos": p.get("position"), "win": p["isVictory"],
                   "nw": s.get("networthPerMinute"),
                   "items": [(x["time"], x["itemId"]) for x in s.get("itemPurchases") or []],
                   "parsed": s.get("itemPurchases") is not None,
                   "deaths": [x["time"] for x in s.get("deathEvents") or []],
                   "dmg": [dmg.get("physicalDamage"), dmg.get("magicalDamage"), dmg.get("pureDamage")]})
    return {"id": m["id"], "dur": m["durationSeconds"], "players": ps}


with OUT.open("a", encoding="utf-8") as f:
    for k in range(0, len(todo), 20):
        chunk = todo[k:k + 20]
        q = "{ " + " ".join(f"m{j}: match(id: {mid}) {{ {FIELDS} }}" for j, mid in enumerate(chunk)) + " }"
        for _ in range(40):
            try:
                res = http._stratz(cfg, q)
                break
            except Exception as e:  # noqa: BLE001
                print(f"出错 {str(e)[:100]}，120 秒后重试", flush=True)
                time.sleep(120)
        else:
            print("连续出错，停下", flush=True)
            break
        for j, mid in enumerate(chunk):
            m = res.get(f"m{j}")
            f.write(json.dumps(compact(m) if m else {"id": mid, "missing": True}) + "\n")
        f.flush()
        if (k // 20) % 25 == 0:
            print(k + len(chunk), "/", len(todo), flush=True)
print("done", flush=True)
