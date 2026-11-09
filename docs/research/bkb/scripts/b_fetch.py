"""第 2 步：STRATZ 抓每局 10 个人的出装时间、死亡、每分钟经济、位置。可中断、可续（fetched.jsonl）。
STRATZ 免费 token 每小时 1500 次请求（一次 20 局），撞上限每 2 分钟重试，最多等约 80 分钟。"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from dota_assistant.core import config  # noqa: E402
from dota_assistant.core.config import DATA_DIR  # noqa: E402
from dota_assistant.sources import http  # noqa: E402

D = DATA_DIR / "research" / "bkb"
BATCH = 20
cfg = config.load()
ids = [r["match_id"] for r in json.loads((D / "matches.json").read_text(encoding="utf-8"))]
FETCHED = D / "fetched.jsonl"
done = set()
if FETCHED.exists():
    done = {json.loads(line)["id"] for line in FETCHED.read_text(encoding="utf-8").splitlines()}
todo = [i for i in ids if i not in done]
print(f"一共 {len(ids)}，已取 {len(done)}，还要 {len(todo)}", flush=True)

FIELDS = ("id durationSeconds players { heroId isRadiant position isVictory "
          "stats { networthPerMinute itemPurchases { time itemId } deathEvents { time attacker } } }")


def compact(m: dict) -> dict:
    ps = []
    for p in m["players"]:
        s = p.get("stats") or {}
        ps.append({"h": p["heroId"], "rad": p["isRadiant"], "pos": p.get("position"), "win": p["isVictory"],
                   "nw": s.get("networthPerMinute"), "parsed": s.get("itemPurchases") is not None,
                   "items": [(x["time"], x["itemId"]) for x in s.get("itemPurchases") or []],
                   "deaths": [(x["time"], x.get("attacker")) for x in s.get("deathEvents") or []]})
    return {"id": m["id"], "dur": m["durationSeconds"], "players": ps}


with FETCHED.open("a", encoding="utf-8") as f:
    t0 = time.time()
    for k in range(0, len(todo), BATCH):
        chunk = todo[k:k + BATCH]
        q = "{ " + " ".join(f"m{j}: match(id: {mid}) {{ {FIELDS} }}" for j, mid in enumerate(chunk)) + " }"
        for attempt in range(40):
            try:
                res = http._stratz(cfg, q)
                break
            except Exception as e:
                print(f"出错 {str(e)[:80]}，120 秒后重试", flush=True)
                time.sleep(120)
        else:
            print("连续出错，停下（下次接着取）", flush=True)
            break
        for j, mid in enumerate(chunk):
            m = res.get(f"m{j}")
            f.write(json.dumps(compact(m) if m else {"id": mid, "missing": True}) + "\n")
        f.flush()
        n = k + len(chunk)
        if (k // BATCH) % 25 == 0:
            el = time.time() - t0
            print(f"{n}/{len(todo)}  {el / 60:.1f} 分钟", flush=True)
print("done", flush=True)
