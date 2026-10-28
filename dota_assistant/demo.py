"""演示：不开 Dota，在桌面上直接看叠加层 + 语音的效果。

按真实对局（默认玩家 2026-10-21 00:20 那局卓尔）的选人顺序，每隔几秒往控制面板里填一个英雄，
叠加层和语音跟着变；选完后当一个「假 Dota」：先停在策略时间（弹本局要点），再把这局录像（OpenDota 已解析）
按 GSI 的格式加速推给本地 GSI 端口，走和真实游戏一样的路（GSI 服务 → 主控 → 本局要点 / 局内缺口提醒 → 叠加层弹卡片）。
"""
from __future__ import annotations

import logging
import threading
import time

import requests

from .core.config import Config

# 比赛 9054096051 的真实选人顺序（OpenDota picks_bans），玩家在天辉打一号位
DEMO_MATCH = 9054096051
DEMO_PICKS = [("敌", "风行者"), ("友", "巫妖"), ("友", "司夜刺客"), ("敌", "沉默术士"), ("敌", "冥魂大帝"),
              ("我", "卓尔游侠"), ("敌", "露娜"), ("友", "撼地者"), ("敌", "灰烬之灵"), ("友", "瘟疫法师")]
DEMO_BANS = "赏金猎人,自然先知,大地之灵,修补匠,幽鬼,暗影萨满"
FRAME_SECONDS = 0.5                            # 和 GSI 配置里的 throttle 一样，每 0.5 秒推一次
RESPAWN = 30                                   # 录像里只有死亡时间，复活时间粗略按 30 秒算（只影响 hero.alive）
STRATEGY_SECONDS = 12                          # 策略时间至少停多久（真实秒），给本局要点拉数据 + 看一眼
STRATEGY_MAX = 120                             # AI 打法还没写完就多停一会，最多这么久


def gsi_frames(m: dict, me: dict, hero_key: str, speed: float, hold=lambda: False):
    """把一局录像变成一串 GSI 推送（玩家视角）。speed = 每真实秒走多少游戏秒。
    录像里没有「当时身上有什么」，装备用到这一刻为止买过的全部东西代替（和 coach-sim 口径一致）。
    hold()：返回 True 时策略时间继续停（等 AI 打法写完），最多 STRATEGY_MAX 秒。"""
    team = "radiant" if me["player_slot"] < 128 else "dire"
    hero = {"id": me["hero_id"], "name": f"npc_dota_hero_{hero_key}"}
    deaths = sorted(d["time"] for d in me.get("deaths_log") or [])
    buys = sorted((x["time"], x["key"]) for x in me.get("purchase_log") or [])
    lh, nw = me.get("lh_t") or [], me.get("gold_t") or []

    def frame(state: str, t: float) -> dict:
        i = max(0, int(t // 60))
        owned = list(dict.fromkeys(k for bt, k in buys if bt <= t))
        return {
            "map": {"matchid": str(m["match_id"]), "game_state": state, "clock_time": int(t)},
            "player": {"team_name": team, "deaths": sum(1 for d in deaths if d <= t),
                       "last_hits": lh[min(i, len(lh) - 1)] if lh else 0,
                       "net_worth": nw[min(i, len(nw) - 1)] if nw else None},
            "hero": {**hero, "alive": not any(d <= t < d + RESPAWN for d in deaths)},
            "items": {f"slot{j}": {"name": f"item_{k}"} for j, k in enumerate(owned)},
        }
    waited = 0.0
    while waited < STRATEGY_SECONDS or (hold() and waited < STRATEGY_MAX):
        yield frame("DOTA_GAMERULES_STATE_STRATEGY_TIME", -90)
        waited += FRAME_SECONDS
    t = -90.0
    while t < 0:
        yield frame("DOTA_GAMERULES_STATE_PRE_GAME", t)
        t += speed * FRAME_SECONDS
    while t <= m["duration"]:
        yield frame("DOTA_GAMERULES_STATE_GAME_IN_PROGRESS", t)
        t += speed * FRAME_SECONDS
    yield frame("DOTA_GAMERULES_STATE_POST_GAME", m["duration"])


def run(cfg: Config, step_seconds: float = 4.0, browser: bool = False, game_speed: float = 60) -> int:
    from .live import Live
    from .webui import serve

    live = Live(cfg)
    live.mode = "manual"                       # 手动输入：不截屏识别
    live.side_mode = "radiant"                 # 我在天辉
    live.position = 1
    live.bans_text = DEMO_BANS

    # 录像在后台线程里拉（要联网），选人演示不用等它
    match: dict = {}

    def fetch():
        try:
            match.update(requests.get(f"https://api.opendota.com/api/matches/{DEMO_MATCH}", timeout=60).json())
        except Exception as e:
            logging.getLogger(__name__).warning("演示录像没拉到", exc_info=True)
            match["error"] = str(e)
    threading.Thread(target=fetch, daemon=True).start()

    def pick(side: str, name: str) -> None:
        if side == "我":
            # 模拟 GSI 告诉我们「你选了卓尔」；端口被占用时退回填进己方格子
            hero = live.db.find(name)
            if live.gsi:
                live.gsi.state.raw = {"map": {"game_state": "DOTA_GAMERULES_STATE_HERO_SELECTION"},
                                      "player": {"team_name": "radiant"},
                                      "hero": {"name": f"npc_dota_hero_{hero.key}"}}
                live.gsi.state.updated = time.time()
                live.refresh()
                return
            side = "友"
        key = "radiant" if side == "友" else "dire"
        i = live.slots[key].index("")
        live.slots[key][i] = name
        live.refresh()

    def notice(title: str, kind: str, text: str, say: str | None = None) -> None:
        live.show_notice(title, [(kind, text)])
        if say:
            live.speaker.say(say)

    def director():
        time.sleep(1.5)
        for side, name in DEMO_PICKS:
            live.submit(pick, side, name)
            time.sleep(step_seconds)
        for _ in range(30):                    # 录像还没拉回来就等一会
            if match:
                break
            time.sleep(1)
        me = next((p for p in match.get("players", []) if p.get("account_id") == cfg.account_id), None)
        if not live.gsi:
            return live.submit(notice, "演示结束", "dim", f"GSI 端口 {cfg.gsi_port} 被占用，演示不了局内部分")
        if not me or not me.get("purchase_log"):
            why = match.get("error") or "录像里找不到你 / 还没解析"
            return live.submit(notice, "演示结束", "dim", f"局内部分演示不了：{why}")
        url = f"http://127.0.0.1:{cfg.gsi_port}/"
        gsi = requests.Session()
        gsi.trust_env = False                  # 系统设了 HTTP_PROXY 时，发本机 GSI 端口别走代理（会被代理吞掉，助手收不到）
        started = False
        # AI 打法写完后再多看 10 秒才出门
        done_at = []

        def hold() -> bool:
            b = live.brief
            if b is None or (cfg.ai_plan and b.plan is None and "正在写" in b.plan_note):
                return True
            done_at.append(done_at[0] if done_at else time.time())
            return time.time() - done_at[0] < 10
        for f in gsi_frames(match, me, live.db.by_id[me["hero_id"]].key, game_speed, hold):
            if not started and f["map"]["game_state"] != "DOTA_GAMERULES_STATE_STRATEGY_TIME":
                # 出门后切回自动：叠加层按局内规则显示（策略时间还算选人阶段，自动模式会去截屏识别，所以等到这时）
                started = True
                live.submit(lambda: setattr(live, "mode", "auto"))
            gsi.post(url, json=f, timeout=5).raise_for_status()
            time.sleep(FRAME_SECONDS)
        live.submit(lambda: (setattr(live, "mode", "manual"), notice("演示结束", "dim", "关掉控制面板即可退出")))

    threading.Thread(target=director, daemon=True).start()
    return serve(live, browser)
