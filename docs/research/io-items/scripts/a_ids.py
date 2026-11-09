"""第 1 步：OpenDota explorer 找 7.41f 以来超凡~冠绝天梯全英雄选择里有艾欧的对局（只要编号、双方阵容、胜负）。
可续：每块结果追加进 data/research/io-items/io_chunks.jsonl，最后合成 io_matches.json。"""
import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from dota_assistant.sources import http  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

IO = common.take_target(sys.argv)          # 研究的英雄（变量名沿用艾欧时的 IO）
OUT = common.data_dir(IO)
PATCH_START = 1789430400          # 7.41f（STRATZ gameVersions：2026-09-15 08:00）
SUB = 500_000
HI = 9_080_000_000 if len(sys.argv) < 2 else int(sys.argv[1])   # 比当前最新编号大一点即可，start_time 卡版本
LO = 8_999_200_000                # 7.41f 开始前后（更早的块按 start_time 全是 0 局）
CHUNKS = OUT / "io_chunks.jsonl"
done = {}
if CHUNKS.exists():
    for line in CHUNKS.read_text(encoding="utf-8").splitlines():
        c = json.loads(line)
        done[(c["lo"], c["hi"])] = c["rows"]


def sql(q: str) -> list[dict]:
    """服务器 5xx / 断连接：等着重试；SQL 自己超时（explorer err）：抛出去让调用方拆小"""
    for attempt in range(30):
        try:
            return http._sql(q)
        except http.APIError:
            raise
        except requests.RequestException as e:
            print(f"  网络 / 服务器问题（{str(e)[:60]}），60 秒后重试", flush=True)
            time.sleep(60)
    raise RuntimeError("OpenDota 一直连不上")


def run(lo: int, hi: int, f) -> None:
    if (lo, hi) in done:
        return
    q = (f"SELECT match_id, start_time, duration, radiant_win, avg_rank_tier, radiant_team, dire_team "
         f"FROM public_matches WHERE match_id > {lo} AND match_id <= {hi} AND start_time >= {PATCH_START} "
         f"AND game_mode = 22 AND lobby_type = 7 AND duration >= 900 AND avg_rank_tier BETWEEN 70 AND 89 "
         f"AND NOT (0 = ANY(radiant_team)) AND NOT (0 = ANY(dire_team)) "
         f"AND ({IO} = ANY(radiant_team) OR {IO} = ANY(dire_team))")
    try:
        r = sql(q)
    except http.APIError as e:
        if hi - lo <= 250_000:
            print("放弃", lo, hi, e, flush=True)
            return
        mid = (lo + hi) // 2
        run(lo, mid, f)
        run(mid, hi, f)
        return
    done[(lo, hi)] = r
    f.write(json.dumps({"lo": lo, "hi": hi, "rows": r}) + "\n")
    f.flush()
    print(lo, len(r), "累计", sum(len(v) for v in done.values()), flush=True)


with CHUNKS.open("a", encoding="utf-8") as f:
    # 大块必超时（4M 一块要拆三次才成），直接按 50 万一块；隔一块取一块，均匀覆盖整个版本、省一半时间
    for s in range(LO, HI, SUB * 2):
        run(s, min(s + SUB, HI), f)
rows = {r["match_id"]: r for v in done.values() for r in v}
(OUT / "io_matches.json").write_text(json.dumps(list(rows.values())), encoding="utf-8")
print("total", len(rows))
