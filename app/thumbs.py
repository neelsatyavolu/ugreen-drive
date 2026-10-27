"""Cached image/video thumbnails. Read-only against the share; thumbs live in a
local cache dir (IFD_THUMB_CACHE_DIR) keyed by (path, mtime, size, thumb size),
so renames and edits regenerate naturally and stale entries are just ignored.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageOps

log = logging.getLogger("uvicorn.error")

ALLOWED_SIZES = (64, 256, 512)
# SVG excluded (Pillow can't rasterize it); HEIC would need pillow-heif.
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif"}
VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi"}
JPEG_QUALITY = 80
FFMPEG_TIMEOUT_S = 15
MAX_CONCURRENT = max(1, int(os.environ.get("IFD_THUMB_CONCURRENCY", "4")))

# Cap concurrent generation so one photo-folder scroll can't fork dozens of
# Pillow/ffmpeg jobs; per-key locks stop duplicate work on request bursts.
_gen_slots = threading.Semaphore(MAX_CONCURRENT)
_key_locks: dict[str, threading.Lock] = {}
_key_locks_guard = threading.Lock()

# Background prefetch ("warm") pool. One fewer worker than the semaphore so an
# interactive request can always grab a generation slot ahead of the backlog.
_warm_pool = ThreadPoolExecutor(
    max_workers=max(1, MAX_CONCURRENT - 1), thread_name_prefix="thumb-warm"
)
_warm_pending: set[str] = set()
_warm_guard = threading.Lock()


def warm_paths(paths: list[Path], size: int, *, open_source=None) -> int:
    """Queue background thumbnail generation; returns how many were queued.

    Already-pending paths are skipped; cache hits return instantly in the job.
    ``open_source(path)`` must return an authorized open (``.path``, ``.dev_path()``,
    ``.stat``, ``.close()``). Without it, ``path`` is read directly — tests and
    callers that already dropped privileges.
    """
    queued = 0
    for src in paths:
        pending_key = str(src)
        with _warm_guard:
            if pending_key in _warm_pending:
                continue
            _warm_pending.add(pending_key)

        def job(src=src, pending_key=pending_key, open_source=open_source):
            opened = None
            try:
                read_path = None
                stat_result = None
                key_path = src
                if open_source is not None:
                    opened = open_source(src)
                    read_path = Path(opened.dev_path())
                    stat_result = opened.stat
                    key_path = opened.path
                get_or_create(key_path, size, read_path=read_path, stat_result=stat_result)
            except Exception:
                log.warning("thumb: warm failed for %s", src, exc_info=True)
            finally:
                if opened is not None:
                    opened.close()
                with _warm_guard:
                    _warm_pending.discard(pending_key)

        _warm_pool.submit(job)
        queued += 1
    return queued


def cache_dir() -> Path:
    return Path(os.environ.get("IFD_THUMB_CACHE_DIR", "/tmp/ifd-thumbs"))


def snap_size(size: int) -> int:
    return min(ALLOWED_SIZES, key=lambda allowed: abs(allowed - size))


def thumb_kind(name: str) -> str | None:
    ext = Path(name).suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return None


def cache_key(abs_path: str, mtime_ns: int, fsize: int, thumb_size: int) -> str:
    raw = f"{abs_path}\x00{mtime_ns}\x00{fsize}\x00{thumb_size}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _lock_for(key: str) -> threading.Lock:
    with _key_locks_guard:
        return _key_locks.setdefault(key, threading.Lock())


def _make_image_thumb(src: Path, dest: Path, size: int) -> bool:
    try:
        with Image.open(src) as im:
            # JPEG DCT-scaled decode (1/2..1/8 res) — ~5-10x faster for photos.
            # No-op for other formats. Must run before pixels are loaded.
            im.draft("RGB", (size, size))
            im = ImageOps.exif_transpose(im)
            im.thumbnail((size, size))
            im.convert("RGB").save(dest, "JPEG", quality=JPEG_QUALITY)
        return True
    except Exception:
        log.warning("thumb: image generation failed for %s", src, exc_info=True)
        return False


def _dev_fd(src: Path) -> int | None:
    """fd number when ``src`` is an authorized ``/dev/fd/N``."""
    text = str(src)
    prefix = "/dev/fd/"
    if not text.startswith(prefix) or not text[len(prefix):].isdigit():
        return None
    return int(text[len(prefix):])


def _run_ffmpeg(cmd: list[str], src: Path, timeout: int) -> subprocess.CompletedProcess[bytes]:
    """``close_fds`` drops every fd except those in ``pass_fds``."""
    fd = _dev_fd(src)
    kwargs: dict = {"capture_output": True, "timeout": timeout}
    if fd is not None:
        kwargs["pass_fds"] = (fd,)
    try:
        return subprocess.run(cmd, **kwargs)
    finally:
        if fd is not None:
            try:
                os.set_inheritable(fd, False)
            except OSError:
                pass


def _make_video_thumb(src: Path, dest: Path, size: int) -> bool:
    cmd = [
        "ffmpeg", "-loglevel", "error", "-ss", "1", "-i", str(src),
        "-frames:v", "1", "-vf", f"scale='min({size},iw)':-2",
        "-f", "image2", "-y", str(dest),
    ]
    try:
        proc = _run_ffmpeg(cmd, src, FFMPEG_TIMEOUT_S)
        return proc.returncode == 0 and dest.exists() and dest.stat().st_size > 0
    except (subprocess.TimeoutExpired, OSError):
        log.warning("thumb: ffmpeg failed for %s", src, exc_info=True)
        return False


def get_or_create(
    src: Path, size: int, *, read_path: Path | None = None, stat_result=None
) -> Path | None:
    """Return the cached thumbnail path for `src`, generating it if needed.

    None = no thumbnail possible (unsupported type, unreadable, corrupt).
    When ``read_path`` is set (an already-authorized ``/dev/fd/N``), pixels are
    read from that fd. ``src`` is only the cache identity and the filename.
    """
    kind = thumb_kind(src.name)
    if kind is None:
        return None
    source = read_path or src
    try:
        stat = stat_result if stat_result is not None else source.stat()
    except OSError:
        return None
    size = snap_size(size)
    key = cache_key(str(src), stat.st_mtime_ns, stat.st_size, size)
    out = cache_dir() / f"{key}.jpg"
    if out.exists():
        return out
    with _lock_for(key):
        if out.exists():
            return out
        with _gen_slots:
            cache_dir().mkdir(parents=True, exist_ok=True)
            partial = out.with_name(f".{out.name}.partial")
            made = (
                _make_image_thumb(source, partial, size)
                if kind == "image"
                else _make_video_thumb(source, partial, size)
            )
            if not made:
                partial.unlink(missing_ok=True)
                result = None
            else:
                partial.replace(out)
                result = out
    # Locks are one-shot: waiters re-check the cache, so dropping is safe and
    # keeps the dict from growing with every file ever thumbnailed.
    with _key_locks_guard:
        _key_locks.pop(key, None)
    return result
