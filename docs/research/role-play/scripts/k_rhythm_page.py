"""对局节奏（第 88 节）出网页：rhythm_template.html + rhythm.json → data/mockups/7.42-对局节奏.html。

图表和表格里的数字都由页面脚本从 rhythm.json 现算；下面 TEXT 是看完 h_rhythm.py 输出后手写的结论文字，
重跑数据后要按新数字改。

    python -X utf8 docs/research/role-play/scripts/k_rhythm_page.py
"""
import json
import time
from pathlib import Path

from common import D

from dota_assistant.core.config import DATA_DIR

TEMPLATE = Path(__file__).resolve().parents[1] / "rhythm_template.html"
OUT = DATA_DIR / "mockups" / "7.42-对局节奏.html"


def text(r: dict) -> dict:
    a, b = r["742"], r["741"]
    m = r["meta"]
    day = lambda t: time.strftime("%m-%d %H:%M", time.gmtime(t))   # noqa: E731
    return {
        "TITLE": "7.42 对局节奏：时间没变，优势变便宜了",
        "SUB": (f"7.42 上线（11-18 21:00 UTC）到 {day(m['st_max'])} UTC 的 {a['n']:,} 局（含 11-24 的 7.42a，从 34.7 万局里随机抽），"
                f"对照 7.41f 同条件 {b['n']:,} 局。都是「这局实际发生了什么」，不是更新日志的推论。"),
        "LEAD": ("<b>节奏本身几乎没动</b>：中位时长 38.3 分钟（7.41f 38.5）、每分钟击杀 1.99（1.94）、15～25 分钟是打架高峰，"
                 "推塔时间和 7.41f 一样。<b>变的是优势的含金量</b>：10 分钟领先 2000 的一方最后赢 74.1%（7.41f 77.4%），"
                 "15 分钟落后 2000 的翻盘率 25.5%（23.0%）；经济差也更难拉开，20 分钟中位领先 4110（4576）。"
                 "20 分钟以后差距缩小，25 分钟以后两版本追平。上线 7 天、7.42a 前后都是这个样子，没有在变。"
                 "<b>→ 前期领先了别浪，落后了别放弃；核心的钱更多要从线上来。</b>"),
        "NOTE_KILLS": "每局平均。0～5 分钟 7.42 多半个人头（6.9 对 6.4，含开号角前），之后两条线基本重合；25 分钟后双方都开始收着打。",
        "NOTE_WIN": ("按该分钟的经济领先分档。领先 2000 这一档样本大（每版本上千局），7.42 在 10～15 分钟低 2.5～3.3 个点、20 分钟低 1 个点、25 分钟以后追平。"
                     "领先 5000 在 10 分钟很少见（两版本各几百局，误差 ±3～4 个点），只看 15 分钟以后：15～20 分钟低 1 个点左右，25 分钟以后一样。"),
        "NOTE_DAILY": ("每天一个点（上线后每 24 小时一段，第 7 天只到 11-25 05:43 UTC，局数少）。虚线是 7.41f 整体。"
                       "两个数字每天都在 7.41f 那一侧、没有往回走——不是上线头两天大家没摸清的临时现象。"
                       "下表按时段分：7.42a（11-24）以后的局数还少，误差大，但看不出变化。"),
        "NOTE_TOWER": "全场第 1 / 3 / 6 / 9 座塔被推掉的中位时间（不区分哪一路、几塔）。两版本差不到 0.2 分钟——7.42 没有变成快节奏版本。（只看上线头两天时快了 0.2～0.3 分钟，后来回去了。）",
        "NOTE_LH": "10～20 分钟正补（线上兵 + 野怪）。1 号位 112 → 100，2、3 号位各少 4～7 个；4、5 号位基本没变。7.42 重做了野区（普通营地只刷在自己半场、狗头人 3 只变 2 只等），核心的钱更多来自线上兵。",
        "NOTE_CHECK": ("同一时段（上线头两天）两份独立抽的样本，一份是 11-20 为第 85 节抽的，一份是这次新抽的，算出来的数字应该一样——"
                       "击杀、塔、正补、领先和翻盘都对得上（差别在误差以内）；只有时长新样本短 0.4 分钟（平均 39.2 对 39.6，略超出误差，"
                       "可能是 OpenDota 11-20 时收录还不全，没查清），不影响「时长没变」的结论。"
                       "旧样本按编号去掉了上线前的 151 局（第 85 节当时混进了这些局，约 1%）。"),
        "FOOT": (
            f"<b>样本：</b>超凡～冠绝天梯、全英雄选择、≥15 分钟、10 人都在。7.42 从 OpenDota 取上线后全部 34.7 万局随机抽 3 万，"
            f"STRATZ 抓详情（2026-11-25），开始时间 {day(m['st_min'])} ～ {day(m['st_max'])} UTC；7.41f 用第 85 节的样本。"
            "两边同一套过滤：位置齐全、每分钟身价有数据、没人掉线（身价掉到最高一半以下或 15 分钟不到 1500）。"
            "<br><b>口径：</b>击杀 = 英雄击杀事件（第一个窗口含开号角前）；一个窗口的平均只算活到窗口结束的局。"
            "领先 / 翻盘用 STRATZ 每分钟经济差（radiantNetworthLeads），翻盘 = 第 15 分钟落后 ≥2000、最后赢了。"
            "塔的时间 = 全场第 i 座被推掉的塔的中位时间（不分哪一路、几塔）。± 是 95% 区间。"
            "7.42a 的时间按 Valve 更新日志取 11-24 08:00 UTC（只精确到日）。"
            "<br><b>注意：</b>都是相关不是因果——领先的队本来就更会打。"
            "<br><b>脚本：</b>docs/research/role-play/scripts/ i_rhythm_ids.py → j_rhythm_fetch.py → h_rhythm.py → k_rhythm_page.py。"
        ),
    }


def main() -> None:
    r = json.loads((D / "rhythm.json").read_text(encoding="utf-8"))
    html = TEMPLATE.read_text(encoding="utf-8")
    for k, v in text(r).items():
        html = html.replace("{{" + k + "}}", v)
    html = html.replace("{{DATA}}", json.dumps(r, ensure_ascii=False, separators=(",", ":")))
    assert "{{" not in html, html[html.index("{{"):html.index("{{") + 40]
    OUT.write_text(html, encoding="utf-8")
    print("写出", OUT, f"{len(html) / 1024:.0f} KB")


if __name__ == "__main__":
    main()
