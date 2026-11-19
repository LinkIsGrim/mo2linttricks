"""Read the file index of a Cyberpunk 2077 .archive (RDAR) without touching file data.

Ported from rfuzzo's red4lib (MIT, https://github.com/rfuzzo/red4lib), src/archive/*.rs.
Only the header, the optional LXRS name footer (uncompressed variant) and the
file table are read, so even multi-GB archives scan in milliseconds.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from pathlib import Path

HEADER_MAGIC = 0x52414452  # "RDAR"
LXRS_MAGIC = 0x4C585253
HEADER = struct.Struct("<IIQIQIQ")  # magic, version, index_pos, index_size, debug_pos, debug_size, filesize
HEADER_EXTENDED_SIZE = 0xAC  # custom data (LXRS) starts here
INDEX = struct.Struct("<IIQIII")  # table_offset, table_size, crc, entries, segments, dependencies
ENTRY = struct.Struct("<QQIIIII20s")  # name hash, timestamp, inline, seg start/end, dep start/end, sha1
SEGMENT = struct.Struct("<QII")  # offset, stored (compressed) size, uncompressed size


class ArchiveError(ValueError):
    pass


@dataclass
class ArchiveIndex:
    hashes: list[int] = field(default_factory=list)
    names: dict[int, str] = field(default_factory=dict)  # from an uncompressed LXRS footer
    names_compressed: bool = False  # LXRS present but Kraken-compressed (not readable here)
    # hash -> [(offset, stored size, size), ...]; the index's own sha1 field is unusable
    # (WolvenKit-built archives write one constant value), so content is compared via these
    segments: dict[int, list[tuple[int, int, int]]] = field(default_factory=dict)


def fnv1a64(text: str) -> int:
    h = 0xCBF29CE484222325
    for b in text.encode("utf-8"):
        h = ((h ^ b) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


def _cstrings(data: bytes, count: int) -> list[str]:
    out, pos = [], 0
    for _ in range(count):
        end = data.find(b"\0", pos)
        if end < 0:
            break
        out.append(data[pos:end].decode("utf-8", "replace"))
        pos = end + 1
    return out


def read_index(path: Path) -> ArchiveIndex:
    with open(path, "rb") as f:
        head = f.read(HEADER.size + 4)
        if len(head) < HEADER.size:
            raise ArchiveError(f"{path.name}: too small")
        magic, _ver, index_pos, _isz, _dpos, _dsz, _fsz = HEADER.unpack_from(head)
        if magic != HEADER_MAGIC:
            raise ArchiveError(f"{path.name}: not an RDAR archive")
        out = ArchiveIndex()

        custom_len = struct.unpack_from("<I", head, HEADER.size)[0] if len(head) >= HEADER.size + 4 else 0
        if custom_len:
            f.seek(HEADER_EXTENDED_SIZE)
            lx = f.read(20)
            if len(lx) == 20:
                lmagic, _lver, size, zsize, count = struct.unpack("<IIIIi", lx)
                if lmagic == LXRS_MAGIC and count > 0:
                    if size == zsize:
                        for name in _cstrings(f.read(size), count):
                            out.names[fnv1a64(name)] = name
                    else:
                        out.names_compressed = True

        f.seek(index_pos)
        idx = f.read(INDEX.size)
        if len(idx) < INDEX.size:
            raise ArchiveError(f"{path.name}: truncated index")
        _off, _tsz, _crc, n_entries, n_seg, _ndep = INDEX.unpack(idx)
        table = f.read(ENTRY.size * n_entries)
        if len(table) < ENTRY.size * n_entries:
            raise ArchiveError(f"{path.name}: truncated file table")
        segs = f.read(SEGMENT.size * n_seg)
        seglist = [SEGMENT.unpack_from(segs, i * SEGMENT.size) for i in range(len(segs) // SEGMENT.size)]
        for i in range(n_entries):
            e = ENTRY.unpack_from(table, i * ENTRY.size)
            out.segments[e[0]] = seglist[e[3] : e[4]]
        out.hashes = sorted(out.segments)
        return out


def content_signature(path: Path, segments: list[tuple[int, int, int]]) -> tuple[int, str]:
    """(uncompressed size, sha1 of the stored bytes) for one file. Equal => identical file.

    Different stored bytes with the same size almost always mean different content
    (same tool, same input => same compressed bytes), different sizes always do.
    """
    h = hashlib.sha1()
    size = 0
    with open(path, "rb") as f:
        for off, zsize, usize in segments:
            f.seek(off)
            h.update(f.read(zsize))
            size += usize
    return size, h.hexdigest()
