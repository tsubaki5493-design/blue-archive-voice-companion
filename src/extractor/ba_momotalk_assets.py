"""
Blue Archive (Steam PC) MomoTalk UI Assets & Call Audio FX
---------------------------------------------------------
Extracts and caches authentic MomoTalk UI assets (student portraits, school emblems,
MomoTalk / Phone icons) from a read-only Steam Blue Archive installation into
`%LOCALAPPDATA%\\VoiceCompanionData\\cache\\momotalk_ui\\`, with crisp procedural
fallbacks when Blue Archive is not installed.

Also provides a zero-dependency telephone outgoing ringback tone (`통화음`) and
connect/hangup sound effect synthesizer (`RingbackStreamState` / `MomoTalkCallAudioFX`).
"""

from __future__ import annotations

import bisect
from collections import OrderedDict
import hashlib
import json
import os
import struct
import threading
import zlib
from pathlib import Path
from typing import Any, Callable

import numpy as np

from src.extractor.ba_molru_extractor import DEFAULT_GAME_DIR, discover_steam_blue_archive_dir
from src.utils.app_paths import get_cache_dir


def _discover_ba_root(custom_hint: str | Path | None = None) -> Path | None:
    if custom_hint is not None:
        p = Path(custom_hint)
        if p.name.lower() == "bluearchive_data":
            p = p.parent
        gd = p / "BlueArchive_Data" / "StreamingAssets" / "PUB" / "Resource" / "GameData" / "Windows"
        if gd.is_dir():
            return p
        return None
    found = discover_steam_blue_archive_dir()
    if found is not None:
        gd = found / "BlueArchive_Data" / "StreamingAssets" / "PUB" / "Resource" / "GameData" / "Windows"
        if gd.is_dir():
            return found
    return None

# School Korean name -> Blue Archive School_Icon_<CODE> mapping
SCHOOL_CODE_MAP: dict[str, str] = {
    "게헨나 학원": "GEHENNA",
    "트리니티 종합학원": "TRINITY",
    "밀레니엄 사이언스 스쿨": "MILLENNIUM",
    "아비도스 고등학교": "ABYDOS",
    "백귀야행 연합학원": "HYAKKIYAKO",
    "산해경 고급중학교": "SHANHAIJING",
    "붉은겨울 연방학원": "REDWINTER",
    "발키리 경찰학교": "VALKYRIE",
    "SRT 특수학원": "SRT",
    "아리우스 분교": "Arius",
    "하이랜더 철도학원": "HIGHLANDER",
    "토키와다이 중학교 (콜라보)": "Tokiwadai",
    "사쿠가와 중학교 (콜라보)": "SAKUGAWA",
    "총학생회": "ETC",
    "샬레 / 싯딤의 상자": "ETC",
    "와일드헌트 예술학원": "ETC",
    "오디세이아 해양고등학교": "ETC",
    "크로노스 스쿨": "ETC",
    "데카그라마톤": "ETC",
    "칠수인 (일곱 죄수)": "ETC",
    "기타 / 콜라보": "ETC",
}

SCHOOL_ACCENT_RGB: dict[str, tuple[int, int, int]] = {
    "GEHENNA": (196, 62, 82),
    "TRINITY": (229, 138, 92),
    "MILLENNIUM": (72, 148, 224),
    "ABYDOS": (70, 178, 208),
    "HYAKKIYAKO": (224, 114, 146),
    "SHANHAIJING": (78, 168, 116),
    "REDWINTER": (196, 78, 72),
    "VALKYRIE": (106, 134, 186),
    "SRT": (104, 120, 138),
    "Arius": (108, 102, 136),
    "HIGHLANDER": (186, 132, 88),
    "Tokiwadai": (136, 114, 192),
    "SAKUGAWA": (106, 168, 136),
    "ETC": (118, 138, 168),
}

# Explicit alias table for character JSON IDs whose Steam portrait stem differs
CHARACTER_PORTRAIT_ALIASES: dict[str, tuple[str, ...]] = {
    "Hatsune_Miku": ("HatsuneMiku", "Miku"),
    "Misaka_Mikoto": ("MisakaMikoto", "Misaka"),
    "Shokuhou_Misaki": ("ShokuhouMisaki", "Shokuhou"),
    "Saten_Ruiko": ("SatenRuiko", "Saten"),
    "Aris": ("Aris", "Alice"),
    "Utaha": ("Utaha",),
    "NerU": ("Neru",),
    "Shiroko": ("Shiroko",),
    "Shiroko_Terror": ("Shiroko_Terror", "ShirokoTerror", "CH0263"),
    "Hoshino": ("Hoshino",),
    "GSC_President": ("GSCPresident", "President"),
    "Prof_Niyaniya": ("Niyaniya", "ProfNiyaniya"),
}


# ---------------------------------------------------------------------------
# Pure-Python UnityFS + SerializedFile v22 + Texture2D Decoder
# ---------------------------------------------------------------------------

def lz4_decompress(src: bytes, uncompressed_size: int) -> bytes:
    """Decompress a raw UnityFS LZ4 block without external C extensions."""
    if uncompressed_size <= 0:
        return b""
    out = bytearray()
    i = 0
    n = len(src)
    while i < n and len(out) < uncompressed_size:
        token = src[i]
        i += 1
        lit_len = token >> 4
        if lit_len == 15:
            while i < n:
                b = src[i]
                i += 1
                lit_len += b
                if b != 255:
                    break
        out.extend(src[i : i + lit_len])
        i += lit_len
        if i >= n or len(out) >= uncompressed_size:
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if offset <= 0 or offset > len(out):
            break
        match_len = (token & 0x0F) + 4
        if (token & 0x0F) == 15:
            while i < n:
                b = src[i]
                i += 1
                match_len += b
                if b != 255:
                    break
        start = len(out) - offset
        if offset >= match_len:
            out.extend(out[start : start + match_len])
        elif offset == 1:
            out.extend(out[start : start + 1] * match_len)
        else:
            pat = out[start : start + offset]
            reps, rem = divmod(match_len, offset)
            if reps:
                out.extend(pat * reps)
            if rem:
                out.extend(pat[:rem])
    return bytes(out[:uncompressed_size])


def parse_unityfs_header(bundle_path: Path) -> dict[str, Any]:
    """Parse ONLY the UnityFS header, block table, and node table without reading/decompressing `.resS` payloads."""
    with bundle_path.open("rb") as fh:
        head = fh.read(512)
        pos = 0

        def _rstr() -> str:
            nonlocal pos, head
            while True:
                end = head.find(b"\x00", pos)
                if end != -1:
                    val = head[pos:end].decode("utf-8", errors="replace")
                    pos = end + 1
                    return val
                more = fh.read(1024)
                if not more:
                    raise ValueError(f"Truncated UnityFS header in {bundle_path}")
                head += more

        sig = _rstr()
        if sig != "UnityFS":
            raise ValueError(f"Unsupported bundle signature: {sig!r}")
        while len(head) < pos + 24:
            more = fh.read(512)
            if not more:
                raise ValueError(f"Truncated UnityFS version fields in {bundle_path}")
            head += more
        ver = struct.unpack_from(">I", head, pos)[0]
        pos += 4
        _rstr()  # unity_version
        _rstr()  # unity_revision
        while len(head) < pos + 20:
            more = fh.read(512)
            if not more:
                raise ValueError(f"Truncated UnityFS size fields in {bundle_path}")
            head += more
        size, c_ib, u_ib, flags = struct.unpack_from(">qIII", head, pos)
        pos += 20
        if ver >= 7:
            pos = (pos + 15) & ~15
        if flags & 0x80:
            fh.seek(size - c_ib)
            ib_raw = fh.read(c_ib)
            data_pos = pos
        else:
            fh.seek(pos)
            ib_raw = fh.read(c_ib)
            data_pos = pos + c_ib
        if flags & 0x200:
            data_pos = (data_pos + 15) & ~15

    comp = flags & 0x3F
    ib = lz4_decompress(ib_raw, u_ib) if comp in (2, 3) else ib_raw

    ip = 16
    b_cnt = struct.unpack_from(">I", ib, ip)[0]
    ip += 4
    blocks: list[tuple[int, int, int, int, int]] = []
    block_u_ends: list[int] = []
    u_cursor = 0
    f_cursor = data_pos
    for k in range(b_cnt):
        u_sz, c_sz, b_fl = struct.unpack_from(">IIH", ib, ip + 10 * k)
        u_end = u_cursor + u_sz
        blocks.append((u_cursor, u_end, f_cursor, c_sz, b_fl))
        block_u_ends.append(u_end)
        u_cursor = u_end
        f_cursor += c_sz
    ip += 10 * b_cnt

    n_cnt = struct.unpack_from(">I", ib, ip)[0]
    ip += 4
    nodes: list[tuple[int, int, str]] = []
    for _ in range(n_cnt):
        ofs, sz, _st = struct.unpack_from(">qqI", ib, ip)
        ip += 20
        end = ib.index(b"\x00", ip)
        nm = ib[ip:end].decode("utf-8", errors="replace")
        ip = end + 1
        nodes.append((ofs, sz, nm))

    return {"blocks": blocks, "block_u_ends": block_u_ends, "nodes": nodes}


def read_unityfs_node_slice(
    bundle_path: Path,
    node_selector: Callable[[str], bool],
    rel_offset: int = 0,
    max_bytes: int | None = None,
    header_cache: dict[str, dict[str, Any]] | None = None,
    block_cache: OrderedDict[tuple[str, int], bytes] | None = None,
    cache_lock: threading.Lock | None = None,
) -> bytes | None:
    """
    Read and decompress ONLY the 128 KB UnityFS LZ4 blocks covering `[rel_offset : rel_offset + max_bytes]`
    of the first matching bundle node. Avoids decompressing entire multi-megabyte `.resS` texture atlases.
    """
    hdr_key = str(bundle_path)
    hdr: dict[str, Any] | None = None
    if header_cache is not None:
        if cache_lock is not None:
            with cache_lock:
                hdr = header_cache.get(hdr_key)
        else:
            hdr = header_cache.get(hdr_key)
    if hdr is None:
        hdr = parse_unityfs_header(bundle_path)
        if header_cache is not None:
            if cache_lock is not None:
                with cache_lock:
                    header_cache[hdr_key] = hdr
            else:
                header_cache[hdr_key] = hdr

    node = next((n for n in hdr["nodes"] if node_selector(n[2])), None)
    if node is None:
        return None
    node_ofs, node_sz, _ = node
    req_start = max(0, int(rel_offset))
    if req_start >= node_sz:
        return b""
    avail = node_sz - req_start
    want = avail if max_bytes is None else max(0, min(int(max_bytes), avail))
    if want == 0:
        return b""

    u0 = node_ofs + req_start
    u1 = u0 + want
    blocks = hdr["blocks"]
    u_ends = hdr["block_u_ends"]
    idx = bisect.bisect_right(u_ends, u0)

    out = bytearray()
    fh = None
    try:
        while idx < len(blocks):
            b_u0, b_u1, f_pos, c_sz, b_fl = blocks[idx]
            if b_u0 >= u1:
                break
            blk_key = (hdr_key, idx)
            raw_blk: bytes | None = None
            if block_cache is not None:
                if cache_lock is not None:
                    with cache_lock:
                        raw_blk = block_cache.get(blk_key)
                        if raw_blk is not None:
                            block_cache.move_to_end(blk_key)
                else:
                    raw_blk = block_cache.get(blk_key)
                    if raw_blk is not None:
                        block_cache.move_to_end(blk_key)
            if raw_blk is None:
                if fh is None:
                    fh = bundle_path.open("rb")
                fh.seek(f_pos)
                comp_chunk = fh.read(c_sz)
                bc = b_fl & 0x3F
                u_sz = b_u1 - b_u0
                raw_blk = lz4_decompress(comp_chunk, u_sz) if bc in (2, 3) else comp_chunk
                if block_cache is not None:
                    if cache_lock is not None:
                        with cache_lock:
                            block_cache[blk_key] = raw_blk
                            block_cache.move_to_end(blk_key)
                            while len(block_cache) > 48:
                                block_cache.popitem(last=False)
                    else:
                        block_cache[blk_key] = raw_blk
                        block_cache.move_to_end(blk_key)
                        while len(block_cache) > 48:
                            block_cache.popitem(last=False)
            s0 = max(0, u0 - b_u0)
            s1 = min(len(raw_blk), u1 - b_u0)
            if s1 > s0:
                out.extend(raw_blk[s0:s1])
            idx += 1
    finally:
        if fh is not None:
            fh.close()
    return bytes(out)


def unpack_unityfs(bundle_path: Path) -> dict[str, bytes]:
    """Read an unencrypted UnityFS bundle into `{node_name: node_bytes}`."""
    data = bundle_path.read_bytes()
    pos = 0

    def _rstr() -> str:
        nonlocal pos
        end = data.index(b"\x00", pos)
        val = data[pos:end].decode("utf-8", errors="replace")
        pos = end + 1
        return val

    sig = _rstr()
    if sig != "UnityFS":
        raise ValueError(f"Unsupported bundle signature: {sig!r}")
    ver = struct.unpack_from(">I", data, pos)[0]
    pos += 4
    _rstr()  # unity_version
    _rstr()  # unity_revision
    size, c_ib, u_ib, flags = struct.unpack_from(">qIII", data, pos)
    pos += 20
    if ver >= 7:
        pos = (pos + 15) & ~15
    if flags & 0x80:
        ib_raw = data[size - c_ib : size]
    else:
        ib_raw = data[pos : pos + c_ib]
        pos += c_ib
    if flags & 0x200:
        pos = (pos + 15) & ~15

    comp = flags & 0x3F
    ib = lz4_decompress(ib_raw, u_ib) if comp in (2, 3) else ib_raw

    ip = 16
    b_cnt = struct.unpack_from(">I", ib, ip)[0]
    ip += 4
    blocks = [struct.unpack_from(">IIH", ib, ip + 10 * k) for k in range(b_cnt)]
    ip += 10 * b_cnt

    n_cnt = struct.unpack_from(">I", ib, ip)[0]
    ip += 4
    nodes: list[tuple[int, int, str]] = []
    for _ in range(n_cnt):
        ofs, sz, _st = struct.unpack_from(">qqI", ib, ip)
        ip += 20
        end = ib.index(b"\x00", ip)
        nm = ib[ip:end].decode("utf-8", errors="replace")
        ip = end + 1
        nodes.append((ofs, sz, nm))

    stream = bytearray()
    for u_sz, c_sz, b_fl in blocks:
        chunk = data[pos : pos + c_sz]
        pos += c_sz
        bc = b_fl & 0x3F
        stream.extend(lz4_decompress(chunk, u_sz) if bc in (2, 3) else chunk)

    raw_stream = bytes(stream)
    return {nm: raw_stream[ofs : ofs + sz] for ofs, sz, nm in nodes}


def parse_serialized_texture2d_entries(cab_bytes: bytes) -> list[dict[str, Any]]:
    """Extract all Texture2D metadata headers from a Unity 2021.3 SerializedFile (v22)."""
    if len(cab_bytes) < 64:
        return []
    endian = cab_bytes[16]
    le = "<" if endian == 0 else ">"
    data_ofs = struct.unpack_from(">q", cab_bytes, 32)[0]
    p = cab_bytes.index(b"\x00", 48) + 1
    p += 4  # target_platform
    enable_tt = cab_bytes[p]
    p += 1
    tcnt = struct.unpack_from(le + "i", cab_bytes, p)[0]
    p += 4

    types: list[int] = []
    for _ in range(tcnt):
        cid = struct.unpack_from(le + "i", cab_bytes, p)[0]
        p += 7 + (16 if cid == 114 else 0) + 16
        if enable_tt:
            nn, ssz = struct.unpack_from(le + "ii", cab_bytes, p)
            p += 8 + nn * 32 + ssz
            nd = struct.unpack_from(le + "i", cab_bytes, p)[0]
            p += 4 + nd * 4
        types.append(cid)

    ocnt = struct.unpack_from(le + "i", cab_bytes, p)[0]
    p += 4
    out: list[dict[str, Any]] = []
    for _ in range(ocnt):
        p = (p + 3) & ~3
        _pid, bs, bsz, ti = struct.unpack_from(le + "qqIi", cab_bytes, p)
        p += 24
        if types[ti] != 28:
            continue
        ob = cab_bytes[data_ofs + bs : data_ofs + bs + bsz]
        if len(ob) < 120:
            continue
        nlen = struct.unpack_from("<I", ob, 0)[0]
        if nlen <= 0 or 4 + nlen + 100 > len(ob):
            continue
        name = ob[4 : 4 + nlen].decode("utf-8", errors="replace")
        off = (4 + nlen + 3) & ~3
        w, h, complete_sz, _mips_stripped, tex_fmt, mip_cnt = struct.unpack_from("<6i", ob, off + 8)
        if w <= 0 or h <= 0 or w > 4096 or h > 4096:
            continue
        # Verified Unity 2021.3.56f2 Texture2D layout: image_data vector sits at off+84
        inline_len = struct.unpack_from("<I", ob, off + 84)[0]
        p_data = off + 88
        stream_off = 0
        stream_sz = 0
        inline_bytes = b""
        if inline_len > 0 and p_data + inline_len <= len(ob):
            inline_bytes = ob[p_data : p_data + inline_len]
        else:
            p_stream = (p_data + inline_len + 3) & ~3
            if p_stream + 12 <= len(ob):
                stream_off, stream_sz = struct.unpack_from("<QI", ob, p_stream)
        out.append(
            {
                "name": name,
                "w": int(w),
                "h": int(h),
                "complete_sz": int(complete_sz),
                "fmt": int(tex_fmt),
                "mip_cnt": int(mip_cnt),
                "stream_off": int(stream_off),
                "stream_sz": int(stream_sz),
                "inline_bytes": inline_bytes,
            }
        )
    return out


def _rgb565_to_rgb888(c: np.ndarray) -> np.ndarray:
    r = ((c >> 11) & 0x1F).astype(np.uint16)
    g = ((c >> 5) & 0x3F).astype(np.uint16)
    b = (c & 0x1F).astype(np.uint16)
    r = (r << 3) | (r >> 2)
    g = (g << 2) | (g >> 4)
    b = (b << 3) | (b >> 2)
    return np.stack([r, g, b], axis=-1).astype(np.uint8)


def _decode_dxt_color_blocks(c0: np.ndarray, c1: np.ndarray, cbits: np.ndarray, dxt1_alpha: bool) -> tuple[np.ndarray, np.ndarray | None]:
    """Decode DXT1/BC1 4x4 color LUTs for all blocks vectorized (`N` blocks -> `(N, 16, 3)` + optional alpha)."""
    rgb0 = _rgb565_to_rgb888(c0).astype(np.uint16)
    rgb1 = _rgb565_to_rgb888(c1).astype(np.uint16)
    n_blk = c0.shape[0]
    pal = np.zeros((n_blk, 4, 3), dtype=np.uint8)
    pal[:, 0, :] = rgb0.astype(np.uint8)
    pal[:, 1, :] = rgb1.astype(np.uint8)

    gt = c0 > c1
    if not dxt1_alpha:
        gt = np.ones_like(c0, dtype=bool)
    pal[gt, 2, :] = ((2 * rgb0[gt] + rgb1[gt] + 1) // 3).astype(np.uint8)
    pal[gt, 3, :] = ((rgb0[gt] + 2 * rgb1[gt] + 1) // 3).astype(np.uint8)

    if dxt1_alpha:
        le_mask = ~gt
        if np.any(le_mask):
            pal[le_mask, 2, :] = ((rgb0[le_mask] + rgb1[le_mask]) // 2).astype(np.uint8)
            pal[le_mask, 3, :] = 0

    shifts = (np.arange(16, dtype=np.uint32) * 2)[None, :]
    idx = (cbits[:, None] >> shifts) & 0x3
    rows = np.arange(n_blk)[:, None]
    rgb_px = pal[rows, idx]  # (n_blk, 16, 3)

    if not dxt1_alpha:
        return rgb_px, None
    alpha_px = np.full((n_blk, 16), 255, dtype=np.uint8)
    if np.any(~gt):
        transparent = (~gt[:, None]) & (idx == 3)
        alpha_px[transparent] = 0
    return rgb_px, alpha_px


def decode_texture2d_rgba(raw_bytes: bytes, w: int, h: int, tex_fmt: int) -> np.ndarray | None:
    """Decode Unity Texture2D bytes (`RGBA32=4`, `RGB24=3`, `DXT1=10`, `DXT5=12`) into top-down `(h, w, 4)` uint8."""
    if w <= 0 or h <= 0 or not raw_bytes:
        return None
    if tex_fmt == 4:  # RGBA32
        need = w * h * 4
        if len(raw_bytes) < need:
            return None
        rgba = np.frombuffer(raw_bytes[:need], dtype=np.uint8).reshape(h, w, 4).copy()
        return rgba[::-1].copy()

    if tex_fmt == 3:  # RGB24
        need = w * h * 3
        if len(raw_bytes) < need:
            return None
        rgb = np.frombuffer(raw_bytes[:need], dtype=np.uint8).reshape(h, w, 3)
        rgba = np.empty((h, w, 4), dtype=np.uint8)
        rgba[..., :3] = rgb
        rgba[..., 3] = 255
        return rgba[::-1].copy()

    bw = max(1, (w + 3) // 4)
    bh = max(1, (h + 3) // 4)
    n_blk = bw * bh

    if tex_fmt == 12:  # DXT5 / BC3
        need = n_blk * 16
        if len(raw_bytes) < need:
            return None
        blk = np.frombuffer(raw_bytes[:need], dtype=np.uint8).reshape(n_blk, 16)
        a0 = blk[:, 0].astype(np.uint16)
        a1 = blk[:, 1].astype(np.uint16)
        abits = np.zeros(n_blk, dtype=np.uint64)
        for b_i in range(6):
            abits |= blk[:, 2 + b_i].astype(np.uint64) << (8 * b_i)

        alut = np.zeros((n_blk, 8), dtype=np.uint8)
        alut[:, 0] = a0.astype(np.uint8)
        alut[:, 1] = a1.astype(np.uint8)
        gt = a0 > a1
        if np.any(gt):
            for k in range(6):
                alut[gt, 2 + k] = (((6 - k) * a0[gt] + (k + 1) * a1[gt] + 3) // 7).astype(np.uint8)
        if np.any(~gt):
            ngt = ~gt
            for k in range(4):
                alut[ngt, 2 + k] = (((4 - k) * a0[ngt] + (k + 1) * a1[ngt] + 2) // 5).astype(np.uint8)
            alut[ngt, 6] = 0
            alut[ngt, 7] = 255

        ashifts = (np.arange(16, dtype=np.uint64) * 3)[None, :]
        aidx = (abits[:, None] >> ashifts) & 0x7
        alpha_px = alut[np.arange(n_blk)[:, None], aidx]

        c0 = blk[:, 8].astype(np.uint16) | (blk[:, 9].astype(np.uint16) << 8)
        c1 = blk[:, 10].astype(np.uint16) | (blk[:, 11].astype(np.uint16) << 8)
        cbits = (
            blk[:, 12].astype(np.uint32)
            | (blk[:, 13].astype(np.uint32) << 8)
            | (blk[:, 14].astype(np.uint32) << 16)
            | (blk[:, 15].astype(np.uint32) << 24)
        )
        rgb_px, _ = _decode_dxt_color_blocks(c0, c1, cbits, dxt1_alpha=False)

        rgba_blk = np.empty((n_blk, 16, 4), dtype=np.uint8)
        rgba_blk[..., :3] = rgb_px
        rgba_blk[..., 3] = alpha_px
        img = rgba_blk.reshape(bh, bw, 4, 4, 4).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 4)
        return img[:h, :w, :][::-1].copy()

    if tex_fmt == 10:  # DXT1 / BC1
        need = n_blk * 8
        if len(raw_bytes) < need:
            return None
        blk = np.frombuffer(raw_bytes[:need], dtype=np.uint8).reshape(n_blk, 8)
        c0 = blk[:, 0].astype(np.uint16) | (blk[:, 1].astype(np.uint16) << 8)
        c1 = blk[:, 2].astype(np.uint16) | (blk[:, 3].astype(np.uint16) << 8)
        cbits = (
            blk[:, 4].astype(np.uint32)
            | (blk[:, 5].astype(np.uint32) << 8)
            | (blk[:, 6].astype(np.uint32) << 16)
            | (blk[:, 7].astype(np.uint32) << 24)
        )
        rgb_px, alpha_px = _decode_dxt_color_blocks(c0, c1, cbits, dxt1_alpha=True)
        rgba_blk = np.empty((n_blk, 16, 4), dtype=np.uint8)
        rgba_blk[..., :3] = rgb_px
        rgba_blk[..., 3] = alpha_px
        img = rgba_blk.reshape(bh, bw, 4, 4, 4).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 4)
        return img[:h, :w, :][::-1].copy()

    return None


# ---------------------------------------------------------------------------
# Pure-Python PNG Encoder, Anti-Aliased Resizer & Avatar Compositor
# ---------------------------------------------------------------------------

def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)


def encode_png_rgba(rgba: np.ndarray, compress_level: int = 1) -> bytes:
    """Encode `(H, W, 4)` uint8 numpy array to a PNG byte buffer."""
    arr = np.ascontiguousarray(rgba, dtype=np.uint8)
    h, w, c = arr.shape
    if c != 4:
        raise ValueError(f"Expected RGBA array shape (H, W, 4), got {arr.shape}")
    padded = np.empty((h, w * 4 + 1), dtype=np.uint8)
    padded[:, 0] = 0
    padded[:, 1:] = arr.reshape(h, w * 4)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    idat = zlib.compress(padded.tobytes(), level=compress_level)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b"")


def resize_rgba(rgba: np.ndarray, out_w: int, out_h: int) -> np.ndarray:
    """Anti-aliased box/bilinear RGBA resizer using float32 numpy interpolation."""
    src = rgba.astype(np.float32)
    # Premultiply alpha to avoid dark halos on transparent borders
    a = src[..., 3:4] / 255.0
    src_pm = np.concatenate([src[..., :3] * a, src[..., 3:4]], axis=-1)

    in_h, in_w = src.shape[:2]
    if in_h > out_h * 2 or in_w > out_w * 2:
        # 2x step downsampling for smooth anti-aliased portraits/school emblems
        cur = src_pm
        ch, cw = in_h, in_w
        while ch >= out_h * 2 and cw >= out_w * 2:
            nh, nw = ch // 2, cw // 2
            cur = cur[: nh * 2, : nw * 2].reshape(nh, 2, nw, 2, 4).mean(axis=(1, 3))
            ch, cw = nh, nw
        src_pm = cur
        in_h, in_w = ch, cw

    y_coords = (np.arange(out_h, dtype=np.float32) + 0.5) * (in_h / float(out_h)) - 0.5
    x_coords = (np.arange(out_w, dtype=np.float32) + 0.5) * (in_w / float(out_w)) - 0.5
    y0 = np.clip(np.floor(y_coords).astype(np.int32), 0, in_h - 1)
    y1 = np.clip(y0 + 1, 0, in_h - 1)
    x0 = np.clip(np.floor(x_coords).astype(np.int32), 0, in_w - 1)
    x1 = np.clip(x0 + 1, 0, in_w - 1)
    wy = (y_coords - np.floor(y_coords))[:, None, None]
    wx = (x_coords - np.floor(x_coords))[None, :, None]

    top = src_pm[y0][:, x0] * (1.0 - wx) + src_pm[y0][:, x1] * wx
    bot = src_pm[y1][:, x0] * (1.0 - wx) + src_pm[y1][:, x1] * wx
    dst_pm = top * (1.0 - wy) + bot * wy

    out_a = np.clip(dst_pm[..., 3:4], 0.0, 255.0)
    safe_a = np.where(out_a > 0.5, out_a / 255.0, 1.0)
    rgb = np.where(out_a > 0.5, dst_pm[..., :3] / safe_a, 0.0)
    out = np.empty((out_h, out_w, 4), dtype=np.uint8)
    out[..., :3] = np.clip(np.round(rgb), 0, 255).astype(np.uint8)
    out[..., 3:4] = np.clip(np.round(out_a), 0, 255).astype(np.uint8)
    return out


def render_circular_avatar_png(
    rgba: np.ndarray,
    size: int,
    bg_rgb: tuple[int, int, int] | None = None,
    ring_rgb: tuple[int, int, int] = (218, 226, 236),
    ring_px: float = 1.6,
    inner_fill_rgb: tuple[int, int, int] = (244, 247, 251),
) -> bytes:
    """
    Center-crop (upper-face biased for BA 252x204 portraits), resize, and mask into an
    anti-aliased MomoTalk circular avatar PNG.
    """
    h, w = rgba.shape[:2]
    side = min(h, w)
    x0 = max(0, (w - side) // 2)
    y0 = max(0, min(h - side, int((h - side) * 0.35)))
    cropped = rgba[y0 : y0 + side, x0 : x0 + side]
    sq = resize_rgba(cropped, size, size).astype(np.float32)

    # Blend portrait onto soft off-white backing first (portraits often have alpha corners)
    pa = sq[..., 3:4] / 255.0
    fill = np.array(inner_fill_rgb, dtype=np.float32)[None, None, :]
    comp_rgb = sq[..., :3] * pa + fill * (1.0 - pa)

    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32) + 0.5
    cx = cy = size / 2.0
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    r_outer = (size / 2.0) - 0.6
    r_inner = max(1.0, r_outer - ring_px)

    outer_cov = np.clip(r_outer + 0.6 - dist, 0.0, 1.0)[..., None]
    ring_cov = np.clip(dist - r_inner + 0.5, 0.0, 1.0)[..., None] * outer_cov
    ring_arr = np.array(ring_rgb, dtype=np.float32)[None, None, :]
    ringed_rgb = comp_rgb * (1.0 - ring_cov) + ring_arr * ring_cov

    out = np.zeros((size, size, 4), dtype=np.uint8)
    if bg_rgb is not None:
        bg_arr = np.array(bg_rgb, dtype=np.float32)[None, None, :]
        final_rgb = ringed_rgb * outer_cov + bg_arr * (1.0 - outer_cov)
        out[..., :3] = np.clip(np.round(final_rgb), 0, 255).astype(np.uint8)
        out[..., 3] = 255
    else:
        out[..., :3] = np.clip(np.round(ringed_rgb), 0, 255).astype(np.uint8)
        out[..., 3] = np.clip(np.round(outer_cov[..., 0] * 255.0), 0, 255).astype(np.uint8)
    return encode_png_rgba(out)


def render_fallback_avatar_png(
    name_kr: str,
    school_kr: str,
    size: int = 50,
    bg_rgb: tuple[int, int, int] | None = None,
    ring_rgb: tuple[int, int, int] = (218, 226, 236),
) -> bytes:
    """Generate a crisp Blue Archive MomoTalk circular badge avatar when a Steam texture is unavailable."""
    code = SCHOOL_CODE_MAP.get(school_kr, "ETC")
    accent = SCHOOL_ACCENT_RGB.get(code, (118, 138, 168))
    hsh = hashlib.md5((name_kr or "BA").encode("utf-8")).digest()
    # Slight pastel shift per character
    c1 = np.array(
        [
            min(245, int(accent[0] * 0.72 + 55 + (hsh[0] % 18))),
            min(245, int(accent[1] * 0.72 + 55 + (hsh[1] % 18))),
            min(245, int(accent[2] * 0.72 + 55 + (hsh[2] % 18))),
        ],
        dtype=np.float32,
    )
    c2 = np.clip(c1 + np.array([28.0, 24.0, 32.0], dtype=np.float32), 0, 252)

    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32) + 0.5
    grad = ((yy / max(1, size)) * 0.65 + (xx / max(1, size)) * 0.35)[..., None]
    canvas = np.zeros((size, size, 4), dtype=np.float32)
    canvas[..., :3] = c1[None, None, :] * (1.0 - grad) + c2[None, None, :] * grad

    # Draw stylized subtle Blue Archive halo + shoulder silhouette inside the badge
    cx = cy = size / 2.0
    dist_halo = np.sqrt((xx - cx) ** 2 + ((yy - size * 0.27) * 1.45) ** 2)
    halo_mask = np.clip(1.0 - np.abs(dist_halo - size * 0.16) / 1.4, 0.0, 1.0)[..., None]
    dist_head = np.sqrt((xx - cx) ** 2 + (yy - size * 0.48) ** 2)
    head_mask = np.clip(size * 0.165 - dist_head, 0.0, 1.0)[..., None]
    dist_body = np.sqrt(((xx - cx) * 0.78) ** 2 + (yy - size * 0.88) ** 2)
    body_mask = np.clip(size * 0.28 - dist_body, 0.0, 1.0)[..., None]
    sil_mask = np.maximum(halo_mask * 0.85, np.maximum(head_mask, body_mask)) * 0.90
    canvas[..., :3] = canvas[..., :3] * (1.0 - sil_mask) + 255.0 * sil_mask
    canvas[..., 3] = 255.0

    return render_circular_avatar_png(
        canvas.astype(np.uint8),
        size=size,
        bg_rgb=bg_rgb,
        ring_rgb=ring_rgb,
        ring_px=1.6,
        inner_fill_rgb=(245, 247, 250),
    )


def render_momotalk_emblem_png(size: int = 30) -> bytes:
    """Crisp fallback/vector MomoTalk peach speech-bubble emblem."""
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32) + 0.5
    cx, cy = size * 0.50, size * 0.46
    r = size * 0.36
    dist = np.sqrt((xx - cx) ** 2 + ((yy - cy) * 1.06) ** 2)
    cov = np.clip(r + 0.8 - dist, 0.0, 1.0)
    # Speech tail bottom-left
    tail = np.clip(
        1.0 - np.maximum(np.abs((xx - size * 0.26) - (yy - size * 0.68) * -0.45) - size * 0.07, 0.0),
        0.0,
        1.0,
    ) * ((yy >= size * 0.60) & (yy <= size * 0.88)).astype(np.float32)
    shape_a = np.clip(np.maximum(cov, tail), 0.0, 1.0)

    rgb = np.zeros((size, size, 3), dtype=np.float32)
    rgb[..., 0] = 255.0
    rgb[..., 1] = 255.0
    rgb[..., 2] = 255.0
    # 3 coral dots inside bubble
    dots = np.zeros((size, size), dtype=np.float32)
    for dx in (-0.18, 0.0, 0.18):
        dd = np.sqrt((xx - (cx + size * dx)) ** 2 + (yy - cy) ** 2)
        dots = np.maximum(dots, np.clip(size * 0.068 - dd, 0.0, 1.0))
    rgb[..., 0] = rgb[..., 0] * (1.0 - dots) + 251.0 * dots
    rgb[..., 1] = rgb[..., 1] * (1.0 - dots) + 115.0 * dots
    rgb[..., 2] = rgb[..., 2] * (1.0 - dots) + 144.0 * dots

    out = np.zeros((size, size, 4), dtype=np.uint8)
    out[..., :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    out[..., 3] = np.clip(np.round(shape_a * 255.0), 0, 255).astype(np.uint8)
    return encode_png_rgba(out)


def _crop_tight_square_rgba(rgba: np.ndarray, pad_ratio: float = 0.08) -> np.ndarray:
    """Crop RGBA image tightly to non-transparent pixels and center inside a padded square."""
    if rgba.ndim != 3 or rgba.shape[2] < 4:
        return rgba
    mask = rgba[..., 3] > 8
    if not np.any(mask):
        return rgba
    ys, xs = np.nonzero(mask)
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    cropped = rgba[y0:y1, x0:x1]
    ch, cw = cropped.shape[:2]
    side = max(ch, cw)
    pad = max(1, int(round(side * pad_ratio)))
    out_side = side + pad * 2
    sq = np.zeros((out_side, out_side, 4), dtype=np.uint8)
    oy = (out_side - ch) // 2
    ox = (out_side - cw) // 2
    sq[oy : oy + ch, ox : ox + cw] = cropped
    return sq


def render_momotalk_heart_png(size: int = 24) -> bytes:
    """
    Crisp vector fallback matching Blue Archive's iconic MomoTalk faceted pink heart
    (used on the right edge of student profile rows when Steam assets are unavailable).
    """
    sz = max(12, int(size))
    # 2x supersampled anti-aliasing for silky smooth edges at 22~28px
    ss = sz * 2
    yy, xx = np.mgrid[0:ss, 0:ss].astype(np.float32) + 0.5
    # Map to normalized heart coordinates: x in [-1.32, 1.32], y inverted in [-1.20, 1.38]
    nx = (xx / ss - 0.5) * 2.68
    ny = (0.54 - yy / ss) * 2.68
    # Classic smooth algebraic heart implicit curve: (x^2 + y^2 - 1)^3 - x^2 * y^3 <= 0
    # Slight vertical stretch on lobes for authentic Blue Archive plump heart silhouette
    hy = ny - 0.06 * np.sqrt(np.abs(nx) + 1e-4)
    val = (nx * nx + hy * hy - 0.96) ** 3 - (nx * nx) * (hy ** 3) * 1.15
    # Approximate SDF normalization via analytic gradient magnitude for uniform AA border
    gx = 6.0 * nx * (nx * nx + hy * hy - 0.96) ** 2 - 2.30 * nx * (hy ** 3)
    gy = 6.0 * hy * (nx * nx + hy * hy - 0.96) ** 2 - 3.45 * (nx * nx) * (hy * hy)
    grad_mag = np.sqrt(gx * gx + gy * gy) + 1e-4
    sdf_px = (val / grad_mag) * (ss / 2.68)
    alpha = np.clip(0.5 - sdf_px, 0.0, 1.0)

    # Blue Archive faceted coral-pink crystal palette (#FFA7CA -> #FB7390)
    t_grad = np.clip((yy / ss) * 0.72 + (xx / ss) * 0.28, 0.0, 1.0)
    top_rgb = np.array([255.0, 168.0, 206.0], dtype=np.float32)
    bot_rgb = np.array([251.0, 115.0, 148.0], dtype=np.float32)
    rgb = top_rgb[None, None, :] * (1.0 - t_grad[..., None]) + bot_rgb[None, None, :] * t_grad[..., None]

    # Subtle triangular diamond facet shimmer (echoing Img_Growth_Effect_HeartIcon)
    f1 = ((np.floor((xx + yy * 0.55) / (ss * 0.22)) + np.floor((yy - xx * 0.35) / (ss * 0.22))) % 2) * 0.06
    rgb = np.clip(rgb + f1[..., None] * 255.0, 0.0, 255.0)

    # Soft upper-left crystal highlight glow
    hl = np.exp(-(((nx + 0.52) / 0.42) ** 2 + ((ny - 0.48) / 0.36) ** 2)) * 0.36
    rgb = rgb * (1.0 - hl[..., None]) + 255.0 * hl[..., None]

    hi = np.zeros((ss, ss, 4), dtype=np.float32)
    hi[..., :3] = np.clip(rgb, 0.0, 255.0)
    hi[..., 3] = np.clip(alpha * 255.0, 0.0, 255.0)
    # 2x box downsample with premultiplied alpha
    a_hi = hi[..., 3:4] / 255.0
    pm = np.concatenate([hi[..., :3] * a_hi, a_hi], axis=-1)
    pm_ds = pm.reshape(sz, 2, sz, 2, 4).mean(axis=(1, 3))
    out_a = pm_ds[..., 3:4]
    out_rgb = np.where(out_a > 1e-4, pm_ds[..., :3] / np.maximum(out_a, 1e-4), 0.0)
    out = np.zeros((sz, sz, 4), dtype=np.uint8)
    out[..., :3] = np.clip(np.round(out_rgb), 0, 255).astype(np.uint8)
    out[..., 3] = np.clip(np.round(out_a[..., 0] * 255.0), 0, 255).astype(np.uint8)
    return encode_png_rgba(out)


def render_fallback_school_icon_png(school_kr: str, size: int = 22) -> bytes:
    """Clean geometric circular school crest fallback when Steam school icons aren't loaded."""
    code = SCHOOL_CODE_MAP.get(school_kr, "ETC")
    accent = np.array(SCHOOL_ACCENT_RGB.get(code, (118, 138, 168)), dtype=np.float32)
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32) + 0.5
    cx = cy = size / 2.0
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    outer = np.clip(size * 0.44 + 0.6 - dist, 0.0, 1.0)
    inner = np.clip(size * 0.31 + 0.6 - dist, 0.0, 1.0)
    cross = np.maximum(
        np.clip(1.4 - np.abs(xx - cx), 0.0, 1.0),
        np.clip(1.4 - np.abs(yy - cy), 0.0, 1.0),
    ) * inner

    out = np.zeros((size, size, 4), dtype=np.uint8)
    rgb = np.zeros((size, size, 3), dtype=np.float32)
    rgb[:] = accent[None, None, :] * 0.92
    rgb = rgb * (1.0 - inner[..., None] * 0.72) + 250.0 * (inner[..., None] * 0.72)
    rgb = rgb * (1.0 - cross[..., None] * 0.85) + accent[None, None, :] * (cross[..., None] * 0.85)
    out[..., :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    out[..., 3] = np.clip(np.round(outer * 255.0), 0, 255).astype(np.uint8)
    return encode_png_rgba(out)


# ---------------------------------------------------------------------------
# Cached MomoTalk Steam Asset Manager
# ---------------------------------------------------------------------------

def _candidate_portrait_tokens(char_data: dict[str, Any]) -> list[str]:
    tokens: list[str] = []
    ptoks = char_data.get("portrait_tokens") or []
    if isinstance(ptoks, (list, tuple)):
        for raw_t in ptoks:
            tok = str(raw_t or "").strip()
            if tok and tok not in tokens:
                tokens.append(tok)
            if "_" in tok and tok.rsplit("_", 1)[-1].isdigit():
                base_tok = tok.rsplit("_", 1)[0]
                if base_tok and base_tok not in tokens:
                    tokens.append(base_tok)

    cid = str(char_data.get("id") or "").strip()
    if cid in CHARACTER_PORTRAIT_ALIASES:
        tokens.extend(CHARACTER_PORTRAIT_ALIASES[cid])

    lvp = char_data.get("local_voice_packs") or {}
    if isinstance(lvp, dict):
        for lst in lvp.values():
            if not isinstance(lst, list):
                continue
            for vp in lst:
                tail = str(vp).replace("\\", "/").split("/")[-1].replace(".molru", "")
                for pref in ("JP_", "KR_"):
                    if tail.upper().startswith(pref):
                        tail = tail[len(pref) :]
                if tail and tail not in tokens:
                    tokens.append(tail)

    mp = str(char_data.get("molru_prefix") or "").rstrip("_")
    for pref in ("JP_", "KR_"):
        if mp.upper().startswith(pref):
            mp = mp[len(pref) :]
    if mp and mp not in tokens:
        tokens.append(mp)

    for fallback in (cid, cid.replace("_", "")):
        if fallback and fallback not in tokens:
            tokens.append(fallback)
    return tokens


def resolve_portrait_key(char_data: dict[str, Any], portraits_index: dict[str, Any]) -> str | None:
    """Resolve `config/characters/<id>.json` against indexed Steam `Student_Portrait_*` / `NPC_Portrait_*` keys."""
    if not portraits_index:
        return None
    tokens = _candidate_portrait_tokens(char_data)
    for tok in tokens:
        tl = tok.lower()
        for prefix in ("student_portrait_", "npc_portrait_"):
            k = prefix + tl
            if k in portraits_index:
                return k
    for tok in tokens:
        tl = tok.lower()
        if len(tl) < 3:
            continue
        hits = [
            pk
            for pk in portraits_index
            if (pk.endswith("_" + tl) or ("_" + tl + "_" in pk)) and not pk.endswith("_small")
        ]
        if hits:
            return sorted(hits, key=len)[0]
    return None


class MomoTalkAssetManager:
    """Lazy, thread-safe extractor & disk cache for MomoTalk portraits, school crests, and UI icons."""

    INDEX_VERSION = 3

    def __init__(self, ba_root_hint: str | Path | None = None) -> None:
        self._ba_root_hint = ba_root_hint
        self._lock = threading.Lock()
        self._bundle_cache_lock = threading.Lock()
        self._index: dict[str, Any] | None = None
        self._raw_rgba_cache: dict[str, np.ndarray] = {}
        self._bundle_header_cache: dict[str, dict[str, Any]] = {}
        self._block_cache: OrderedDict[tuple[str, int], bytes] = OrderedDict()
        self._cached_env_dir: str | None = None
        self._cached_root_path: Path | None = None
        self._rendered_dir_path: Path | None = None
        self._rendered_files: set[str] | None = None
        self._indexing_thread: threading.Thread | None = None

    def set_ba_root_hint(self, ba_root_hint: str | Path | None) -> None:
        """Update Steam Blue Archive root hint at runtime and clear stale empty bundle index."""
        with self._lock:
            self._ba_root_hint = ba_root_hint
            self._index = None

    def _ensure_cache_paths(self) -> tuple[Path, Path, set[str]]:
        env_dir = os.environ.get("VOICE_COMPANION_DATA_DIR", "")
        with self._lock:
            if (
                self._cached_env_dir == env_dir
                and self._cached_root_path is not None
                and self._rendered_dir_path is not None
                and self._rendered_files is not None
            ):
                return self._cached_root_path, self._rendered_dir_path, self._rendered_files
            root = get_cache_dir() / "momotalk_ui"
            rendered = root / "rendered"
            rendered.mkdir(parents=True, exist_ok=True)
            try:
                files = set(os.listdir(rendered))
            except Exception:
                files = set()
            self._cached_env_dir = env_dir
            self._cached_root_path = root
            self._rendered_dir_path = rendered
            self._rendered_files = files
            return root, rendered, files

    @property
    def cache_root(self) -> Path:
        return self._ensure_cache_paths()[0]

    def _resource_root_dir(self) -> Path | None:
        ba = _discover_ba_root(self._ba_root_hint)
        if ba is None:
            return None
        res = ba / "BlueArchive_Data" / "StreamingAssets" / "PUB" / "Resource"
        return res if res.is_dir() else None

    def _game_data_dir(self) -> Path | None:
        res = self._resource_root_dir()
        if res is None:
            return None
        gd = res / "GameData" / "Windows"
        return gd if gd.is_dir() else None

    def ensure_index(self) -> dict[str, Any]:
        with self._lock:
            if self._index is not None:
                return self._index

        res_root = self._resource_root_dir()
        gd = self._game_data_dir()
        if gd is None or res_root is None:
            empty = {"version": self.INDEX_VERSION, "gd": "", "portraits": {}, "schools": {}, "ui_icons": {}}
            with self._lock:
                self._index = empty
            return empty

        idx_path = self.cache_root / f"bundle_index_v{self.INDEX_VERSION}.json"
        if idx_path.exists():
            try:
                loaded = json.loads(idx_path.read_text(encoding="utf-8"))
                if (
                    isinstance(loaded, dict)
                    and loaded.get("version") == self.INDEX_VERSION
                    and loaded.get("gd") == str(gd)
                    and isinstance(loaded.get("portraits"), dict)
                ):
                    with self._lock:
                        self._index = loaded
                    return loaded
            except Exception:
                pass

        portraits: dict[str, dict[str, Any]] = {}
        schools: dict[str, dict[str, Any]] = {}
        ui_icons: dict[str, dict[str, Any]] = {}

        preload_win = res_root / "Preload" / "Windows"
        scan_specs: list[tuple[Path, str, str]] = [
            (gd, "uis-01_common-01_character-*.bundle", "portraits"),
            (preload_win, "prologgroup-assets-*01_common-01_character*.bundle", "portraits"),
            (gd, "uis-01_common-11_schoolicon-*.bundle", "schools"),
            (gd, "uis-03_scenario-03_emoticon-sprite-*.bundle", "ui_icons"),
            (gd, "uis-00_images-_mxload-2024-11-18_000_*.bundle", "ui_icons"),
        ]
        for base_dir, pat, bucket in scan_specs:
            if not base_dir.is_dir():
                continue
            for bp in sorted(base_dir.glob(pat)):
                try:
                    cab = read_unityfs_node_slice(
                        bp,
                        lambda k: not k.endswith((".resS", ".resource")),
                        header_cache=self._bundle_header_cache,
                        block_cache=self._block_cache,
                        cache_lock=self._bundle_cache_lock,
                    )
                except Exception:
                    continue
                if cab is None:
                    continue
                try:
                    rel_bundle = str(bp.relative_to(res_root)).replace("\\", "/")
                except Exception:
                    rel_bundle = f"GameData/Windows/{bp.name}"
                for entry in parse_serialized_texture2d_entries(cab):
                    nm = entry["name"]
                    nlow = nm.lower()
                    meta = {
                        "name": nm,
                        "bundle": bp.name,
                        "bundle_rel": rel_bundle,
                        "w": entry["w"],
                        "h": entry["h"],
                        "fmt": entry["fmt"],
                        "stream_off": entry["stream_off"],
                        "stream_sz": entry["stream_sz"],
                    }
                    if bucket == "portraits":
                        portraits[nlow] = meta
                    elif bucket == "schools":
                        if nlow.startswith("school_icon_"):
                            schools[nm[len("School_Icon_") :].upper()] = meta
                    else:
                        if any(k in nlow for k in ("momo", "phone", "sns", "star", "heart")):
                            ui_icons[nlow] = meta

        idx_obj = {
            "version": self.INDEX_VERSION,
            "gd": str(gd),
            "portraits": portraits,
            "schools": schools,
            "ui_icons": ui_icons,
        }
        try:
            idx_path.write_text(json.dumps(idx_obj, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        with self._lock:
            self._index = idx_obj
        return idx_obj

    def ensure_index_async(self, on_ready: Callable[[], None] | None = None) -> None:
        with self._lock:
            already = self._index is not None
            thr = self._indexing_thread
        if already:
            if on_ready is not None:
                on_ready()
            return
        if thr is not None and thr.is_alive():
            return

        def _worker() -> None:
            self.ensure_index()
            if on_ready is not None:
                try:
                    on_ready()
                except Exception:
                    pass

        t = threading.Thread(target=_worker, daemon=True, name="momotalk-asset-index")
        with self._lock:
            self._indexing_thread = t
        t.start()

    def is_index_ready(self) -> bool:
        with self._lock:
            return self._index is not None

    def _extract_texture_rgba(self, meta: dict[str, Any]) -> np.ndarray | None:
        cache_key = f"{meta.get('bundle_rel') or meta.get('bundle')}|{meta.get('name')}"
        with self._bundle_cache_lock:
            if cache_key in self._raw_rgba_cache:
                return self._raw_rgba_cache[cache_key]

        res_root = self._resource_root_dir()
        gd = self._game_data_dir()
        bp: Path | None = None
        if res_root is not None and meta.get("bundle_rel"):
            cand = res_root / str(meta["bundle_rel"])
            if cand.is_file():
                bp = cand
        if bp is None and gd is not None and meta.get("bundle"):
            cand = gd / str(meta["bundle"])
            if cand.is_file():
                bp = cand
        if bp is None:
            return None

        try:
            raw_pixels = b""
            s_sz = int(meta.get("stream_sz") or 0)
            s_off = int(meta.get("stream_off") or 0)
            if s_sz > 0:
                sliced = read_unityfs_node_slice(
                    bp,
                    lambda k: k.endswith(".resS"),
                    rel_offset=s_off,
                    max_bytes=s_sz,
                    header_cache=self._bundle_header_cache,
                    block_cache=self._block_cache,
                    cache_lock=self._bundle_cache_lock,
                )
                if sliced is not None and len(sliced) == s_sz:
                    raw_pixels = sliced
            if not raw_pixels:
                cab = read_unityfs_node_slice(
                    bp,
                    lambda k: not k.endswith((".resS", ".resource")),
                    header_cache=self._bundle_header_cache,
                    block_cache=self._block_cache,
                    cache_lock=self._bundle_cache_lock,
                )
                if cab is not None:
                    for entry in parse_serialized_texture2d_entries(cab):
                        if entry["name"] == meta["name"]:
                            raw_pixels = entry.get("inline_bytes") or b""
                            break
            rgba = decode_texture2d_rgba(raw_pixels, int(meta["w"]), int(meta["h"]), int(meta["fmt"]))
            if rgba is not None:
                with self._bundle_cache_lock:
                    if len(self._raw_rgba_cache) > 120:
                        self._raw_rgba_cache.clear()
                    self._raw_rgba_cache[cache_key] = rgba
            return rgba
        except Exception:
            return None

    @staticmethod
    def _portrait_cache_name(
        char_data: dict[str, Any],
        size: int = 50,
        bg_rgb: tuple[int, int, int] | None = None,
        ring_rgb: tuple[int, int, int] = (218, 226, 236),
    ) -> str:
        raw_cid = str(char_data.get("entry_id") or char_data.get("id") or "unknown")
        cid = raw_cid.replace("/", "_").replace("\\", "_")
        ring_tag = f"{ring_rgb[0]:02x}{ring_rgb[1]:02x}{ring_rgb[2]:02x}"
        if bg_rgb is None:
            return f"pa_{cid}_{size}_alpha_{ring_tag}.png"
        bg_tag = f"{bg_rgb[0]:02x}{bg_rgb[1]:02x}{bg_rgb[2]:02x}_{ring_tag}"
        return f"p_{cid}_{size}_{bg_tag}.png"

    @staticmethod
    def _school_cache_name(school_kr: str, size: int = 22) -> str:
        code = SCHOOL_CODE_MAP.get(school_kr, "ETC").upper()
        return f"sch_{code}_{size}.png"

    @staticmethod
    def _ui_cache_name(icon_kind: str, size: int = 28) -> str:
        kind = icon_kind.lower().strip()
        return f"ui_{kind}_{size}.png"

    def _portrait_cache_path(
        self,
        char_data: dict[str, Any],
        size: int = 50,
        bg_rgb: tuple[int, int, int] | None = None,
        ring_rgb: tuple[int, int, int] = (218, 226, 236),
    ) -> Path:
        return self._ensure_cache_paths()[1] / self._portrait_cache_name(
            char_data, size=size, bg_rgb=bg_rgb, ring_rgb=ring_rgb
        )

    def _school_cache_path(self, school_kr: str, size: int = 22) -> Path:
        return self._ensure_cache_paths()[1] / self._school_cache_name(school_kr, size=size)

    def _ui_cache_path(self, icon_kind: str, size: int = 28) -> Path:
        return self._ensure_cache_paths()[1] / self._ui_cache_name(icon_kind, size=size)

    def _mark_rendered_cached(self, fname: str) -> None:
        with self._lock:
            if self._rendered_files is not None:
                self._rendered_files.add(fname)

    def has_cached_portrait(
        self,
        char_data: dict[str, Any],
        size: int = 50,
        bg_rgb: tuple[int, int, int] | None = None,
        ring_rgb: tuple[int, int, int] = (218, 226, 236),
    ) -> bool:
        fname = self._portrait_cache_name(char_data, size=size, bg_rgb=bg_rgb, ring_rgb=ring_rgb)
        return fname in self._ensure_cache_paths()[2]

    def has_cached_school_icon(self, school_kr: str, size: int = 22) -> bool:
        return self._school_cache_name(school_kr, size=size) in self._ensure_cache_paths()[2]

    def has_cached_ui_icon(self, icon_kind: str, size: int = 28) -> bool:
        return self._ui_cache_name(icon_kind, size=size) in self._ensure_cache_paths()[2]

    def get_portrait_png(
        self,
        char_data: dict[str, Any],
        size: int = 50,
        bg_rgb: tuple[int, int, int] | None = None,
        ring_rgb: tuple[int, int, int] = (218, 226, 236),
        use_steam_if_unindexed: bool = True,
        cached_only: bool = False,
    ) -> bytes:
        """
        Return circular PNG bytes for a character.
        Uses disk cache under `%LOCALAPPDATA%\\VoiceCompanionData\\cache\\momotalk_ui\\rendered\\`.
        When `cached_only=True` and the disk cache misses, returns the fast fallback PNG immediately.
        """
        cid = str(char_data.get("id") or "unknown")
        _, rendered_dir, files = self._ensure_cache_paths()
        fname = self._portrait_cache_name(char_data, size=size, bg_rgb=bg_rgb, ring_rgb=ring_rgb)
        steam_cache_file = rendered_dir / fname
        if fname in files:
            try:
                return steam_cache_file.read_bytes()
            except Exception:
                with self._lock:
                    if self._rendered_files is not None:
                        self._rendered_files.discard(fname)

        if not cached_only:
            idx = self._index if (self._index is not None or not use_steam_if_unindexed) else self.ensure_index()
            if idx is not None:
                portraits = idx.get("portraits") or {}
                pkey = resolve_portrait_key(char_data, portraits)
                if pkey and pkey in portraits:
                    rgba = self._extract_texture_rgba(portraits[pkey])
                    if rgba is not None:
                        png_bytes = render_circular_avatar_png(rgba, size=size, bg_rgb=bg_rgb, ring_rgb=ring_rgb)
                        try:
                            steam_cache_file.write_bytes(png_bytes)
                            self._mark_rendered_cached(fname)
                        except Exception:
                            pass
                        return png_bytes

        return render_fallback_avatar_png(
            name_kr=str(char_data.get("name_kr") or cid),
            school_kr=str(char_data.get("school") or ""),
            size=size,
            bg_rgb=bg_rgb,
            ring_rgb=ring_rgb,
        )

    def get_school_icon_png(
        self,
        school_kr: str,
        size: int = 22,
        use_steam_if_unindexed: bool = True,
        cached_only: bool = False,
    ) -> bytes:
        code = SCHOOL_CODE_MAP.get(school_kr, "ETC").upper()
        _, rendered_dir, files = self._ensure_cache_paths()
        fname = self._school_cache_name(school_kr, size=size)
        cache_file = rendered_dir / fname
        if fname in files:
            try:
                return cache_file.read_bytes()
            except Exception:
                with self._lock:
                    if self._rendered_files is not None:
                        self._rendered_files.discard(fname)

        if not cached_only:
            idx = self._index if (self._index is not None or not use_steam_if_unindexed) else self.ensure_index()
            if idx is not None:
                schools = idx.get("schools") or {}
                meta = schools.get(code) or schools.get("ETC")
                if meta is not None:
                    rgba = self._extract_texture_rgba(meta)
                    if rgba is not None:
                        resized = resize_rgba(rgba, size, size)
                        png_bytes = encode_png_rgba(resized)
                        try:
                            cache_file.write_bytes(png_bytes)
                            self._mark_rendered_cached(fname)
                        except Exception:
                            pass
                        return png_bytes
        return render_fallback_school_icon_png(school_kr, size=size)

    def get_ui_icon_png(
        self,
        icon_kind: str,
        size: int = 28,
        use_steam_if_unindexed: bool = True,
        cached_only: bool = False,
    ) -> bytes:
        kind = icon_kind.lower().strip()
        _, rendered_dir, files = self._ensure_cache_paths()
        fname = self._ui_cache_name(kind, size=size)
        cache_file = rendered_dir / fname
        if fname in files:
            try:
                return cache_file.read_bytes()
            except Exception:
                with self._lock:
                    if self._rendered_files is not None:
                        self._rendered_files.discard(fname)

        target_names = {
            "momotalk": ("emoji_momotalk",),
            "phone": ("emoji_phone",),
            "star": ("icon_momotalk_star",),
            "heart": ("img_growth_effect_hearticon",),
        }.get(kind, (kind,))

        if not cached_only:
            idx = self._index if (self._index is not None or not use_steam_if_unindexed) else self.ensure_index()
            if idx is not None:
                ui_icons = idx.get("ui_icons") or {}
                for tn in target_names:
                    if tn in ui_icons:
                        rgba = self._extract_texture_rgba(ui_icons[tn])
                        if rgba is not None:
                            if kind == "heart":
                                rgba = _crop_tight_square_rgba(rgba, pad_ratio=0.08)
                            png_bytes = encode_png_rgba(resize_rgba(rgba, size, size))
                            try:
                                cache_file.write_bytes(png_bytes)
                                self._mark_rendered_cached(fname)
                            except Exception:
                                pass
                            return png_bytes
        if kind == "heart":
            return render_momotalk_heart_png(size=size)
        return render_momotalk_emblem_png(size=size)


# ---------------------------------------------------------------------------
# Realistic Outgoing Call Waiting Ringback Tone & Call Chimes
# ---------------------------------------------------------------------------

class MomoTalkCallAudioFX:
    """Synthesizes warm stereo modern messenger outgoing ringback, pickup, and hangup chimes."""

    @staticmethod
    def _synth_warm_note(
        sr: int,
        freq_hz: float,
        dur_sec: float,
        *,
        attack_sec: float = 0.007,
        decay_rate: float = 7.2,
        brightness: float = 0.30,
        stereo_spread: float = 0.0014,
    ) -> np.ndarray:
        """Synthesize a smooth stereo electric-kalimba/marimba note `(N, 2)` float32."""
        n = max(64, int(round(sr * max(0.02, dur_sec))))
        t = np.arange(n, dtype=np.float32) / float(sr)

        att_n = max(8, int(round(sr * attack_sec)))
        att_n = min(att_n, max(8, n // 3))
        rel_n = max(16, int(round(sr * min(0.028, dur_sec * 0.25))))
        rel_n = min(rel_n, max(8, n // 2))

        env = np.exp(-t * float(decay_rate)).astype(np.float32)
        att_curve = 0.5 * (1.0 - np.cos(np.linspace(0.0, np.pi, att_n, dtype=np.float32)))
        rel_curve = 0.5 * (1.0 + np.cos(np.linspace(0.0, np.pi, rel_n, dtype=np.float32)))
        env[:att_n] *= att_curve
        env[-rel_n:] *= rel_curve

        nyq_limit = sr * 0.42
        out = np.empty((n, 2), dtype=np.float32)
        for ch, detune in enumerate((1.0 - stereo_spread, 1.0 + stereo_spread)):
            f1 = freq_hz * detune
            sig = np.sin(2.0 * np.pi * f1 * t) * np.exp(-t * float(decay_rate))
            if brightness > 0.0 and (2.0 * f1) < nyq_limit:
                sig += (
                    (0.22 * brightness)
                    * np.sin(2.0 * np.pi * (2.0 * f1) * t)
                    * np.exp(-t * float(decay_rate * 2.1))
                )
            if brightness > 0.0 and (3.0 * f1) < nyq_limit:
                sig += (
                    (0.06 * brightness)
                    * np.sin(2.0 * np.pi * (3.0 * f1) * t)
                    * np.exp(-t * float(decay_rate * 3.8))
                )
            sig *= env[:n] / np.maximum(np.exp(-t * float(decay_rate)), 1e-6)
            out[:, ch] = sig.astype(np.float32)
        return out

    @staticmethod
    def _mix_note(buf: np.ndarray, note: np.ndarray, start_idx: int, gain_l: float = 1.0, gain_r: float = 1.0) -> None:
        if start_idx >= len(buf) or len(note) == 0:
            return
        usable = min(len(note), len(buf) - start_idx)
        if usable <= 0:
            return
        buf[start_idx : start_idx + usable, 0] += note[:usable, 0] * float(gain_l)
        buf[start_idx : start_idx + usable, 1] += note[:usable, 1] * float(gain_r)

    @staticmethod
    def _apply_stereo_ambience(stereo: np.ndarray, sr: int) -> np.ndarray:
        """Add subtle warm stereo early reflections without lengthening the buffer."""
        if len(stereo) < 16:
            return stereo
        out = stereo.copy()
        lpf_kernel = np.array([0.25, 0.5, 0.25], dtype=np.float32)
        warm_src_l = np.convolve(stereo[:, 0], lpf_kernel, mode="same").astype(np.float32)
        warm_src_r = np.convolve(stereo[:, 1], lpf_kernel, mode="same").astype(np.float32)
        taps = (
            (0.068, 0.22, 0.12),
            (0.142, 0.09, 0.16),
            (0.225, 0.08, 0.04),
        )
        for delay_sec, gain_l, gain_r in taps:
            d = int(round(sr * delay_sec))
            if 0 < d < len(out):
                out[d:, 0] += warm_src_l[:-d] * float(gain_l) + warm_src_r[:-d] * float(gain_r * 0.45)
                out[d:, 1] += warm_src_r[:-d] * float(gain_r) + warm_src_l[:-d] * float(gain_l * 0.45)
        edge = max(8, min(len(out) // 8, int(round(sr * 0.015))))
        ramp = 0.5 * (1.0 - np.cos(np.linspace(0.0, np.pi, edge, dtype=np.float32)))
        out[:edge] *= ramp[:, None]
        out[-edge:] *= ramp[::-1, None]
        return out

    @classmethod
    def _normalize_to_target(cls, stereo: np.ndarray, target_peak: float) -> np.ndarray:
        peak = float(np.max(np.abs(stereo))) if len(stereo) else 0.0
        if peak <= 1e-6:
            return stereo.astype(np.float32)
        return np.clip(stereo * (float(target_peak) / peak), -0.92, 0.92).astype(np.float32)

    @classmethod
    def build_dial_and_ringback_cycle(cls, sample_rate: int, volume: float = 0.22) -> tuple[np.ndarray, np.ndarray]:
        """
        Return `(intro_stereo, loop_stereo)` as `float32` `(N, 2)` arrays:
        - `intro_stereo`: soft modern messenger call-initiated chime + brief pause
        - `loop_stereo`: clean melodic 2-pulse marimba-bell ringback ("또로롱~ 또로롱~") + calm pause (3.0s cycle)
        """
        sr = max(8000, int(sample_rate))
        vol = float(max(0.02, min(0.8, volume)))

        # Tactile glassy call-start intro (0.26s)
        intro_len = max(256, int(round(sr * 0.26)))
        intro = np.zeros((intro_len, 2), dtype=np.float32)
        n_intro_hi = cls._synth_warm_note(sr, 880.00, 0.16, attack_sec=0.004, decay_rate=14.0, brightness=0.36)
        n_intro_lo = cls._synth_warm_note(sr, 523.25, 0.14, attack_sec=0.005, decay_rate=16.0, brightness=0.20)
        cls._mix_note(intro, n_intro_lo, 0, 0.42, 0.46)
        cls._mix_note(intro, n_intro_hi, int(round(sr * 0.012)), 0.92, 0.88)
        intro = cls._apply_stereo_ambience(intro, sr)
        intro = cls._normalize_to_target(intro, vol * 0.65)

        # 3.0s smooth modern messenger ringback cycle (double warm C5->E5->G5 pulse + calm rest)
        cycle_len = max(sr * 2, int(round(sr * 3.00)))
        loop = np.zeros((cycle_len, 2), dtype=np.float32)
        pulse_offsets = (
            (0.000, 1.00),
            (0.540, 0.82),
        )
        arp_notes = (
            (0.000, 523.25, 0.26, 9.0, 0.78, 0.72),   # C5
            (0.072, 659.25, 0.28, 8.2, 0.80, 0.88),   # E5
            (0.144, 783.99, 0.56, 6.2, 0.96, 0.96),   # G5
        )
        for base_sec, p_scale in pulse_offsets:
            for dt, freq, dur, dec, gl, gr in arp_notes:
                note = cls._synth_warm_note(
                    sr,
                    freq,
                    dur,
                    attack_sec=0.008,
                    decay_rate=dec,
                    brightness=0.30,
                )
                cls._mix_note(loop, note, int(round(sr * (base_sec + dt))), gl * p_scale, gr * p_scale)

        loop = cls._apply_stereo_ambience(loop, sr)
        loop = cls._normalize_to_target(loop, vol * 0.82)
        return intro, loop

    @classmethod
    def build_connect_pickup_chime(cls, sample_rate: int, volume: float = 0.22) -> np.ndarray:
        """Warm crisp 2-step pickup chime played the moment the call connects."""
        sr = max(8000, int(sample_rate))
        vol = float(max(0.02, min(0.8, volume)))
        total_len = max(400, int(round(sr * 0.44)))
        buf = np.zeros((total_len, 2), dtype=np.float32)

        n_pop = cls._synth_warm_note(sr, 392.00, 0.05, attack_sec=0.003, decay_rate=32.0, brightness=0.18)
        n_g5 = cls._synth_warm_note(sr, 783.99, 0.20, attack_sec=0.005, decay_rate=10.5, brightness=0.36)
        n_e5 = cls._synth_warm_note(sr, 659.25, 0.30, attack_sec=0.006, decay_rate=7.8, brightness=0.22)
        n_c6 = cls._synth_warm_note(sr, 1046.50, 0.34, attack_sec=0.006, decay_rate=6.8, brightness=0.34)
        cls._mix_note(buf, n_pop, 0, 0.32, 0.32)
        cls._mix_note(buf, n_g5, int(round(sr * 0.008)), 0.85, 0.78)
        cls._mix_note(buf, n_e5, int(round(sr * 0.072)), 0.38, 0.42)
        cls._mix_note(buf, n_c6, int(round(sr * 0.072)), 0.92, 1.00)

        buf = cls._apply_stereo_ambience(buf, sr)
        return cls._normalize_to_target(buf, vol * 0.86)

    @classmethod
    def build_hangup_tone(cls, sample_rate: int, volume: float = 0.18) -> np.ndarray:
        """Polite soft descending two-note call-ended cue."""
        sr = max(8000, int(sample_rate))
        vol = float(max(0.02, min(0.8, volume)))
        total_len = max(360, int(round(sr * 0.38)))
        buf = np.zeros((total_len, 2), dtype=np.float32)

        n_hi = cls._synth_warm_note(sr, 783.99, 0.18, attack_sec=0.006, decay_rate=10.5, brightness=0.28)
        n_lo = cls._synth_warm_note(sr, 587.33, 0.26, attack_sec=0.007, decay_rate=8.0, brightness=0.22)
        cls._mix_note(buf, n_hi, 0, 0.85, 0.80)
        cls._mix_note(buf, n_lo, int(round(sr * 0.085)), 0.88, 0.92)

        buf = cls._apply_stereo_ambience(buf, sr)
        return cls._normalize_to_target(buf, vol * 0.78)


class RingbackStreamState:
    """Thread-safe streaming cursor feeding outgoing ringback chunks into PortAudio idle frames."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = False
        self._paused = False
        self._in_intro = True
        self._pos = 0
        self._wave_cache: dict[int, tuple[np.ndarray, np.ndarray]] = {}

    @property
    def is_active(self) -> bool:
        with self._lock:
            return bool(self._active and not self._paused)

    def start(self) -> None:
        with self._lock:
            self._active = True
            self._paused = False
            self._in_intro = True
            self._pos = 0

    def pause(self) -> None:
        with self._lock:
            self._paused = True

    def resume(self) -> None:
        with self._lock:
            if self._active:
                self._paused = False

    def stop(self) -> None:
        with self._lock:
            self._active = False
            self._paused = False
            self._in_intro = True
            self._pos = 0

    def _get_waves(self, sample_rate: int) -> tuple[np.ndarray, np.ndarray]:
        sr = max(8000, int(sample_rate))
        waves = self._wave_cache.get(sr)
        if waves is None:
            waves = MomoTalkCallAudioFX.build_dial_and_ringback_cycle(sr)
            self._wave_cache[sr] = waves
        return waves

    def next_chunk(self, n_frames: int, sample_rate: int, volume: float = 1.0) -> np.ndarray:
        n = max(1, int(n_frames))
        with self._lock:
            if (not self._active) or self._paused:
                return np.zeros((n, 2), dtype=np.float32)
            intro, loop = self._get_waves(sample_rate)
            out = np.empty((n, 2), dtype=np.float32)
            filled = 0
            while filled < n:
                buf = intro if self._in_intro else loop
                rem_buf = len(buf) - self._pos
                if rem_buf <= 0:
                    if self._in_intro:
                        self._in_intro = False
                    self._pos = 0
                    continue
                take = min(n - filled, rem_buf)
                out[filled : filled + take] = buf[self._pos : self._pos + take]
                filled += take
                self._pos += take
                if self._pos >= len(buf):
                    if self._in_intro:
                        self._in_intro = False
                    self._pos = 0
        scale = float(max(0.0, min(1.5, volume)))
        if scale != 1.0:
            out = np.clip(out * scale, -0.95, 0.95).astype(np.float32)
        return out
