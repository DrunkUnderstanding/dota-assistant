"""从 Dota 安装目录的资源包里取选人网格用的英雄图（只读磁盘上的静态文件，不碰游戏进程 / 内存）。

选人网格里每个格子显示的是 `panorama/images/heroes/selection/npc_dota_hero_<key>_png.vtex_c`，
在 `game/dota/pak01_dir.vpk`（+ pak01_NNN.vpk）里。网上的官方竖版图（`_vert.jpg`）是老美术，约 40 个英雄对不上，
所以直接用游戏自己的图（2026-10-23 实测：用老图 127 格只有 88 格可靠，用这批图见 HANDOFF 第 19 节）。

- VPK v2：目录树 + 数据在编号分卷里（archive 0x7fff 表示数据跟在目录文件后面）。
- vtex_c：Source 2 资源。多数英雄的图在资源里直接嵌了一张 PNG；新英雄是 DXT5（YCoCg 编码，flags 8），这里自己解。
游戏更新后图会变，`extract_selection` 按 vpk 修改时间判断要不要重取。
"""
from __future__ import annotations

import json
import logging
import struct
from pathlib import Path

import cv2
import numpy as np

from ..core import vpk as vpk_file
from ..core.config import DATA_DIR

GRID_PORTRAIT_DIR = DATA_DIR / "portraits_grid"
SELECTION = "panorama/images/heroes/selection/npc_dota_hero_{}_png.vtex_c"
STAMP = GRID_PORTRAIT_DIR / "source.json"
log = logging.getLogger(__name__)


# ---------- vtex_c ----------

def _texture_info(b: bytes) -> tuple[int, int, int, int]:
    """(数据起点, 宽, 高, 格式)。资源头：u32 头大小 … 块表（四字符名 + 相对偏移 + 大小）；DATA 块里 +20 是宽高、+26 是格式。"""
    head = struct.unpack_from("<I", b, 0)[0]
    block_offset, blocks = struct.unpack_from("<II", b, 8)
    pos = 8 + block_offset
    for _ in range(blocks):
        name = b[pos:pos + 4]
        offset = struct.unpack_from("<I", b, pos + 4)[0]
        if name == b"DATA":
            d = pos + 4 + offset
            w, h = struct.unpack_from("<HH", b, d + 20)
            return head, w, h, b[d + 26]
        pos += 12
    raise ValueError("vtex 里没有 DATA 块")


def _rgb565(c: np.ndarray) -> np.ndarray:
    return np.stack([((c >> 11) & 31) * 255 // 31, ((c >> 5) & 63) * 255 // 63, (c & 31) * 255 // 31], -1)


def dxt5(data: bytes, w: int, h: int) -> np.ndarray:
    """DXT5（BC3）→ RGBA。每 4×4 块 16 字节：8 字节 alpha（两个端点 + 3 位索引）+ 8 字节颜色（两个 RGB565 + 2 位索引）"""
    bw, bh = (w + 3) // 4, (h + 3) // 4
    blk = np.frombuffer(data[:bw * bh * 16], np.uint8).reshape(-1, 16)
    a0, a1 = blk[:, 0].astype(np.int32), blk[:, 1].astype(np.int32)
    abits = np.zeros(len(blk), np.uint64)
    for i in range(6):
        abits |= blk[:, 2 + i].astype(np.uint64) << np.uint64(8 * i)
    aidx = np.stack([((abits >> np.uint64(3 * k)) & np.uint64(7)).astype(np.int64) for k in range(16)], 1)
    big = a0 > a1
    apal = np.zeros((len(blk), 8), np.int32)
    apal[:, 0], apal[:, 1] = a0, a1
    for k in range(2, 8):
        apal[:, k] = ((8 - k) * a0 + (k - 1) * a1) // 7
    for k in range(2, 6):
        apal[:, k] = np.where(big, apal[:, k], ((6 - k) * a0 + (k - 1) * a1) // 5)
    apal[:, 6] = np.where(big, apal[:, 6], 0)
    apal[:, 7] = np.where(big, apal[:, 7], 255)
    alpha = np.take_along_axis(apal, aidx, 1)

    c0 = blk[:, 8].astype(np.int32) | (blk[:, 9].astype(np.int32) << 8)
    c1 = blk[:, 10].astype(np.int32) | (blk[:, 11].astype(np.int32) << 8)
    e0, e1 = _rgb565(c0), _rgb565(c1)
    cpal = np.stack([e0, e1, (2 * e0 + e1) // 3, (e0 + 2 * e1) // 3], 1)
    cbits = blk[:, 12:16].astype(np.uint32) @ np.array([1, 1 << 8, 1 << 16, 1 << 24], np.uint32)
    cidx = np.stack([((cbits >> np.uint32(2 * k)) & 3).astype(np.int64) for k in range(16)], 1)
    rgb = np.take_along_axis(cpal, cidx[..., None].repeat(3, 2), 1)
    px = np.concatenate([rgb, alpha[..., None]], 2).reshape(bh, bw, 4, 4, 4)
    return px.transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 4)[:h, :w].astype(np.uint8)


def ycocg_to_bgr(rgba: np.ndarray) -> np.ndarray:
    """YCoCg-DXT5：R = Co，G = Cg，B = 缩放，A = Y"""
    f = rgba.astype(np.float32)
    scale = np.floor(f[..., 2] / 8) + 1
    co, cg, y = (f[..., 0] - 128) / scale, (f[..., 1] - 128) / scale, f[..., 3]
    return np.clip(np.stack([y - co - cg, y + cg, y + co - cg], -1), 0, 255).astype(np.uint8)


def decode_vtex(b: bytes) -> np.ndarray:
    """vtex_c → BGR 图"""
    png = b.find(b"\x89PNG")
    if png >= 0:
        img = cv2.imdecode(np.frombuffer(b[png:], np.uint8), cv2.IMREAD_COLOR)
        if img is not None:
            return img
    start, w, h, fmt = _texture_info(b)
    if fmt != 2:
        raise ValueError(f"不支持的贴图格式 {fmt}")
    return ycocg_to_bgr(dxt5(b[start:], w, h))


# ---------- 导出 ----------

def extract_selection(dota_dir: Path, heroes: list[dict], out_dir: Path = GRID_PORTRAIT_DIR,
                      force: bool = False) -> tuple[int, list[str]]:
    """把每个英雄的网格图存成 out_dir/<id>.png。返回 (导出张数, 没取到的英雄 key)。vpk 没变就跳过。"""
    vpk_path = dota_dir / "game" / "dota" / "pak01_dir.vpk"
    mtime = int(vpk_path.stat().st_mtime)
    stamp = out_dir / STAMP.name
    if not force and stamp.exists() and json.loads(stamp.read_text(encoding="utf-8")).get("mtime") == mtime \
            and all((out_dir / f"{h['id']}.png").exists() for h in heroes):
        return 0, []
    vpk = vpk_file.VPK(vpk_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    n, missing = 0, []
    for h in heroes:
        path = SELECTION.format(h["key"])
        try:
            img = decode_vtex(vpk.read(path))
        except (KeyError, ValueError) as e:
            log.warning("网格图没取到：%s（%s）", h["key"], e)
            missing.append(h["key"])
            continue
        ok, buf = cv2.imencode(".png", img)
        buf.tofile(str(out_dir / f"{h['id']}.png"))
        n += 1
    stamp.write_text(json.dumps({"mtime": mtime, "count": n}), encoding="utf-8")
    log.info("网格图：从游戏资源导出 %d 张%s", n, f"，没取到 {missing}" if missing else "")
    return n, missing
