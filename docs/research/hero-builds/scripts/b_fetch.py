"""第 2 步：STRATZ 抓每局 10 个人的出装时间、位置、输赢（字段和 docs/research/item-vs 一样，直接复用那边的抓取函数）。
可中断、可续：结果追加到 data/research/hero-builds/fetched.jsonl.gz。每小时留 150 次给助手自己用。

    python -X utf8 docs/research/hero-builds/scripts/b_fetch.py
"""
import gzip
import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from dota_assistant.core import config  # noqa: E402
from dota_assistant.core.config import DATA_DIR  # noqa: E402

spec = importlib.util.spec_from_file_location("iv_fetch", ROOT / "docs" / "research" / "item-vs" / "scripts" / "b_fetch.py")
iv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(iv)

D = DATA_DIR / "research" / "hero-builds"


def main() -> None:
    cfg = config.load()
    ids = json.loads((D / "matches.json").read_text(encoding="utf-8"))
    out = D / "fetched.jsonl.gz"
    done = iv.read_done(out)
    todo = [i for i in ids if i not in done]
    print(f"一共 {len(ids)}，已取 {len(done)}，还要 {len(todo)}", flush=True)
    t0 = time.time()
    for k in range(0, len(todo), iv.BATCH):
        chunk = todo[k:k + iv.BATCH]
        q = "{ " + " ".join(f"m{j}: match(id: {mid}) {{ {iv.FIELDS} }}" for j, mid in enumerate(chunk)) + " }"
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
        with gzip.open(out, "at", encoding="utf-8") as f:
            for j, mid in enumerate(chunk):
                m = data.get(f"m{j}")
                f.write(json.dumps(iv.compact(m) if m else {"id": mid, "missing": True}) + "\n")
        n = k + len(chunk)
        if (k // iv.BATCH) % 10 == 0:
            print(f"{n}/{len(todo)}  {(time.time() - t0) / 60:.1f} 分钟  本小时还剩 {left} 次", flush=True)
        if left < iv.RESERVE:
            wait = 3600 - time.time() % 3600 + 30
            print(f"本小时只剩 {left} 次，等 {wait / 60:.0f} 分钟", flush=True)
            time.sleep(wait)
    print("done", flush=True)


if __name__ == "__main__":
    main()
