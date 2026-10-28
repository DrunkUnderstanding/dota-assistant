"""大招冷却用到的知识库条目：每个英雄要计时的大招（pick）、各级冷却（cooldowns），
以及神杖 / 魔晶改冷却的原样写法（datafeed 丢了符号，从游戏文件取，存 data/kb/cd_ops.json）。

只读知识库条目、游戏文件，不管这一局的计时（那是 domain 层 ultcd 的事）。`kb update` 时顺带调 update_ops。
"""
from __future__ import annotations

import json
import re

from .. import kb

MIN_CD = 40                  # 1 级冷却 ≥ 这么多秒的大招才给小条
OPS_PATH = kb.KB_DIR / "cd_ops.json"
HERO_FILE = "scripts/npc/heroes/npc_dota_hero_{}.txt"
MIRROR = "https://raw.githubusercontent.com/SteamDatabase/GameTracking-Dota2/master/game/dota/pak01_dir/" + HERO_FILE


def cooldowns(ab: kb.Entry) -> list[float]:
    """每一级的冷却（秒）；官方只给一个数就是各级一样"""
    return [float(v) for v in re.findall(r"\d+(?:\.\d+)?", ab.cooldown or "")]


def pick(h: kb.HeroKB) -> kb.Entry | None:
    """这个英雄要计时的大招"""
    ults = [ab for ab in h.abilities if ab.ultimate and not ab.granted and (cds := cooldowns(ab)) and cds[0] >= MIN_CD]
    return max(ults, key=lambda ab: cooldowns(ab)[0], default=None)


# ---------- 神杖 / 魔晶的符号：从游戏文件取 ----------

def parse_kv(text: str) -> dict:
    """Valve KeyValues（只认带引号的键值和大括号，// 注释跳过）；重复的键留第一个"""
    root: dict = {}
    stack, key = [root], None
    for m in re.finditer(r'//[^\n]*|"((?:[^"\\]|\\.)*)"|([{}])', text):
        tok, brace = m.group(1), m.group(2)
        if m.group(0).startswith("//"):
            continue
        if brace == "{":
            d: dict = {}
            stack[-1].setdefault(key, d)
            stack.append(d)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        elif key is None:
            key = tok
        else:
            stack[-1].setdefault(key, tok)
            key = None
    return root


def _find(d: dict, key: str):
    """在嵌套字典里找第一个叫 key 的值"""
    if key in d:
        return d[key]
    for v in d.values():
        if isinstance(v, dict) and (r := _find(v, key)) is not None:
            return r
    return None


def cd_ops(kv_text: str, ability: str) -> dict[str, str]:
    """一个英雄文件里，这个技能 AbilityCooldown 的神杖 / 魔晶写法：{"scepter": "-10"}"""
    ab = _find(parse_kv(kv_text), ability)
    cd = _find(ab, "AbilityCooldown") if isinstance(ab, dict) else None
    if not isinstance(cd, dict):
        return {}
    return {k: cd[f"special_bonus_{k}"] for k in ("scepter", "shard") if isinstance(cd.get(f"special_bonus_{k}"), str)}


def _reader():
    """(来源说明, 读一个英雄文件的函数)：本机装了 Dota 读资源包，没装读 GitHub 上的同步"""
    from ..core.dota_install import find_dota
    dota = find_dota()
    vpk_path = dota / "game" / "dota" / "pak01_dir.vpk" if dota else None
    if vpk_path and vpk_path.exists():
        from ..core.vpk import VPK
        vpk = VPK(vpk_path)
        return f"本机游戏文件 {vpk_path}", lambda key: vpk.read(HERO_FILE.format(key)).decode("utf-8", "replace")

    import requests

    def get(key: str) -> str:
        r = requests.get(MIRROR.format(key), timeout=30)
        r.raise_for_status()
        return r.text
    return "GitHub SteamDatabase/GameTracking-Dota2", get


def update_ops(base: kb.KnowledgeBase | None = None, log=print, force: bool = False, reader=None) -> dict:
    """对面会给小条的大招里，神杖 / 魔晶改冷却的那些，从游戏文件取原样写法存到 data/kb/cd_ops.json。版本没变不重取"""
    base = base or kb.KnowledgeBase.load()
    if base is None:
        return {}
    old = _read_ops_file()
    if not force and old.get("patch") == base.patch:
        return old.get("ops", {})
    need = [(h.key, ab.key) for h in base.heroes.values() if (ab := pick(h)) and (ab.cd_scepter or ab.cd_shard)]
    source, read = reader or _reader()
    ops = {}
    for hero_key, ab_key in need:
        if found := cd_ops(read(hero_key), ab_key):
            ops[ab_key] = found
    OPS_PATH.parent.mkdir(parents=True, exist_ok=True)
    OPS_PATH.write_text(json.dumps({"patch": base.patch, "source": source, "ops": ops}, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    log(f"  大招神杖 / 魔晶冷却：{len(ops)}/{len(need)} 个（{source}）")
    return ops


def _read_ops_file() -> dict:
    try:
        return json.loads(OPS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_ops(base: kb.KnowledgeBase | None) -> dict:
    """存着的神杖 / 魔晶写法；和知识库不是同一版本就不用（数值可能变了）"""
    d = _read_ops_file()
    return d.get("ops", {}) if base and d.get("patch") == base.patch else {}
