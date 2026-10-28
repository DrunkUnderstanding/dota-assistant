# 真鼠标测试用的本地服务：选人阶段（叠加层「?」）→ 局内（顶栏「?」），帮认 / 队友战绩都写到这个临时目录
import sys, time, threading, json
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, "D:/dota")
from dota_assistant import live as live_mod
from dota_assistant.domain import mates
from dota_assistant.core.config import load
from dota_assistant.game.gsi import GameState
from dota_assistant.live import Live
from dota_assistant.vision.recognize import recognize, read_image
from dota_assistant.webui import WebServer
port, tmp = int(sys.argv[1]), Path(sys.argv[2])
cfg = load()
cfg.ai_plan = False; cfg.buyback_auto = False; cfg.voice = False; cfg.jungle_cues = False
live = Live(cfg, screen_factory=lambda: None, use_gsi=False, use_hotkeys=False, overlay_file=tmp / "overlay.json")
live.fix_file = tmp / "fixes.jsonl"
live.mates = mates.Mates(tmp / "mates.json")
live.mode = "manual"
live.gsi = SimpleNamespace(state=GameState(), stop=lambda: None)
st = live.gsi.state
st.update({"map": {"matchid": "1", "game_state": "DOTA_GAMERULES_STATE_HERO_SELECTION"},
           "player": {"team_name": "radiant", "team_slot": 0}})
b = recognize(read_image(Path("D:/dota/shots/20261023-014333.png")), live.cal, live.bank, 0.7, keep_images=True)
v = recognize(read_image(Path("D:/dota/shots/20261023-014347.png")), live.cal, live.bank, 0.7, keep_images=True)
for side in ("radiant", "dire"):
    for i in range(5):
        live._crops["blank"][(side, i)] = getattr(b, side)[i].image
        live._crops["sel"][(side, i)] = getattr(v, side)[i].image
        g = getattr(v, side)[i].guess
        live.hints[side][i] = {"score": round(getattr(v, side)[i].score, 2), "guess": live.db.by_id[g].name_cn if g in live.db.by_id else ""}
live.slots["radiant"] = [""] * 5
live.slots["dire"] = [""] * 5
live.learn_slot = lambda *a: 0          # 别往本机 templates/ 里存
live._in_selection = True
phase = {"v": "draft", "t0": 0.0}
def keep():
    while True:
        time.sleep(1)
        if phase["v"] == "game":
            clock = 600 + time.time() - phase["t0"]
            st.update({"map": {"matchid": "1", "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS",
                               "clock_time": int(clock), "game_time": int(clock + 90), "paused": False},
                       "player": {"team_name": "radiant", "team_slot": 0, "deaths": 0, "gold": 1000},
                       "hero": {"name": "npc_dota_hero_drow_ranger", "alive": True, "level": 10}})
        else:
            st.updated = time.time()
threading.Thread(target=keep, daemon=True).start()
def lines():                             # 往 ctl.txt 追加一行发命令
    f = tmp / "ctl.txt"; f.write_text("", encoding="utf-8"); seen = 0
    while True:
        time.sleep(0.3)
        all_ = f.read_text(encoding="utf-8").splitlines()
        for l in all_[seen:]:
            yield l
        seen = len(all_)
def ctl():                               # game = 切到局内，并把阵容填好、空出夜魇第 3 格
    for line in lines():
        cmd = line.strip()
        if cmd == "game":
            def go():
                for side, names in (("radiant", ["卓尔游侠", "水晶室女", "斧王", "莱恩", "帕克"]),
                                    ("dire", ["幻影刺客", "拉比克", "", "风行者", "末日使者"])):
                    for i, n in enumerate(names):
                        live.slots[side][i] = n
                live.refresh()
            live.submit(go)
            phase["t0"] = time.time(); phase["v"] = "game"
        elif cmd.startswith("eval "):
            fut = live.submit(lambda: print("EVAL", json.dumps(eval(cmd[5:]), ensure_ascii=False, default=str), flush=True))
threading.Thread(target=ctl, daemon=True).start()
srv = WebServer(live, port).start()
live.start()
live.submit(live.refresh)
print("ready", srv.url, flush=True)
threading.Event().wait()
