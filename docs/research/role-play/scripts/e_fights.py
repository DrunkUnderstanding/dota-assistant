"""第 5 步（玩家追问「多参团了怎么少死」）：补抓每局 10 人的击杀 / 助攻 / 死亡事件带坐标，死亡带 STRATZ 的死因标签
（是否走过对面眼、是否想 TP 走、是否被秒、死时是否在打架、是否有治疗没用……）。
只抓 c_an 能用的那些局；可中断、可续，追加到 data/research/role-play/e{742,741}.jsonl.gz。

    python -X utf8 docs/research/role-play/scripts/e_fights.py
"""
import gzip
import json
import time

from common import D, iv

from dota_assistant.core import config

BATCH = 35           # 40 局一次 STRATZ 回 400（查询太复杂）
FIELDS = ("id players { heroId isRadiant stats { "
          "deathEvents { time attacker positionX positionY isWardWalkThrough isAttemptTpOut isDieBack isBurst "
          "isEngagedOnDeath hasHealAvailable isTracked goldLost timeDead } "
          "killEvents { time target positionX positionY } assistEvents { time target positionX positionY } } }")
FLAGS = ("isWardWalkThrough", "isAttemptTpOut", "isDieBack", "isBurst", "isEngagedOnDeath", "hasHealAvailable", "isTracked")


def compact(m: dict) -> dict:
    ps = []
    for p in m["players"]:
        s = p.get("stats") or {}
        ps.append({"h": p["heroId"], "rad": p["isRadiant"],
                   "deaths": [[x["time"], x.get("attacker"), x.get("positionX"), x.get("positionY"),
                               "".join("1" if x.get(f) else "0" for f in FLAGS), x.get("goldLost"), x.get("timeDead")]
                              for x in s.get("deathEvents") or []],
                   "kills": [[x["time"], x.get("target"), x.get("positionX"), x.get("positionY")] for x in s.get("killEvents") or []],
                   "assists": [[x["time"], x.get("target"), x.get("positionX"), x.get("positionY")] for x in s.get("assistEvents") or []]})
    return {"id": m["id"], "players": ps}


def main() -> None:
    import c_an                                   # 只抓分析里用得上的局（去掉掉线等）
    cfg = config.load()
    todo = {}
    for v in ("742", "741"):
        ids = [m["id"] for m in c_an.load(v)]
        done = iv.read_done(D / f"e{v}.jsonl.gz")
        todo[v] = [i for i in ids if i not in done]
        print(f"{v}：要 {len(ids)}，已取 {len(done)}，还要 {len(todo[v])}", flush=True)
    t0, n = time.time(), 0
    while any(todo.values()):
        for v in ("742", "741"):
            chunk, todo[v] = todo[v][:BATCH], todo[v][BATCH:]
            if not chunk:
                continue
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
            with gzip.open(D / f"e{v}.jsonl.gz", "at", encoding="utf-8") as f:
                for j, mid in enumerate(chunk):
                    m = data.get(f"m{j}")
                    f.write(json.dumps(compact(m) if m else {"id": mid, "missing": True}) + "\n")
            n += 1
            if n % 20 == 0:
                print(f"{n} 次  剩 {len(todo['742'])} / {len(todo['741'])}  {(time.time() - t0) / 60:.1f} 分钟  "
                      f"本小时还剩 {left} 次", flush=True)
            if left < iv.RESERVE:
                wait = 3600 - time.time() % 3600 + 30
                print(f"本小时只剩 {left} 次，等 {wait / 60:.0f} 分钟", flush=True)
                time.sleep(wait)
    print("done", flush=True)


if __name__ == "__main__":
    main()
