"""把 map_template.html + viz_data.json + 本机缓存的图标拼成一个能双击打开的网页「精灵老鹿小地图.html」。

用法（在仓库根目录）：
  python docs/research/io-leshrac/build_page.py                       生成 docs/research/io-leshrac/精灵老鹿小地图.html
  python docs/research/io-leshrac/build_page.py --minimap 小地图.png   再把自己截的小地图嵌进去当默认底图

图标（英雄头像、小地图头像、技能、装备）都读本机 data/ 里的缓存，缺的现从 Valve 官方 CDN 下（和助手界面同一套），
图片和生成的网页都不进 git（见同目录 .gitignore）。
--minimap：方形、天辉在左下的整张小地图（世界坐标 ±9472 的正方形，和 domain/camps.py 的 BOUND 一致）；
不是方形的整屏截图按 config.toml 的 minimap_x / minimap_y / minimap_size（按 1080 高量）裁。
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))

from dota_assistant.core import config  # noqa: E402
from dota_assistant.sources import assets  # noqa: E402

ABILITIES = ("wisp_tether", "wisp_overcharge", "wisp_relocate", "leshrac_diabolic_edict", "leshrac_lightning_storm",
             "leshrac_pulse_nova", "leshrac_split_earth", "leshrac_greater_lightning_storm")
ITEMS = ("ring_of_basilius", "arcane_boots", "kaya", "yasha_and_kaya", "mekansm", "guardian_greaves", "kaya_and_sange",
         "veil_of_discord", "black_king_bar", "bloodstone", "shivas_guard", "ultimate_scepter", "magic_wand", "soul_ring",
         "holy_locket", "smoke_of_deceit", "glimmer_cape")


def _read(path: Path) -> np.ndarray:
    # cv2.imread 认不了中文路径，先按字节读
    img = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise SystemExit(f"读不了图片：{path}")
    return img


def _uri(img: np.ndarray, box: tuple[int, int], fmt: str = ".png") -> str:
    h, w = img.shape[:2]
    k = min(box[0] / w, box[1] / h, 1)
    if k < 1:
        img = cv2.resize(img, (round(w * k), round(h * k)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(fmt, img, [cv2.IMWRITE_JPEG_QUALITY, 90] if fmt == ".jpg" else [])
    mime = "image/jpeg" if fmt == ".jpg" else "image/png"
    return f"data:{mime};base64," + base64.b64encode(buf.tobytes()).decode("ascii")


def _need(path: Path | None, what: str) -> Path:
    if not path or not path.is_file():
        raise SystemExit(f"缺图标：{what}。先跑 python -m dota_assistant update（下头像 / 装备图标），联网后再试")
    return path


def icons() -> dict[str, str]:
    if not all((assets.ITEM_ICON_DIR / f"{k}.png").is_file() for k in ITEMS):
        assets.fetch_item_icons()
    if not all((assets.PORTRAIT_DIR / f"{h}.png").is_file() for h in (91, 52)):
        assets.fetch_portraits()
    out = {
        "hero_io": _uri(_read(_need(assets.fetch_icon("heroes/icons/wisp.png"), "艾欧小地图头像")), (32, 32)),
        "hero_le": _uri(_read(_need(assets.fetch_icon("heroes/icons/leshrac.png"), "拉席克小地图头像")), (32, 32)),
        "por_io": _uri(_read(_need(assets.PORTRAIT_DIR / "91.png", "艾欧头像")), (192, 108)),
        "por_le": _uri(_read(_need(assets.PORTRAIT_DIR / "52.png", "拉席克头像")), (192, 108)),
    }
    for a in ABILITIES:
        out[a] = _uri(_read(_need(assets.fetch_icon(f"abilities/{a}.png"), a)), (64, 64))
    for it in ITEMS:
        out[it] = _uri(_read(_need(assets.ITEM_ICON_DIR / f"{it}.png", it)), (66, 48))
    return out


def minimap(path: Path) -> str:
    img = _read(path)
    h, w = img.shape[:2]
    if abs(w / h - 1) > 0.1:                      # 整屏截图：按设置里的小地图位置裁
        cfg = config.load()
        k = h / 1080
        x, y, s = round(cfg.minimap_x * k), round(cfg.minimap_y * k), round(cfg.minimap_size * k)
        canvas = np.zeros((s, s) + img.shape[2:], img.dtype)
        sx, sy = max(x, 0), max(y, 0)
        ex, ey = min(x + s, w), min(y + s, h)
        canvas[sy - y:ey - y, sx - x:ex - x] = img[sy:ey, sx:ex]
        img = canvas
    if img.ndim == 3 and img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    return _uri(img, (800, 800), ".jpg")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--minimap", type=Path, help="自己截的小地图（方形整张地图，或整屏截图）")
    ap.add_argument("--out", type=Path, default=HERE / "精灵老鹿小地图.html")
    a = ap.parse_args()
    page = (HERE / "map_template.html").read_text(encoding="utf-8")
    data = (HERE / "viz_data.json").read_text(encoding="utf-8")
    page = page.replace("/*DATA*/null", data).replace("/*ICONS*/null", json.dumps(icons(), separators=(",", ":")))
    page = page.replace("/*MINIMAP*/null", json.dumps(minimap(a.minimap)) if a.minimap else "null")
    head = '<!doctype html>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
    a.out.write_text(head + page, encoding="utf-8")
    print(f"已生成 {a.out}（{a.out.stat().st_size // 1024} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
