"""1/2/3 号位打法研究的公共部分：STRATZ 字段、压缩、路径。"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from dota_assistant.core.config import DATA_DIR  # noqa: E402

D = DATA_DIR / "research" / "role-play"
D.mkdir(parents=True, exist_ok=True)

_spec = importlib.util.spec_from_file_location("iv_fetch", ROOT / "docs" / "research" / "item-vs" / "scripts" / "b_fetch.py")
iv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(iv)

CUT_742 = 1795035600          # 7.42 上线（2026-11-18 21:00 UTC，胜率监控 watch.CUT）。
# 2026-11-25 前这里误写成 1763499600（2025 年），a_ids 的 start_time 条件等于没起作用，第 85 节样本混进约 1% 上线前（151 局）的局（第 88 节）
FARM = "{ id count gold }"
FIELDS = (
    "id durationSeconds didRadiantWin bottomLaneOutcome midLaneOutcome topLaneOutcome radiantNetworthLeads "
    "towerDeaths { time npcId isRadiant attacker } "
    "playbackData { roshanEvents { time } } "
    "players { heroId isRadiant position isVictory lane kills deaths assists numLastHits numDenies "
    "networth level heroDamage towerDamage heroHealing "
    "stats { networthPerMinute lastHitsPerMinute experiencePerMinute campStack "
    "killEvents { time target isSolo isGank isSmoke } deathEvents { time attacker } assistEvents { time target } "
    "runes { time rune action } locationReport { positionX positionY } itemPurchases { time itemId } "
    f"farmDistributionReport {{ creepLocation {FARM} neutralLocation {FARM} ancientLocation {FARM} "
    f"buildings {FARM} bountyGold {FARM} other {FARM} }} }} }}")


def _farm(r):
    if r is None:
        return None
    if isinstance(r, dict):
        return [r.get("id"), r.get("count"), r.get("gold")]
    return [[x.get("id"), x.get("count"), x.get("gold")] for x in r]


def compact(m: dict) -> dict:
    ps = []
    for p in m["players"]:
        s = p.get("stats") or {}
        fd = s.get("farmDistributionReport") or {}
        ps.append({
            "h": p["heroId"], "rad": p["isRadiant"], "pos": p.get("position"), "win": p["isVictory"],
            "lane": p.get("lane"), "k": p.get("kills"), "d": p.get("deaths"), "a": p.get("assists"),
            "lh": p.get("numLastHits"), "dn": p.get("numDenies"), "nwend": p.get("networth"), "lvl": p.get("level"),
            "hd": p.get("heroDamage"), "td": p.get("towerDamage"), "hh": p.get("heroHealing"),
            "parsed": s.get("itemPurchases") is not None,
            "nw": s.get("networthPerMinute"), "lhm": s.get("lastHitsPerMinute"), "xpm": s.get("experiencePerMinute"),
            "stack": s.get("campStack"),
            "kills": [(x["time"], x.get("target"), int(bool(x.get("isSolo"))), int(bool(x.get("isGank"))),
                       int(bool(x.get("isSmoke")))) for x in s.get("killEvents") or []],
            "deaths": [(x["time"], x.get("attacker")) for x in s.get("deathEvents") or []],
            "assists": [(x["time"], x.get("target")) for x in s.get("assistEvents") or []],
            "runes": [(x["time"], x.get("rune"), x.get("action")) for x in s.get("runes") or []],
            "loc": [(x.get("positionX"), x.get("positionY")) for x in s.get("locationReport") or []],
            "items": [(x["time"], x["itemId"]) for x in s.get("itemPurchases") or []],
            "farm": {k: _farm(fd.get(k)) for k in ("creepLocation", "neutralLocation", "ancientLocation",
                                                    "buildings", "bountyGold", "other")} if fd else None,
        })
    pb = m.get("playbackData") or {}
    return {"id": m["id"], "dur": m["durationSeconds"], "rw": m.get("didRadiantWin"),
            "lanes": [m.get("topLaneOutcome"), m.get("midLaneOutcome"), m.get("bottomLaneOutcome")],
            "lead": m.get("radiantNetworthLeads"),
            "towers": [(x["time"], x.get("npcId"), x.get("isRadiant"), x.get("attacker")) for x in m.get("towerDeaths") or []],
            "rosh": [x["time"] for x in pb.get("roshanEvents") or []],
            "players": ps}
