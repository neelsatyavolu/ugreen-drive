"""Minimal streaming ZIP (STORE) for multi-file downloads without buffering whole files."""

from __future__ import annotations

import os
import stat
import struct
import time
import zlib
from pathlib import Path
from typing import Iterator

# Larger reads cut syscall/Python overhead when streaming media folders.
_READ_CHUNK = 4 * 1024 * 1024

# ZIP32 unsigned max. Tests monkeypatch this to exercise ZIP64 without 4GiB files.
ZIP32_MAX = 0xFFFFFFFF
_ZIP32_MARKER = 0xFFFFFFFF
_ZIP16_MARKER = 0xFFFF
_ZIP64_VERSION = 45
_ZIP_VERSION = 20
_GP_DATA_DESCRIPTOR = 0x08
_ZIP64_EXTRA_ID = 1
_ZIP64_EOCD_REMAINING = 44  # bytes after signature+size fields
# "Version made by" host byte: Unix, so unzip / Archive Utility honour the mode bits below.
_HOST_UNIX = 3 << 8
# Scripts that must stay runnable after unzipping (e.g. the Mac installers' .command files).
_EXECUTABLE_SUFFIXES = {".command", ".sh", ".tool"}


def _dos_time(ts: float | None = None) -> tuple[int, int]:
    t = time.localtime(ts if ts is not None else time.time())
    dos_time = (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)
    dos_date = ((t.tm_year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday
    return dos_time, dos_date


def _is_app_program(path: Path) -> bool:
    """A Mac app's program file: Something.app/Contents/MacOS/<name> (no suffix to go by)."""
    contents = path.parent.parent
    return path.parent.name == "MacOS" and contents.name == "Contents" and contents.parent.suffix.lower() == ".app"


def _unix_mode(path: Path) -> int:
    """rwxr-xr-x for scripts and app programs; rw-r--r-- otherwise. On-disk bits are ignored: the NAS reports 0777."""
    executable = path.suffix.lower() in _EXECUTABLE_SUFFIXES or _is_app_program(path)
    return stat.S_IFREG | (0o755 if executable else 0o644)


def _u32(n: int) -> int:
    return n if n <= ZIP32_MAX else _ZIP32_MARKER


def _u16_count(n: int) -> int:
    return n if n <= _ZIP16_MARKER else _ZIP16_MARKER


def _zip64_extra(values: list[int]) -> bytes:
    if not values:
        return b""
    return struct.pack("<HH" + "Q" * len(values), _ZIP64_EXTRA_ID, 8 * len(values), *values)


def _member_name(arcname: str) -> bytes:
    name = arcname.replace("\\", "/").lstrip("/")
    if not name or name.startswith("../") or "/../" in f"/{name}/":
        raise ValueError(f"Invalid archive name: {arcname!r}")
    return name.encode("utf-8")


def _plan_member(name_b: bytes, size: int, local_offset: int) -> dict[str, object]:
    """Byte layout for one STORE member — shared by the streamer and Content-Length."""
    zip64_size = size > ZIP32_MAX
    local_extra = _zip64_extra([0, 0]) if zip64_size else b""
    extra_vals: list[int] = []
    cd_size = size
    cd_offset = local_offset
    if size > ZIP32_MAX:
        extra_vals.extend([size, size])
        cd_size = _ZIP32_MARKER
    if local_offset > ZIP32_MAX:
        extra_vals.append(local_offset)
        cd_offset = _ZIP32_MARKER
    cd_extra = _zip64_extra(extra_vals)
    return {
        "zip64_size": zip64_size,
        "version": _ZIP64_VERSION if zip64_size else _ZIP_VERSION,
        "local_extra": local_extra,
        "local_len": 30 + len(name_b) + len(local_extra),
        "desc_len": 24 if zip64_size else 16,
        "cd_extra": cd_extra,
        "cd_len": 46 + len(name_b) + len(cd_extra),
        "cd_version": _ZIP64_VERSION if extra_vals else _ZIP_VERSION,
        "cd_size": cd_size,
        "cd_offset": cd_offset,
    }


def _eocd_len(entry_count: int, central_size: int, central_offset: int) -> int:
    if (
        entry_count > _ZIP16_MARKER
        or central_size > ZIP32_MAX
        or central_offset > ZIP32_MAX
    ):
        return 56 + 20 + 22  # zip64 EOCD + locator + classic EOCD
    return 22


def zip_store_content_length(entries: list[tuple[str, Path]], *, size_of=None) -> int:
    """Exact byte length of ``stream_zip_store(entries)`` from current file sizes.

    ``size_of`` stats as the NAS user. The default follows ``path.stat`` for callers
    that already hold the file as themselves.
    """
    offset = 0
    central_size = 0
    for arcname, path in entries:
        name_b = _member_name(arcname)
        size = int(size_of(path) if size_of else path.stat().st_size)
        plan = _plan_member(name_b, size, offset)
        central_size += int(plan["cd_len"])
        offset += int(plan["local_len"]) + size + int(plan["desc_len"])
    return offset + central_size + _eocd_len(len(entries), central_size, offset)


def stream_zip_store(entries: list[tuple[str, Path]], *, open_file=None) -> Iterator[bytes]:
    """
    Yield a ZIP archive for (arcname, absolute_path) pairs using STORE (no recompress).

    Suitable for already-compressed media (mp4, jpg, zip, …) and keeps CPU low on the NAS.
    Streams file bodies in 4 MiB chunks; never buffers the full archive.
    Uses ZIP64 when a size or offset exceeds 4GiB-1 so folder zips of large
    add-on packs (and nested .zip/.rar members) stay readable.

    ``open_file`` must return a binary file opened as the NAS user. The body is
    read from that handle, never by reopening the path as the server user.
    """
    opener = open_file or (lambda path: open(path, "rb"))
    central_chunks: list[bytes] = []
    offset = 0

    for arcname, path in entries:
        name_b = _member_name(arcname)
        with opener(path) as handle:
            st = os.fstat(handle.fileno())
            size = int(st.st_size)
            dos_time, dos_date = _dos_time(st.st_mtime)
            local_offset = offset
            plan = _plan_member(name_b, size, local_offset)
            zip64_size = bool(plan["zip64_size"])
            extra = plan["local_extra"]
            # Local header: bit 3 → CRC/sizes follow in a data descriptor.
            # ZIP64 sizes are 0xFFFFFFFF in the 32-bit fields (and 0 in extra
            # until the descriptor), matching CPython zipfile.
            local_csize = _ZIP32_MARKER if zip64_size else 0
            local = struct.pack(
                "<IHHHHHIIIHH",
                0x04034B50,
                plan["version"],
                _GP_DATA_DESCRIPTOR,
                0,  # STORE
                dos_time,
                dos_date,
                0,
                local_csize,
                local_csize,
                len(name_b),
                len(extra),
            )
            local += name_b
            local += extra
            yield local
            offset += len(local)

            crc = 0
            written = 0
            while True:
                block = handle.read(_READ_CHUNK)
                if not block:
                    break
                crc = zlib.crc32(block, crc) & 0xFFFFFFFF
                written += len(block)
                offset += len(block)
                yield block

        if written != size:
            size = written

        if zip64_size:
            desc = struct.pack("<IIQQ", 0x08074B50, crc, size, size)
        else:
            desc = struct.pack("<IIII", 0x08074B50, crc, size, size)
        yield desc
        offset += len(desc)

        cd_plan = _plan_member(name_b, size, local_offset)
        cd_extra = cd_plan["cd_extra"]
        central = struct.pack(
            "<IHHHHHHIIIHHHHHII",
            0x02014B50,
            _HOST_UNIX | int(cd_plan["cd_version"]),
            cd_plan["cd_version"],
            _GP_DATA_DESCRIPTOR,
            0,
            dos_time,
            dos_date,
            crc,
            cd_plan["cd_size"],
            cd_plan["cd_size"],
            len(name_b),
            len(cd_extra),
            0,
            0,
            0,
            _unix_mode(path) << 16,
            cd_plan["cd_offset"],
        )
        central += name_b
        central += cd_extra
        central_chunks.append(central)

    central_blob = b"".join(central_chunks)
    central_offset = offset
    yield central_blob
    offset += len(central_blob)

    n = len(central_chunks)
    cd_size = len(central_blob)
    need_zip64_end = _eocd_len(n, cd_size, central_offset) > 22
    if need_zip64_end:
        zip64_eocd = struct.pack(
            "<IQHHIIQQQQ",
            0x06064B50,
            _ZIP64_EOCD_REMAINING,
            _ZIP64_VERSION,
            _ZIP64_VERSION,
            0,
            0,
            n,
            n,
            cd_size,
            central_offset,
        )
        yield zip64_eocd
        locator = struct.pack("<IIQI", 0x07064B50, 0, offset, 1)
        yield locator
        offset += len(zip64_eocd) + len(locator)

    end = struct.pack(
        "<IHHHHIIH",
        0x06054B50,
        0,
        0,
        _u16_count(n),
        _u16_count(n),
        _u32(cd_size),
        _u32(central_offset),
        0,
    )
    yield end
