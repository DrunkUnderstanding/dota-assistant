"""装备价值换算：按游戏商店官方分类，把每件物品的属性折成金钱，和售价比，出单文件网页。

    python -X utf8 docs/research/item-value/scripts/build.py

输入：本地知识库 data/kb/kb.json（价格、属性数值，跟着 kb 的版本走）、商店分类 scripts/shops.txt（游戏文件，
第一次运行从 GitHub 上的 dotabuff/d2vpkr 镜像下载，缓存到 data/research/item-value/shops.txt；加 --refresh 重新下载）。
输出：data/research/item-value/item_value.json、data/mockups/<版本>-装备价值换算.html。

算法（玩家 2026-11-27 定）：每项属性的单价 = 只加这一项、最便宜的基础装（生命 = 毛毛帽 2 金 / 点……）；
力 / 敏 / 智每点换成的东西按游戏内说明（DOTA_HeroStats_*_Desc），主属性每点 +1 攻击、全才每点 +0.45 攻击；
只算面板上的固定属性，主动 / 光环 / 触发效果不算。
"""
import base64
import io
import json
import re
import site
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.append(site.getusersitepackages())
import requests  # noqa: E402
from PIL import Image  # noqa: E402

from dota_assistant.core.config import DATA_DIR  # noqa: E402

OUT = DATA_DIR / "research" / "item-value"
PAGE = Path(__file__).resolve().parents[1] / "page.html"
SHOPS_URL = "https://raw.githubusercontent.com/dotabuff/d2vpkr/master/dota/scripts/shops.txt"

# shops.txt 段名 → 游戏内中文分类名（dota_schinese 的 DOTA_Shop_Category_*；基础 / 升级各有一个「军备」，前面加上大类区分）
CATS = [
    ("consumables", "基础物品", "消耗品"), ("attributes", "基础物品", "属性"),
    ("weapons_armor", "基础物品", "军备"), ("misc", "基础物品", "奥术"),
    ("secretshop", "基础物品", "神秘商店"),
    ("basics", "升级物品", "常用"), ("support", "升级物品", "辅助"), ("magics", "升级物品", "法器"),
    ("defense", "升级物品", "防具"), ("weapons", "升级物品", "武器"), ("artifacts", "升级物品", "军备"),
]

# 单价基准：(属性, 中文名, 基准物品, 物品上的数值字段)
BASE = [
    ("attr", "单项属性（力 / 敏 / 智）", "gauntlets", "bonus_strength"),
    ("armor", "护甲", "ring_of_protection", "bonus_armor"),
    ("attack", "攻击速度", "gloves", "bonus_attack_speed"),
    ("damage", "攻击力", "blades_of_attack", "bonus_damage"),
    ("health", "生命值", "fluffy_hat", "bonus_health"),
    ("mana", "魔法值", "wizard_hat", "bonus_mana"),
    ("hp_regen", "生命恢复", "ring_of_regen", "bonus_health_regen"),
    ("mana_regen", "魔法恢复", "sobi_mask", "bonus_mana_regen"),
    ("spell_resist", "魔法抗性 %", "shawl", "bonus_magical_armor"),
    ("move_speed", "移动速度", "boots", "bonus_movement_speed"),
    ("lifesteal", "吸血 %", "lifesteal", "lifesteal_percent"),
    ("spell_lifesteal", "技能吸血 %", "voodoo_mask", "spell_lifesteal"),
    ("evasion", "闪避 %", "talisman_of_evasion", "bonus_evasion"),
    ("aoe_bonus", "作用范围", "chasm_stone", "aoe_bonus"),
]
# 每点属性换成什么（游戏内说明 DOTA_HeroStats_Strength/Agility/Intelligence_Desc）
PER_ATTR = {"str": {"health": 20, "hp_regen": 0.1},
            "agi": {"attack": 1, "armor": 0.16},
            "int": {"mana": 12, "mana_regen": 0.05, "spell_resist": 0.125}}
UNIVERSAL_DMG = 0.45      # 全才英雄每点属性 +0.45 攻击（DOTA_Loadout_PrimaryAttribute_Universal_Tooltip）
NO_PRICE = {"attack_range": "攻击距离", "attack_range_melee": "攻击距离（近战）", "cast_range": "施法距离",
            "cooldown_reduction": "冷却缩减", "restoration_amp": "恢复增强", "projectile_speed": "弹道速度"}
NOT_NEUTRAL = 4294967295


def load_shops(refresh):
    p = OUT / "shops.txt"
    if refresh or not p.exists():
        r = requests.get(SHOPS_URL, timeout=30)
        r.raise_for_status()
        OUT.mkdir(parents=True, exist_ok=True)
        p.write_text(r.text, encoding="utf-8", newline="\n")
    text = p.read_text(encoding="utf-8")
    cats = []
    for key, group, name in CATS:
        m = re.search(r'\n\t"%s"[^\n]*\n\t\{(.*?)\n\t\}' % key, text, re.S)
        if not m:
            raise SystemExit(f"shops.txt 里没有分类 {key}，游戏可能改了商店结构")
        cats.append((key, group, name, re.findall(r'"item"\s+"item_([a-z0-9_]+)"', m.group(1))))
    return cats


class Items:
    def __init__(self, kbdata):
        self.by = {x["name"][5:]: x for x in kbdata["items"]}

    def sv(self, k, name):
        for s in self.by[k]["special_values"]:
            if s["name"] == name:
                return s["values_float"][0]
        return 0

    def cost(self, k):
        return self.by[k]["item_cost"]


def main():
    kbdata = json.loads((DATA_DIR / "kb" / "kb.json").read_text(encoding="utf-8"))
    cats = load_shops("--refresh" in sys.argv)
    it = Items(kbdata)

    price = {k: it.cost(item) / it.sv(item, field) for k, _, item, field in BASE}
    hp1 = it.cost("flask") / (it.sv("flask", "health_regen") * it.sv("flask", "buff_duration"))
    mp1 = it.cost("clarity") / (it.sv("clarity", "mana_regen") * it.sv("clarity", "buff_duration"))
    attr = {a: sum(v * price[k] for k, v in per.items()) for a, per in PER_ATTR.items()}

    def stats(k):
        """返回 (面板价值, [力, 敏, 智, 全才] 价值, 没算进去的, 有没有属性)"""
        s = a = i = sel = ex = 0.0
        unp, seen = [], False
        for v in it.by[k]["special_values"]:
            h = v.get("heading_loc") or ""
            if not (h.startswith(("+", "-")) and "$" in h):
                continue
            seen = True
            var = h[1:].lstrip("$")
            val = v["values_float"][0] * (1 if h[0] == "+" else -1)
            if var in ("str", "agi", "int", "all"):
                s += val if var in ("str", "all") else 0
                a += val if var in ("agi", "all") else 0
                i += val if var in ("int", "all") else 0
            elif var == "selected_attrib":
                sel = val
            elif var == "move_speed" and v.get("is_percentage"):
                unp.append(f"{val:g}% 移速")
            elif var in price:
                ex += val * price[var]
            else:
                unp.append(f"{val:g}{'%' if v.get('is_percentage') else ''} {NO_PRICE.get(var, var)}")
        ms = it.sv(k, "bonus_movement_speed_ranged")   # 鞋的移速分近战 / 远程、没有 heading，按远程（低的那个）算
        if ms:
            ex += ms * price["move_speed"]
            seen = True
        panel = (s + a + i + sel) * price["attr"] + ex
        base = s * attr["str"] + a * attr["agi"] + i * attr["int"] + ex
        r = [base + s * price["damage"], base + a * price["damage"], base + i * price["damage"],
             base + (s + a + i) * UNIVERSAL_DMG * price["damage"]]
        if sel:   # 动力鞋：切到主属性
            r = [r[0] + sel * (attr["str"] + price["damage"]), r[1] + sel * (attr["agi"] + price["damage"]),
                 r[2] + sel * (attr["int"] + price["damage"]), r[3] + sel * (max(attr.values()) + UNIVERSAL_DMG * price["damage"])]
        return panel, r, unp, seen

    sv = it.sv
    # 消耗品：一次性效果按治疗药膏 / 净化药水的「每点回复多少钱」折算
    consum = {
        "flask": (it.cost("flask"), "回血的定价基准：回 %d 血" % (sv("flask", "health_regen") * sv("flask", "buff_duration"))),
        "clarity": (it.cost("clarity"), "回蓝的定价基准：回 %d 蓝" % (sv("clarity", "mana_regen") * sv("clarity", "buff_duration"))),
        "tango": (3 * sv("tango", "health_regen") * sv("tango", "buff_duration") * hp1,
                  "吃 3 次树，每次 %d 血" % (sv("tango", "health_regen") * sv("tango", "buff_duration"))),
        "enchanted_mango": (sv("enchanted_mango", "hp_regen") * price["hp_regen"] + sv("enchanted_mango", "replenish_amount") * mp1,
                            "带着 +%g 回血，吃掉回 %d 蓝" % (sv("enchanted_mango", "hp_regen"), sv("enchanted_mango", "replenish_amount"))),
        "faerie_fire": (sv("faerie_fire", "bonus_damage") * price["damage"] + sv("faerie_fire", "hp_restore") * hp1,
                        "带着 +%d 攻击，吃掉回 %d 血" % (sv("faerie_fire", "bonus_damage"), sv("faerie_fire", "hp_restore"))),
        "blood_grenade": (sv("blood_grenade", "bonus_health") * price["health"], "只算带着的 +%d 生命，扔出去的伤害 / 减速没算" % sv("blood_grenade", "bonus_health")),
        "infused_raindrop": (sv("infused_raindrop", "mana_regen") * price["mana_regen"], "只算 +%g 回蓝，挡魔法伤害没算" % sv("infused_raindrop", "mana_regen")),
    }
    # 有主动的：把一次主动折成钱，写进说明（不进百分比）
    sip = sv("bottle", "health_restore") * hp1 + sv("bottle", "mana_restore") * mp1
    charge = sv("magic_wand", "restore_per_charge") * (hp1 + mp1)
    tq = sv("tranquil_boots", "broken_movement_speed") * price["move_speed"] + sv("tranquil_boots", "broken_health_regen") * price["hp_regen"]
    extra = {
        "bottle": f"每口回 {sv('bottle', 'health_restore'):g} 血 + {sv('bottle', 'mana_restore'):g} 蓝 ≈ {sip:.0f} 金，满瓶 3 口 ≈ {3 * sip:.0f} 金；"
                  f"喝完 {it.cost('bottle') / (3 * sip):.1f} 瓶回本，用存下的神符会灌满",
        "magic_wand": f"主动每点充能回 {sv('magic_wand', 'restore_per_charge'):g} 血 + 蓝 ≈ {charge:.1f} 金，满 {sv('magic_wand', 'max_charges'):g} 点 ≈ {charge * sv('magic_wand', 'max_charges'):.0f} 金（没算进百分比）",
        "magic_stick": f"每点充能 ≈ {charge:.1f} 金，满 {sv('magic_stick', 'max_charges'):g} 点 ≈ {charge * sv('magic_stick', 'max_charges'):.0f} 金；攒 {it.cost('magic_stick') / charge:.0f} 点回本",
        "soul_ring": f"主动：换 {sv('soul_ring', 'mana_gain'):g} 蓝 ≈ {sv('soul_ring', 'mana_gain') * mp1:.0f} 金，扣同样多的血 ≈ {sv('soul_ring', 'mana_gain') * hp1:.0f} 金（没算进百分比）",
        "tranquil_boots": f"这是没被打断时；被打断后只剩 {sv('tranquil_boots', 'broken_movement_speed'):g} 移速 + {sv('tranquil_boots', 'broken_health_regen'):g} 回血 ≈ {tq / it.cost('tranquil_boots') * 100:.0f}%",
        "headdress": "光环只算了自己一份，覆盖几个队友就再乘几",
        "ring_of_basilius": "光环只算了自己一份，覆盖几个队友就再乘几",
        "buckler": "光环只算了自己一份，覆盖几个队友就再乘几",
    }

    out_cats, used, missing = [], set(), []
    for key, group, name, keys in cats:
        rows = []
        for k in keys:
            if k not in it.by:
                missing.append(k)
                continue
            x = it.by[k]
            if x["item_neutral_tier"] != NOT_NEUTRAL:
                continue
            c = it.cost(k)
            if k in consum:
                v, note = consum[k]
                row = dict(k=k, n=x["name_loc"], c=c, kind="consumable", panel=v, r=[v] * 4, note=note)
            else:
                panel, r, unp, seen = stats(k)
                notes = []
                if unp:
                    notes.append("没算：" + "、".join(unp))
                if k in extra:      # 没有属性的那条提示由网页在条形图位置显示

                    notes.append(extra[k])
                row = dict(k=k, n=x["name_loc"], c=c, kind="stats" if seen else "none", panel=panel, r=r, note="；".join(notes))
            row["panel"] = round(row["panel"], 1)
            row["r"] = [round(v, 1) for v in row["r"]]
            rows.append(row)
            used.add(k)
        out_cats.append(dict(key=key, group=group, name=name, items=rows))

    data = dict(
        patch=kbdata.get("patch"), made=int(time.time()),
        base=[dict(k=k, name=nm, item=item, n=it.by[item]["name_loc"], c=it.cost(item), per=round(price[k], 2)) for k, nm, item, _ in BASE],
        once=dict(hp=round(hp1, 3), mp=round(mp1, 3)),
        attr={a: round(v, 1) for a, v in attr.items()}, per_attr=PER_ATTR, uni=UNIVERSAL_DMG,
        cats=out_cats,
    )
    used |= {b[2] for b in BASE} | {"flask", "clarity"}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "item_value.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    if missing:
        print("商店里有、知识库里没有（跳过）：", "、".join(missing))
    print("单价：" + "，".join(f"{b['name']} {b['per']}" for b in data["base"]))
    print("每点属性：" + "，".join(f"{a} {v}" for a, v in data["attr"].items()))
    print(sum(len(c["items"]) for c in out_cats), "件")
    render(data, used)


def icon_css(keys):
    css = []
    for k in sorted(keys):
        p = DATA_DIR / "items" / f"{k}.png"
        if not p.exists():
            print("缺图标", p)
            continue
        im = Image.open(p).convert("RGB").resize((66, 48), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        css.append(f".i-{k}{{background-image:url(data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()})}}")
    return "\n".join(css)


def render(data, used):
    page = PAGE.read_text(encoding="utf-8")
    js = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = page.replace("/*ICONS*/", icon_css(used)).replace("/*DATA*/null", js)
    out = DATA_DIR / "mockups" / f"{data['patch']}-装备价值换算.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8", newline="\n")
    print(out, len(page.encode("utf-8")) // 1024, "KB")


if __name__ == "__main__":
    main()
