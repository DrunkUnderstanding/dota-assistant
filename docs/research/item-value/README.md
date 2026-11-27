# 装备价值换算（2026-11-27）

玩家想知道魔瓶、怨灵系带这类前期装备「真实值多少钱」，之后要按游戏商店的官方分类把全部物品都换算一遍、做成网页。产出单文件网页 `data/mockups/<版本>-装备价值换算.html`（双击打开，约 500 KB，图标都内嵌）。

## 算法

- **单价**（玩家定）：每项属性按「只加这一项、最便宜的基础装」定价——生命 = 毛毛帽 2 金 / 点、魔法 = 巫师帽、护甲 = 守护指环、攻速 = 加速手套、攻击 = 攻击之爪、单项属性 = 力量手套、回血 = 回复戒指、回蓝 = 贤者面罩、魔抗 = 披巾、移速 = 速度之靴（11.1，比风灵之纹 15 便宜）、吸血 = 吸血面具、技能吸血 = 巫毒面具、闪避 = 闪避护符、作用范围 = 裂隙之石。一次性回复按治疗药膏 / 净化药水。
- **属性换成什么**：按游戏内说明（`DOTA_HeroStats_*_Desc`）：力 20 生命 + 0.1 回血；敏 1 攻速 + 0.16 护甲；智 12 魔法 + 0.05 回蓝 + 0.125% 魔抗。主属性每点再 +1 攻击，全才每点 +0.45 攻击（`DOTA_Loadout_PrimaryAttribute_Universal_Tooltip`）。
- **价值** = 面板固定属性 × 单价之和；百分比 = 价值 ÷ 售价。分「面板」（属性按 46.7 金 / 点，不分英雄）和力 / 敏 / 智 / 全才四种英雄。
- 面板属性取知识库 special_values 里带 `heading_loc`（`+$str` 这种）的项，和游戏里物品说明上方那几行一致；鞋的移速分近战 / 远程、没有 heading，按远程算。
- 不算：主动、光环（只算自己那份）、触发效果；攻击距离 / 施法距离 / 冷却缩减 / 恢复增强 / 弹道速度 / 百分比移速没有单项基础装，写进「没算」。消耗品按一次性回复折算，独有功能（回城、雾、眼、粉、魔晶）不换算。魔瓶 / 魔杖 / 魔棒 / 灵魂之戒的主动、静谧之鞋被打断后的值写在说明里，不进百分比。

## 数据

- 价格、属性：本地知识库 `data/kb/kb.json`（Valve datafeed，跟着 `kb update` 的版本走）。
- 商店分类：游戏文件 `scripts/shops.txt`。本机没装 Dota（`find_dota` 找不到），SteamDatabase 的 GameTracking-Dota2 也没收这个文件，所以从 GitHub 上的 `dotabuff/d2vpkr` 镜像下载，缓存到 `data/research/item-value/shops.txt`。分类中文名取自游戏的 `dota_schinese.txt`（`DOTA_Shop_Category_*`）：基础物品的消耗品 / 属性 / 军备 / 奥术、神秘商店，升级物品的常用 / 辅助 / 法器 / 防具 / 武器 / 军备（基础和升级各有一个「军备」）。
- shops.txt 里的侦察守卫知识库里没有，跳过。

## 运行

```
python -X utf8 docs/research/item-value/scripts/build.py            # 约 2 秒
python -X utf8 docs/research/item-value/scripts/build.py --refresh  # 重新下载 shops.txt（游戏改了商店分类时）
```

出新版本后先 `python -m dota_assistant kb update` 再跑，网页文件名带版本号。`page.html` 是网页模板；整理好的数据另存 `data/research/item-value/item_value.json`。
