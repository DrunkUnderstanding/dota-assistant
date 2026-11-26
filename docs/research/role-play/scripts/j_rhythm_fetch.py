"""对局节奏（第 88 节）重拉第 2 步：STRATZ 抓 rhythm_ids.json 里每局的节奏字段（比 b_fetch 轻很多：
时长、开始时间、胜负、每分钟经济差、推塔时间、每人位置 / 每分钟身价补刀 / 击杀助攻死亡时间）。
可中断、可续，追加到 data/research/role-play/r742.jsonl.gz；格式和 f742 兼容（h_rhythm 能直接读）。
每小时给助手自己留 iv.RESERVE 次。

    python -X utf8 docs/research/role-play/scripts/j_rhythm_fetch.py [每次局数]
"""
import gzip
import json
import sys
import time

from common import D, iv

from dota_assistant.core import config

BATCH = int(sys.argv[1]) if len(sys.argv) > 1 else 40
FIELDS = ("id durationSeconds startDateTime didRadiantWin radiantNetworthLeads towerDeaths { time isRadiant } "
          "players { heroId isRadiant position isVictory kills "
          "stats { networthPerMinute lastHitsPerMinute killEvents { time } deathEvents { time } assistEvents { time } } }")
OUT = D / "r742.jsonl.gz"


def compact(m: dict) -> dict:
    ps = []
    for p in m["players"]:
        s = p.get("stats") or {}
        ps.append({"h": p["heroId"], "rad": p["isRadiant"], "pos": p.get("position"), "win": p["isVictory"],
                   "k": p.get("kills"), "nw": s.get("networthPerMinute"), "lhm": s.get("lastHitsPerMinute"),
                   "kills": [[x["time"]] for x in s.get("killEvents") or []],
                   "deaths": [[x["time"]] for x in s.get("deathEvents") or []],
                   "assists": [[x["time"]] for x in s.get("assistEvents") or []]})
    return {"id": m["id"], "dur": m["durationSeconds"], "st": m.get("startDateTime"), "rw": m.get("didRadiantWin"),
            "lead": m.get("radiantNetworthLeads"),
            "towers": [[x["time"], None, x.get("isRadiant"), None] for x in m.get("towerDeaths") or []],
            "players": ps}


def main() -> None:
    cfg = config.load()
    ids = [i for i, _ in json.loads((D / "rhythm_ids.json").read_text(encoding="utf-8"))]
    done = iv.read_done(OUT)
    todo = [i for i in ids if i not in done]
    print(f"一共 {len(ids)}，已取 {len(done)}，还要 {len(todo)}，每次 {BATCH} 局", flush=True)
    t0, n = time.time(), 0
    while todo:
        chunk, todo = todo[:BATCH], todo[BATCH:]
        q = "{ " + " ".join(f"m{j}: match(id: {mid}) {{ {FIELDS} }}" for j, mid in enumerate(chunk)) + " }"
        for _ in range(60):
            try:
                data, left = iv.post(cfg, q)
                break
            except Exception as e:                       # noqa: BLE001  限速 / 断网：等一会儿再试
                print(f"出错 {str(e)[:80]}，120 秒后重试", flush=True)
                time.sleep(120)
        else:
            print("连续出错，停下（下次接着取）", flush=True)
            return
        with gzip.open(OUT, "at", encoding="utf-8") as f:
            for j, mid in enumerate(chunk):
                m = data.get(f"m{j}")
                f.write(json.dumps(compact(m) if m else {"id": mid, "missing": True}) + "\n")
        n += 1
        if n % 20 == 0:
            print(f"{n} 次  剩 {len(todo)}  {(time.time() - t0) / 60:.1f} 分钟  本小时还剩 {left} 次", flush=True)
        if left < iv.RESERVE:
            wait = 3600 - time.time() % 3600 + 30
            print(f"本小时只剩 {left} 次，等 {wait / 60:.0f} 分钟", flush=True)
            time.sleep(wait)
    print("done", flush=True)


if __name__ == "__main__":
    main()
