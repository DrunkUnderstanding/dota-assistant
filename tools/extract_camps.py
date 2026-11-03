"""从本机 Dota 游戏文件读野点 / 莲花池 / 小地图范围，和 domain/camps.py 里写死的表对一遍（改版挪了野点时用）。

读的是 game/dota/maps/dota.vpk 里的 maps/dota/entities/*.vents_c（地图实体，二进制 KV3 v5，zstd / LZ4 压缩），
刷新框是每个野点触发器模型（同一包里的 *.vmdl_c）PHYS 块的碰撞盒范围，
只读磁盘上的静态文件，不碰游戏进程。KV3 的读法照 ValveResourceFormat 的 BinaryKV3.cs（只写了这几个文件用得到的部分）。

要额外装两个包（产品不需要，所以不在 requirements.txt 里）：pip install zstandard lz4
用法：python tools/extract_camps.py            打印野点表，和 camps.CAMPS 不一样的标出来
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dota_assistant.core.dota_install import find_dota  # noqa: E402
from dota_assistant.core.vpk import VPK  # noqa: E402
from dota_assistant.domain import camps  # noqa: E402

LUMPS = ("default_ents", "world_layer_radiant_base", "world_layer_dire_base")
_HEADER = "<IHHiiiiHHiiiiiiiiiiiiiiiiii"   # 魔数 + GUID 之后的头（v5）
# 打包数组里各种数怎么读：(struct 格式, 哪一道, 字节数)
_PACKED = {2: ("B", 1, 1), 22: ("b", 1, 1), 23: ("B", 1, 1), 20: ("h", 2, 2), 21: ("H", 2, 2), 11: ("i", 4, 4),
           12: ("I", 4, 4), 19: ("f", 4, 4), 3: ("q", 8, 8), 4: ("Q", 8, 8), 5: ("d", 8, 8)}


class _Lanes:
    """KV3 的 1 / 2 / 4 / 8 字节四道数据，各自往前读"""

    def __init__(self, b1, b2, b4, b8):
        self.buf = {1: b1, 2: b2, 4: b4, 8: b8}
        self.pos = {1: 0, 2: 0, 4: 0, 8: 0}

    def read(self, fmt: str, lane: int, size: int, n: int = 1):
        v = struct.unpack_from(f"<{n}{fmt}", self.buf[lane], self.pos[lane])
        self.pos[lane] += size * n
        return v if n > 1 else v[0]


def _align(o: int, a: int) -> int:
    return (o + a - 1) // a * a


def _split(buf: bytes, start: int, counts) -> tuple[list[bytes], int]:
    """按 1 / 2 / 4 / 8 字节各几个切开（中间按大小对齐）"""
    out, o = [], start
    for size, n in zip((1, 2, 4, 8), counts):
        o = _align(o, size)
        out.append(buf[o:o + size * n])
        o += size * n
    return out, o


def parse_kv3(data: bytes):
    if data[1:4] != b"3VK" or data[0] != 5:
        raise ValueError(f"只认 KV3 v5：{data[:4]!r}")
    (comp, _, _, c1, c4, c8, ctypes, _, _, _, ctotal, nblobs, _, c2, _,
     ub1, cb1, ub2, cb2, d1, d2, d4, d8, _, cobj, _, _) = struct.unpack_from(_HEADER, data, 20)
    pos = 20 + struct.calcsize(_HEADER)

    def unpack(raw: bytes, size: int) -> bytes:
        if comp == 0:
            return raw
        if comp == 1:
            import lz4.block
            return lz4.block.decompress(raw, uncompressed_size=size)
        import zstandard
        return zstandard.ZstdDecompressor().decompressobj().decompress(raw)
    buf1 = unpack(data[pos:pos + cb1], ub1)
    buf2 = unpack(data[pos + cb1:pos + cb1 + cb2], ub2)
    (s1, s2, s4, s8), _ = _split(buf1, 0, (c1, c2, c4, c8))
    nstr = struct.unpack_from("<i", s4)[0]
    parts = s1.split(b"\0")
    strings = [p.decode("utf-8", "replace") for p in parts[:nstr]]
    aux = _Lanes(s1[sum(len(p) + 1 for p in parts[:nstr]):], s2, s4[4:], s8)
    objlen = buf2[:cobj * 4]
    (m1, m2, m4, m8), o = _split(buf2, cobj * 4, (d1, d2, d4, d8))
    types = buf2[o:o + ctypes]
    blobs = _blobs(comp, buf2[o + ctypes:], nblobs, data[pos + cb1 + cb2:])
    main = _Lanes(m1, m2, m4, m8)
    st = {"t": 0, "obj": 0}

    def rtype() -> int:
        t = types[st["t"]]
        st["t"] += 1 + bool(t & 0x80) + bool(t & 0x40)   # 0x80 后面跟一个标志字节，0x40 再跳一个
        return t & 0x3F

    def value(t: int, lane: _Lanes):
        if t == 1:
            return None
        if t in (13, 14):
            return t == 13
        if t in (15, 16, 26, 27):
            return 1 if t in (16, 27) else 0
        if t in (17, 18):
            return float(t - 17)
        if t in _PACKED:
            return lane.read(*_PACKED[t])
        if t == 6:
            i = main.read("i", 4, 4)
            return strings[i] if 0 <= i < len(strings) else ""
        if t == 7:
            st["blob"] = st.get("blob", -1) + 1
            return blobs[st["blob"]]
        if t == 8:
            return [value(rtype(), main) for _ in range(main.read("i", 4, 4))]
        if t in (10, 24, 25):
            n = main.read("i", 4, 4) if t == 10 else main.read("B", 1, 1)
            sub, el = rtype(), aux if t == 25 else main
            if sub in _PACKED:
                v = el.read(_PACKED[sub][0], _PACKED[sub][1], _PACKED[sub][2], n)
                return list(v) if n > 1 else [v]
            return [value(sub, el) for _ in range(n)]
        if t == 9:
            n = struct.unpack_from("<i", objlen, st["obj"])[0]
            st["obj"] += 4
            out = {}
            for _ in range(n):
                tt = rtype()
                key = strings[main.read("i", 4, 4)]
                out[key] = value(tt, main)
            return out
        raise ValueError(f"不认识的 KV3 类型 {t}")
    return value(rtype(), main)


def blocks(raw: bytes) -> dict[bytes, bytes]:
    """Source 2 资源文件（*_c）里的各块：DATA / PHYS …"""
    _, _, _, off, count = struct.unpack_from("<IHHII", raw, 0)
    out = {}
    for i in range(count):
        p = 8 + off + 12 * i
        o, size = struct.unpack_from("<II", raw, p + 4)
        out[raw[p:p + 4]] = raw[p + 4 + o:p + 4 + o + size]
    return out


def _blobs(comp: int, tail: bytes, n: int, stream: bytes) -> list[bytes]:
    """KV3 v5 的二进制块（碰撞盒的顶点之类）：buffer2 里类型表后面是每块长度 + 0xFFEEDD00 + 每个压缩帧的长度（LZ4），
    数据接在两个 buffer 后面；LZ4 帧是串起来的，后面的帧能引用前面解出来的内容（照 ValveResourceFormat 的读法）"""
    if not n:
        return []
    lengths = struct.unpack_from(f"<{n}i", tail)
    rest = tail[4 * n + 4:]
    if comp == 0:
        out, p = [], 0
        for size in lengths:
            out.append(stream[p:p + size])
            p += size
        return out
    if comp == 2:                                     # zstd：所有块压成一个帧
        import zstandard
        raw = zstandard.ZstdDecompressor().decompressobj().decompress(stream)
        out, p = [], 0
        for size in lengths:
            out.append(raw[p:p + size])
            p += size
        return out
    if comp != 1:
        raise ValueError(f"不认识的压缩方式 {comp}")
    import lz4.block
    out, p, k, history = [], 0, 0, b""
    for size in lengths:
        blob = b""
        while len(blob) < size:
            frame = struct.unpack_from("<H", rest, 2 * k)[0]
            k += 1
            want = min(16384, size - len(blob))
            part = lz4.block.decompress(stream[p:p + frame], uncompressed_size=want, dict=history[-65536:])
            p += frame
            blob += part
            history += part
        out.append(blob)
    return out


def entities(vpk: VPK) -> list[dict]:
    out = []
    for name in LUMPS:
        root = parse_kv3(blocks(vpk.read(f"maps/dota/entities/{name}.vents_c"))[b"DATA"])
        out += [e["keyValues3Data"]["values"] for e in root["m_entityKeyValues"]]
    return out


def spawn_box(vpk: VPK, trigger: dict) -> tuple[int, int, int, int]:
    """刷新框（按住 Alt 显示的黄框）= 触发器原点 + 它模型的碰撞盒范围：(x 最小, y 最小, x 最大, y 最大)"""
    phys = parse_kv3(blocks(vpk.read(trigger["model"] + "_c"))[b"PHYS"])
    b = phys["m_parts"][0]["m_rnShape"]["m_hulls"][0]["m_Hull"]["m_Bounds"]
    ox, oy = trigger["origin"][:2]
    lo, hi = b["m_vMinBounds"], b["m_vMaxBounds"]
    return round(ox + lo[0]), round(oy + lo[1]), round(ox + hi[0]), round(oy + hi[1])


def main() -> int:
    dota = find_dota()
    if not dota:
        print("找不到 Dota 安装目录")
        return 1
    vpk = VPK(dota / "game" / "dota" / "maps" / "dota.vpk")
    ents = entities(vpk)
    triggers = {e["targetname"]: e for e in ents if "neutralcamp" in str(e.get("targetname")) and e.get("model")}
    found = sorted(((e["VolumeName"].removeprefix("[PR#]neutralcamp_"), int(e["NeutralType"]),
                     round(e["origin"][0]), round(e["origin"][1]), spawn_box(vpk, triggers[e["VolumeName"]]))
                    for e in ents if e.get("classname") == "npc_dota_neutral_spawner"),
                   key=lambda c: (c[0].split("_")[0], int(c[0].split("_")[1])))
    known = {c[0]: c for c in camps.CAMPS}
    print(f"野点 {len(found)} 个（camps.CAMPS 里 {len(known)} 个）：")
    for c in found:
        print(f"  {c!r},{'' if known.get(c[0]) == c else '   ← 和 camps.CAMPS 不一样'}")
    for name in sorted(set(known) - {c[0] for c in found}):
        print(f"  {name} 游戏文件里没有了")
    for e in ents:
        if e.get("classname") in ("npc_dota_lotus_pool", "dota_minimap_boundary"):
            print(f"  {e['classname']} {e.get('targetname', '')} {[round(v) for v in e['origin'][:2]]}")
    return 0 if all(known.get(c[0]) == c for c in found) and len(found) == len(known) else 2


if __name__ == "__main__":
    sys.exit(main())
