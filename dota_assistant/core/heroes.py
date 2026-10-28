"""英雄表：id ↔ 中文名 / 英文名 / 玩家常用别名。"""
from __future__ import annotations

from dataclasses import dataclass

# 玩家常用简称 → 英文名（官方英文名不会因中文译名调整而变，比直接映射中文稳）
ALIASES = {
    "AM": "Anti-Mage", "敌法": "Anti-Mage",
    "PA": "Phantom Assassin", "幻刺": "Phantom Assassin",
    "PL": "Phantom Lancer", "猴子": "Phantom Lancer",
    "SF": "Shadow Fiend", "影魔": "Shadow Fiend",
    "TB": "Terrorblade", "魂守": "Terrorblade",
    "BB": "Bristleback", "钢背": "Bristleback",
    "DK": "Dragon Knight", "龙骑": "Dragon Knight",
    "ES": "Earthshaker", "小牛": "Earthshaker",
    "CM": "Crystal Maiden", "冰女": "Crystal Maiden",
    "WR": "Windranger", "风行": "Windranger",
    "QOP": "Queen of Pain", "女王": "Queen of Pain",
    "SK": "Sand King", "沙王": "Sand King",
    "FV": "Faceless Void", "虚空": "Faceless Void",
    "TA": "Templar Assassin", "圣堂": "Templar Assassin",
    "WK": "Wraith King", "骷髅王": "Wraith King",
    "SB": "Spirit Breaker", "白牛": "Spirit Breaker",
    "SS": "Shadow Shaman", "小Y": "Shadow Shaman",
    "WD": "Witch Doctor", "巫医": "Witch Doctor",
    "OD": "Outworld Destroyer", "黑鸟": "Outworld Destroyer",
    "NP": "Nature's Prophet", "先知": "Nature's Prophet",
    "LC": "Legion Commander", "军团": "Legion Commander",
    "CK": "Chaos Knight", "混沌": "Chaos Knight",
    "MK": "Monkey King", "大圣": "Monkey King",
    "DP": "Death Prophet",
    "VS": "Vengeful Spirit", "复仇": "Vengeful Spirit",
    "SD": "Shadow Demon", "毒狗": "Venomancer",
    "蓝猫": "Storm Spirit", "火猫": "Ember Spirit", "土猫": "Earth Spirit", "紫猫": "Void Spirit",
    "小小": "Tiny", "斧王": "Axe", "蓝胖": "Ogre Magi", "大树": "Treant Protector",
    "人马": "Centaur Warrunner", "屠夫": "Pudge", "卡尔": "Invoker", "小鱼": "Slark",
    "小黑": "Drow Ranger", "火枪": "Sniper", "美杜莎": "Medusa", "一姐": "Medusa",
    "电魂": "Storm Spirit", "滚滚": "Pangolier", "马尔斯": "Mars", "玛尔斯": "Mars",
}


@dataclass(frozen=True)
class Hero:
    id: int
    key: str          # npc_dota_hero_xxx 去掉前缀
    name_cn: str
    name_en: str
    roles: tuple[str, ...]
    attr: str


class HeroDB:
    def __init__(self, heroes: list[Hero]):
        self.by_id = {h.id: h for h in heroes}
        self._lookup: dict[str, int] = {}
        for h in heroes:
            for k in (h.name_cn, h.name_en, h.key, h.key.replace("_", " ")):
                self._lookup[_norm(k)] = h.id
        en = {_norm(h.name_en): h.id for h in heroes}
        for alias, name_en in ALIASES.items():
            if _norm(name_en) in en:
                self._lookup.setdefault(_norm(alias), en[_norm(name_en)])

    def __iter__(self):
        return iter(self.by_id.values())

    def find(self, text: str) -> Hero:
        """按中文名 / 英文名 / 别名查英雄；唯一的子串匹配也接受。"""
        n = _norm(text)
        if n in self._lookup:
            return self.by_id[self._lookup[n]]
        hits = {hid for k, hid in self._lookup.items() if n and n in k}
        if len(hits) == 1:
            return self.by_id[hits.pop()]
        if hits:
            names = "、".join(sorted(self.by_id[i].name_cn for i in hits))
            raise KeyError(f"「{text}」有多个匹配：{names}")
        raise KeyError(f"找不到英雄「{text}」")

    def find_many(self, items: list[str]) -> list[Hero]:
        return [self.find(s) for s in items if s.strip()]


def _norm(s: str) -> str:
    return "".join(s.lower().split()).replace("-", "").replace("'", "").replace("_", "")
