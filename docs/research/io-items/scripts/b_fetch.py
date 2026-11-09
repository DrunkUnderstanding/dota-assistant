"""第 2 步：按「对面阵容对艾欧的克制合计」分组抽样，从 STRATZ 取艾欧的出装时间、死亡记录、全场每分钟经济。
可中断、可续：已取过的编号记在 fetched.jsonl。
用法：python b_fetch.py [每组场数，默认 20000]"""
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from dota_assistant.core import config  # noqa: E402
from dota_assistant.domain.engine import Engine  # noqa: E402
from dota_assistant.sources import assets, http  # noqa: E402
from dota_assistant.sources import stats as sm  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

IO = common.take_target(sys.argv)          # 研究的英雄（变量名沿用艾欧时的 IO）
D = common.data_dir(IO)
BATCH = 20
PER_GROUP = int(sys.argv[1]) if len(sys.argv) > 1 else 20000

cfg = config.load()
eng = Engine(assets.load_heroes(), sm.load_stats(cfg), sm.load_positions(cfg))
rows = json.loads((D / "io_matches.json").read_text(encoding="utf-8"))


def hard_score(r: dict) -> float:
    """对面 5 人对艾欧的克制合计（logit，越负越难打）"""
    enemies = r["dire_team"] if IO in r["radiant_team"] else r["radiant_team"]
    return sum(eng.advantage(IO, e) for e in enemies)


for r in rows:
    r["score"] = hard_score(r)
rows.sort(key=lambda r: r["score"])
cut = rows[len(rows) // 3]["score"]          # 最难打的 1/3 为「难打组」
(D / "groups.json").write_text(json.dumps({"cut": cut, "n": len(rows),
                                           "score": {r["match_id"]: r["score"] for r in rows}}), encoding="utf-8")
hard = [r["match_id"] for r in rows if r["score"] < cut]
rest = [r["match_id"] for r in rows if r["score"] >= cut]
rng = random.Random(7)
ids = rng.sample(hard, min(PER_GROUP, len(hard))) + rng.sample(rest, min(PER_GROUP, len(rest)))
rng.shuffle(ids)
print(f"艾欧对局 {len(rows)}，难打线 {cut:.3f}，难打 {len(hard)} / 其余 {len(rest)}，本次取 {len(ids)}", flush=True)

FETCHED = D / "fetched.jsonl"
done = set()
if FETCHED.exists():
    for line in FETCHED.read_text(encoding="utf-8").splitlines():
        done.add(json.loads(line)["id"])
todo = [i for i in ids if i not in done]
print(f"已取 {len(done)}，还要 {len(todo)}", flush=True)

FIELDS = ("id didRadiantWin durationSeconds gameVersionId "
          "players { heroId isRadiant position isVictory "
          "stats { networthPerMinute itemPurchases { time itemId } deathEvents { time attacker byAbility byItem } } }")


def compact(m: dict) -> dict:
    """只留艾欧的出装 / 死亡，其余人只要英雄、阵营、位置、经济"""
    ps = []
    for p in m["players"]:
        s = p.get("stats") or {}
        q = {"h": p["heroId"], "rad": p["isRadiant"], "pos": p.get("position"), "nw": s.get("networthPerMinute")}
        if p["heroId"] == IO:
            q["items"] = [(x["time"], x["itemId"]) for x in s.get("itemPurchases") or []]
            q["parsed"] = s.get("itemPurchases") is not None
            q["deaths"] = [(x["time"], x.get("attacker"), x.get("byAbility"), x.get("byItem")) for x in s.get("deathEvents") or []]
            q["win"] = p["isVictory"]
        ps.append(q)
    return {"id": m["id"], "dur": m["durationSeconds"], "ver": m.get("gameVersionId"), "players": ps}


with FETCHED.open("a", encoding="utf-8") as f:
    t0 = time.time()
    for k in range(0, len(todo), BATCH):
        chunk = todo[k:k + BATCH]
        q = "{ " + " ".join(f"m{j}: match(id: {mid}) {{ {FIELDS} }}" for j, mid in enumerate(chunk)) + " }"
        # STRATZ 免费 token：每小时 1500 次、每天 15000 次（429 头里 x-ratelimit-*，2026-11-06 实查），
        # 一次 20 局 → 一小时最多约 3 万局；撞上每小时上限就每 2 分钟试一次，最多等约 80 分钟
        for attempt in range(40):
            try:
                res = http._stratz(cfg, q)
                break
            except Exception as e:
                wait = 120
                print(f"出错 {str(e)[:80]}，{wait} 秒后重试", flush=True)
                time.sleep(wait)
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
            print(f"{n}/{len(todo)}  {el / 60:.1f} 分钟，预计还要 {el / n * (len(todo) - n) / 60:.0f} 分钟", flush=True)
print("done", flush=True)
