"""英雄表、头像、顶栏图标、装备常量 / 图标：下载并缓存在 data/ 里的死数据。

数据源：中文英雄名 Valve 官方 datafeed；英雄定位标签 OpenDota /heroStats；图标 Valve 官方 CDN；装备常量 OpenDota。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

import requests

from ..core.config import DATA_DIR
from ..core.heroes import Hero, HeroDB
from . import http

VALVE_HEROLIST = "https://www.dota2.com/datafeed/herolist?language=schinese"
logger = logging.getLogger(__name__)


# ---------- 英雄表 ----------

def fetch_heroes() -> list[dict]:
    cn = {h["id"]: h["name_loc"] for h in http._get(VALVE_HEROLIST)["result"]["data"]["heroes"]}
    out = []
    for h in http._get(f"{http.OPENDOTA}/heroStats"):
        out.append({
            "id": h["id"],
            "key": h["name"].removeprefix("npc_dota_hero_"),
            "name_cn": cn.get(h["id"], h["localized_name"]),
            "name_en": h["localized_name"],
            "roles": h["roles"],
            "attr": h["primary_attr"],
            "img": h["img"].split("?")[0],
        })
    return out


CDN = "https://cdn.cloudflare.steamstatic.com"
PORTRAIT_DIR = DATA_DIR / "portraits"


def fetch_portraits(log=http._say) -> None:
    """下载每个英雄的横版头像（256×144），屏幕识别的初始模板。已有的跳过。"""
    PORTRAIT_DIR.mkdir(parents=True, exist_ok=True)
    heroes = json.loads((DATA_DIR / "heroes.json").read_text(encoding="utf-8"))
    n = 0
    for h in heroes:
        out = PORTRAIT_DIR / f"{h['id']}.png"
        if out.exists():
            continue
        r = http._session.get(CDN + h["img"], timeout=60)
        r.raise_for_status()
        out.write_bytes(r.content)
        n += 1
    log(f"  头像：新下载 {n} 张，共 {len(list(PORTRAIT_DIR.glob('*.png')))} 张")


ICON_DIR = DATA_DIR / "icons"
# 技能图标 abilities/<技能 key>.png、金币 icons/gold.png、英雄小图标 heroes/icons/<英雄 key>.png（买活自动记比对消息栏用）
ICON_PATH = re.compile(r"(abilities|icons|heroes/icons)/[a-z0-9_]+\.png")
_icon_locks: dict[str, threading.Lock] = {}   # 每张图一把锁：页面一打开 10 个买活小条同时要金币图标，只下一次；别的图不用等
_icon_failed: set[str] = set()                 # 这次运行下不到的不再试（断网时每张都要等超时）


def fetch_icon(rel: str) -> Path | None:
    """顶栏小条用的图标：第一次从 Valve 官方 CDN 下（dota2.com 网页用的同一套），存 data/icons，之后读本地。
    下不到（断网 / 没这张）返回 None，页面退回显示文字。不走 _session：它断连要重试好几轮，页面等不起"""
    if not ICON_PATH.fullmatch(rel):
        return None
    out = ICON_DIR / rel
    if not out.is_file() and rel not in _icon_failed:
        with _icon_locks.setdefault(rel, threading.Lock()):
            if not out.is_file() and rel not in _icon_failed:
                _download_icon(rel, out)
                if not out.is_file():
                    _icon_failed.add(rel)
    return out if out.is_file() else None


def _download_icon(rel: str, out: Path) -> None:
    try:
        r = requests.get(f"{CDN}/apps/dota2/images/dota_react/{rel}", timeout=10)
        r.raise_for_status()
    except requests.RequestException as e:
        logger.warning("图标下载失败 %s：%s", rel, e)
        return
    if not r.headers.get("Content-Type", "").startswith("image/"):
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part")        # 写完再改名：别的请求不会读到半张
    tmp.write_bytes(r.content)
    tmp.replace(out)


ICONS_DONE = ICON_DIR / "prefetched.txt"   # 预下过一遍的标记（写的是知识库版本），start 看它决定要不要补下


# ---------- 装备 ----------

ITEM_ICON_DIR = DATA_DIR / "items"
ITEMS_CONST = DATA_DIR / "opendota_items.json"


def items_const() -> dict:
    """OpenDota 装备常量：key → {cost, components, …}"""
    if not ITEMS_CONST.exists():
        import requests
        ITEMS_CONST.write_text(requests.get("https://api.opendota.com/api/constants/items", timeout=60).text,
                               encoding="utf-8")
    return json.loads(ITEMS_CONST.read_text(encoding="utf-8"))


def fetch_items_const(log=http._say) -> None:
    """OpenDota 装备常量（价格 / 合成 / 图标地址，items_const 读）。以前只在没有时下一次、之后不再更新，
    版本改了价格 / 出了新装备跟不上；现在跟全量更新一起刷新"""
    raw = http._get(f"{http.OPENDOTA}/constants/items")
    ITEMS_CONST.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    log(f"  装备常量：{len(raw)} 件")


def fetch_item_icons(log=http._say) -> None:
    """下载装备图标（88×64，出装推荐显示成图标）：OpenDota 装备常量里的图片地址，配方共用一张不下。已有的跳过"""
    from concurrent.futures import ThreadPoolExecutor
    ITEM_ICON_DIR.mkdir(parents=True, exist_ok=True)
    todo = [(ITEM_ICON_DIR / f"{k}.png", v["img"].split("?")[0]) for k, v in items_const().items()
            if v.get("img") and not k.startswith("recipe_") and not (ITEM_ICON_DIR / f"{k}.png").exists()]

    def get(job: tuple[Path, str]) -> None:
        r = http._session.get(CDN + job[1], timeout=60)
        r.raise_for_status()
        job[0].write_bytes(r.content)
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(get, todo))
    log(f"  装备图标：新下载 {len(todo)} 张，共 {len(list(ITEM_ICON_DIR.glob('*.png')))} 张")


def load_heroes() -> HeroDB:
    raw = json.loads((DATA_DIR / "heroes.json").read_text(encoding="utf-8"))
    return HeroDB([Hero(r["id"], r["key"], r["name_cn"], r["name_en"], tuple(r["roles"]), r["attr"])
                   for r in raw])
