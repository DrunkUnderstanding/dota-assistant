"""第 4 步：把 result.json 写成单文件网页 data/mockups/7.42-1到3号位打法.html（结论文字在上一级目录的 conclusions.json，看完数据手写）。

    python -X utf8 docs/research/role-play/scripts/d_page.py
"""
import html
import json
import math
import time
from pathlib import Path

from common import D

from dota_assistant.core.config import DATA_DIR

OUT = DATA_DIR / "mockups" / "7.42-1到3号位打法.html"
HEROES = {h["id"]: h for h in json.loads((DATA_DIR / "heroes.json").read_text(encoding="utf-8"))}
POS = ["POSITION_1", "POSITION_2", "POSITION_3"]
PN = {"POSITION_1": "1 号位", "POSITION_2": "2 号位（中路）", "POSITION_3": "3 号位（劣势路）"}
FEAT = {"kp": ("参与击杀", "每多参与 1 次击杀（击杀或助攻）"), "dth": ("死亡", "每多死 1 次"),
        "lh": ("正补", "每多补 10 个兵 / 野"), "push": ("站位靠前", "离自家泉水平均远 10%（往对面压）"),
        "tw": ("亲手推塔", "每多推掉 1 座塔（最后一下是自己）"), "rune": ("拿符", "每多拿 1 个赏金 / 神符 / 智慧符"),
        "stack": ("屯野", "每多屯 1 组野")}
WIN_NAME = {"0-10": "0～10 分钟", "10-20": "10～20 分钟", "20-30": "20～30 分钟"}
LANE_ORDER = ["大优", "优", "平", "劣", "大劣"]
E = html.escape


def sig(b, se):
    return abs(b) > 1.96 * se


def num(b, se, digits=1, good_if_neg=False):
    cls = ("pos" if (b > 0) != good_if_neg else "neg") if sig(b, se) else "flat"
    return f'<span class="v {cls}">{b:+.{digits}f}</span><span class="ci">±{1.96 * se:.{digits}f}</span>'


def delta(c, o, digits=1):
    d, s = c["b"] - o["b"], math.hypot(c["se"], o["se"])
    if not sig(d, s):
        return '<span class="flat">差不多</span>'
    # 不用「更值钱 / 没那么值钱」：死亡这种负数系数变得更负是「代价更大」，按正负说反而误导
    return f'<span class="v chg">比 7.41f {d:+.{digits}f}</span>'


def heroes_table(r42, r41, n_min=120):
    rows = []
    for h, (n, w) in r42["heroes"].items():
        if n < n_min:
            continue
        p = w / n
        o = r41["heroes"].get(h)
        rows.append((p, int(h), n, 1.96 * math.sqrt(p * (1 - p) / n), (o[1] / o[0]) if o and o[0] >= 100 else None))
    rows.sort(reverse=True)
    out = []
    for p, h, n, c, o in rows[:10]:
        old = f"{o:.1%}" if o is not None else "—"
        out.append(f"<tr><td>{E(HEROES.get(h, {}).get('name_cn', str(h)))}</td><td class='r'>{p:.1%} <span class='ci'>±{c * 100:.0f}</span></td>"
                   f"<td class='r'>{n}</td><td class='r muted'>{old}</td></tr>")
    return ("<table><thead><tr><th>英雄</th><th class='r'>7.42 胜率</th><th class='r'>人次</th><th class='r'>7.41f</th></tr></thead><tbody>"
            + "".join(out) + "</tbody></table>")


def role_section(pos, R42, R41, C):
    d42, d41 = R42["desc"]["roles"][pos], R41["desc"]["roles"][pos]
    h = [f'<section class="card role" id="{pos}"><h2>{PN[pos]}</h2>']
    c = C.get(pos) or {}
    if c.get("points"):
        h.append('<ol class="points">' + "".join(f"<li>{x}</li>" for x in c["points"]) + "</ol>")
    # 行为 → 胜率
    h.append('<h3>同样局势下，多做一点这件事，胜率差多少（百分点）</h3>')
    h.append('<p class="note">和对面同位置比，每个时间段一个模型；已控制时间段开始时的两队经济差和双方英雄。'
             '绿 / 红 = 超出误差范围；灰 = 分不出来。括号是 7.41f 同一个数。</p>')
    h.append('<div class="tw"><table><thead><tr><th>做了什么</th>' + "".join(f"<th class='r'>{WIN_NAME[w]}</th>" for w in WIN_NAME) + "</tr></thead><tbody>")
    feats = list(R42["win"][f"{pos}|0-10"]["coef"])
    for f in feats:
        cells = []
        for w in WIN_NAME:
            a, b = R42["win"][f"{pos}|{w}"]["coef"][f], R41["win"][f"{pos}|{w}"]["coef"][f]
            cells.append(f"<td class='r'>{num(a['b'], a['se'])}<div class='old'>({b['b']:+.1f}) {delta(a, b)}</div>"
                         f"<div class='old'>平均 {a['mean'] / (10 if f == 'lh' else 1):.1f}{'×10' if f == 'lh' else ''}</div></td>")
        h.append(f"<tr><td><b>{FEAT[f][0]}</b><div class='old'>{FEAT[f][1]}</div></td>{''.join(cells)}</tr>")
    h.append("</tbody></table></div>")
    # 翻盘
    a, b = R42["comeback"][pos], R41["comeback"][pos]
    if "coef" in a and "coef" in b:
        h.append(f'<h3>15 分钟落后 2000 以上：15～25 分钟这个位置做什么和翻盘有关</h3>'
                 f'<p class="note">7.42 {a["n"]} 局、翻盘 {a["base"]:.1%}（7.41f {b["n"]} 局、{b["base"]:.1%}）；控制了落后多少和双方英雄。</p>')
        h.append("<div class='tw'><table><thead><tr><th>做了什么</th><th class='r'>7.42</th><th class='r'>7.41f</th></tr></thead><tbody>")
        for f, x in a["coef"].items():
            y = b["coef"][f]
            h.append(f"<tr><td>{FEAT[f][1]}</td><td class='r'>{num(x['b'], x['se'])}</td><td class='r muted'>{y['b']:+.1f}</td></tr>")
        h.append("</tbody></table></div>")
    # 对线 / 节奏 / 钱来源
    lane = "".join(
        f"<tr><td>{k}</td><td class='r'>{d42['lane'][k]['win']:.0%} <span class='ci'>{d42['lane'][k]['n']}</span></td>"
        f"<td class='r muted'>{d41['lane'][k]['win']:.0%}</td></tr>" for k in LANE_ORDER if k in d42["lane"] and k in d41["lane"])
    fs42, fs41 = d42["farm_share"], d41["farm_share"]
    jg42 = fs42.get("neutralLocation", 0) + fs42.get("ancientLocation", 0)
    jg41 = fs41.get("neutralLocation", 0) + fs41.get("ancientLocation", 0)
    tempo = [("10 分钟经济", f"{d42['nw10']:.0f}", f"{d41['nw10']:.0f}"),
             ("20 分钟经济", f"{d42['nw20']:.0f}", f"{d41['nw20']:.0f}"),
             ("20 分钟占全队经济", f"{d42['share20']:.1%}", f"{d41['share20']:.1%}"),
             ("10～20 分钟正补", f"{d42['w10-20']['lh']:.0f}", f"{d41['w10-20']['lh']:.0f}"),
             ("10～20 分钟参与击杀", f"{d42['w10-20']['kp']:.1f}", f"{d41['w10-20']['kp']:.1f}"),
             ("整局钱来自野区（含远古）", f"{jg42:.0%}", f"{jg41:.0%}"),
             ("第一件大件（中位分钟）", f"{d42['big1_min']:.1f}", f"{d41['big1_min']:.1f}"),
             ("第二件大件（中位分钟）", f"{d42['big2_min']:.1f}", f"{d41['big2_min']:.1f}"),
             ("出黑皇杖的比例 / 中位分钟", f"{d42['bkb_share']:.0%} / {d42['bkb_min'] or 0:.0f}", f"{d41['bkb_share']:.0%} / {d41['bkb_min'] or 0:.0f}")]
    tt = "".join(f"<tr><td>{a}</td><td class='r'>{b}</td><td class='r muted'>{c}</td></tr>" for a, b, c in tempo)
    types = "".join(f"<tr><td>{k}</td><td class='r'>{d42['types'][k]['win']:.1%} <span class='ci'>{d42['types'][k]['n']}</span></td>"
                    f"<td class='r muted'>{d41['types'][k]['win']:.1%}</td></tr>" for k in ("近战", "远程", "法系爆发", "非法系")
                    if k in d42["types"] and k in d41["types"])
    fb = " ".join(f"{E(x['name'])} {x['share']:.0%}" for x in d42["first_big"][:6])
    h.append('<div class="grid3">'
             f"<div><h3>节奏（7.42 / 7.41f）</h3><table><tbody>{tt}</tbody></table><p class='note'>第一件大件最常见：{fb}</p></div>"
             f"<div><h3>对线结果 → 这个人最后的胜率</h3><table><thead><tr><th>对线</th><th class='r'>7.42 <span class='ci'>人次</span></th><th class='r'>7.41f</th></tr></thead><tbody>{lane}</tbody></table>"
             f"<h3>英雄类型 → 胜率</h3><table><tbody>{types}</tbody></table><p class='note'>法系爆发 = 英雄标签带 Nuker。</p></div>"
             f"<div><h3>这个位置 7.42 胜率高的英雄（本样本）</h3>{heroes_table(d42, d41)}<p class='note'>只列 ≥120 人次的；样本两天，±5～9 个点，只当参考，选人以助手里的数据为准。</p></div>"
             "</div>")
    if c.get("examples"):
        h.append("<h3>实际对局</h3><div class='ex'>" + "".join(c["examples"]) + "</div>")
    h.append("</section>")
    return "\n".join(h)


def env_section(R42, R41, C):
    a, b = R42["desc"], R41["desc"]
    rows = [("局数", f"{a['n']}", f"{b['n']}"),
            ("平均 / 中位时长（分）", f"{a['dur']['mean']:.1f} / {a['dur']['median']:.1f}", f"{b['dur']['mean']:.1f} / {b['dur']['median']:.1f}"),
            ("30 分钟内结束", f"{a['dur']['lt30']:.0%}", f"{b['dur']['lt30']:.0%}"),
            ("打到 50 分钟以上", f"{a['dur']['ge50']:.0%}", f"{b['dur']['ge50']:.0%}"),
            ("每分钟击杀", f"{a['kpm']:.2f}", f"{b['kpm']:.2f}"),
            ("前 10 分钟击杀（两队合计）", f"{a['kills10']:.1f}", f"{b['kills10']:.1f}"),
            ("先推掉一座塔的一方胜率", f"{a['first_tower_win']:.1%}", f"{b['first_tower_win']:.1%}")]
    tt = "".join(f"<tr><td>{x}</td><td class='r'>{y}</td><td class='r muted'>{z}</td></tr>" for x, y, z in rows)
    cb = []
    for minute in (10, 15, 20, 25, 30):
        cells = []
        for L in (2000, 5000, 10000):
            x, y = a["comeback"][f"{minute}_{L}"], b["comeback"][f"{minute}_{L}"]
            if x["p"] is None or y["p"] is None:
                cells.append("<td class='r muted'>—</td>")
                continue
            cells.append(f"<td class='r'>{x['p']:.0%} <span class='muted'>({y['p']:.0%})</span><div class='old'>落后方翻盘 {1 - x['p']:.0%}</div></td>")
        cb.append(f"<tr><td>第 {minute} 分钟</td>{''.join(cells)}</tr>")
    gap = []
    for minute in ("10", "15", "20"):
        g42, g41 = R42["gap"][minute]["coef"], R41["gap"][minute]["coef"]
        cells = "".join(f"<td class='r'>{num(g42[j]['b'], g42[j]['se'])}<div class='old'>({g41[j]['b']:+.1f}) {delta(g42[j], g41[j])}</div></td>" for j in range(5))
        gap.append(f"<tr><td>第 {minute} 分钟</td>{cells}</tr>")
    sh42, sh41 = R42["share"]["coef"], R41["share"]["coef"]
    share = "".join(f"<td class='r'>{num(sh42[j]['b'], sh42[j]['se'])}<div class='old'>({sh41[j]['b']:+.1f})</div></td>" for j in range(3))
    pts = "".join(f"<li>{x}</li>" for x in C.get("env", []))
    return f"""<section class="card" id="env"><h2>版本大环境：7.42 和 7.41f 比</h2>
<ol class="points">{pts}</ol>
<div class="grid2"><div><table><thead><tr><th></th><th class='r'>7.42</th><th class='r'>7.41f</th></tr></thead><tbody>{tt}</tbody></table></div>
<div><h3>第 N 分钟领先的一方最后赢的比例（括号 7.41f）</h3><div class='tw'><table><thead><tr><th></th><th class='r'>领先 ≥2000</th><th class='r'>≥5000</th><th class='r'>≥10000</th></tr></thead><tbody>{''.join(cb)}</tbody></table></div></div></div>
<h3>哪个位置的经济差最决定胜负：和对面同位置每多 1000 经济，胜率 +几个点</h3>
<p class="note">五个位置的经济差一起放进模型（互相控制）。括号 7.41f。</p>
<div class='tw'><table><thead><tr><th></th><th class='r'>1 号位</th><th class='r'>2 号位</th><th class='r'>3 号位</th><th class='r'>4 号位</th><th class='r'>5 号位</th></tr></thead><tbody>{''.join(gap)}</tbody></table></div>
<h3>全队经济一样多时，钱多分给谁：第 20 分钟占全队经济每多 5 个百分点（从 4、5 号位那里挪过来）</h3>
<div class='tw'><table><thead><tr><th></th><th class='r'>1 号位</th><th class='r'>2 号位</th><th class='r'>3 号位</th></tr></thead><tbody><tr><td>胜率 +几个点</td>{share}</tr></tbody></table></div>
</section>"""


CSS = """:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;
--border:rgba(11,11,11,.10);--wash:rgba(11,11,11,.04);--accent:#2a78d6;--up:#006300;--down:#d03b3b}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;
--muted:#898781;--grid:#2c2c2a;--border:rgba(255,255,255,.10);--wash:rgba(255,255,255,.05);--accent:#3987e5;--up:#0ca30c;--down:#e66767}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;
--border:rgba(255,255,255,.10);--wash:rgba(255,255,255,.05);--accent:#3987e5;--up:#0ca30c;--down:#e66767}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.6 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:24px 16px 48px;display:flex;flex-direction:column;gap:16px}
h1{font-size:22px;margin:0}h2{font-size:18px;margin:0 0 8px}h3{font-size:14px;margin:16px 0 6px}
.sub{margin:4px 0 0;color:var(--ink2)}
nav a{color:var(--accent);margin-right:14px;text-decoration:none}
.card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px 16px;min-width:0}
.points{margin:4px 0 8px;padding-left:22px}.points li{margin:6px 0}
.note{color:var(--ink2);font-size:12.5px;margin:2px 0 6px}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:5px 8px;border-bottom:1px solid var(--grid);text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12.5px}
.r{text-align:right}.muted{color:var(--muted)}
.tw{overflow-x:auto}
.v{font-weight:700}.v.pos{color:var(--up)}.v.neg{color:var(--down)}.v.chg{color:var(--accent)}.flat{color:var(--muted)}
.ci{color:var(--muted);font-size:11.5px;margin-left:3px;font-weight:400}
.old{color:var(--muted);font-size:11.5px;font-weight:400}
.grid2{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.4fr);gap:16px}
.grid3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}
.ex{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px}
.ex .m{border:1px solid var(--border);border-radius:6px;padding:8px 10px;background:var(--page);font-size:13px}
.ex .m a{color:var(--accent)}
.foot{color:var(--ink2);font-size:13px}.foot p{margin:4px 0}
@media (max-width:760px){.grid2,.grid3{grid-template-columns:1fr}}"""


def die_section(C) -> str:
    """玩家追问「多参团了怎么少死」：f_fight.py / g_how.py 的结果。"""
    fp, hp = D / "fight.json", D / "how.json"
    if not (fp.exists() and hp.exists()):
        return ""
    F, H = json.loads(fp.read_text(encoding="utf-8")), json.loads(hp.read_text(encoding="utf-8"))
    pts = "".join(f"<li>{x}</li>" for x in C.get("die", []))
    # 表 1：哪种死法伤害大
    t1 = "".join(
        f"<tr><td>{PN[p]}</td>" + "".join(
            f"<td class='r'>{num(F['742'][p]['win'][k]['b'], F['742'][p]['win'][k]['se'])}<div class='old'>({F['741'][p]['win'][k]['b']:+.1f})</div></td>"
            for k in ("团战里拿到 1 个击杀 / 助攻", "团战里死 1 次", "被单抓死 1 次")) + "</tr>" for p in POS)
    # 表 2：大团里自己什么时候到
    order = ["对面先倒，自己跟上", "自己先拿到击杀 / 助攻", "队友先倒，自己后到", "自己第一个倒下"]
    t2 = "".join(
        f"<tr><td>{k}</td>" + "".join(
            f"<td class='r'>{H['742'][p]['tf']['tf'][k]['died']:.0%} / {H['742'][p]['tf']['tf'][k]['won']:.0%}"
            f"<div class='old'>占 {H['742'][p]['tf']['tf'][k]['share']:.0%}</div></td>" for p in POS) + "</tr>" for k in order)
    # 表 3：同一波里什么让人少死
    keys = list(H["742"]["POSITION_1"]["fight"]["coef"])[2:]
    t3 = "".join(
        f"<tr><td>{k}</td>" + "".join(
            f"<td class='r'>{num(H['742'][p]['fight']['coef'][k]['b'], H['742'][p]['fight']['coef'][k]['se'], good_if_neg=True)}</td>" for p in POS) + "</tr>"
        for k in keys)
    # 表 4：参团多的人里，少死的和多死的差在哪
    rows4 = [("10～30 分钟胜率", "win", "{:.0%}"), ("参了几波团", "fights", "{:.1f}"), ("拿到击杀 / 助攻", "kp", "{:.1f}"),
             ("团里死几次", "died", "{:.1f}"), ("被单抓死几次", "solo", "{:.2f}"), ("打架在对面半场的比例", "enemyhalf", "{:.0%}"),
             ("打架时身上有保命装", "save", "{:.0%}"), ("10～30 分钟正补", "lh", "{:.0f}"), ("10 分钟时全队经济差", "lead10", "{:+.0f}")]
    t4 = ""
    for lab, k, fmt in rows4:
        t4 += f"<tr><td>{lab}</td>" + "".join(
            f"<td class='r'>{fmt.format(H['742'][p]['people']['参团多·少死'][k])}</td><td class='r muted'>{fmt.format(H['742'][p]['people']['参团多·多死'][k])}</td>"
            for p in POS) + "</tr>"
    head3 = "".join(f"<th class='r'>{PN[p].split('（')[0]}</th>" for p in POS)
    head4 = "".join(f"<th class='r'>{PN[p].split('（')[0]} 少死</th><th class='r'>多死</th>" for p in POS)
    return f"""<section class="card" id="die"><h2>多参团了，怎么少死</h2>
<ol class="points">{pts}</ol>
<h3>哪种死法最伤：10～30 分钟每多一次，胜率差几个点（括号 7.41f）</h3>
<p class="note">一波 = 20 秒内、相距不远的一串英雄死亡；只有自己死、对面没人死、身边最多一个队友的算「被单抓」，其余算「团战里死」。已控制 10 分钟时的经济差和双方英雄。</p>
<div class="tw"><table><thead><tr><th></th><th class='r'>团战里拿 1 个击杀 / 助攻</th><th class='r'>团战里死 1 次</th><th class='r'>被单抓死 1 次</th></tr></thead><tbody>{t1}</tbody></table></div>
<h3>大团（这一波死 ≥3 人）里自己什么时候到：自己死的比例 / 这一波赢的比例</h3>
<div class="tw"><table><thead><tr><th>自己第一次出现在击杀记录里时</th>{head3}</tr></thead><tbody>{t2}</tbody></table></div>
<h3>参了同一波团，什么让人少死：死的概率差几个点（绿 = 更不容易死）</h3>
<p class="note">已控制自己的经济、全队经济差、第几分钟、英雄。「附近」= 开打前 5 秒离这波中心约 4000 码以内（站位采样）。</p>
<div class="tw"><table><thead><tr><th></th>{head3}</tr></thead><tbody>{t3}</tbody></table></div>
<h3>10～30 分钟参团多的人（拿到击杀 / 助攻在中位数以上）：少死的一半 vs 多死的一半</h3>
<div class="tw"><table><thead><tr><th></th>{head4}</tr></thead><tbody>{t4}</tbody></table></div>
</section>"""


def main() -> None:
    res = json.loads((D / "result.json").read_text(encoding="utf-8"))
    cpath = Path(__file__).resolve().parents[1] / "conclusions.json"     # 看完数据手写的结论，进 git
    C = json.loads(cpath.read_text(encoding="utf-8")) if cpath.exists() else {}
    R42, R41 = res["vers"]["742"], res["vers"]["741"]
    body = [f"""<header><h1>7.42 的 1 / 2 / 3 号位怎么打更容易赢</h1>
<p class="sub">实际对局：7.42 上线（11-19 05:00 北京时间）后超凡～冠绝天梯全英雄选择 {R42['desc']['n']:,} 局，对照 7.41f 同分段 {R41['desc']['n']:,} 局；
都是随机抽的、≥15 分钟、10 人都有位置。数据来自 OpenDota（抽编号）+ STRATZ（每局击杀 / 死亡时间、每分钟经济补刀、站位、推塔、对线结果、出装）。整理于 {time.strftime('%m-%d %H:%M')}。</p>
<p class="sub">{C.get('lead', '')}</p>
<nav><a href="#env">版本大环境</a><a href="#POSITION_1">1 号位</a><a href="#POSITION_2">2 号位</a><a href="#POSITION_3">3 号位</a><a href="#die">多参团怎么少死</a><a href="#foot">怎么算的 / 局限</a></nav></header>"""]
    body.append(env_section(R42, R41, C))
    for pos in POS:
        body.append(role_section(pos, R42, R41, C))
    body.append(die_section(C))
    foot = "".join(f"<p>{x}</p>" for x in C.get("foot", []))
    body.append(f'<footer class="card foot" id="foot"><h2>怎么算的 / 局限</h2>{foot}</footer>')
    page = (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>7.42 号位打法</title><style>{CSS}</style></head><body><div class="wrap">{"".join(body)}</div></body></html>')
    OUT.write_text(page, encoding="utf-8")
    print(OUT, len(page))


if __name__ == "__main__":
    main()
