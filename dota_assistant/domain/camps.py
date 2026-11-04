"""野点标记：野区提醒到点时，在游戏画面里（web/world.html，镜头位置见 vision/camera.py）或小地图上（web/minimap.html）
对应的野点画个圈、写上几秒打。

和 jungle.py 的野区提醒是同一套「谁提醒什么」（Plan.kinds，点 × 关掉的种类也一起不画），这里只管「在哪」：
- 屯野：只圈自己英雄附近的野点（GSI 的 hero.xpos / ypos，玩家自己的位置本来就看得到），最近的那个写几秒到几秒
- 拉野：五号位圈自家优势路的小野、旁边的大野；三 / 四号位圈劣势路边上对面野区的大野（pull_plan），各写各的时间段
- 封野：四号位圈对面优势路的小野（去封），五号位圈自家那个（看有没有被封）
圈比提醒卡片早亮（MAP_LEAD），留出走过去的时间。

几秒屯 / 拉：每个野点按游戏文件 scripts/creep_pull_timings.txt（pak01_dir.vpk 里，Dota 自己按住 Alt 时野点上
「堆野 / 拉野」那两个圈用的就是它；2026-11-03 在游戏里对过：天辉下路小野 Alt 显示「堆野 :55、拉野 :19」，文件里 55~57、19~21）。
屯 / 拉都是一段时间（stack_start~stack_end、pull_1_start~pull_1_end）。文件里拉野每分钟有两次（:19 和 :49 这种，
对应每 30 秒一波兵），两次都提醒（玩家 2026-11-03 要的）。哪个阵营能拉哪个野点也是文件里写的（radiant_pull / dire_pull）：
天辉能拉 good_1、good_2（自家优势路）和 evil_1（劣势路边上夜魇的大野），夜魇能拉 evil_2、evil_1 和 good_2。
读不到这个文件时退回 config 的 stack_second / pull_second：大野早 1 秒、远古早 2 秒（TeamSmurf 7.41d），只拉自家小野。

野点坐标：本机游戏文件 game/dota/maps/dota.vpk 里 maps/dota/entities/default_ents.vents_c 的 npc_dota_neutral_spawner
（2026-11-03 用 tools/extract_camps.py 读的，当时版本 7.41f）；小地图范围是同一文件里两个 dota_minimap_boundary（±9472）。
拿玩家 2026-11-02 的实战截图对过：28 个野点里 21 个和小地图上的野点图标对上，偏差中位数约 1 像素（1080 高）。
哪个是「拉野点」是按地图位置认的（优势路一塔外、离兵线最近的小野 + 旁边的大野），游戏文件里没有直接写；改版挪了野点要重新读。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..kb.cd_ops import parse_kv
from . import jungle

log = logging.getLogger(__name__)

BOUND = 9472                 # 小地图 = 世界坐标 x / y 都在 ±BOUND 的正方形
SMALL, MEDIUM, LARGE, ANCIENT = range(4)
TYPE_NAMES = ("小野", "中野", "大野", "远古")
STACK_EARLY = (0, 0, 1, 2)   # 各种野点比 stack_second 早几秒打

# (游戏文件里的名字, 种类, x, y, 刷新框 (x 最小, y 最小, x 最大, y 最大))；good_ 在天辉野区，evil_ 在夜魇野区。
# x / y 是刷野点（npc_dota_neutral_spawner）；刷新框 = 按住 Alt 显示的黄框（触发器原点 + 模型碰撞盒）
CAMPS = (
    ("good_1", SMALL, 3978, -5027, (3647, -5438, 4443, -4648)),
    ("good_2", LARGE, 4649, -3699, (4379, -4313, 5122, -3495)),
    ("good_4", MEDIUM, 186, -5197, (-251, -5532, 636, -4776)),
    ("good_5", LARGE, -1454, -3356, (-2036, -3881, -988, -3034)),
    ("good_7", MEDIUM, -4013, 992, (-4368, 535, -3696, 1255)),
    ("good_8", ANCIENT, -5015, -96, (-5349, -592, -4245, 385)),
    ("good_9", MEDIUM, -1983, -4815, (-2304, -5088, -1619, -4393)),
    ("good_11", MEDIUM, -720, -7696, (-1068, -8019, -469, -7350)),
    ("good_12", MEDIUM, -2415, -8402, (-2856, -8768, -2104, -8032)),
    ("good_13", LARGE, -8313, -553, (-8645, -815, -7877, -47)),
    ("good_14", SMALL, 2768, -8336, (2344, -8714, 2999, -8027)),
    ("good_15", MEDIUM, 1922, -3975, (1458, -4448, 2206, -3618)),
    ("good_16", SMALL, -8023, -1838, (-8357, -2173, -7608, -1392)),
    ("good_20", MEDIUM, 4416, -8432, (4072, -8634, 4727, -7947)),
    ("evil_1", LARGE, -4824, 3915, (-5212, 3584, -4496, 4398)),
    ("evil_2", SMALL, -3911, 4829, (-4260, 4550, -3620, 5341)),
    ("evil_4", MEDIUM, 1224, 4176, (783, 3772, 1521, 4420)),
    ("evil_5", LARGE, 1064, 2580, (637, 2142, 1539, 2931)),
    ("evil_6", MEDIUM, -852, 4940, (-1216, 4520, -316, 5304)),
    ("evil_7", LARGE, 7928, -120, (7617, -409, 8257, 295)),
    ("evil_8", ANCIENT, 4352, 48, (3528, -402, 4670, 528)),
    ("evil_9", MEDIUM, 3392, -1408, (3008, -1536, 3776, -896)),
    ("evil_11", SMALL, -2880, 7376, (-3319, 7060, -2609, 7764)),
    ("evil_12", MEDIUM, 2016, 7896, (1629, 7524, 2531, 8268)),
    ("evil_13", SMALL, 8430, 1263, (8192, 896, 8704, 1536)),
    ("evil_14", MEDIUM, 336, 7696, (-40, 7358, 654, 8062)),
    ("evil_15", MEDIUM, -2596, 3850, (-2923, 3543, -2213, 4171)),
    ("evil_20", MEDIUM, -4208, 8336, (-4595, 7904, -3885, 8608)),
)
_BY_NAME = {c[0]: c for c in CAMPS}
# 优势路拉野点：(小野, 旁边的大野)。天辉优势路是下路，夜魇是上路
PULL = {"radiant": ("good_1", "good_2"), "dire": ("evil_2", "evil_1")}

TIMINGS_FILE = "scripts/creep_pull_timings.txt"
MAP_LEAD = 20                # 屯野 / 拉野：小地图比到点早这么多秒亮（卡片是 jungle.LEAD 秒），留出走过去的时间
NEAR = 2000                  # 屯野只圈离英雄这么近的野点（世界单位；小地图上约 30 像素）
NEAR_MAX = 3                 # 最多圈几个


@dataclass(frozen=True)
class Timing:
    """一个野点几秒屯、几秒拉（游戏文件 creep_pull_timings.txt 的 stack_start~stack_end / pull_1_start~pull_1_end）"""
    stack: tuple[int, int] | None = None
    pulls: dict = field(default_factory=dict)      # 阵营 → 每分钟几次拉野的 ((起, 止), …)（文件里有的阵营才有）


def parse_timings(text: str) -> dict[str, Timing]:
    """creep_pull_timings.txt → {野点名（good_1 这种）: Timing}"""
    def num(v):
        return int(v) if isinstance(v, str) and v.strip().isdigit() else None
    def span(d: dict, a: str, b: str):
        start = num(d.get(a))
        return None if start is None else (start, max(start, num(d.get(b)) or start))
    root = parse_kv(text).get("CREEP_PULL_TIMINGS") or {}
    out = {}
    for name, d in root.items():
        if not (isinstance(d, dict) and name.startswith("neutralcamp_")):
            continue
        pulls = {}
        for team in ("radiant", "dire"):
            t = d.get(f"{team}_pull")
            if isinstance(t, dict) and (ws := tuple(w for k in (1, 2) if (w := span(t, f"pull_{k}_start", f"pull_{k}_end")))):
                pulls[team] = ws
        out[name.removeprefix("neutralcamp_")] = Timing(span(d, "stack_start", "stack_end"), pulls)
    return out


def load_timings(dota_dir: Path | None) -> dict[str, Timing]:
    """从本机 Dota 资源包读；读不到（没装 / 改版换了格式）返回空表，用 config 的秒数"""
    if not dota_dir:
        return {}
    try:
        from ..core.vpk import VPK
        vpk = VPK(Path(dota_dir) / "game" / "dota" / "pak01_dir.vpk")
        out = parse_timings(vpk.read(TIMINGS_FILE).decode("utf-8", "replace"))
    except (OSError, KeyError, ValueError):
        log.warning("读不到游戏文件里的屯野 / 拉野秒数（%s），用 config 的 stack_second / pull_second", TIMINGS_FILE, exc_info=True)
        return {}
    log.info("屯野 / 拉野秒数：游戏文件里 %d 个野点", len(out))
    return out


def pull_plan(timings: dict[str, Timing], team: str | None, pos: int) -> tuple[jungle.Pull, ...] | None:
    """这个位置这局该拉哪几次野（给 jungle.make）：五号位自家优势路小野 + 大野，三 / 四号位劣势路边上对面野区的大野；
    时间段按游戏文件里这个阵营的。没有游戏文件 / 不知道阵营是 None（jungle 退回 config 的秒数）"""
    if not timings or team not in PULL:
        return None
    enemy = "dire" if team == "radiant" else "radiant"
    want = []
    if pos == 5:
        want += [(PULL[team][0], "small"), (PULL[team][1], "big")]
    if pos in jungle.OFFLANE_PULLERS:
        want.append((PULL[enemy][1], "off"))
    out = []
    for name, role in want:
        for w in (timings.get(name) or Timing()).pulls.get(team, ()):
            out.append(jungle.Pull(name, role, *w))
    return tuple(out)


def uv(x: float, y: float) -> tuple[float, float]:
    """世界坐标 → 小地图上的位置（0~1，左上角是 0,0；y 朝上所以要翻过来）"""
    return round((x + BOUND) / (2 * BOUND), 4), round((BOUND - y) / (2 * BOUND), 4)


def _mark(kind: str, x: float, y: float, label: str, at: int | None, main: bool = True, box=None,
          end: int | None = None) -> dict:
    """at / end：该动手的时间段（游戏时钟秒；end 没有就是到点那一下）"""
    u, v = uv(x, y)
    return {"kind": kind, "x": x, "y": y, "u": u, "v": v, "label": label, "at": at, "end": at if end is None else end,
            "main": main, "box": list(box) if box else None}


def _camp_mark(kind: str, name: str, label: str, at: int | None, main: bool = True, end: int | None = None) -> dict:
    _, _, x, y, box = _BY_NAME[name]
    return _mark(kind, x, y, label, at, main, box, end)


def _window(clock: float, start: int, end: int, until: int, linger: int) -> int | None:
    """clock 在哪一分钟的「start 秒前 MAP_LEAD 秒 ~ end 秒过后 linger 秒」里：返回那一分钟（1 ~ until-1），不在就是 None"""
    m = int((clock + MAP_LEAD - start) // 60)
    if 1 <= m < until and m * 60 + start - MAP_LEAD <= clock <= m * 60 + end + linger:
        return m
    return None


def _span(a: int, b: int) -> str:
    return f":{a:02d}" if a == b else f":{a:02d}~:{b:02d}"


def _dist2(c, hero) -> float:
    return (c[2] - hero[0]) ** 2 + (c[3] - hero[1]) ** 2


def marks(plan: jungle.Plan | None, team: str | None, clock: float, hero: tuple[float, float] | None = None,
          muted=(), timings: dict[str, Timing] | None = None) -> list[dict]:
    """这一刻小地图上画哪些圈。team 是 GSI 的 radiant / dire（不知道就不画拉 / 封）；hero 是自己英雄的世界坐标。
    每个圈：x / y（世界坐标）、box（刷新框）、u / v（小地图上的位置）、kind（stack / pull / block）、label、at（该动手的游戏时钟秒，页面自己倒数）、
    main（False = 次要的，画淡一点）。拉野按 plan.pulls；timings 是 load_timings 读的每个野点的屯野时间，没有就按 plan 里的秒数"""
    if plan is None:
        return []
    kinds = plan.kinds - set(muted)
    out: list[dict] = []
    enemy = {"radiant": "dire", "dire": "radiant"}.get(team or "")
    if "block" in kinds and enemy and jungle.BLOCK_FROM <= clock < jungle.FIRST_SPAWN:
        name = PULL[enemy][0] if plan.pos == 4 else PULL[team][0]
        out.append(_camp_mark("block", name, "封野" if plan.pos == 4 else "被封没", jungle.FIRST_SPAWN))
    timings = timings or {}
    if "pull" in kinds and enemy:
        words = {"small": "拉", "big": "拉大野", "off": "劣势路拉大野"}
        for p in plan.pulls:
            name = p.camp or PULL[team][0]           # 没有游戏文件时的那一次是自家小野
            if name in _BY_NAME and (m := _window(clock, p.start, p.end, jungle.PULL_UNTIL, jungle.LINGER)):
                out.append(_camp_mark("pull", name, f"{words[p.role]} {_span(p.start, p.end)}", m * 60 + p.start,
                                      end=m * 60 + p.end))
    if "stack" in kinds and hero:
        near = sorted((c for c in CAMPS if _dist2(c, hero) <= NEAR ** 2), key=lambda c: _dist2(c, hero))[:NEAR_MAX]
        for i, (name, typ, *_) in enumerate(near):
            w = (timings.get(name) or Timing()).stack
            if w is None:
                sec = plan.stack_second - STACK_EARLY[typ]
                w = (sec, sec + 2)
            if m := _window(clock, w[0], w[1], jungle.STACK_UNTIL, jungle.LINGER):
                out.append(_camp_mark("stack", name, f"{TYPE_NAMES[typ]} {_span(*w)}", m * 60 + w[0], main=i == 0,
                                      end=m * 60 + w[1]))
    # 同一个野点同时有几样（拉野点 :49 拉、:55 屯）：先到的排第 0 行，牌子一行一行往上叠，免得叠在一起看不见（2026-11-03 实测）
    seen: dict = {}
    for mk in sorted(out, key=lambda mk: (mk["at"] is None, mk["at"] or 0)):
        spot = (mk["x"], mk["y"])
        mk["row"] = seen.get(spot, 0)
        seen[spot] = mk["row"] + 1
    return out
