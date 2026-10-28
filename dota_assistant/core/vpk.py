"""最小的 VPK v2 读取：Dota 资源包 game/dota/pak01_dir.vpk（+ pak01_NNN.vpk），只读磁盘上的静态文件，不碰游戏进程 / 内存。

VPK v2：目录树 + 数据在编号分卷里（archive 0x7fff 表示数据跟在目录文件后面）。
选人网格图（gamefiles）、大招冷却读英雄 KV 文件（ultcd）都用它。
"""
from __future__ import annotations

import struct
from pathlib import Path


class VPK:
    """最小的 VPK v2 读取：建目录、按路径取文件"""

    def __init__(self, dir_path: Path):
        self.base = str(dir_path)[:-len("_dir.vpk")]
        self.entries: dict[str, tuple[int, int, int, bytes]] = {}
        with open(dir_path, "rb") as f:
            sig, ver, tree = struct.unpack("<III", f.read(12))
            if sig != 0x55AA1234:
                raise ValueError(f"不是 VPK 文件：{dir_path}")
            header = 28 if ver == 2 else 12
            f.seek(header)
            raw = f.read(tree)
        self.data_offset = header + tree
        self._dir = dir_path
        pos = 0

        def string() -> str:
            nonlocal pos
            end = raw.index(b"\0", pos)
            s = raw[pos:end].decode("utf-8", "replace")
            pos = end + 1
            return s

        while ext := string():
            while path := string():
                while name := string():
                    _, preload, archive, offset, length, _ = struct.unpack_from("<IHHIIH", raw, pos)
                    pos += 18
                    data = raw[pos:pos + preload]
                    pos += preload
                    full = (f"{path}/" if path.strip() else "") + f"{name}.{ext}"
                    self.entries[full] = (archive, offset, length, data)

    def read(self, path: str) -> bytes:
        archive, offset, length, preload = self.entries[path]
        if archive == 0x7FFF:
            src, offset = self._dir, self.data_offset + offset
        else:
            src = Path(f"{self.base}_{archive:03d}.vpk")
        with open(src, "rb") as f:
            f.seek(offset)
            return preload + f.read(length)
