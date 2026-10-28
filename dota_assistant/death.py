"""死亡复盘：死的时候说一下「刚才是怎么死的」。

只看 GSI 里自己英雄死前一小段的状态：血量怎么掉的、被什么状态控住、保命装备 / 技能用没用、死在哪、掉了多少钱。
全是规则，不用大模型（死亡读秒就几十秒，等不起）。阈值都是实现假设（玩家未确认），在文件头。
GSI 玩家视角不给「谁杀的、对面来了几个」，这里也不猜，只说自己这边看得出来的东西。

**字段名按 Valve GSI 的写法（hero.health / stunned / xpos…、items 的 cooldown / can_cast、abilities），
还没在真实对局的推送里核对过**；每次死亡会把死前那段推送原样存到 shots/deaths/，拿来核对和调阈值。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .core.config import ROOT

DUMP_DIR = ROOT / "shots" / "deaths"
WINDOW = 20                  # 往回看多少秒
REGEN_TOL = 3                # 血量回升不到最大血量的 3% 当作还在掉血（回复 / 吸血的小波动）
BURST = 3                    # 这么多秒内从半血以上到死，算「被瞬间打爆」
LOW_HP = 30                  # 低于这个百分比算残血
LOW_HP_LONG = 4              # 残血后又撑了 ≥ 这么多秒才死，提一句
LOW_START = 50               # 这波开始时血量低于这个百分比，提一句
CONTROL_SHARE = 0.5          # 硬控时间占这波 ≥ 一半，算「被控死」
LOW_MANA = 15
WAND_CHARGES = 5             # 魔杖 / 魔棒至少几层没按才提

FLAGS = {"stunned": "眩晕", "hexed": "妖术", "silenced": "沉默", "muted": "锁闭", "disarmed": "缴械", "break": "破坏"}
HARD = ("stunned", "hexed")              # 动不了也按不了
NO_ITEMS = ("stunned", "hexed", "muted")  # 用不了物品

# 能保命 / 跑路的主动装备（实现假设，手挑的）
SAVE_ITEMS = ("black_king_bar", "blink", "overwhelming_blink", "swift_blink", "arcane_blink", "force_staff",
              "hurricane_pike", "glimmer_cape", "ghost", "cyclone", "wind_waker", "ethereal_blade", "lotus_orb",
              "manta", "satanic", "silver_edge", "invis_sword", "shadow_amulet", "blade_mail", "disperser",
              "pavise", "solar_crest", "crimson_guard", "pipe", "mekansm", "guardian_greaves", "aeon_disk",
              "harpoon", "faerie_fire", "magic_wand", "magic_stick")
WANDS = ("magic_wand", "magic_stick")
# 官方原文：「受到来自敌方英雄或肉山的攻击时，闪烁匕首将在3秒内无法使用。」这 3 秒不算「用过 / 在冷却」
BLINKS = ("blink", "overwhelming_blink", "swift_blink", "arcane_blink")
BLINK_LOCK = 3
# 保命 / 跑路技能：技能名或说明正文里有这些说法（关键词是实现假设；全英雄跑过一遍，有少量误判）。大招另外都算
SAVE_RE = r"冲到|冲向|冲锋|传送至|传送到|闪烁|跳跃|冲刺|无敌|免疫伤害|获得.{0,6}(技能|减益)免疫|进入隐身|隐身状态|变为隐身|隐匿|强驱散"
SAVE_EXCLUDE = r"显形|敌[方人].{0,8}(冲|跳跃|传送)|间跳跃|跳跃.{0,4}(攻击|目标|敌)|隐身单位|隐身的|无法.{0,6}(闪烁|隐身|移动|传送)"
USABLE_SLOTS = tuple(f"slot{i}" for i in range(6)) + ("neutral0",)


@dataclass
class Recap:
    clock: float                                   # 死亡时的游戏时间（秒）
    deaths: int                                    # 本局第几次
    lines: list[tuple[str, str]] = field(default_factory=list)   # (warn / row / dim, 文字)
    # 下面是结构化结果，给「这种死法该出什么」（itembuild.death_pick）用
    cause: str = ""               # control 被控死 / burst 瞬间打爆 / low 残血拖太久 / chased 一直没脱离 / "" 其他
    secs: dict[str, float] = field(default_factory=dict)   # FLAGS 里每种状态持续了几秒
    start: float = 100            # 这波开始时的血量百分比
    immune: float = 0             # 开着技能免疫的秒数
    saves: dict[str, list[str]] = field(default_factory=dict)   # used / unused / blocked / nocast / hit_lock / cooling → 内部名
    owned: set[str] = field(default_factory=set)            # 死的时候身上的装备（去掉 item_）

    @property
    def title(self) -> str:
        t = int(self.clock)
        return f"死亡复盘 · {t // 60:02d}:{t % 60:02d} · 第 {self.deaths} 次"


def _hero(raw: dict) -> dict:
    return raw.get("hero") or {}


def _deaths(raw: dict) -> int:
    return int((raw.get("player") or {}).get("deaths") or 0)


def _hp(raw: dict) -> float | None:
    h = _hero(raw)
    if h.get("max_health"):
        return 100 * float(h.get("health") or 0) / float(h["max_health"])
    return float(h["health_percent"]) if h.get("health_percent") is not None else None


def _slots(raw: dict, prefix: str, usable=None) -> dict[str, dict]:
    """名字（去掉 item_ 前缀）→ 格子内容；同名的取第一个"""
    out = {}
    for slot, it in (raw.get(prefix) or {}).items():
        name = (it or {}).get("name") or ""
        if usable and slot not in usable or not name or name == "empty":
            continue
        out.setdefault(name.removeprefix("item_"), it)
    return out


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def region(x: float, y: float, team: str | None) -> str:
    """地图坐标 → 大概位置。天辉在左下、夜魇在右上；河道大致是 x + y = 0，中路大致是 x = y。
    分界线是按地图大小估的（实现假设），没在真实坐标上核对过。"""
    mine = "radiant" if team != "dire" else "dire"
    side = "radiant" if x + y < 0 else "dire"
    half = "自家" if side == mine else "对面"
    if x < -4000 and y < -3700 or x > 4000 and y > 3500:
        return f"{half}高地"
    if abs(x - y) < 1800:
        return f"中路（{half}半场）"
    if x < -4500 or y > 4500:
        return f"上路（{half}半场）"
    if y < -4500 or x > 4500:
        return f"下路（{half}半场）"
    return f"{half}野区"


def _charges(it: dict) -> float:
    return _num(it.get("charges", it.get("item_charges")))


def _join(xs) -> str:
    return "、".join(xs)


def analyze(history: list[tuple[float, dict]], team: str | None = None, names: dict[str, str] | None = None,
            save_abilities: set[str] | None = None) -> Recap | None:
    """history：[(收到时的时间戳, GSI 推送)]，按时间顺序，最后一条是刚死之后的。
    names：内部名 → 中文名（物品不带 item_ 前缀）；save_abilities：算「保命技能」的技能内部名（大招另外都算）。
    找不到「这次死亡」（历史里没有死前的推送）返回 None。"""
    names = names or {}
    if not history:
        return None
    final = _deaths(history[-1][1])
    d = next((i for i in range(1, len(history))
              if _deaths(history[i][1]) >= final > _deaths(history[i - 1][1])), None)
    if d is None:
        return None
    w_d, dead = history[d]
    rec = Recap(float((dead.get("map") or {}).get("clock_time") or 0), final)
    rec.owned = set(_slots(history[d - 1][1], "items"))
    before = [(w, r) for w, r in history[:d] if w >= w_d - WINDOW and _hero(r).get("alive", True)
              and _hp(r) is not None]
    if before:
        _fight(rec, before, w_d, team, names, save_abilities or set())
    _after(rec, history[d - 1][1], history[-1][1])
    return rec


def _fight(rec: Recap, before, w_d: float, team, names, save_abilities) -> None:
    hp = [_hp(r) for _, r in before]
    # 这波从哪开始：从死前往回找，直到血量明显回升过（说明那之前还在回血 / 没挨打）；取这段里最后一次血最多的时刻
    k = len(hp) - 1
    while k > 0 and hp[k - 1] >= hp[k] - REGEN_TOL:
        k -= 1
    peak = max(hp[k:])
    s = max(i for i in range(k, len(hp)) if hp[i] >= peak - 1)
    fight = before[s:]
    w0, start = fight[0][0], hp[s]
    dur = max(w_d - w0, 0.5)
    whole = s == 0 and w0 <= w_d - WINDOW + 1      # 往回看到头了还在掉血（推送每秒至少一次：游戏时间每秒都变）

    # 每种状态各持续了多久：两次推送之间按前一次的状态算（GSI 状态变了就推，间隔就是持续时间）
    spans = [(r, (fight[i + 1][0] if i + 1 < len(fight) else w_d) - w) for i, (w, r) in enumerate(fight)]
    secs = {f: sum(t for r, t in spans if _hero(r).get(f)) for f in FLAGS}
    hard = sum(t for r, t in spans if any(_hero(r).get(f) for f in HARD))
    low = next((w for (w, _), v in zip(fight, hp[s:]) if v < LOW_HP), None)
    by_time = [f for f, t in sorted(secs.items(), key=lambda x: -x[1]) if t >= 0.5]

    # 一句话结论
    span = f"{dur:.1f}" if dur < 10 else f"{dur:.0f}"
    controlled = hard >= 1 and hard >= CONTROL_SHARE * dur
    rec.secs, rec.start = secs, start
    rec.cause = ("control" if controlled else "burst" if dur <= BURST and start >= 50
                 else "low" if low is not None and w_d - low >= LOW_HP_LONG else "chased" if whole else "")
    if controlled:
        how = _join(FLAGS[f] for f in by_time if f in HARD) or "控"
        head = f"被控死：{span} 秒内从 {start:.0f}% 血到死，其中 {hard:.1f} 秒被{how}"
    elif dur <= BURST and start >= 50:
        head = f"被瞬间打爆：{span} 秒从 {start:.0f}% 血到死"
    elif low is not None and w_d - low >= LOW_HP_LONG:
        head = f"残血拖太久：{span} 秒内从 {start:.0f}% 血掉到死，{LOW_HP}% 以下还待了 {w_d - low:.0f} 秒"
    elif whole:
        head = f"持续掉血 {WINDOW} 秒以上，一直没脱离（{WINDOW} 秒前还有 {start:.0f}% 血）"
    else:
        head = f"{span} 秒内从 {start:.0f}% 血被打死"
    rec.lines.append(("warn", head))
    if start < LOW_START and not whole:
        rec.lines.append(("row", f"这波开始时只有 {start:.0f}% 血"))
    ctrl = [f"{FLAGS[f]} {secs[f]:.1f} 秒" for f in by_time if not (controlled and f in HARD)]
    if ctrl:
        rec.lines.append(("row", ("另外被" if controlled else "被") + "、".join(ctrl)))
    immune = rec.immune = sum(t for r, t in spans if _hero(r).get("magicimmune"))
    if immune >= 0.5:
        rec.lines.append(("row", f"开着技能免疫（{immune:.1f} 秒）也没顶住"))
    if _hero(fight[0][1]).get("smoked"):
        rec.lines.append(("row", "开着雾时被抓"))

    _saves(rec, fight, names, save_abilities)
    last = fight[-1][1]
    mana = _hero(last).get("mana_percent")
    if mana is not None and _num(mana) < LOW_MANA:
        rec.lines.append(("row", f"死前只剩 {_num(mana):.0f}% 蓝"))
    h = _hero(last)
    if "xpos" in h and "ypos" in h:
        rec.lines.append(("row", f"死在{region(_num(h['xpos']), _num(h['ypos']), team)}"))


def _saves(rec: Recap, fight, names, save_abilities) -> None:
    """保命装备 / 技能：用了的、好的没用的、被控着用不了的、在冷却的"""
    first, last = fight[0][1], fight[-1][1]
    used, unused, blocked, nocast, hit_lock, cooling = [], [], [], [], [], []
    keys: dict[int, list[str]] = {}        # 上面每个列表 → 对应的内部名（同顺序）

    def put(lst: list, label: str, name: str) -> None:
        lst.append(label)
        keys.setdefault(id(lst), []).append(name)

    def judge(name: str, kind: str, slots_of) -> None:
        a, z = slots_of(first).get(name), slots_of(last).get(name)
        if a is None or z is None:
            return
        lock = BLINK_LOCK if name in BLINKS else 0
        cd0 = _num(a.get("cooldown"))
        label = names.get(name, name)
        went = [slots_of(r).get(name) or {} for _, r in fight]
        if any(_num(x.get("cooldown")) > max(cd0, lock) for x in went) or \
                (name in WANDS and _charges(z) < _charges(a)):
            put(used, label, name)
        elif cd0 > lock:
            put(cooling, label, name)
        elif name in WANDS and _charges(z) < WAND_CHARGES:
            return
        else:
            blockers = NO_ITEMS if kind == "item" else HARD + ("silenced",)
            free = [x for x, (_, r) in zip(went, fight) if not any(_hero(r).get(f) for f in blockers)]
            label += f"（{int(_charges(z))} 层）" if name in WANDS else ""
            # 跳刀：这波第一条推送是挨打之前，那一下没跳不算失误，从挨打以后看
            ready = free[1:] if name in BLINKS and free and free[0] is went[0] else free
            if not free:
                put(blocked, label, name)
            elif any(x.get("can_cast", True) and _num(x.get("cooldown")) <= 0 for x in ready):
                put(unused, label, name)
            elif name in BLINKS:
                put(hit_lock, label, name)
            else:
                put(nocast, label, name)          # 没被控也按不了：多半是蓝不够

    for name in SAVE_ITEMS:
        judge(name, "item", lambda r: _slots(r, "items", USABLE_SLOTS))
    for name, ab in _slots(last, "abilities").items():
        if _num(ab.get("level")) >= 1 and not ab.get("passive") and (ab.get("ultimate") or name in save_abilities):
            judge(name, "ability", lambda r: _slots(r, "abilities"))
    rec.saves = {k: keys.get(id(v), []) for k, v in (("used", used), ("unused", unused), ("blocked", blocked),
                                                       ("nocast", nocast), ("hit_lock", hit_lock), ("cooling", cooling))}
    if unused:
        rec.lines.append(("warn", f"{_join(unused)} 当时是好的，没用上"))
    if blocked:
        rec.lines.append(("row", f"{_join(blocked)} 是好的，但一直被控着用不了"))
    if nocast:
        rec.lines.append(("row", f"{_join(nocast)} 不在冷却但放不出来（蓝不够？）"))
    if hit_lock:
        rec.lines.append(("row", f"{_join(hit_lock)} 一直在挨打，用不了（受到英雄攻击后 3 秒内不能用）"))
    if used:
        rec.lines.append(("row", f"用了{_join(used)}，还是没活下来"))
    if cooling:
        rec.lines.append(("dim", f"{_join(cooling)} 当时在冷却"))


def _after(rec: Recap, alive: dict, now: dict) -> None:
    """死后：复活时间、掉的钱、买活"""
    h, p = _hero(now), now.get("player") or {}
    bits = []
    if h.get("respawn_seconds"):
        bits.append(f"复活 {int(_num(h['respawn_seconds']))} 秒")
    g0, g1 = (alive.get("player") or {}).get("gold"), p.get("gold")
    if g0 is not None and g1 is not None and _num(g0) - _num(g1) > 0:
        bits.append(f"掉了 {int(_num(g0) - _num(g1))} 金")
    if h.get("buyback_cost") is not None and g1 is not None:
        cost, gold = int(_num(h["buyback_cost"])), int(_num(g1))
        from .core.gsi_fields import buyback_left
        if buyback_left(now) > 0:
            bits.append("买活冷却中")
        else:
            bits.append(f"买活 {cost} 金（{'够' if gold >= cost else f'还差 {cost - gold}'}）")
    if bits:
        rec.lines.append(("dim", " · ".join(bits)))


def names_from_kb(base, hero_key: str | None) -> tuple[dict[str, str], set[str]]:
    """知识库 → (内部名 → 中文名, 自己英雄的保命技能)"""
    names = {k: e.name for k, e in base.items.items()}
    saves = set()
    me = next((h for h in base.heroes.values() if h.key == hero_key), None)
    for ab in me.abilities if me else []:
        names[ab.key] = ab.name
        text = f"{ab.name}。{ab.text}"
        if re.search(SAVE_RE, text) and not re.search(SAVE_EXCLUDE, text):
            saves.add(ab.key)
    return names, saves


def dump(history: list[tuple[float, dict]], rec: Recap | None) -> Path:
    """死前那段推送原样存下来（核对字段名、调阈值用），最多留最近 50 份（同名的死亡截图一起删）。返回存的文件"""
    DUMP_DIR.mkdir(parents=True, exist_ok=True)
    out = {"recap": rec.lines if rec else None,
           "pushes": [{"t": round(w, 2), **r} for w, r in history]}
    path = DUMP_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    for old in sorted(DUMP_DIR.glob("*.json"))[:-50]:
        old.unlink(missing_ok=True)
        for shot in DUMP_DIR.glob(f"{old.stem}-*.png"):
            shot.unlink(missing_ok=True)
    return path
