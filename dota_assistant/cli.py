"""命令行入口，python -m dota_assistant <命令>：

  update [--player]            拉取 / 刷新数据（--player 只刷新个人数据，几秒）
  suggest --enemy PA,斧王 --ally 冰女 --ban 敌法师 [--pos 1]    命令行里手动推荐
          [--me 卓尔 --side radiant]                          已选好英雄：看对面分路 + 我这路的对线提示
  start                        一键启动（「启动助手.bat」就是调这个）：自动补齐配置 / 界面组件 / GSI / 数据再开助手
  run [--browser]              实时助手：叠加层 + 语音 + 控制面板（界面用 Electron；--browser 浏览器预览）
  demo [--browser]             不开 Dota 演示叠加层和语音（按真实对局逐个选人，再弹局内提醒）
  calibrate <截图>             在选人截图上框出英雄格子
  recognize <截图>             用当前校准识别一张截图（调试用），输出标注图
  label <截图> [更多截图…]      逐格确认 / 纠正识别结果，存成学习模板
  gsi-install <Dota 安装目录>  写入 Game State Integration 配置
  gsi-dump                     只收 GSI 推送并存档（确认选人时 GSI 给了什么）
  say <文字>                   测试语音
  backtest [--matches N]       用统计之后的新对局检验推荐和胜负的关系
  lanes-test [--matches N]     用同一批对局检验「对面分路」猜得准不准
  lane-test [--matches N]      回测：3 / 6 分钟时看击杀 / 死亡 / 等级能不能猜出对线优劣；屯野 / 莲花和对线结果
  lane-review                  实战记录（3 / 6 / 10 分钟自己的数据）对照 STRATZ 赛后的对线结果
  backtest-ai [--moments N]    大模型选人盲测（用 Claude Code 订阅，和统计引擎同题对比）
  coach-sim <比赛编号...>       拿已解析的录像重放，看「局内缺口提醒」当时会说什么
  death-sim [文件...]           拿 shots/deaths/ 里存下的真实死亡重放，看「按死法补装」会说什么（默认全部）
  kb hero <英雄> / kb item <物品> / kb find <关键词或正则> / kb update [--force]
                               查当前版本官方技能 / 物品说明
  kb mistake [--wrong 错误说法 --right 正确说法 --hero X --item Y --who 开发助手 --evidence 原文 --has-tag 标签]
                               列出 / 记录 AI 常见误判（knowledge/mistakes.toml）
  logs [-n 行数] [--open]       看最新一份日志的末尾（--open 打开日志文件夹）

运行类命令（start / run / demo / update …）每次在 logs/ 下写一个日志文件，见 log.py。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .app import update
from .core import config
from .core import log as logs
from .sources import assets, player
from .sources import stats as stats_mod

# 这些命令写日志文件；查询类命令（suggest / kb / say …）只在控制台显示警告，免得把有用的日志挤掉
FILE_LOG_COMMANDS = {"start", "run", "demo", "update", "calibrate", "label", "gsi-dump", "backtest", "backtest-ai",
                     "coach-sim", "brief-sim", "lanes-test", "lane-test"}


def _split(s: str | None) -> list[str]:
    if not s:
        return []
    return [x.strip() for x in s.replace("，", ",").replace("、", ",").split(",") if x.strip()]


def cmd_suggest(cfg: config.Config, args) -> int:
    from .domain import advisor as adv
    if args.all_roles:
        cfg.position_filter = False
    if args.top:
        cfg.top = args.top
    a = adv.Advisor(cfg)
    try:
        enemies, allies, bans = (a.db.find_many(_split(x)) for x in (args.enemy, args.ally, args.ban))
        me = a.db.find(args.me) if args.me else None
    except KeyError as e:
        print(e.args[0])
        return 1
    pos = args.pos or cfg.position
    bracket = (f"{player.MEDALS[cfg.rank_min]}~{player.MEDALS[cfg.rank_max]}"
               if cfg.rank_min and cfg.rank_max else "全段位")
    who = f" | {a.me['name']}（{player.rank_name(a.me['rank_tier'])}）" if a.me else ""
    print(f"数据：最近 {cfg.days} 天 {a.engine.matches} 局天梯（{bracket}）{who}")
    for kind, text in adv.render_lines(a.advise(adv.Draft(allies, enemies, bans, me), pos), args.side):
        if kind == "title":
            continue
        print(f"【{text}】" if kind == "head" else ("  " + text if kind == "row" else text))
    return 0


def cmd_recognize(cfg: config.Config, path: Path) -> int:
    import cv2
    from .vision.recognize import SURE_GAP, SURE_SCORE, Calibration, TemplateBank, read_image, recognize, write_image
    cal = Calibration.load()
    if not (cal and cal.ready):
        print("还没校准：先运行 python -m dota_assistant calibrate <截图>")
        return 1
    db = assets.load_heroes()
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
            names.append(f"{hero.name_cn if hero else '空'}{'' if ok else '?'}({s.score:.2f}/+{s.gap:.2f})")
            x, y, rw, rh = int(r[0] * w), int(r[1] * h), int(r[2] * w), int(r[3] * h)
            cv2.rectangle(out, (x, y), (x + rw, y + rh), (80, 220, 80) if ok else (80, 80, 240), 2)
            cv2.putText(out, (hero.key if hero else "empty")[:12], (x, y + rh + 16),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        if names:
            print(f"{side:8}", "  ".join(names))
    preview = path.with_name(path.stem + "_识别.png")
    write_image(preview, out)
    print(f"（括号里：相似度 / 比第二像的别的英雄高出多少。? = 没认出，按空位处理：要高出 {SURE_GAP} 以上、"
          f"相似度到 {min(cfg.match_threshold, SURE_SCORE)}）标注图：{preview}")
    return 0


def cmd_label(cfg: config.Config, paths: list[Path]) -> int:
    from .vision.recognize import EMPTY, Calibration, TemplateBank, read_image, recognize
    cal = Calibration.load()
    if not (cal and cal.ready):
        print("还没校准：先运行 python -m dota_assistant calibrate <截图>")
        return 1
    db = assets.load_heroes()
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
        kb.update([h.id for h in assets.load_heroes()], force=args.force)
        return 0
    base = kb.KnowledgeBase.load()
    if base is None:
        print("还没有知识库，先运行：python -m dota_assistant kb update")
        return 1
    rules, mistakes = kb.load_rules(), kb.load_mistakes()
    q = " ".join(args.query)
    try:
        if args.action == "hero":
            print(kb.render_hero(base.heroes[assets.load_heroes().find(q).id], rules, base.patch, mistakes))
        elif args.action == "item":
            print(kb.render_item(base.item(q), rules, base.patch, mistakes))
        elif args.action == "mistake":
            return _kb_mistake(base, mistakes, args)
        else:
            hits = base.search(q)
            for who, ab, sent in hits[:80]:
                print(f"{who} · {ab}：{sent}")
            print(f"（共 {len(hits)} 条{'，只显示前 80' if len(hits) > 80 else ''}）")
    except KeyError as e:
        print(e.args[0])
        return 1
    return 0


def _kb_mistake(base, mistakes, args) -> int:
    """没给 --wrong 就列出全部；给了就记一条。英雄 / 物品名换成知识库里的正式名，查不到会报错"""
    import datetime
    from . import kb
    if not args.wrong:
        for m in mistakes:
            about = "、".join(x for x in (m.hero, m.item) if x) or "通用"
            print(f"[{about}] {m.date} {m.who}\n  ✗ {m.wrong}\n  ✓ {m.right}" + (f"\n  依据：{m.evidence}" if m.evidence else ""))
        print(f"（共 {len(mistakes)} 条，{kb.MISTAKES_PATH}）")
        return 0
    if not args.right:
        print("要同时给 --right（查原文后的正确说法）")
        return 1
    m = kb.Mistake(args.wrong, args.right, who=args.who, date=datetime.date.today().isoformat(),
                   evidence=args.evidence, has_tag=args.has_tag)
    if args.hero:
        m.hero = base.heroes[assets.load_heroes().find(args.hero).id].name
    if args.item:
        m.item = base.item(args.item).name
    if m.has_tag:
        tags = base.heroes[assets.load_heroes().find(m.hero).id].tags() if m.hero else base.item(m.item).tags() if m.item else {}
        if m.has_tag not in tags:
            print(f"「{m.hero or m.item or '通用'}」没有「{m.has_tag}」标签，没记。现有标签：{'、'.join(tags) or '无'}")
            return 1
    kb.add_mistake(m)
    print(f"已记到 {kb.MISTAKES_PATH}")
    return 0


def cmd_logs(args) -> int:
    if args.open:
        print(f"已打开 {logs.open_dir()}")
        return 0
    path = logs.latest()
    if path is None:
        print(f"还没有日志（{logs.LOG_DIR}）")
        return 1
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    print(f"== {path}（共 {len(lines)} 行，下面是最后 {min(args.n, len(lines))} 行）")
    print("\n".join(lines[-args.n:]))
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):                 # 被重定向到管道 / 文件时默认是 GBK
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
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
    sp.add_argument("--me", help="我已经选了的英雄：不再推荐，改给对面分路 + 我这路的对线提示")
    sp.add_argument("--side", choices=("radiant", "dire"), help="我方阵营（分路显示成上 / 中 / 下路）")
    sub.add_parser("start", help="一键启动：自动准备好一切再开助手（启动助手.bat 用的就是它）")
    up = sub.add_parser("run", help="实时助手")
    up.add_argument("--browser", action="store_true", help="不开 Electron，用浏览器打开界面（叠加层只是预览）")
    dp = sub.add_parser("demo", help="演示叠加层和语音")
    dp.add_argument("--step", type=float, default=4.0, help="每一步间隔秒数")
    dp.add_argument("--browser", action="store_true", help="不开 Electron，用浏览器打开界面")
    dp.add_argument("--speed", type=float, default=60, help="局内部分每真实秒走多少游戏秒（60 = 1 秒 1 分钟）")
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
    lt = sub.add_parser("lanes-test", help="回测：对面分路猜得准不准")
    lt.add_argument("--matches", type=int, default=3000)
    lv = sub.add_parser("lane-test", help="回测：3 / 6 分钟时看击杀 / 死亡 / 等级能不能猜出对线优劣，屯野 / 莲花和对线结果")
    lv.add_argument("--matches", type=int, default=3000)
    sub.add_parser("lane-review", help="实战记录：3 / 6 / 10 分钟记下的自己数据，对照 STRATZ 赛后的对线结果")
    ap2 = sub.add_parser("backtest-ai", help="大模型选人盲测")
    ap2.add_argument("--moments", type=int, default=300)
    ap2.add_argument("--model", default="claude-opus-5-5")
    ap2.add_argument("--workers", type=int, default=3)
    cs = sub.add_parser("coach-sim", help="局内缺口提醒：录像重放")
    cs.add_argument("match_ids", type=int, nargs="+")
    dp = sub.add_parser("death-sim", help="死亡复盘 + 按死法补装：拿存下的真实死亡推送重放")
    dp.add_argument("files", type=Path, nargs="*")
    bs = sub.add_parser("brief-sim", help="本局要点：拿录像看选完人时会给什么")
    bs.add_argument("match_ids", type=int, nargs="+")
    bs.add_argument("--ai", action="store_true", help="同时让大模型写前 / 中 / 后期打法（走 Claude Code 订阅）")
    kp = sub.add_parser("kb", help="当前版本知识库")
    kp.add_argument("action", choices=["hero", "item", "find", "update", "mistake"])
    kp.add_argument("query", nargs="*")
    kp.add_argument("--force", action="store_true", help="update 时即使版本没变也重拉")
    kp.add_argument("--wrong", default="", help="mistake：AI 的错误说法（不给就列出全部误判）")
    kp.add_argument("--right", default="", help="mistake：查原文后的正确说法")
    kp.add_argument("--hero", default="", help="mistake：涉及的英雄")
    kp.add_argument("--item", default="", help="mistake：涉及的物品")
    kp.add_argument("--who", default="打法页 AI", help="mistake：谁说错的（打法页 AI / 开发助手）")
    kp.add_argument("--evidence", default="", help="mistake：官方原文依据")
    kp.add_argument("--has-tag", default="", help="mistake：英雄 / 物品确实有的机制标签，单元测试逐版本核对")
    sub.add_parser("gsi-dump", help="记录 GSI 推送内容")
    yp = sub.add_parser("say", help="测试语音")
    yp.add_argument("text", nargs="+")
    op = sub.add_parser("logs", help="看最新的日志")
    op.add_argument("-n", type=int, default=60, help="显示最后多少行")
    op.add_argument("--open", action="store_true", help="打开日志文件夹")
    args = ap.parse_args(argv)
    cfg = config.load()
    logs.setup(cfg, args.cmd, to_file=args.cmd in FILE_LOG_COMMANDS
               or (args.cmd == "kb" and args.action == "update"))

    if args.cmd == "update":
        (player.update_player if args.player else update.update)(cfg)
        return 0
    if args.cmd == "suggest":
        return cmd_suggest(cfg, args)
    if args.cmd == "start":
        from .start import main as start
        return start()
    if args.cmd == "run":
        from .webui import run
        return run(cfg, browser=args.browser)
    if args.cmd == "demo":
        from .demo import run as demo
        return demo(cfg, args.step, browser=args.browser, game_speed=args.speed)
    if args.cmd == "calibrate":
        from .vision.calibrate import calibrate
        return 0 if calibrate(args.image) else 1
    if args.cmd == "recognize":
        return cmd_recognize(cfg, args.image)
    if args.cmd == "label":
        import glob
        # Windows 的 cmd 不展开通配符，这里自己展开
        paths = [Path(x) for pat in args.images for x in (glob.glob(str(pat)) or [str(pat)])]
        return cmd_label(cfg, paths)
    if args.cmd == "gsi-install":
        from .game.gsi import install_cfg
        path = install_cfg(args.dota_dir, cfg.gsi_port)
        config.save({"dota_dir": str(args.dota_dir)})   # 以后一键启动、导出网格图都用这个目录，不再自动找
        print(f"已写入 {path}（Dota 目录也记进了 config.toml）\n"
              "还需要在 Steam → Dota 2 → 属性 → 启动选项 里加上：-gamestateintegration，然后重开 Dota")
        return 0
    if args.cmd == "backtest":
        from .backtest import run as backtest
        print(backtest(cfg, args.matches, do_tune=args.tune))
        return 0
    if args.cmd == "lanes-test":
        from . import backtest
        from .domain import lanes
        stats = stats_mod.load_stats(cfg)
        if not (stats.get("hi_id") and cfg.stratz_token):
            print("需要统计数据（update）和 STRATZ token")
            return 1
        matches = backtest.fetch_matches(cfg, stats["hi_id"], args.matches)
        print(lanes.evaluate(matches, stats_mod.load_positions(cfg)))
        return 0
    if args.cmd == "lane-test":
        from .lanestate import run as lane_test
        print(lane_test(cfg, args.matches))
        return 0
    if args.cmd == "lane-review":
        from .lanestate import review
        print(review(cfg))
        return 0
    if args.cmd == "backtest-ai":
        from .aibacktest import run as ai_run
        print(ai_run(cfg, args.moments, args.model, args.workers))
        return 0
    if args.cmd == "coach-sim":
        from .domain.coach import simulate
        for mid in args.match_ids:
            print(simulate(cfg, mid), end=chr(10) * 2)
        return 0
    if args.cmd == "death-sim":
        from .domain.itembuild import death_sim
        print(death_sim(cfg, args.files or sorted((config.ROOT / "shots" / "deaths").glob("*.json"))))
        return 0
    if args.cmd == "brief-sim":
        from .domain.brief import simulate
        for mid in args.match_ids:
            print(simulate(cfg, mid, ai=args.ai), end=chr(10) * 2)
        return 0
    if args.cmd == "kb":
        return cmd_kb(cfg, args)
    if args.cmd == "gsi-dump":
        from .game.gsi import dump
        dump(cfg.gsi_port, config.ROOT / "shots" / "gsi")
        return 0
    if args.cmd == "say":
        import time
        from .game.speech import Speaker
        s = Speaker(cfg.voice_rate)
        s.say(" ".join(args.text))
        time.sleep(max(2.0, len("".join(args.text)) * 0.25))
        s.close()
        return 0
    if args.cmd == "logs":
        return cmd_logs(args)
    return 1
