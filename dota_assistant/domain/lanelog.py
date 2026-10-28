"""对线实战记录：live 在 3:00 / 6:00 / 10:00 把自己的 GSI 数据记进 data/lane_log.jsonl，赛后 lane-review 对照
（research/lanestate.py 的 `review`）。从 lanestate.py 拆出来：live 只依赖这部分，不用连带 import 回测代码。
"""
from __future__ import annotations

import json

from ..core.config import DATA_DIR

LOG_FILE = DATA_DIR / "lane_log.jsonl"
LOG_MINUTES = (3, 6, 10)     # 实战记录：两个选择时刻 + 对线快结束


# ---------- 实战记录：live 在 3:00 / 6:00 / 10:00 记一笔，赛后对照 ----------

def log_moment(record: dict) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
