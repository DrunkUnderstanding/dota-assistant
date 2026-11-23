"""第 2 步：STRATZ 抓每局详情（击杀 / 助攻 / 死亡时间、每分钟经济 / 补刀 / 经验、位置轨迹、符、屯野、
farm 来源、推塔、肉山、对线结果、出装）。可中断、可续：两个版本交替抓，追加到 data/research/role-play/f{742,741}.jsonl.gz。
每小时留 150 次给助手自己用。

    python -X utf8 docs/research/role-play/scripts/b_fetch.py
"""
import gzip
import json
import time

from common import D, FIELDS, compact, iv

from dota_assistant.core import config

BATCH = 20           # 25 局一次 STRATZ 回 400（查询太复杂），20 可以


def main() -> None:
    cfg = config.load()
    ids = json.loads((D / "ids.json").read_text(encoding="utf-8"))
    todo = {}
    for v, lst in ids.items():
        done = iv.read_done(D / f"f{v}.jsonl.gz")
        todo[v] = [i for i in lst if i not in done]
        print(f"{v}：一共 {len(lst)}，已取 {len(done)}，还要 {len(todo[v])}", flush=True)
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
            with gzip.open(D / f"f{v}.jsonl.gz", "at", encoding="utf-8") as f:
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
