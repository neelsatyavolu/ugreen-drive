"""Path-safe file operations under drive root, optionally as a NAS uid/gid."""

from __future__ import annotations

import hashlib
import os
import pwd
import re
import secrets
import shutil
import stat
import threading
import time
import unicodedata
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from config import get_settings

# When set, all drive_root()/resolve_rel() ops use this share folder instead of DRIVE_ROOT.
_active_share_root: ContextVar[Path | None] = ContextVar("active_share_root", default=None)

# seteuid/setegid/initgroups are process credentials from Python's POV and must
# not interleave across threads (list + upload + delete under different NAS users).
_as_user_lock = threading.Lock()
# Bound wait so a stuck holder cannot fill the thread pool forever (site-wide hang).
_AS_USER_LOCK_TIMEOUT_S = 90.0
_HOST_GROUP = Path("/host-etc/group")


def _host_group_rows() -> list[tuple[str, int, list[str]]]:
    """Parse host /etc/group (directory bind; file binds of /etc/group go stale)."""
    if not _HOST_GROUP.is_file():
        return []
    try:
        text = _HOST_GROUP.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows: list[tuple[str, int, list[str]]] = []
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) < 4:
            continue
        try:
            gid = int(parts[2])
        except ValueError:
            continue
        members = [m.strip() for m in parts[3].split(",") if m.strip()]
        rows.append((parts[0], gid, members))
    return rows


def _host_gids_for(username: str, primary_gid: int) -> list[int]:
    """Supplementary gids from host /etc/group when container nss is stale."""
    gids = {int(primary_gid)}
    if not username:
        return [primary_gid]
    for _name, gid, members in _host_group_rows():
        if username in members:
            gids.add(gid)
    return list(gids) if gids else [primary_gid]


def host_group_names(username: str, primary_gid: int) -> list[str]:
    """Primary + supplementary group names from host /etc/group."""
    names: list[str] = []
    seen: set[str] = set()
    by_gid: dict[int, str] = {}
    for gname, gid, members in _host_group_rows():
        by_gid[gid] = gname
        if username and username in members and gname not in seen:
            names.append(gname)
            seen.add(gname)
    primary = by_gid.get(int(primary_gid))
    if primary:
        if primary in seen:
            names.remove(primary)
        names.insert(0, primary)
    return names


class FSError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def drive_root() -> Path:
    override = _active_share_root.get()
    if override is not None:
        if not override.is_dir():
            raise FSError(f"Share root missing: {override}", 500)
        return override
    root = Path(get_settings().drive_root).resolve()
    if not root.is_dir():
        raise FSError(f"Drive root missing: {root}", 500)
    return root


@contextmanager
def use_share_root(root: Path) -> Iterator[Path]:
    """Scope file ops to a specific shared folder root."""
    root = root.resolve()
    if not root.is_dir():
        raise FSError(f"Share root missing: {root}", 404)
    token = _active_share_root.set(root)
    try:
        yield root
    finally:
        _active_share_root.reset(token)


def resolve_rel(rel: str) -> Path:
    """Resolve a user-supplied relative path under drive root (no escape)."""
    root = drive_root()
    rel = (rel or "").strip().lstrip("/")
    # Block traversal segments
    parts = [p for p in Path(rel).parts if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise FSError("Invalid path", 400)
    target = (root.joinpath(*parts)).resolve()
    try:
        target.relative_to(root)
    except ValueError as e:
        raise FSError("Path outside drive", 400) from e
    return target


class AuthorizedFile:
    """A file opened as the NAS user.

    Serve or hash this fd. Reopening ``path`` later runs as the server user
    (root on the NAS) and would follow a symlink swapped in after the check.
    """

    def __init__(self, fd: int, path: Path, name: str, stat_result: os.stat_result):
        self.fd = fd
        self.path = path
        self.name = name
        self.stat = stat_result

    def dev_path(self) -> str:
        if self.fd < 0:
            raise FSError("File is closed", 500)
        return f"/dev/fd/{self.fd}"

    def rewind(self) -> None:
        """macOS ``/dev/fd/N`` is a dup and shares this offset. Serve from the start."""
        if self.fd >= 0:
            os.lseek(self.fd, 0, os.SEEK_SET)

    def close(self) -> None:
        fd = self.fd
        self.fd = -1
        if fd >= 0:
            os.close(fd)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def _fd_realpath(fd: int) -> Path:
    """Path the kernel opened, so a symlink race cannot point outside the share."""
    try:
        return Path(os.readlink(f"/proc/self/fd/{fd}")).resolve()
    except OSError:
        pass
    import fcntl

    # macOS F_GETPATH rejects a buffer longer than MAXPATHLEN (1024).
    try:
        raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
    except (AttributeError, OSError, ValueError) as e:
        raise FSError("Could not verify the opened file", 500) from e
    text = raw.split(b"\0", 1)[0].decode() if isinstance(raw, bytes) else str(raw).split("\0", 1)[0]
    return Path(text).resolve()


def open_authorized_path(
    path: Path, root: Path, uid: int, gid: int, *, name: str | None = None
) -> AuthorizedFile:
    """Open one regular file as ``uid`` and refuse anything outside ``root``."""
    root = root.resolve()
    try:
        resolved = path.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError) as e:
        raise FSError("Path outside drive", 400) from e
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        with as_user(uid, gid):
            fd = os.open(resolved, flags)
    except PermissionError as e:
        raise FSError("Permission denied", 403) from e
    except OSError as e:
        raise FSError("Not found", 404) from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise FSError("Not found", 404)
        _fd_realpath(fd).relative_to(root)
    except FSError:
        os.close(fd)
        raise
    except ValueError as e:
        os.close(fd)
        raise FSError("Path outside drive", 400) from e
    except Exception:
        os.close(fd)
        raise
    return AuthorizedFile(fd, resolved, name or resolved.name, st)


def resolve_within(base_rel: str, sub_rel: str) -> tuple[Path, str]:
    """Resolve `sub_rel` inside folder `base_rel` → (absolute path, normalized sub path).

    For public folder links. `base_rel` is the canonical path stored at mint;
    if it now resolves elsewhere (a symlink was swapped in) it is refused.
    Also refuses `..`, the recycle bin, and symlinks that land outside the folder.
    """
    base = resolve_rel(base_rel)
    if base != drive_root().joinpath(base_rel):
        raise FSError("Not found", 404)
    parts = [p for p in Path((sub_rel or "").strip().lstrip("/")).parts if p not in ("", ".")]
    if any(p in ("..", RECYCLE_NAME) for p in parts):
        raise FSError("Invalid path", 400)
    target = base.joinpath(*parts).resolve()
    try:
        sub = target.relative_to(base)
    except ValueError as e:
        raise FSError("Path outside shared folder", 400) from e
    return target, sub.as_posix() if sub.parts else ""


@contextmanager
def as_user(uid: int, gid: int, username: str | None = None) -> Iterator[None]:
    """Temporarily set effective uid/gid **and supplementary groups**.

    UGOS share ACLs are usually group-based. seteuid/setegid alone leave the
    process with root's (or empty) group list, so permission checks miss
    secondary groups like `students` / `teachers`. We call initgroups() first.
    Requires root + CAP_SETUID/CAP_SETGID.

    Serialized across threads: euid/egid changes must not interleave while
    another request is mid-op under a different NAS identity.

    Callers must keep the critical section short (open/stat/rename/chown). Do
    not hold this across network-paced I/O (upload body pump) — that starves
    every other FS op and can exhaust the thread pool until /api/health hangs.
    """
    # Real uid, not euid: another thread may hold the lock with a student euid,
    # and treating that as "non-root" would run this op as *that* student.
    if os.getuid() != 0:
        # Non-root: run as container user; rely on mount permissions
        yield
        return

    if not _as_user_lock.acquire(timeout=_AS_USER_LOCK_TIMEOUT_S):
        raise FSError("Server busy with another file operation — retry shortly", 503)
    try:
        old_uid, old_gid = os.geteuid(), os.getegid()
        try:
            old_groups = os.getgroups()
        except OSError:
            old_groups = []

        name = (username or "").strip() or None
        if not name:
            try:
                name = pwd.getpwuid(uid).pw_name
            except KeyError:
                name = None

        try:
            # Must set groups while still euid 0.
            # initgroups() uses container NSS; /etc/group is a stale file bind
            # after UGOS/userd rewrites it. Prefer the host directory bind.
            if name:
                if _HOST_GROUP.is_file():
                    os.setgroups(_host_gids_for(name, gid))
                else:
                    try:
                        os.initgroups(name, gid)
                    except (OSError, KeyError):
                        os.setgroups(_host_gids_for(name, gid))
            else:
                os.setgroups([gid])
            os.setegid(gid)
            os.seteuid(uid)
            yield
        finally:
            # Regain root before restoring group list.
            os.seteuid(0)
            os.setegid(0)
            try:
                os.setgroups(old_groups)
            except OSError:
                pass
            os.setegid(old_gid)
            os.seteuid(old_uid)
    finally:
        _as_user_lock.release()


@contextmanager
def as_root() -> Iterator[None]:
    """Hold the credential lock and run as euid 0.

    Chunk-upload staging under /tmp must not race with as_user() dropping
    euid on another thread (os.replace then hits EACCES).
    """
    if os.getuid() != 0:  # real uid; see as_user
        yield
        return
    if not _as_user_lock.acquire(timeout=_AS_USER_LOCK_TIMEOUT_S):
        raise FSError("Server busy with another file operation — retry shortly", 503)
    try:
        os.seteuid(0)
        os.setegid(0)
        yield
    finally:
        _as_user_lock.release()


@contextmanager
def _translate_os_errors() -> Iterator[None]:
    """Map filesystem errors onto FSError so the API answers with a useful status."""
    try:
        yield
    except FSError:
        raise
    except PermissionError as e:
        raise FSError("Permission denied", 403) from e
    except FileExistsError as e:
        raise FSError("Target exists", 409) from e
    except FileNotFoundError as e:
        raise FSError("Not found", 404) from e
    except OSError as e:
        raise FSError(e.strerror or "Filesystem error", 400) from e


def _rel_to_root(abs_path: str, root: Path) -> str:
    """POSIX relative path of abs_path under root, no extra stat."""
    root_s = os.fspath(root)
    if abs_path == root_s:
        return ""
    prefix = root_s if root_s.endswith(os.sep) else root_s + os.sep
    if abs_path.startswith(prefix):
        return abs_path[len(prefix) :].replace("\\", "/")
    rel = str(Path(abs_path).relative_to(root)).replace("\\", "/")
    return "" if rel == "." else rel


def _entry_from_stat(name: str, rel: str, st: os.stat_result) -> dict[str, Any]:
    is_dir = stat.S_ISDIR(st.st_mode)
    is_link = stat.S_ISLNK(st.st_mode)
    return {
        "name": name,
        "path": rel,
        "is_dir": is_dir,
        "is_link": is_link,
        "size": 0 if is_dir else st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
        # Exact value for the CLI's `expect_mtime_ns` overwrite guard.
        "mtime_ns": st.st_mtime_ns,
        "mode": stat.filemode(st.st_mode),
    }


def _entry(path: Path, root: Path) -> dict[str, Any]:
    st = path.lstat()
    rel = str(path.relative_to(root)).replace("\\", "/")
    if rel == ".":
        rel = ""
    return _entry_from_stat(path.name, rel, st)


def _is_hidden_entry(name: str) -> bool:
    """Hide macOS junk and in-progress transfer temp files (not #recycle — shown as Recycle in UI)."""
    if name in (".DS_Store", "Thumbs.db", "desktop.ini"):
        return True
    # Chunked-upload staging / internal markers
    if name.startswith(".ifd-"):
        return True
    lower = name.lower()
    # Ugreen File Manager / transfer temps (e.g. *.ug-tmp)
    if lower.endswith(".ug-tmp") or lower.endswith(".ugtmp"):
        return True
    # Common incomplete-download markers
    if lower.endswith(".tmp") or lower.endswith(".partial") or lower.endswith(".crdownload"):
        return True
    return False


def list_dir(rel: str, uid: int, gid: int) -> dict[str, Any]:
    root = drive_root()
    path = resolve_rel(rel)
    items: list[dict[str, Any]] = []
    with as_user(uid, gid):
        try:
            # scandir + DirEntry.stat is one syscall per child (iterdir+lstat was two).
            with os.scandir(path) as it:
                for entry in it:
                    if _is_hidden_entry(entry.name):
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    items.append(_entry_from_stat(entry.name, _rel_to_root(entry.path, root), st))
        except FileNotFoundError as e:
            raise FSError("Not found", 404) from e
        except NotADirectoryError as e:
            raise FSError("Not a directory", 400) from e
        except PermissionError as e:
            raise FSError("Permission denied", 403) from e
    items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return {
        "path": "" if path == root else str(path.relative_to(root)).replace("\\", "/"),
        "items": items,
    }


# ---------------------------------------------------------------------------
# Recursive folder sizes (du-like). Cached + time-bounded so a huge tree
# cannot hang list/hydrate. Symlinks are not followed.
# ---------------------------------------------------------------------------
_DIR_SIZE_CACHE: dict[str, tuple[float, int, bool]] = {}
_DIR_SIZE_CACHE_TTL = 180.0  # complete results
_DIR_SIZE_CACHE_TTL_PARTIAL = 45.0  # incomplete walks refresh sooner
_DIR_SIZE_CACHE_MAX = 2500
_DIR_SIZE_MAX_ENTRIES = 100_000
_DIR_SIZE_DEFAULT_BUDGET_S = 8.0


def _dir_size_cache_key(path: Path) -> str:
    return f"{drive_root()}|{path}"


def _dir_size_cache_get(key: str) -> tuple[int, bool] | None:
    hit = _DIR_SIZE_CACHE.get(key)
    if hit is None:
        return None
    expires, size, incomplete = hit
    if time.monotonic() > expires:
        _DIR_SIZE_CACHE.pop(key, None)
        return None
    return int(size), bool(incomplete)


def _dir_size_cache_set(key: str, size: int, incomplete: bool) -> None:
    ttl = _DIR_SIZE_CACHE_TTL_PARTIAL if incomplete else _DIR_SIZE_CACHE_TTL
    _DIR_SIZE_CACHE[key] = (time.monotonic() + ttl, int(size), bool(incomplete))
    if len(_DIR_SIZE_CACHE) > _DIR_SIZE_CACHE_MAX:
        # Drop oldest ~half by expiry (cheap prune).
        ordered = sorted(_DIR_SIZE_CACHE.items(), key=lambda kv: kv[1][0])
        for drop_key, _ in ordered[: len(ordered) // 2]:
            _DIR_SIZE_CACHE.pop(drop_key, None)


def _walk_dir_size(path: Path, *, deadline: float, max_entries: int = _DIR_SIZE_MAX_ENTRIES) -> tuple[int, bool]:
    """Sum file sizes under path. Returns (bytes, incomplete)."""
    total = 0
    scanned = 0
    incomplete = False
    stack: list[Path] = [path]
    while stack:
        if time.monotonic() > deadline or scanned >= max_entries:
            incomplete = True
            break
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if time.monotonic() > deadline or scanned >= max_entries:
                        incomplete = True
                        break
                    name = entry.name
                    if _is_hidden_entry(name):
                        continue
                    scanned += 1
                    try:
                        # Never follow symlinks (loops + counting foreign trees).
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            try:
                                total += entry.stat(follow_symlinks=False).st_size
                            except OSError:
                                continue
                    except OSError:
                        continue
        except OSError:
            continue
    return total, incomplete


def folder_sizes(
    rels: list[str],
    uid: int,
    gid: int,
    *,
    time_budget_s: float = 20.0,
    per_dir_budget_s: float = _DIR_SIZE_DEFAULT_BUDGET_S,
) -> dict[str, Any]:
    """
    Recursive size for each relative folder path.

    Uses a process-local TTL cache. Walks are time-capped; incomplete results
    are still returned (caller may show "~1.2 GB").
    """
    # Dedupe while preserving order; cap request size.
    seen: set[str] = set()
    paths: list[str] = []
    for raw in rels:
        rel = (raw or "").strip().lstrip("/")
        if rel in seen:
            continue
        seen.add(rel)
        paths.append(rel)
        if len(paths) >= 60:
            break

    overall_deadline = time.monotonic() + max(1.0, float(time_budget_s))
    per_cap = max(0.5, float(per_dir_budget_s))
    out: dict[str, dict[str, Any]] = {}

    with as_user(uid, gid):
        for rel in paths:
            remaining = overall_deadline - time.monotonic()
            if remaining <= 0.05:
                break
            try:
                path = resolve_rel(rel)
            except FSError:
                continue
            try:
                if not path.exists() or not path.is_dir():
                    continue
            except OSError:
                continue

            key = _dir_size_cache_key(path)
            cached = _dir_size_cache_get(key)
            if cached is not None:
                size, incomplete = cached
                out[rel] = {"size": size, "incomplete": incomplete}
                continue

            dir_deadline = time.monotonic() + min(per_cap, remaining)
            size, incomplete = _walk_dir_size(path, deadline=dir_deadline)
            _dir_size_cache_set(key, size, incomplete)
            out[rel] = {"size": size, "incomplete": incomplete}

    return {"sizes": out}


# Kind keywords → extensions (for queries like "video" / "photos" / "pdf").
_KIND_EXTS: dict[str, frozenset[str]] = {
    "video": frozenset({"mp4", "mov", "avi", "mkv", "mxf", "m4v", "webm", "mpg", "mpeg", "wmv", "r3d", "braw"}),
    "videos": frozenset({"mp4", "mov", "avi", "mkv", "mxf", "m4v", "webm", "mpg", "mpeg", "wmv", "r3d", "braw"}),
    "image": frozenset({"png", "jpg", "jpeg", "gif", "webp", "tif", "tiff", "heic", "svg", "bmp", "dng", "cr2", "arw"}),
    "images": frozenset({"png", "jpg", "jpeg", "gif", "webp", "tif", "tiff", "heic", "svg", "bmp", "dng", "cr2", "arw"}),
    "photo": frozenset({"png", "jpg", "jpeg", "gif", "webp", "tif", "tiff", "heic", "svg", "bmp", "dng", "cr2", "arw"}),
    "photos": frozenset({"png", "jpg", "jpeg", "gif", "webp", "tif", "tiff", "heic", "svg", "bmp", "dng", "cr2", "arw"}),
    "audio": frozenset({"wav", "mp3", "aac", "aiff", "aif", "m4a", "flac", "ogg"}),
    "doc": frozenset({"pdf", "doc", "docx", "txt", "md", "rtf", "xls", "xlsx", "ppt", "pptx", "csv"}),
    "docs": frozenset({"pdf", "doc", "docx", "txt", "md", "rtf", "xls", "xlsx", "ppt", "pptx", "csv"}),
    "document": frozenset({"pdf", "doc", "docx", "txt", "md", "rtf", "xls", "xlsx", "ppt", "pptx", "csv"}),
    "documents": frozenset({"pdf", "doc", "docx", "txt", "md", "rtf", "xls", "xlsx", "ppt", "pptx", "csv"}),
    "pdf": frozenset({"pdf"}),
    "zip": frozenset({"zip", "rar", "7z", "tar", "gz", "tgz", "dmg", "iso"}),
    "archive": frozenset({"zip", "rar", "7z", "tar", "gz", "tgz", "dmg", "iso"}),
    "folder": frozenset(),  # special: dirs only
    "folders": frozenset(),
}

_SPLIT_RE = re.compile(r"[\s_\-./\\]+")


def _normalize_search_text(value: str) -> str:
    """Lowercase + strip accents so 'cafe' matches 'Café'."""
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold()


def _search_tokens(query: str) -> list[str]:
    return [t for t in _SPLIT_RE.split(_normalize_search_text(query)) if t]


def _ext_of(name: str) -> str:
    if not name or name.startswith("."):
        return ""
    dot = name.rfind(".")
    return name[dot + 1 :].casefold() if dot > 0 else ""


def _subsequence_match(hay: str, needle: str) -> bool:
    """True if needle chars appear in order in hay (typo-tolerant quick filter)."""
    if not needle:
        return True
    i = 0
    for ch in hay:
        if ch == needle[i]:
            i += 1
            if i == len(needle):
                return True
    return False


def _score_match(
    name: str,
    rel_path: str,
    is_dir: bool,
    tokens: list[str],
    raw_query: str,
    kind_exts: frozenset[str] | None,
    dirs_only: bool,
) -> int | None:
    """
    Rank a candidate. Higher is better. None = no match.

    Prefers exact / prefix / substring name hits, then path tokens, then
    light fuzzy subsequence. Kind keywords (video, pdf, …) filter extensions.
    """
    if dirs_only and not is_dir:
        return None

    name_n = _normalize_search_text(name)
    path_n = _normalize_search_text(rel_path)
    # Parent path without the leaf for "location" matching.
    slash = path_n.rfind("/")
    parent_n = path_n[:slash] if slash >= 0 else ""
    ext = _ext_of(name)

    if dirs_only and not tokens:
        depth = path_n.count("/")
        return 180 - min(depth, 20)

    if kind_exts is not None:
        if is_dir:
            return None
        if ext not in kind_exts:
            return None
        # Kind-only query (e.g. just "video") — still rank by depth (shallower first).
        if not tokens:
            depth = path_n.count("/")
            return 200 - min(depth, 20)

    if not tokens:
        return None

    q = _normalize_search_text(raw_query).strip()
    score = 0

    # Whole-query name matches (strongest).
    if name_n == q:
        score = 1000
    elif name_n.startswith(q):
        score = 860
    elif q in name_n:
        score = 720
    else:
        # All tokens must appear in name or path.
        for tok in tokens:
            in_name = tok in name_n
            in_path = tok in path_n
            if not in_name and not in_path:
                # Fuzzy: subsequence only on short single-token queries.
                if len(tokens) == 1 and len(tok) >= 3 and _subsequence_match(name_n, tok):
                    score += 120
                    continue
                return None
            if in_name:
                if name_n.startswith(tok):
                    score += 280
                elif name_n == tok:
                    score += 320
                else:
                    score += 200
            elif tok in parent_n:
                score += 90
            else:
                score += 70

    # Prefer shallower paths (easier to find) and folders slightly for name ties.
    depth = path_n.count("/")
    score -= min(depth, 12) * 4
    if is_dir:
        score += 15
    # Exact extension typed as ".mp4" or "mp4" alone handled via kind; boost if query ends with ext.
    if not is_dir and ext and (q == ext or q == f".{ext}" or q.endswith(f".{ext}")):
        score += 40
    return score


def search(
    query: str,
    rel: str,
    uid: int,
    gid: int,
    *,
    limit: int = 40,
    max_entries: int = 30_000,
    time_budget_s: float = 2.5,
) -> dict[str, Any]:
    """
    Recursive filename/path search under `rel` (empty = whole share).

    Runs as the NAS user so ACL-denied dirs are skipped. Caps walk size and
    wall time so a huge share can't hang the API.
    """
    raw = (query or "").strip()
    if len(raw) < 1:
        raise FSError("Query required", 400)
    if len(raw) > 200:
        raise FSError("Query too long", 400)

    limit = max(1, min(int(limit), 80))
    max_entries = max(100, min(int(max_entries), 80_000))

    tokens = _search_tokens(raw)
    kind_exts: frozenset[str] | None = None
    dirs_only = False

    # Leading kind keyword: "video promo", "pdf agenda", or bare "photos".
    if tokens:
        head = tokens[0]
        if head in _KIND_EXTS:
            if head in ("folder", "folders"):
                dirs_only = True
            else:
                kind_exts = _KIND_EXTS[head]
            tokens = tokens[1:]
        elif head.startswith(".") and len(head) > 1 and head[1:].isalnum():
            kind_exts = frozenset({head[1:]})
            tokens = tokens[1:]
        elif len(tokens) == 1 and head.isalnum() and len(head) <= 5 and head in {
            ext for exts in _KIND_EXTS.values() for ext in exts
        }:
            # Bare extension like "mp4" / "pdf".
            kind_exts = frozenset({head})
            tokens = []

    # Need either tokens or a kind filter.
    if not tokens and kind_exts is None and not dirs_only:
        # Fall back to raw substring tokens from the unsplit query.
        tokens = _search_tokens(raw) or [_normalize_search_text(raw)]

    root = drive_root()
    start = resolve_rel(rel)
    start_rel = "" if start == root else str(start.relative_to(root)).replace("\\", "/")

    hits: list[tuple[int, dict[str, Any]]] = []
    scanned = 0
    truncated = False
    deadline = time.monotonic() + max(0.4, float(time_budget_s))

    with as_user(uid, gid):
        if not start.exists():
            raise FSError("Not found", 404)
        if not start.is_dir():
            raise FSError("Not a directory", 400)

        # BFS so shallow folders (and their files) surface before deep trees.
        queue: deque[Path] = deque([start])
        while queue:
            if time.monotonic() > deadline or scanned >= max_entries:
                truncated = True
                break
            current = queue.popleft()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        if time.monotonic() > deadline or scanned >= max_entries:
                            truncated = True
                            break
                        name = entry.name
                        if _is_hidden_entry(name):
                            continue
                        # Skip Samba recycle bin (noise for normal search).
                        if name == "#recycle":
                            continue
                        scanned += 1
                        try:
                            is_dir = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            continue
                        try:
                            rel_path = str(Path(entry.path).relative_to(root)).replace("\\", "/")
                        except ValueError:
                            continue
                        if rel_path == ".":
                            continue

                        score = _score_match(
                            name, rel_path, is_dir, tokens, raw, kind_exts, dirs_only
                        )
                        if score is not None:
                            try:
                                st = entry.stat(follow_symlinks=False)
                                item = {
                                    "name": name,
                                    "path": rel_path,
                                    "is_dir": is_dir,
                                    "is_link": entry.is_symlink(),
                                    "size": 0 if is_dir else int(st.st_size),
                                    "mtime": datetime.fromtimestamp(
                                        st.st_mtime, tz=timezone.utc
                                    ).isoformat(),
                                    "score": score,
                                    "parent": (
                                        rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
                                    ),
                                }
                            except OSError:
                                item = {
                                    "name": name,
                                    "path": rel_path,
                                    "is_dir": is_dir,
                                    "is_link": False,
                                    "size": 0,
                                    "mtime": None,
                                    "score": score,
                                    "parent": (
                                        rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
                                    ),
                                }
                            hits.append((score, item))
                            # Bound memory on broad kind-only queries (e.g. "video").
                            if len(hits) > limit * 8:
                                hits.sort(
                                    key=lambda pair: (-pair[0], pair[1]["name"].casefold())
                                )
                                hits = hits[: limit * 3]

                        if is_dir:
                            try:
                                queue.append(Path(entry.path))
                            except OSError:
                                pass
            except PermissionError:
                continue
            except FileNotFoundError:
                continue
            except OSError:
                continue

    hits.sort(key=lambda pair: (-pair[0], pair[1]["name"].casefold()))
    results = [item for _, item in hits[:limit]]
    return {
        "query": raw,
        "path": start_rel,
        "results": results,
        "count": len(results),
        "scanned": scanned,
        "truncated": truncated,
        "has_more": truncated or len(hits) > limit,
    }


def mkdir(rel: str, name: str, uid: int, gid: int) -> dict[str, Any]:
    name = (name or "").strip()
    if not name or "/" in name or name in (".", ".."):
        raise FSError("Invalid folder name")
    parent = resolve_rel(rel)
    target = resolve_rel(str(Path(rel) / name) if rel else name)
    with as_user(uid, gid), _translate_os_errors():
        if not parent.is_dir():
            raise FSError("Parent not found", 404)
        target.mkdir(exist_ok=False)
        root = drive_root()
        return _entry(target, root)


def ensure_dir(rel: str, uid: int, gid: int) -> dict[str, Any]:
    """Create nested directory path under drive root (like mkdir -p)."""
    root = drive_root()
    if not rel or rel in (".", "/"):
        return _entry(root, root)
    parts = [p for p in Path(rel).parts if p not in ("", ".", "..")]
    if any(p == ".." for p in Path(rel).parts):
        raise FSError("Invalid path", 400)
    with as_user(uid, gid), _translate_os_errors():
        current = root
        for part in parts:
            current = current / part
            if current.exists() and not current.is_dir():
                raise FSError(f"Not a directory: {part}", 400)
            current.mkdir(exist_ok=True)
            try:
                os.chown(current, uid, gid)
            except OSError:
                pass
        return _entry(current, root)


def rename(rel: str, new_name: str, uid: int, gid: int) -> dict[str, Any]:
    new_name = (new_name or "").strip()
    if not new_name or "/" in new_name or new_name in (".", ".."):
        raise FSError("Invalid name")
    src = resolve_rel(rel)
    if src == drive_root():
        raise FSError("Cannot rename drive root")
    dest = src.with_name(new_name)
    # ensure dest still under root
    resolve_rel(str(dest.relative_to(drive_root())))
    with as_user(uid, gid), _translate_os_errors():
        if not src.exists():
            raise FSError("Not found", 404)
        if dest.exists():
            raise FSError("Target exists", 409)
        src.rename(dest)
        return _entry(dest, drive_root())


def move_item(src_rel: str, dest_dir_rel: str, uid: int, gid: int) -> dict[str, Any]:
    src = resolve_rel(src_rel)
    dest_dir = resolve_rel(dest_dir_rel)
    if src == drive_root():
        raise FSError("Cannot move drive root")
    # Reject moving a folder into itself or a descendant (API clients / scripts).
    try:
        dest_dir.resolve().relative_to(src.resolve())
    except (ValueError, OSError):
        pass
    else:
        raise FSError("Cannot move a folder into itself", 400)
    dest = dest_dir / src.name
    resolve_rel(str(dest.relative_to(drive_root())))
    with as_user(uid, gid), _translate_os_errors():
        if not src.exists():
            raise FSError("Not found", 404)
        if not dest_dir.is_dir():
            raise FSError("Destination not a directory", 400)
        if dest.exists():
            raise FSError("Target exists", 409)
        # Re-check after resolve under user (symlinks / races).
        try:
            dest_dir.resolve().relative_to(src.resolve())
        except (ValueError, OSError):
            pass
        else:
            raise FSError("Cannot move a folder into itself", 400)
        shutil.move(str(src), str(dest))
        return _entry(Path(dest), drive_root())


RECYCLE_NAME = "#recycle"


def recycle_dir() -> Path:
    return drive_root() / RECYCLE_NAME


def has_recycle_bin() -> bool:
    """True when this share has a UGOS/Samba-style #recycle folder."""
    try:
        return recycle_dir().is_dir()
    except OSError:
        return False


def _rel_under_root(path: Path) -> Path:
    root = drive_root()
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError as e:
        raise FSError("Path outside drive", 400) from e


def is_under_recycle(rel: str) -> bool:
    rel = (rel or "").strip().strip("/")
    if not rel:
        return False
    parts = Path(rel).parts
    return bool(parts) and parts[0] == RECYCLE_NAME


def _versioned_dest(dest: Path) -> Path:
    """Samba recycle:versions style — name.#1, name.#2, …"""
    if not dest.exists():
        return dest
    for n in range(1, 10_000):
        candidate = dest.with_name(f"{dest.name}.#{n}")
        if not candidate.exists():
            return candidate
    raise FSError("Too many recycle versions", 409)


def _permanent_delete(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def delete(rel: str, uid: int, gid: int, *, permanent: bool = False) -> dict[str, Any]:
    """
    Delete a path.

    Mirrors UGOS Samba VFS recycle when the share has `#recycle`:
      recycle:repository = #recycle
      recycle:keeptree   = Yes
      recycle:versions   = Yes
      recycle:exclude_dir = #recycle

    - Normal files → moved into `#recycle/<original relative path>`
    - Already under `#recycle`, or permanent=True → hard delete
    - No `#recycle` on the share → hard delete (fallback)
    """
    path = resolve_rel(rel)
    root = drive_root()
    if path == root:
        raise FSError("Cannot delete drive root")
    if path == recycle_dir():
        raise FSError("Cannot delete the Recycle folder — empty it instead", 400)

    with as_user(uid, gid), _translate_os_errors():
        if not path.exists():
            raise FSError("Not found", 404)

        under_recycle = is_under_recycle(rel)
        bin_dir = recycle_dir()
        soft = (
            not permanent
            and not under_recycle
            and bin_dir.is_dir()
        )

        if soft:
            rel_path = _rel_under_root(path)
            dest = bin_dir.joinpath(*rel_path.parts)
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.chmod(dest.parent, 0o777)
                except OSError:
                    pass
            except OSError as e:
                raise FSError(f"Cannot prepare Recycle: {e}", 500) from e
            dest = _versioned_dest(dest)
            shutil.move(str(path), str(dest))
            return {
                "ok": True,
                "action": "recycled",
                "path": str(dest.relative_to(root)).replace("\\", "/"),
            }

        _permanent_delete(path)
        return {"ok": True, "action": "deleted", "path": rel}


def empty_recycle(uid: int, gid: int) -> dict[str, Any]:
    """Permanently remove everything inside `#recycle` (not the folder itself)."""
    root = drive_root()
    bin_dir = recycle_dir()
    with as_user(uid, gid), _translate_os_errors():
        if not bin_dir.is_dir():
            raise FSError("This share has no Recycle bin", 404)
        removed = 0
        for child in list(bin_dir.iterdir()):
            # Keep macOS/Windows junk optional — wipe all contents including desktop.ini
            try:
                _permanent_delete(child)
                removed += 1
            except OSError as e:
                raise FSError(f"Could not remove {child.name}: {e}", 403) from e
        return {"ok": True, "removed": removed, "path": RECYCLE_NAME}


def write_upload(rel_dir: str, filename: str, data: bytes, uid: int, gid: int) -> dict[str, Any]:
    """Write a whole in-memory buffer (small files / legacy callers)."""
    return write_upload_stream(
        rel_dir,
        filename,
        iter([data]),
        uid,
        gid,
        max_bytes=len(data),
        expected_bytes=len(data),
    )


def _check_expected_mtime(dest: Path, expect_mtime_ns: int | None) -> None:
    if expect_mtime_ns is None:
        return
    try:
        current = dest.stat().st_mtime_ns
    except FileNotFoundError:
        current = -1
    if current != expect_mtime_ns:
        if expect_mtime_ns == -1:
            raise FSError("A file with that name already exists", 409)
        raise FSError("File changed on the Drive since you opened it", 409)


def write_upload_stream(
    rel_dir: str,
    filename: str,
    chunks: Iterator[bytes],
    uid: int,
    gid: int,
    max_bytes: int = 50 * 1024 * 1024 * 1024,
    expected_bytes: int | None = None,
    expect_mtime_ns: int | None = None,
) -> dict[str, Any]:
    """Stream chunks to disk — avoids holding multi-GB uploads in RAM.

    Nested parents are created as needed (folder upload via webkitRelativePath).
    Uses a unique temp name so concurrent same-name uploads cannot clobber each
    other. Only renames into place after a clean stream; if ``expected_bytes`` is
    set, the byte count must match exactly (aborts leave no final file).
    ``expect_mtime_ns`` (``-1`` = must not exist) refuses with 409 when the
    target changed since the client last saw it.
    """
    filename = Path(filename).name
    if not filename or filename in (".", ".."):
        raise FSError("Invalid filename")
    if expected_bytes is not None and (expected_bytes < 0 or expected_bytes > max_bytes):
        raise FSError("File too large", 413)
    rel_dir = (rel_dir or "").strip().lstrip("/")
    # Folder uploads place files under subdirs that may not exist yet.
    if rel_dir:
        parent_path = resolve_rel(rel_dir)
        if not parent_path.is_dir():
            ensure_dir(rel_dir, uid, gid)
    dest = resolve_rel(str(Path(rel_dir) / filename) if rel_dir else filename)
    parent = dest.parent
    # Unique per attempt (hidden via .partial suffix + leading dot).
    tmp = dest.with_name(f".{dest.name}.{secrets.token_hex(8)}.partial")
    written = 0

    # Hold as_user only for open / replace / chown. The fd stays valid after
    # euid restore, so the network-paced body pump does not block list/search/
    # other users (previous site-wide hang: lock held for entire multi-GB upload).
    out = None
    try:
        with as_user(uid, gid), _translate_os_errors():
            if not parent.is_dir():
                raise FSError("Parent not found", 404)
            if dest.exists() and dest.is_dir():
                raise FSError("Cannot overwrite directory")
            _check_expected_mtime(dest, expect_mtime_ns)
            # O_NOFOLLOW|O_EXCL: a planted symlink at the temp name is not a write target.
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            out = os.fdopen(os.open(tmp, flags, 0o666), "wb")

        assert out is not None
        try:
            for chunk in chunks:
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    raise FSError("File too large", 413)
                out.write(chunk)
            out.flush()
        finally:
            out.close()
            out = None

        if expected_bytes is not None and written != expected_bytes:
            raise FSError(
                f"Incomplete upload (got {written}, expected {expected_bytes})",
                400,
            )

        with as_user(uid, gid), _translate_os_errors():
            return _place_upload(tmp, dest, uid, gid, expect_mtime_ns)
    except Exception:
        if out is not None:
            try:
                out.close()
            except OSError:
                pass
        try:
            if tmp.exists():
                # Root can remove our partial; prefer as_user for ACL safety.
                try:
                    with as_user(uid, gid):
                        if tmp.exists():
                            tmp.unlink()
                except FSError:
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
        except OSError:
            pass
        raise


def _place_upload(tmp: Path, dest: Path, uid: int, gid: int, expect_mtime_ns: int | None) -> dict[str, Any]:
    """Move a finished upload into place. Caller holds as_user(uid, gid)."""
    _check_expected_mtime(dest, expect_mtime_ns)
    if expect_mtime_ns == -1:
        # Create-only: link() fails atomically if a file appeared since the check.
        try:
            os.link(tmp, dest)
        except FileExistsError as e:
            raise FSError("A file with that name already exists", 409) from e
        os.unlink(tmp)
    else:
        # Atomic replace: overwrites existing file without delete-first.
        os.replace(tmp, dest)
    try:
        os.chown(dest, uid, gid)
    except OSError:
        pass
    return _entry(dest, drive_root())


def finalize_partial(
    tmp: Path,
    rel_dir: str,
    filename: str,
    uid: int,
    gid: int,
    *,
    expected_bytes: int,
    expect_mtime_ns: int | None = None,
) -> dict[str, Any]:
    """Move a partial file that chunked-upload pieces were written into
    (chunk_upload) into place, once it holds exactly ``expected_bytes``."""
    filename = Path(filename).name
    if not filename or filename in (".", ".."):
        raise FSError("Invalid filename")
    rel_dir = (rel_dir or "").strip().lstrip("/")
    dest = resolve_rel(str(Path(rel_dir) / filename) if rel_dir else filename)
    if tmp.parent != dest.parent or not tmp.name.endswith(".partial"):
        raise FSError("The upload's folder moved — upload again", 409)
    with as_user(uid, gid), _translate_os_errors():
        if dest.exists() and dest.is_dir():
            raise FSError("Cannot overwrite directory")
        got = os.stat(tmp).st_size
        if got != expected_bytes:
            raise FSError(f"Incomplete upload (got {got}, expected {expected_bytes})", 400)
        return _place_upload(tmp, dest, uid, gid, expect_mtime_ns)


def _encrypted_home_mount(root: Path) -> bool:
    """UGOS's encrypted-home statfs includes the personal-folder limit."""
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return False
    closest = (0, "")
    for line in lines:
        before, sep, after = line.partition(" - ")
        fields = before.split()
        if not sep or len(fields) < 5 or not after.split():
            continue
        mount = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4]))
        if root == mount or mount in root.parents:
            if len(mount.parts) >= closest[0]:
                closest = (len(mount.parts), after.split()[0])
    return closest[1] == "fuse.uggocryptfs"


def disk_usage(*, personal: bool = False) -> dict[str, int | str]:
    """
    Capacity from the active filesystem, as reported by `df` on that mount.
    UGOS encrypted personal mounts report their quota through these counters;
    ordinary filesystems report backing-volume capacity.

    Fields:
      total / used / free — bytes from statvfs (matches `df`)
      scope — "personal" for a UGOS encrypted home, otherwise "volume"
    """
    total, used, free = shutil.disk_usage(drive_root())
    # Clamp for weird filesystems where counters can disagree slightly.
    total = max(0, int(total))
    free = max(0, min(int(free), total))
    used = max(0, min(int(used), total))
    # Prefer identity free + used ≈ total when free is "available to user"
    # (bavail); keep shutil's used for the bar so it tracks df "Used".
    return {
        "total": total,
        "used": used,
        "free": free,
        "scope": "personal" if personal and _encrypted_home_mount(drive_root()) else "volume",
    }


def upload_fingerprint(rel: str, size: int, uid: int, gid: int) -> str | None:
    """SHA-256 of concatenated 8 MiB chunk digests; bounded memory on both ends."""
    path = resolve_rel(rel)
    with as_user(uid, gid), _translate_os_errors():
        try:
            if not path.is_file():
                if not path.exists():
                    return None
                raise FSError("Not a regular file", 400)
            handle = open(path, "rb")
        except FileNotFoundError:
            return None
    # The permission-checked fd remains valid after restoring process credentials.
    with handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise FSError("Not a regular file", 400)
        if before.st_size != size:
            return None
        digest = hashlib.sha256()
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(hashlib.sha256(chunk).digest())
        after = os.fstat(handle.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise FSError("File changed while checking. Try the upload again.", 409)
        return digest.hexdigest()


def open_for_download(rel: str, uid: int, gid: int) -> AuthorizedFile:
    """Open ``rel`` as this NAS user. Callers must read ``fd`` and then close it."""
    path = resolve_rel(rel)
    return open_authorized_path(path, drive_root(), uid, gid, name=path.name)


def open_zip_member(path: Path, root: Path, uid: int, gid: int):
    """Open one zip member as the NAS user. The caller closes the file object."""
    opened = open_authorized_path(path, root, uid, gid)
    fd = opened.fd
    opened.fd = -1  # the file object owns the fd now
    return os.fdopen(fd, "rb")


def zip_member_size(path: Path, root: Path, uid: int, gid: int) -> int:
    """Byte size of a regular file inside ``root``, without following a symlink."""
    root = root.resolve()
    try:
        path.resolve().relative_to(root)
    except (OSError, ValueError) as e:
        raise FSError("Path outside drive", 400) from e
    with as_user(uid, gid):
        st = path.lstat()
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise FSError("Not found", 404)
    return int(st.st_size)


def _unique_arcname(name: str, used: set[str]) -> str:
    """Disambiguate colliding archive member names (file or folder prefix)."""
    if name not in used:
        return name
    # Preserve trailing slash for directory markers (unused today).
    trailing = name.endswith("/")
    base = name[:-1] if trailing else name
    path = Path(base)
    stem, suffix = path.stem, path.suffix
    parent = str(path.parent).replace("\\", "/")
    if parent in ("", "."):
        parent = ""
    n = 2
    while True:
        candidate = f"{stem}-{n}{suffix}"
        if parent:
            candidate = f"{parent}/{candidate}"
        if trailing:
            candidate += "/"
        if candidate not in used:
            return candidate
        n += 1


def _walk_folder_files(root: Path) -> Iterator[tuple[str, Path, int]]:
    """
    Yield (rel_posix, path, size) for regular files under root.

    Skips symlinks, hidden/temp junk, and nested ``#recycle``. Does not follow
    links. Caller must run under ``as_user`` when ACL enforcement is required.
    """
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    name = entry.name
                    if _is_hidden_entry(name):
                        continue
                    # Don't pull the share trash bin into a parent folder zip.
                    if name == RECYCLE_NAME:
                        continue
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        path = Path(entry.path)
                        try:
                            size = int(entry.stat(follow_symlinks=False).st_size)
                        except OSError:
                            continue
                        # Access check: open for read under current credentials.
                        try:
                            with open(path, "rb"):
                                pass
                        except OSError:
                            continue
                        rel = path.relative_to(root).as_posix()
                        yield rel, path, size
                    except OSError:
                        continue
        except OSError:
            continue


def collect_zip_entries(
    rels: list[str],
    uid: int,
    gid: int,
    *,
    max_files: int,
    max_total: int,
) -> list[tuple[str, Path]]:
    """
    Build STORE-zip member list for mixed files and folders.

    Folders are expanded recursively; archive paths preserve the top-level
    folder name (``Folder/sub/file.ext``). Holds ``as_user`` only for the walk
    and access checks — callers must stream the zip outside this lock.
    """
    entries: list[tuple[str, Path]] = []
    total = 0
    used_names: set[str] = set()

    with as_user(uid, gid):
        for raw in rels:
            rel = (raw or "").strip()
            if not rel:
                continue
            path = resolve_rel(rel)
            try:
                if not path.exists():
                    raise FSError("Not found", 404)
                if path.is_symlink():
                    raise FSError("Symlinks can't be downloaded", 400)
                if path.is_file():
                    try:
                        with open(path, "rb"):
                            pass
                    except OSError as e:
                        raise FSError("Permission denied", 403) from e
                    size = int(path.stat().st_size)
                    total += size
                    if total > max_total:
                        raise FSError(
                            f"Selection too large to zip (max {max_total // (1024 ** 3)}GB)",
                            413,
                        )
                    if len(entries) >= max_files:
                        raise FSError(f"Too many files (max {max_files})", 400)
                    arc = _unique_arcname(path.name, used_names)
                    used_names.add(arc)
                    entries.append((arc, path))
                    continue

                if not path.is_dir():
                    raise FSError("Not found", 404)

                # Folder: prefix all members with a unique top-level folder name.
                folder_arc = _unique_arcname(path.name, used_names)
                used_names.add(folder_arc)
                # Reserve the prefix so a sibling file can't collide mid-walk.
                # (actual file arcs are folder_arc/rel)

                for sub_rel, file_path, size in _walk_folder_files(path):
                    total += size
                    if total > max_total:
                        raise FSError(
                            f"Selection too large to zip (max {max_total // (1024 ** 3)}GB)",
                            413,
                        )
                    if len(entries) >= max_files:
                        raise FSError(f"Too many files (max {max_files})", 400)
                    arc = f"{folder_arc}/{sub_rel}"
                    # Walk paths are unique under one folder; still guard collisions.
                    if arc in used_names:
                        arc = _unique_arcname(arc, used_names)
                    used_names.add(arc)
                    entries.append((arc, file_path))
            except FSError:
                raise
            except OSError as e:
                raise FSError("Permission denied", 403) from e

    if not entries:
        raise FSError("Nothing to download (empty folder or no readable files)", 400)
    return entries
