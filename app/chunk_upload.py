"""Chunked / resumable upload sessions for multi-stream transfers through the tunnel.

Sessions live under the process temp dir (not the share listing). When the
caller passes the destination folder, chunks are written straight into the
hidden ``.<name>.<hex>.partial`` file there (at their offsets, as the user), so
completing is a size check and a rename; otherwise (legacy) each chunk is a
file here and completing copies them into place. Either way an empty marker
per received chunk lets the client PUT in parallel and resume after a drop by
re-querying which indices exist.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from fsops import FSError, _translate_os_errors, as_root, as_user, finalize_partial, write_upload_stream

CHUNK_SIZE_DEFAULT = 32 * 1024 * 1024  # 32 MiB
CHUNK_SIZE_MIN = 1 * 1024 * 1024
CHUNK_SIZE_MAX = 32 * 1024 * 1024
MAX_UPLOAD_BYTES = 50 * 1024 * 1024 * 1024  # 50 GiB
SESSION_TTL_SEC = 24 * 3600
SESSION_ROOT = Path(os.environ.get("IFD_CHUNK_UPLOAD_DIR", "") or (Path("/tmp") / "ifd-chunk-uploads"))

_lock = threading.Lock()
# upload_id -> metadata (also mirrored to meta.json on disk for restart resilience)
_sessions: dict[str, dict[str, Any]] = {}


def _now() -> float:
    return time.time()


def _session_dir(upload_id: str) -> Path:
    return SESSION_ROOT / upload_id


def _meta_path(upload_id: str) -> Path:
    return _session_dir(upload_id) / "meta.json"


def _chunk_path(upload_id: str, index: int) -> Path:
    return _session_dir(upload_id) / f"c{index:06d}"


def _safe_filename(name: str) -> str:
    name = Path(name or "").name
    if not name or name in (".", ".."):
        raise FSError("Invalid filename")
    return name


def _load_meta_from_disk(upload_id: str) -> dict[str, Any] | None:
    path = _meta_path(upload_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("upload_id") != upload_id:
            return None
        return data
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def cleanup_expired() -> int:
    """Remove sessions older than TTL. Returns number removed."""
    removed = 0
    now = _now()
    with _lock:
        # Disk first (covers process restarts)
        if SESSION_ROOT.is_dir():
            for child in list(SESSION_ROOT.iterdir()):
                if not child.is_dir():
                    continue
                meta = _load_meta_from_disk(child.name)
                created = float(meta.get("created", 0)) if meta else 0
                if not meta or now - created > SESSION_TTL_SEC:
                    if meta:
                        _remove_partial(meta)
                    shutil.rmtree(child, ignore_errors=True)
                    _sessions.pop(child.name, None)
                    removed += 1
        # Drop stale in-memory entries whose dirs are gone
        for uid in list(_sessions.keys()):
            if not _session_dir(uid).is_dir():
                _sessions.pop(uid, None)
    return removed


def package_upload_max_bytes(rel_dir: str, default: int = MAX_UPLOAD_BYTES) -> int:
    parts = Path(rel_dir).parts
    if (len(parts) == 5 and parts[0] == "Package Storage"
            and parts[1].startswith("Cycle ") and parts[3] == "A-roll B-roll"):
        if parts[4] == "A-roll":
            return 30 * 1024 ** 3
        if parts[4] == "B-roll":
            return 15 * 1024 ** 3
    return default


def create_session(
    *,
    username: str,
    uid: int,
    gid: int,
    share: str,
    rel_dir: str,
    filename: str,
    size: int,
    chunk_size: int | None = None,
    dest_dir: Path | None = None,
) -> dict[str, Any]:
    """Start a session. ``dest_dir``: the resolved destination folder (share
    root pinned by the caller) to write chunks into directly."""
    cleanup_expired()
    filename = _safe_filename(filename)
    max_bytes = package_upload_max_bytes(rel_dir)
    if size < 0 or size > max_bytes:
        raise FSError(f"File too large (max {max_bytes // (1024 * 1024 * 1024)}GB)", 413)
    if size == 0:
        raise FSError("Empty files use the simple upload path", 400)

    cs = int(chunk_size or CHUNK_SIZE_DEFAULT)
    cs = max(CHUNK_SIZE_MIN, min(CHUNK_SIZE_MAX, cs))
    total_chunks = max(1, (size + cs - 1) // cs)

    upload_id = secrets.token_urlsafe(18)
    meta: dict[str, Any] = {
        "upload_id": upload_id,
        "username": username,
        "uid": int(uid),
        "gid": int(gid),
        "share": share,
        "path": (rel_dir or "").strip().lstrip("/"),
        "name": filename,
        "size": int(size),
        "chunk_size": cs,
        "total_chunks": int(total_chunks),
        "created": _now(),
    }
    if dest_dir is not None:
        partial = Path(dest_dir) / f".{filename}.{secrets.token_hex(8)}.partial"
        with as_user(int(uid), int(gid), username or None), _translate_os_errors():
            os.close(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666))
        meta["partial"] = str(partial)
    try:
        with as_root():
            SESSION_ROOT.mkdir(parents=True, exist_ok=True)
            _session_dir(upload_id).mkdir(parents=True, exist_ok=False)
            path = _meta_path(upload_id)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(meta), encoding="utf-8")
            os.replace(tmp, path)
    except OSError as e:
        _remove_partial(meta)
        raise FSError("Could not start upload session — retry", 503) from e
    with _lock:
        _sessions[upload_id] = meta
    return {
        "upload_id": upload_id,
        "chunk_size": cs,
        "total_chunks": total_chunks,
        "size": size,
        "received": [],
    }


def peek_session(upload_id: str) -> dict[str, Any]:
    """Load session metadata without checking the owning user."""
    if not upload_id or "/" in upload_id or ".." in upload_id:
        raise FSError("Invalid upload id", 400)
    with _lock:
        meta = _sessions.get(upload_id)
    if meta is None:
        meta = _load_meta_from_disk(upload_id)
        if meta is None:
            raise FSError("Upload session not found or expired", 404)
        with _lock:
            _sessions[upload_id] = meta
    if _now() - float(meta.get("created", 0)) > SESSION_TTL_SEC:
        raise FSError("Upload session expired", 404)
    return meta


def session_rel_path(meta: dict[str, Any]) -> str:
    folder = str(meta.get("path") or "").strip().strip("/")
    name = str(meta.get("name") or "").strip()
    return f"{folder}/{name}" if folder and name else (name or folder)


def get_session(upload_id: str, *, username: str, uid: int) -> dict[str, Any]:
    meta = peek_session(upload_id)
    if meta.get("username") != username or int(meta.get("uid", -1)) != int(uid):
        raise FSError("Upload session not found or expired", 404)
    return meta


def received_indices(upload_id: str) -> list[int]:
    d = _session_dir(upload_id)
    if not d.is_dir():
        return []
    out: list[int] = []
    for child in d.iterdir():
        name = child.name
        if name.startswith("c") and name[1:].isdigit() and child.is_file():
            out.append(int(name[1:]))
    out.sort()
    return out


def status(upload_id: str, *, username: str, uid: int) -> dict[str, Any]:
    meta = get_session(upload_id, username=username, uid=uid)
    got = received_indices(upload_id)
    return {
        "upload_id": upload_id,
        "chunk_size": meta["chunk_size"],
        "total_chunks": meta["total_chunks"],
        "size": meta["size"],
        "name": meta["name"],
        "path": meta["path"],
        "received": got,
        "bytes_received": sum(
            _chunk_path(upload_id, i).stat().st_size for i in got if _chunk_path(upload_id, i).is_file()
        ),
    }


def write_chunk(
    upload_id: str,
    index: int,
    chunks: Iterator[bytes],
    *,
    username: str,
    uid: int,
    expected_size: int | None = None,
) -> dict[str, Any]:
    meta = get_session(upload_id, username=username, uid=uid)
    total = int(meta["total_chunks"])
    if index < 0 or index >= total:
        raise FSError("Chunk index out of range", 400)

    cs = int(meta["chunk_size"])
    size = int(meta["size"])
    if index < total - 1:
        want = cs
    else:
        want = size - cs * (total - 1)
        if want <= 0:
            want = size if total == 1 else cs

    if meta.get("partial"):
        return _write_chunk_in_place(meta, index, chunks, want, expected_size)

    dest = _chunk_path(upload_id, index)
    # Unique temp per attempt so a retry cannot interleave with an in-flight write.
    tmp = _session_dir(upload_id) / f"c{index:06d}.{secrets.token_hex(8)}.part"
    written = 0
    out = None
    try:
        # Open/replace under as_root; the fd stays valid across euid changes
        # so the body pump does not hold the credential lock.
        with as_root():
            out = open(tmp, "wb")  # noqa: SIM115
        try:
            for piece in chunks:
                if not piece:
                    continue
                written += len(piece)
                if written > want:
                    raise FSError("Chunk larger than expected", 413)
                out.write(piece)
        finally:
            out.close()
            out = None
        if expected_size is not None and written != expected_size:
            raise FSError("Chunk size mismatch", 400)
        if written != want:
            raise FSError(f"Incomplete chunk (got {written}, expected {want})", 400)
        with as_root():
            os.replace(tmp, dest)
    except Exception:
        if out is not None:
            try:
                out.close()
            except OSError:
                pass
        try:
            if tmp.exists():
                with as_root():
                    if tmp.exists():
                        tmp.unlink()
        except OSError:
            pass
        raise

    return {"index": index, "received": written, "ok": True}


def _write_chunk_in_place(
    meta: dict[str, Any], index: int, chunks: Iterator[bytes], want: int, expected_size: int | None
) -> dict[str, Any]:
    """Write one chunk at its offset in the partial file, then mark it received.

    The fd is opened as the user and stays valid after euid is restored, so the
    network-paced body pump doesn't hold the credential lock. A retry rewrites
    the same range; the marker only appears once the whole chunk is written.
    """
    upload_id = meta["upload_id"]
    offset = index * int(meta["chunk_size"])
    with as_user(int(meta["uid"]), int(meta["gid"]), meta.get("username") or None), _translate_os_errors():
        fd = os.open(meta["partial"], os.O_WRONLY)
    written = 0
    try:
        for piece in chunks:
            if not piece:
                continue
            if written + len(piece) > want:
                raise FSError("Chunk larger than expected", 413)
            view = memoryview(piece)
            while view:
                n = os.pwrite(fd, view, offset + written)
                written += n
                view = view[n:]
    except OSError as e:
        raise FSError("Could not write the upload — retry", 503) from e
    finally:
        os.close(fd)
    if expected_size is not None and written != expected_size:
        raise FSError("Chunk size mismatch", 400)
    if written != want:
        raise FSError(f"Incomplete chunk (got {written}, expected {want})", 400)
    with as_root():
        _chunk_path(upload_id, index).touch()
    return {"index": index, "received": written, "ok": True}


def _remove_partial(meta: dict[str, Any]) -> None:
    """Delete a session's partial file, if it has one left."""
    partial = meta.get("partial")
    if not partial:
        return
    try:
        with as_user(int(meta["uid"]), int(meta["gid"]), meta.get("username") or None):
            Path(partial).unlink(missing_ok=True)
    except (FSError, OSError):
        try:
            with as_root():
                Path(partial).unlink(missing_ok=True)
        except OSError:
            pass


def complete_session(
    upload_id: str, *, username: str, uid: int, expect_mtime_ns: int | None = None
) -> dict[str, Any]:
    meta = get_session(upload_id, username=username, uid=uid)
    total = int(meta["total_chunks"])
    got = set(received_indices(upload_id))
    missing = [i for i in range(total) if i not in got]
    if missing:
        raise FSError(f"Missing chunks: {missing[:12]}{'…' if len(missing) > 12 else ''}", 400)

    if meta.get("partial"):
        result = finalize_partial(
            Path(meta["partial"]),
            meta["path"],
            meta["name"],
            int(meta["uid"]),
            int(meta["gid"]),
            expected_bytes=int(meta["size"]),
            expect_mtime_ns=expect_mtime_ns,
        )
        abort_session(upload_id, username=username, uid=uid, force=True)
        return result

    def iter_all() -> Iterator[bytes]:
        for i in range(total):
            path = _chunk_path(upload_id, i)
            with open(path, "rb") as f:
                while True:
                    block = f.read(4 * 1024 * 1024)
                    if not block:
                        break
                    yield block

    result = write_upload_stream(
        meta["path"],
        meta["name"],
        iter_all(),
        int(meta["uid"]),
        int(meta["gid"]),
        max_bytes=int(meta["size"]),
        expected_bytes=int(meta["size"]),
        expect_mtime_ns=expect_mtime_ns,
    )
    # Success — drop staging
    abort_session(upload_id, username=username, uid=uid, force=True)
    return result


def abort_session(
    upload_id: str,
    *,
    username: str,
    uid: int,
    force: bool = False,
) -> dict[str, Any]:
    meta = get_session(upload_id, username=username, uid=uid) if not force else (
        _sessions.get(upload_id) or _load_meta_from_disk(upload_id))
    if meta:
        _remove_partial(meta)  # gone already after a successful complete
    with _lock:
        _sessions.pop(upload_id, None)
    d = _session_dir(upload_id)
    with as_root():
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
    return {"ok": True, "upload_id": upload_id}
