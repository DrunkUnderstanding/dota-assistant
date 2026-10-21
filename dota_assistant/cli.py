"""命令行入口，python -m dota_assistant <命令>：

  update [--player]            拉取 / 刷新数据（--player 只刷新个人数据，几秒）
  suggest --enemy PA,斧王 --ally 冰女 --ban 敌法师 [--pos 1]    命令行里手动推荐
  run                          实时助手：叠加层 + 语音 + 控制面板
  calibrate <截图>             在选人截图上框出英雄格子
  recognize <截图>             用当前校准识别一张截图（调试用），输出标注图
  label <截图> [更多截图…]      逐格确认 / 纠正识别结果，存成学习模板
  gsi-install <Dota 安装目录>  写入 Game State Integration 配置
  say <文字>                   测试语音
  backtest [--matches N]       用统计之后的新对局检验推荐和胜负的关系
  kb hero <英雄> / kb item <物品> / kb find <关键词或正则> / kb update [--force]
                               查当前版本官方技能 / 物品说明
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config, data


def _split(s: str | None) -> list[str]:
    if not s:
        return []
    return [x.strip() for x in s.replace("，", ",").replace("、", ",").split(",") if x.strip()]


def cmd_suggest(cfg: config.Config, args) -> int:
    from . import advisor as adv
    if args.all_roles:
        cfg.position_filter = False
    if args.top:
        cfg.top = args.top
    a = adv.Advisor(cfg)
    try:
        enemies, allies, bans = (a.db.find_many(_split(x)) for x in (args.enemy, args.ally, args.ban))
    except KeyError as e:
        print(e.args[0])
        return 1
    pos = args.pos or cfg.position
    bracket = (f"{data.MEDALS[cfg.rank_min]}~{data.MEDALS[cfg.rank_max]}"
               if cfg.rank_min and cfg.rank_max else "全段位")
    who = f" | {a.me['name']}（{data.rank_name(a.me['rank_tier'])}）" if a.me else ""
    print(f"数据：最近 {cfg.days} 天 {a.engine.matches} 局天梯（{bracket}）{who}")
    for kind, text in adv.render_lines(a.advise(adv.Draft(allies, enemies, bans), pos), None):
        if kind == "title":
            continue
        print(f"【{text}】" if kind == "head" else ("  " + text if kind == "row" else text))
    return 0


def cmd_recognize(cfg: config.Config, path: Path) -> int:
    import cv2
    from .vision import Calibration, TemplateBank, read_image, recognize, write_image
    cal = Calibration.load()
    if not (cal and cal.ready):
        print("还没校准：先运行 python -m dota_assistant calibrate <截图>")
        return 1
    db = data.load_heroes()
    bank = TemplateBank()
    img = read_image(path)
    view = recognize(img, cal, bank, cfg.match_threshold)
    out = img.copy()
    h, w = img.shape[:2]
    for side, rects in (("radiant", cal.radiant), ("dire", cal.dire), ("bans", cal.bans)):
        names = []
        for r, s in zip(rects, getattr(view, side)):
            hero = db.by_id.get(s.guess)
            ok = s.hero_id is not None
            names.append(f"{hero.name_cn if hero else '空'}{'' if ok else '?'}({s.score:.2f})")
            x, y, rw, rh = int(r[0] * w), int(r[1] * h), int(r[2] * w), int(r[3] * h)
            cv2.rectangle(out, (x, y), (x + rw, y + rh), (80, 220, 80) if ok else (80, 80, 240), 2)
            cv2.putText(out, (hero.key if hero else "empty")[:12], (x, y + rh + 16),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        if names:
            print(f"{side:8}", "  ".join(names))
    preview = path.with_name(path.stem + "_识别.png")
    write_image(preview, out)
    print(f"（? = 低于阈值 {cfg.match_threshold}，按空位处理）标注图：{preview}")
    return 0


def cmd_label(cfg: config.Config, paths: list[Path]) -> int:
    from .vision import EMPTY, Calibration, TemplateBank, read_image, recognize
    cal = Calibration.load()
    if not (cal and cal.ready):
        print("还没校准：先运行 python -m dota_assistant calibrate <截图>")
        return 1
    db = data.load_heroes()
    bank = TemplateBank()
    print("每个格子：直接回车 = 认同猜测；输入英雄名 = 纠正；输入 0 = 空位；输入 - = 跳过这个格子")
    saved = 0
    for path in paths:
        print(f"\n== {path.name}")
        view = recognize(read_image(path), cal, bank, cfg.match_threshold, keep_images=True)
        for side, cn in (("radiant", "天辉"), ("dire", "夜魇")):
            for i, s in enumerate(getattr(view, side), 1):
                guess = db.by_id.get(s.guess)
                label = guess.name_cn if guess and s.hero_id else "空位"
                ans = input(f"  {cn}{i} 猜：{label}（{s.score:.2f}）> ").strip()
                if ans == "-":
                    continue
                if ans == "0" or (not ans and not s.hero_id):
                    hid = EMPTY
                elif not ans:
                    hid = s.hero_id
                else:
                    try:
                        hid = db.find(ans).id
                    except KeyError as e:
                        print("   ", e.args[0], "—— 跳过")
                        continue
                bank.learn(hid, s.image)
                saved += 1
    print(f"\n存了 {saved} 个模板，学习模板共 {bank.learned} 张")
    return 0


def cmd_kb(cfg: config.Config, args) -> int:
    from . import kb
    if args.action == "update":
        kb.update([h.id for h in data.load_heroes()], force=args.force)
        return 0
    base = kb.KnowledgeBase.load()
    if base is None:
        print("还没有知识库，先运行：python -m dota_assistant kb update")
        return 1
    rules = kb.load_rules()
    q = " ".join(args.query)
    try:
        if args.action == "hero":
            print(kb.render_hero(base.heroes[data.load_heroes().find(q).id], rules, base.patch))
        elif args.action == "item":
            print(kb.render_item(base.item(q), rules, base.patch))
        else:
            hits = base.search(q)
            for who, ab, sent in hits[:80]:
                print(f"{who} · {ab}：{sent}")
            print(f"（共 {len(hits)} 条{'，只显示前 80' if len(hits) > 80 else ''}）")
    except KeyError as e:
        print(e.args[0])
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="dota_assistant", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("update", help="拉取英雄表、头像、克制 / 配合数据、个人数据")
    up.add_argument("--player", action="store_true", help="只刷新个人段位与英雄使用记录")
    sp = sub.add_parser("suggest", help="根据手动输入的选人局面推荐英雄")
    sp.add_argument("--enemy", help="敌方已选，逗号分隔")
    sp.add_argument("--ally", help="我方已选（不含自己），逗号分隔")
    sp.add_argument("--ban", help="已禁用，逗号分隔")
    sp.add_argument("--pos", type=int, choices=range(1, 6), help="我的位置，默认取 config.toml")
    sp.add_argument("--top", type=int)
    sp.add_argument("--all-roles", action="store_true", help="不按位置筛选")
    sub.add_parser("run", help="实时助手")
    cp = sub.add_parser("calibrate", help="在选人截图上框出英雄格子")
    cp.add_argument("image", type=Path)
    rp = sub.add_parser("recognize", help="识别一张截图（调试）")
    rp.add_argument("image", type=Path)
    lp = sub.add_parser("label", help="逐格纠正识别结果并存成模板")
    lp.add_argument("images", type=Path, nargs="+")
    gp = sub.add_parser("gsi-install", help="写入 GSI 配置文件")
    gp.add_argument("dota_dir", type=Path, help=r"例如 C:\Program Files (x86)\Steam\steamapps\common\dota 2 beta")
    bp = sub.add_parser("backtest", help="样本外回测")
    bp.add_argument("--matches", type=int, default=3000)
    bp.add_argument("--tune", action="store_true", help="网格搜对线权重 / 反制风险权重（一半调参一半验证）")
    kp = sub.add_parser("kb", help="当前版本知识库")
    kp.add_argument("action", choices=["hero", "item", "find", "update"])
    kp.add_argument("query", nargs="*")
    kp.add_argument("--force", action="store_true", help="update 时即使版本没变也重拉")
    yp = sub.add_parser("say", help="测试语音")
    yp.add_argument("text", nargs="+")
    args = ap.parse_args(argv)
    cfg = config.load()

    if args.cmd == "update":
        (data.update_player if args.player else data.update)(cfg)
        return 0
    if args.cmd == "suggest":
        return cmd_suggest(cfg, args)
    if args.cmd == "run":
        from .ui import run
        return run(cfg)
    if args.cmd == "calibrate":
        from .calibrate import calibrate
        return 0 if calibrate(args.image) else 1
    if args.cmd == "recognize":
        return cmd_recognize(cfg, args.image)
    if args.cmd == "label":
        import glob
        # Windows 的 cmd 不展开通配符，这里自己展开
        paths = [Path(x) for pat in args.images for x in (glob.glob(str(pat)) or [str(pat)])]
        return cmd_label(cfg, paths)
    if args.cmd == "gsi-install":
        from .gsi import install_cfg
        path = install_cfg(args.dota_dir, cfg.gsi_port)
        print(f"已写入 {path}\n还需要在 Steam → Dota 2 → 属性 → 启动选项 里加上：-gamestateintegration")
        return 0
    if args.cmd == "backtest":
        from .backtest import run as backtest
        print(backtest(cfg, args.matches, do_tune=args.tune))
        return 0
    if args.cmd == "kb":
        return cmd_kb(cfg, args)
    if args.cmd == "say":
        import time
        from .speech import Speaker
        s = Speaker(cfg.voice_rate)
        s.say(" ".join(args.text))
        time.sleep(max(2.0, len("".join(args.text)) * 0.25))
        s.close()
        return 0
    return 1
