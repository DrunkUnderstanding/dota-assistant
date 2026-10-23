"""当前版本知识库：英雄技能 / 天赋 / 神杖魔晶 / 物品说明，全部来自 Valve 官方 datafeed（和游戏内说明同源）。

为什么要有它：AI 凭记忆说 Dota 机制会记错旧版本（例如不知道射手天赋带克敌机先）。
任何关于「某技能 / 物品能做什么」的判断，先从这里查原文；查不到就说不知道，不要靠记忆补。

三层内容：
1. 官方原文（data/kb/*.json，update 时按版本号自动刷新）：说明里的 %占位符% 换成各级数值。
2. 机制标签（关键词从原文里抽，不是人工标注）：克敌机先、驱散、破坏、无视技能免疫……每个标签附原句，可以核对。
3. 玩家规则（knowledge/rules.toml，进 git）：玩家确认过的经验判断，例如「小黑的金箍棒优先级低」。
4. 常见误判（knowledge/mistakes.toml，进 git）：AI（打法页 / 开发助手）说错过、已经查原文纠正的说法，
   例如「司夜刺客没有破坏」。查英雄 / 物品时一起显示，打法页把相关的交给大模型提前避开。
"""
from __future__ import annotations

import json
import logging
import re
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .config import DATA_DIR, ROOT

KB_DIR = DATA_DIR / "kb"
RULES_PATH = ROOT / "knowledge" / "rules.toml"
MISTAKES_PATH = ROOT / "knowledge" / "mistakes.toml"
FEED = "https://www.dota2.com/datafeed"
logger = logging.getLogger(__name__)  # 不叫 log：下面不少函数有 log 参数（进度提示）


def _say(msg: str) -> None:
    """进度提示：打印出来，同时记进日志"""
    print(msg, flush=True)
    logger.info(msg.strip())
NOT_NEUTRAL = 4294967295

# 标签 → (原文里的关键词, 排除词)。只认官方措辞，宁缺毋滥；命中会附原句，方便人核对。
# 排除词去掉「看着像其实不是」的句子，例如「破坏会使被动失效」说的是这个技能怕破坏，不是它能施加破坏。
TAGS = {
    "克敌机先": (r"克敌机先", None),
    "无视闪避": (r"无视闪避", None),
    "提供闪避": (r"闪避", r"无视闪避|闪避效果为非线性叠加|闪避.{0,2}(几率|概率)?(降低|减少)"),
    "强驱散": (r"强驱散", None),
    "驱散": (r"驱散", r"(不能|无法|不可)被.{0,4}驱散|强驱散|驱散类型"),
    "抵挡 / 反馈指向技能": (r"(抵挡|格挡|反馈|反射).{0,8}指向性技能", None),
    "伤害反弹": (r"反弹.{0,6}伤害|伤害反弹", r"免疫反弹|无法反弹"),
    "不可驱散": (r"(不能|无法|不可)被.{0,4}驱散", None),
    "施加破坏": (r"施加破坏|破坏(其|目标的)?被动|破坏效果|被动技能(无效|失效)", r"^破坏(会|只会|不会)|破坏的负面效果"),
    "怕破坏": (r"^破坏(会|只会)使", None),
    "无视技能免疫": (r"无视(技能|减益)免疫|穿透(技能|减益)免疫", r"不无视|无法无视"),
    # 官方现在叫「减益免疫」，说法有「获得 / 变成 / 为 / 提供」几种（噬魂鬼狂暴是「变成减益免疫」）；
    # 排除「获得无视减益免疫的减速」「对减益免疫单位有效」「幻象为减益免疫」「友方单位变为减益免疫」这类不是自己免疫的句子
    "提供技能免疫": (r"(获得|变成|变为|提供|为)[^。，；]{0,6}(技能|减益)免疫",
                     r"无视(技能|减益)免疫|将挣脱|将移除|将会中止|对(技能|减益)免疫|(技能|减益)免疫(单位|的|下)|幻象|友方|友军"),
    "隐身": (r"(进入|获得|变为|变成|处于|带有|是|为)[^。，；]{0,6}隐(身|匿)|隐藏起来",
             r"隐身单位|脱离|打破|阻止|不会产生|显形"),
    # 只算「让隐身的敌人现形」：排除自己现形（攻击后显形）、不会使…显形、迷雾中显形（那是视野，不是反隐）
    "反隐 / 显形": (r"显形|真视|真实视域", r"不会(被|使)|将会显形|迷雾中显形|自己显形"),
    "沉默": (r"沉默(?!术士)|阻止.{0,12}施法", r"无法成为目标"),
    "眩晕": (r"眩晕|击晕", None),
    "缴械": (r"缴械", r"(他|自身|期间|这段时间内)被缴械|面朝方向被锁定|无法成为目标"),
    "妖术": (r"妖术|变为.{0,4}(动物|小动物)", None),
    "缠绕 / 定身": (r"缠绕|束缚|定身|无法移动|困在原地|冻在原地", r"迷雾缠绕|逃离.{0,2}束缚|缠绕之爪|时无法施放|地之束缚会|将队友封入|友方单位"),
    "减速": (r"减速|降低.{0,6}移动速度", r"减速抗性"),
    "减甲": (r"(降低|减少|减去|削减).{0,10}护甲|护甲.{0,4}(降低|减少)", None),
    "吸血": (r"吸血|生命窃取", None),
    "幻象": (r"幻象", None),
    "召唤": (r"召唤", r"召唤具有"),
    "位移 / 击退": (r"闪烁|传送|冲锋|跳跃|击退|拉向|拖拽", r"闪电.{0,10}跳跃|目标之间跳跃"),
    "物理伤害": (r"物理伤害", None),
    "魔法伤害": (r"魔法伤害", None),
    "纯粹伤害": (r"纯粹伤害", r"免疫.{0,8}纯粹伤害"),
    "治疗": (r"治疗|恢复.{0,6}生命", None),
    "持续施法": (r"持续施法", None),
    "全图": (r"全地图|全图|全局", None),
}

# 注释（notes）多是「能不能打到隐身单位」「被沉默时会不会打断」这类交互说明，不代表技能本身的能力。
# 只有这几个标签允许从注释里取（它们的官方措辞经常只出现在注释里，例如射手天赋的克敌机先）。
NOTES_OK = {"克敌机先", "无视技能免疫", "不可驱散", "怕破坏", "无视闪避"}
# 「控制」类标签：判断对面有多少控制、要不要黑皇杖
CONTROL_TAGS = ("眩晕", "沉默", "缠绕 / 定身", "妖术", "缴械")

# 物品说明里的 $属性 占位符
STAT_LABELS = {
    "$damage": "攻击力", "$agi": "敏捷", "$str": "力量", "$int": "智力", "$all": "全属性",
    "$selected_attrib": "主属性", "$hp_regen": "生命恢复", "$mana_regen": "魔法恢复", "$armor": "护甲",
    "$attack": "攻击速度", "$health": "生命值", "$mana": "魔法值", "$move_speed": "移动速度",
    "$evasion": "闪避", "$spell_resist": "魔法抗性", "$spell_amp": "技能增强", "$lifesteal": "吸血",
    "$attack_range": "攻击距离", "$cast_range": "施法距离", "$health_pct": "生命值", "$mana_pct": "魔法值",
    "$spell_lifesteal": "技能吸血", "$status_resist": "状态抗性", "$slow_resist": "减速抗性",
    "$attack_pct": "攻击速度", "$base_attack": "基础攻击力", "$projectile_speed": "弹道速度",
    "$max_mana_percentage": "最大魔法值", "$aoe_bonus": "作用范围", "$attack_range_melee": "攻击距离（仅近战）",
    "$restoration_amp": "恢复增强", "$cooldown_reduction": "冷却缩减",
}

ITEM_ALIASES = {
    "BKB": "黑皇杖", "MKB": "金箍棒", "跳刀": "闪烁匕首", "大炮": "代达罗斯之殇", "大电锤": "雷神之锤",
    "林肯": "林肯法球", "羊刀": "邪恶镰刀", "A杖": "阿哈利姆神杖", "神杖": "阿哈利姆神杖", "魔晶": "阿哈利姆魔晶",
    "分身": "幻影斧", "蝴蝶": "蝴蝶", "撒旦": "撒旦之邪力", "龙心": "恐鳌之心", "冰眼": "斯嘉蒂之眼",
    "紫苑": "血棘", "隐刀": "影刃", "大隐刀": "白银之锋", "推推": "原力法杖", "大推推": "飓风长戟",
    "魔龙枪": "魔龙枪", "飞鞋": "远行鞋", "假腿": "动力鞋", "相位": "相位鞋", "梅肯": "梅肯斯姆",
    "笛子": "洞察烟斗", "绿杖": "Eul的神圣法杖", "吹风": "Eul的神圣法杖", "以太": "以太透镜",
    "刃甲": "刃甲", "莲花": "清莲宝珠", "天堂": "天堂之戟", "银月": "银月之晶", "狂战": "狂战斧",
    "辉耀": "辉耀", "強袭": "强袭胸甲", "强袭": "强袭胸甲", "否决": "否决坠饰", "希瓦": "希瓦的守护",
}


# ---------- 拉取 ----------

def _get(url: str) -> dict:
    import requests
    for attempt in range(6):
        try:
            r = requests.get(url, timeout=60)
        except requests.exceptions.RequestException as e:
            # dota2.com 并发时偶尔直接断连接（SSL EOF / 10054），歇一下重试
            if attempt == 5:
                raise
            logger.info("连接断开，重试（%s）：%s", e, url)
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 200:
            return r.json()
        time.sleep(2 * (attempt + 1))
    r.raise_for_status()


def current_patch() -> str:
    patches = _get(f"{FEED}/patchnoteslist?language=schinese")["patches"]
    return max(patches, key=lambda p: p["patch_timestamp"])["patch_number"]


def fetch(hero_ids: list[int], log=_say, workers: int = 8) -> dict:
    """整版知识库：英雄 + 物品。约 550 个请求，并发 8，一两分钟。"""
    patch = current_patch()
    log(f"  当前版本 {patch}，拉英雄 {len(hero_ids)} 个 …")
    with ThreadPoolExecutor(workers) as ex:
        heroes = list(ex.map(
            lambda h: _get(f"{FEED}/herodata?language=schinese&hero_id={h}")["result"]["data"]["heroes"][0],
            hero_ids))
    listing = _get(f"{FEED}/itemlist?language=schinese")["result"]["data"]["itemabilities"]
    ids = [i["id"] for i in listing if i["name"].startswith("item_") and not i["name"].startswith("item_recipe")]
    log(f"  拉物品 {len(ids)} 个 …")

    def item(i):
        try:
            return _get(f"{FEED}/itemdata?language=schinese&item_id={i}")["result"]["data"]["items"][0]
        except Exception:
            return None
    with ThreadPoolExecutor(workers) as ex:
        items = [it for it in ex.map(item, ids) if it]
    # 去掉商店买不到、也不是中立物品的（已删除 / 内部物品）
    items = [it for it in items if it.get("item_cost") or it.get("item_neutral_tier") not in (None, NOT_NEUTRAL)]
    return {"patch": patch, "fetched_at": int(time.time()), "heroes": heroes, "items": items}


def update(hero_ids: list[int], log=_say, force: bool = False) -> bool:
    """版本号没变就不重拉。返回是否更新了。"""
    KB_DIR.mkdir(parents=True, exist_ok=True)
    meta = KB_DIR / "kb.json"
    if not force and meta.exists():
        old = json.loads(meta.read_text(encoding="utf-8")).get("patch")
        if old == current_patch():
            log(f"  知识库已是最新版本 {old}")
            return False
    kb = fetch(hero_ids, log)
    meta.write_text(json.dumps(kb, ensure_ascii=False), encoding="utf-8")
    log(f"  知识库 {kb['patch']}：{len(kb['heroes'])} 个英雄、{len(kb['items'])} 件物品")
    return True


# ---------- 文本整理 ----------

def _fmt_num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def _values(vals: list[float]) -> str:
    vals = [v for v in vals]
    if not vals:
        return "?"
    if len(set(vals)) == 1:
        return _fmt_num(vals[0])
    return "/".join(_fmt_num(v) for v in vals)


_BUILTIN = {
    "abilityduration": "durations", "abilitycooldown": "cooldowns", "abilitymanacost": "mana_costs",
    "abilitycastrange": "cast_ranges", "abilitychanneltime": "channel_times", "abilitydamage": "damages",
    "abilitycastpoint": "cast_points",
}


def fill(text: str, ab: dict) -> str:
    """%占位符% → 各级数值；%% → %；去掉 HTML 标签"""
    sv = {s["name"].lower(): s for s in ab.get("special_values") or []}

    def sub(m: re.Match) -> str:
        key = m.group(1)
        if not key:
            return "%"
        k = key.lower()
        if k in sv:
            return _values(sv[k]["values_float"])
        if k in _BUILTIN and ab.get(_BUILTIN[k]):
            return _values([v for v in ab[_BUILTIN[k]] if v] or ab[_BUILTIN[k]])
        return f"[{key}]"
    text = re.sub(r"%([A-Za-z0-9_]*)%", sub, text or "")
    text = re.sub(r"<h1>(.*?)</h1>", r"【\1】", text)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def _talent_values(hero: dict) -> dict[str, float]:
    """天赋名 → 数值。专属天赋的数值挂在被它加强的技能的 special_values.bonuses 里；
    通用天赋（+75 攻击距离之类）的数值在天赋自己的 special_values 里。"""
    out = {}
    for ab in [*hero["abilities"], *(hero.get("facet_abilities") or [])]:
        for s in ab.get("special_values") or []:
            for b in s.get("bonuses") or []:
                out[b["name"]] = b["value"]
    for t in hero.get("talents") or []:
        for s in t.get("special_values") or []:
            if s.get("values_float"):
                out.setdefault(t["name"], s["values_float"][0])
    return out


def _talent_text(t: dict, values: dict[str, float]) -> str:
    def sub(m):
        v = values.get(t["name"])
        return _fmt_num(abs(v)) if v is not None else "?"
    return re.sub(r"\{s:[^}]+\}", sub, t["name_loc"])


@dataclass
class Entry:
    """一个技能或物品的可读条目"""
    name: str
    key: str
    text: str                         # 说明（数值已填）
    notes: list[str] = field(default_factory=list)
    stats: list[str] = field(default_factory=list)       # 物品属性加成
    scepter: str = ""
    shard: str = ""
    cooldown: str = ""
    mana: str = ""
    cost: int = 0
    neutral_tier: int | None = None
    pierces_immunity: bool | None = None                 # 官方字段 immunity：是否无视技能免疫

    def full_text(self) -> str:
        return "\n".join([*self.stats, self.text, *self.notes, self.scepter, self.shard])

    def main_text(self) -> str:
        """说明正文（含属性、神杖 / 魔晶），不含注释"""
        return "\n".join([*self.stats, self.text, self.scepter, self.shard])

    def tags(self) -> dict[str, str]:
        """标签 → 命中的原句。能力类标签只看正文；NOTES_OK 里的标签也看注释"""
        out = tag_sentences(self.main_text())
        for tag, sent in tag_sentences("\n".join(self.notes), only=NOTES_OK).items():
            out.setdefault(tag, sent)
        return out


def tag_sentences(text: str, only: set[str] | None = None) -> dict[str, str]:
    out = {}
    sentences = [x.strip() for x in re.split(r"(?<=[。！；\n])", text) if x.strip()]
    for tag, (pat, exclude) in TAGS.items():
        if only is not None and tag not in only:
            continue
        for s in sentences:
            if re.search(pat, s) and not (exclude and re.search(exclude, s)):
                out[tag] = s
                break
    return out


# 官方 immunity 字段：1 = 无视技能免疫（来自 Valve 数据，比关键词更可靠）
_PIERCES = {1: True, 2: False, 3: False, 4: False}


def _entry(ab: dict, is_item: bool) -> Entry:
    stats = []
    if is_item:
        for s in ab.get("special_values") or []:
            h = s.get("heading_loc") or ""
            if h.startswith(("+", "-")) and "$" in h:
                sign, var = h[0], h[1:]
                label = STAT_LABELS.get(var, var)
                stats.append(f"{sign}{_values(s['values_float'])}{'%' if s.get('is_percentage') else ''} {label}")
    cd = [v for v in ab.get("cooldowns") or [] if v]
    mc = [v for v in ab.get("mana_costs") or [] if v]
    tier = ab.get("item_neutral_tier")
    return Entry(
        name=ab["name_loc"], key=ab["name"], text=fill(ab.get("desc_loc"), ab),
        notes=[fill(n, ab) for n in ab.get("notes_loc") or [] if n],
        stats=stats, scepter=fill(ab.get("scepter_loc"), ab), shard=fill(ab.get("shard_loc"), ab),
        cooldown=_values(cd) if cd else "", mana=_values(mc) if mc else "",
        cost=ab.get("item_cost") or 0,
        neutral_tier=None if tier in (None, NOT_NEUTRAL, -1) else tier + 1,
        pierces_immunity=_PIERCES.get(ab.get("immunity")) if not is_item else None,
    )


@dataclass
class HeroKB:
    id: int
    name: str
    key: str
    attack: str
    attack_range: int
    abilities: list[Entry]
    talents: list[tuple[int, str, str]]        # (等级, 左, 右)
    facets: list[tuple[str, str]]              # (名字, 说明)

    def tags(self) -> dict[str, tuple[str, str]]:
        """标签 → (技能名, 原句)"""
        out = {}
        for ab in self.abilities:
            for tag, sent in ab.tags().items():
                out.setdefault(tag, (ab.name, sent))
        return out

    def with_tag(self, *tags: str) -> list[tuple[Entry, str, str]]:
        """所有带这些标签的技能：[(技能, 标签, 原句)]"""
        return [(ab, t, sent) for ab in self.abilities for t, sent in ab.tags().items() if t in tags]


class KnowledgeBase:
    def __init__(self, raw: dict):
        self.patch = raw["patch"]
        self.heroes: dict[int, HeroKB] = {}
        for h in raw["heroes"]:
            tv = _talent_values(h)
            talents_raw = [t for t in h.get("talents") or []]
            # 天赋按 10/15/20/25 级各两个，官方顺序是从低到高、右左交替
            pairs = [(10 + 5 * i, _talent_text(talents_raw[2 * i + 1], tv), _talent_text(talents_raw[2 * i], tv))
                     for i in range(len(talents_raw) // 2)]
            abilities = [_entry(a, False) for a in h["abilities"]
                         if a.get("name_loc") and not a["name"].startswith(("special_bonus", "generic_hidden"))]
            facets = [(f.get("title_loc") or f.get("name", ""), fill(f.get("description_loc"), f))
                      for f in h.get("facets") or []]
            self.heroes[h["id"]] = HeroKB(h["id"], h["name_loc"], h["name"].removeprefix("npc_dota_hero_"),
                                          "远程" if h.get("attack_capability") == 2 else "近战",
                                          h.get("attack_range") or 0, abilities, pairs, facets)
        self.items = {it["name"].removeprefix("item_"): _entry(it, True) for it in raw["items"]}
        self.item_ids = {it["name"]: it["id"] for it in raw["items"]}
        self._item_lookup = {}
        for k, e in self.items.items():
            for n in (k, e.name, k.replace("_", "")):
                self._item_lookup.setdefault(_norm(n), k)
        for alias, name in ITEM_ALIASES.items():
            k = self._item_lookup.get(_norm(name))
            if k:
                self._item_lookup.setdefault(_norm(alias), k)

    @classmethod
    def load(cls) -> KnowledgeBase | None:
        p = KB_DIR / "kb.json"
        if not p.exists():
            return None
        return cls(json.loads(p.read_text(encoding="utf-8")))

    def item(self, text: str) -> Entry:
        n = _norm(text)
        if n in self._item_lookup:
            return self.items[self._item_lookup[n]]
        hits = {k for name, k in self._item_lookup.items() if n and n in name}
        if len(hits) == 1:
            return self.items[hits.pop()]
        if hits:
            raise KeyError(f"「{text}」有多个匹配：" + "、".join(sorted(self.items[k].name for k in hits)[:12]))
        raise KeyError(f"找不到物品「{text}」")

    def search(self, pattern: str) -> list[tuple[str, str, str]]:
        """[(英雄名 / 物品, 技能名, 命中的句子)]"""
        out = []
        rx = re.compile(pattern)
        for h in self.heroes.values():
            for ab in h.abilities:
                for s in re.split(r"(?<=[。！；\n])", ab.full_text()):
                    if rx.search(s):
                        out.append((h.name, ab.name, s.strip()))
        for e in self.items.values():
            for s in re.split(r"(?<=[。！；\n])", e.full_text()):
                if rx.search(s):
                    out.append(("物品", e.name, s.strip()))
        return out


def _norm(s: str) -> str:
    return "".join(s.lower().split()).replace("-", "").replace("'", "").replace("_", "").replace("的", "")


# ---------- 玩家规则 ----------

@dataclass
class Rule:
    text: str                       # 规则本身
    hero: str = ""                  # 适用的英雄（空 = 通用）
    item: str = ""                  # 涉及的物品
    source: str = ""                # 来源：玩家原话 / 实现假设
    date: str = ""
    evidence: str = ""              # 官方数据里的依据


def load_rules(path: Path = RULES_PATH) -> list[Rule]:
    if not path.exists():
        return []
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    known = Rule.__dataclass_fields__.keys()
    return [Rule(**{k: v for k, v in r.items() if k in known}) for r in raw.get("rule", [])]


# ---------- 常见误判 ----------

@dataclass
class Mistake:
    wrong: str                      # AI 的错误说法
    right: str                      # 查原文后的正确说法
    hero: str = ""                  # 涉及的英雄（空 = 不针对某个英雄）
    item: str = ""                  # 涉及的物品
    who: str = ""                   # 谁说错的：打法页 AI / 开发助手
    date: str = ""
    evidence: str = ""              # 官方原文依据
    has_tag: str = ""               # 可选：hero / item 确实有这个机制标签，单元测试逐版本核对


def load_mistakes(path: Path = MISTAKES_PATH) -> list[Mistake]:
    if not path.exists():
        return []
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    known = Mistake.__dataclass_fields__.keys()
    return [Mistake(**{k: v for k, v in m.items() if k in known}) for m in raw.get("mistake", [])]


def add_mistake(m: Mistake, path: Path = MISTAKES_PATH) -> None:
    """追加一条。tomllib 只读，这里手写；JSON 字符串的转义在 TOML 基本字符串里同样合法"""
    L = ["", "[[mistake]]"] + [f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in m.__dict__.items() if v]
    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


def _mistake_lines(ms: list[Mistake]) -> list[str]:
    if not ms:
        return []
    return ["\n常见误判（AI 以前说错过，以原文为准）："] + [f"  ✗ {m.wrong}\n    ✓ {m.right}" for m in ms]


# ---------- 展示 ----------

def render_hero(h: HeroKB, rules: list[Rule], patch: str, mistakes: list[Mistake] = ()) -> str:
    L = [f"{h.name}（{h.key}）· {h.attack} {h.attack_range} · 版本 {patch}"]
    if h.facets:
        L.append("命石：" + "；".join(f"{n}：{d}" for n, d in h.facets if n))
    for ab in h.abilities:
        head = f"\n【{ab.name}】"
        extra = [x for x in (f"冷却 {ab.cooldown}" if ab.cooldown else "", f"魔耗 {ab.mana}" if ab.mana else "",
                             "无视技能免疫" if ab.pierces_immunity else "") if x]
        L.append(head + ("（" + "，".join(extra) + "）" if extra else ""))
        L.append("  " + ab.text.replace("\n", "\n  "))
        L += [f"  · {n}" for n in ab.notes]
        if ab.scepter:
            L.append(f"  神杖：{ab.scepter}")
        if ab.shard:
            L.append(f"  魔晶：{ab.shard}")
    if h.talents:
        L.append("\n天赋：")
        L += [f"  {lv} 级：{a}  /  {b}" for lv, a, b in reversed(h.talents)]
    tags = h.tags()
    if tags:
        L.append("\n机制标签（关键词从原文抽取，附原句）：")
        L += [f"  {t}：{ab} —— {s}" for t, (ab, s) in tags.items()]
    mine = [r for r in rules if r.hero == h.name]
    if mine:
        L.append("\n玩家规则：")
        L += [f"  · {r.text}（{r.source} {r.date}）" for r in mine]
    L += _mistake_lines([m for m in mistakes if m.hero == h.name])
    return "\n".join(L)


def render_item(e: Entry, rules: list[Rule], patch: str, mistakes: list[Mistake] = ()) -> str:
    price = f"中立物品 {e.neutral_tier} 级" if e.neutral_tier else f"{e.cost} 金"
    L = [f"{e.name}（{e.key}）· {price} · 版本 {patch}"]
    if e.stats:
        L.append("属性：" + "，".join(e.stats))
    if e.cooldown:
        L.append(f"冷却 {e.cooldown}" + (f"，魔耗 {e.mana}" if e.mana else ""))
    L.append(e.text)
    L += [f"· {n}" for n in e.notes]
    tags = e.tags()
    if tags:
        L.append("\n机制标签：" + "、".join(tags))
    mine = [r for r in rules if r.item == e.name]
    if mine:
        L.append("\n玩家规则：")
        L += [f"  · {r.hero + '：' if r.hero else ''}{r.text}（{r.source} {r.date}）" for r in mine]
    L += _mistake_lines([m for m in mistakes if m.item == e.name])
    return "\n".join(L)


def hero_brief(h: HeroKB, rules: list[Rule], mistakes: list[Mistake] = ()) -> str:
    """给出装推荐 / 大模型用的一段紧凑事实：技能名 + 机制标签 + 玩家规则 + 常见误判"""
    tags = h.tags()
    parts = [f"{h.name}：{h.attack}{h.attack_range}；技能 " + "、".join(a.name for a in h.abilities)]
    if tags:
        parts.append("机制 " + "、".join(f"{t}({ab})" for t, (ab, _) in tags.items()))
    parts += [f"规则：{r.text}" for r in rules if r.hero == h.name]
    parts += [f"误判：「{m.wrong}」不对，{m.right}" for m in mistakes if m.hero == h.name]
    return "；".join(parts)
