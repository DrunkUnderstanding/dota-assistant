"""第 2 步：STRATZ 抓每局 10 个人的出装时间、死亡、每分钟经济、位置（字段和黑皇杖研究一样，两边数据能合着用）。
可中断、可续：结果追加到 data/research/item-vs/fetched.jsonl.gz（多段 gzip，断了也能读到断点前）。

STRATZ 免费 token：每小时 1500 次、每天 15000 次。一次请求放 BATCH 局（2026-11-16 试过 60 局一次也行）；
每小时留 RESERVE 次给助手自己用（出装基准、个人数据），剩余次数低于它就等到下个整点。

    python docs/research/item-vs/scripts/b_fetch.py
"""
import gzip
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from dota_assistant.core import config  # noqa: E402
from dota_assistant.core.config import DATA_DIR  # noqa: E402
from dota_assistant.sources import http  # noqa: E402

D = DATA_DIR / "research" / "item-vs"
BATCH = 50
RESERVE = 150
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


def read_done(path: Path) -> set[int]:
    done: set[int] = set()
    if not path.exists():
        return done
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                done.add(json.loads(line)["id"])
    except (EOFError, OSError, json.JSONDecodeError):    # 上次中途被杀，最后一段没写完：读到的都算
        pass
    return done


def post(cfg, query: str) -> tuple[dict, int]:
    """返回 (data, 这个小时还剩几次)"""
    r = http._session.post(http.STRATZ, json={"query": query}, timeout=120,
                           headers={"Authorization": f"Bearer {cfg.stratz_token}", "User-Agent": "STRATZ_API"})
    if r.status_code == 429:
        raise http.APIError("429 限速")
    r.raise_for_status()
    res = r.json()
    if res.get("errors") and not res.get("data"):
        raise http.APIError(str(res["errors"][0].get("message"))[:80])
    return res.get("data") or {}, int(r.headers.get("x-ratelimit-remaining-hour", RESERVE + 1))


def main() -> None:
    cfg = config.load()
    ids = [r["match_id"] for r in json.loads((D / "matches.json").read_text(encoding="utf-8"))]
    out = D / "fetched.jsonl.gz"
    done = read_done(out)
    todo = [i for i in ids if i not in done]
    print(f"一共 {len(ids)}，已取 {len(done)}，还要 {len(todo)}", flush=True)
    t0 = time.time()
    for k in range(0, len(todo), BATCH):
        chunk = todo[k:k + BATCH]
        q = "{ " + " ".join(f"m{j}: match(id: {mid}) {{ {FIELDS} }}" for j, mid in enumerate(chunk)) + " }"
        for _ in range(60):
            try:
                data, left = post(cfg, q)
                break
            except Exception as e:                       # noqa: BLE001  限速 / 断网 / 日限额：等一会儿再试
                print(f"出错 {str(e)[:80]}，120 秒后重试", flush=True)
                time.sleep(120)
        else:
            print("连续出错，停下（下次接着取）", flush=True)
            return
        with gzip.open(out, "at", encoding="utf-8") as f:   # 每批单独一段 gzip，被杀也只丢这一批
            for j, mid in enumerate(chunk):
                m = data.get(f"m{j}")
                f.write(json.dumps(compact(m) if m else {"id": mid, "missing": True}) + "\n")
        n = k + len(chunk)
        if (k // BATCH) % 20 == 0:
            print(f"{n}/{len(todo)}  {(time.time() - t0) / 60:.1f} 分钟  本小时还剩 {left} 次", flush=True)
        if left < RESERVE:
            wait = 3600 - time.time() % 3600 + 30
            print(f"本小时只剩 {left} 次，留给助手自己用，等 {wait / 60:.0f} 分钟", flush=True)
            time.sleep(wait)
    print("done", flush=True)


if __name__ == "__main__":
    main()
