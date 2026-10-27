"""Shared-folder discovery from UGOS Samba + POSIX permission checks."""

from __future__ import annotations

import grp
import os
import pwd
import re
import shlex
import time
from pathlib import Path
from typing import Any

from config import get_settings
from fsops import FSError, as_user, host_group_names, _encrypted_home_mount
import personal_folders


# Empty: open Settings → default_share, else the first shared folder the user can open.
DEFAULT_SHARE = ""

# Samba sections that are not real multi-user data shares in the Drive UI.
_SKIP_SECTIONS = {
    "global",
    "printers",
    "print$",
    "ipc$",
}

# UGOS home-directory templates (`path = %H` / `%u`) — resolved per user.
_HOME_SECTIONS = {
    "homes",
    "personal_folder",
}

# NAS usernames used as ~user personal-drive ids.
_PERSONAL_USER_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Host paths (on the NAS) → paths inside the Drive container.
# UGOS volumes are /volume1, /volume2, …; compose mounts each at /data/volumeN.
_HOST_PREFIX_MAP: list[tuple[str, str]] = [
    *[(f"/volume{n}/", f"/data/volume{n}/") for n in range(1, 10)],
    ("/home/", "/data/home/"),
    *[(f"/volume{n}", f"/data/volume{n}") for n in range(1, 10)],
    ("/home", "/data/home"),
]

_samba_cache: dict[str, Any] = {"mtime": None, "shares": {}, "home_template": None}


def is_nas_admin(username: str) -> bool:
    """True if the NAS Linux user is in the system `admin` group (UGOS administrators)."""
    if not username:
        return False
    try:
        admin = grp.getgrnam("admin")
    except KeyError:
        return False
    if username in admin.gr_mem:
        return True
    try:
        pw = pwd.getpwnam(username)
    except KeyError:
        return False
    return pw.pw_gid == admin.gr_gid


def user_group_names(username: str, primary_gid: int) -> list[str]:
    """Primary + supplementary group names for Samba valid users / ACLs."""
    host = host_group_names(username, primary_gid)
    if host:
        return host
    names: list[str] = []
    seen: set[str] = set()
    try:
        primary = grp.getgrgid(primary_gid).gr_name
        names.append(primary)
        seen.add(primary)
    except KeyError:
        pass
    if not username:
        return names
    try:
        for gr in grp.getgrall():
            if username in gr.gr_mem and gr.gr_name not in seen:
                names.append(gr.gr_name)
                seen.add(gr.gr_name)
    except OSError:
        pass
    return names


def _sanitize_share_id(share: str) -> str:
    share = (share or "").strip().replace("\\", "/")
    if not share or share in (".", "..") or "/" in share or share.startswith("@"):
        raise FSError("Invalid share", 400)
    if share.startswith("~") and not personal_owner(share):
        raise FSError("Invalid share", 400)
    return share


def personal_owner(share: str) -> str | None:
    """Return the NAS username for a `~user` personal-drive id, else None."""
    share = (share or "").strip()
    if not share.startswith("~"):
        return None
    owner = share[1:]
    if not _PERSONAL_USER_RE.fullmatch(owner):
        return None
    return owner


def _personal_share_id(username: str) -> str:
    return f"~{username}"


def _map_host_path(host_path: str) -> Path | None:
    """Map a Samba path on the NAS into the container filesystem."""
    host_path = (host_path or "").strip()
    if not host_path or "%" in host_path:
        # %H / %u home templates — not a fixed shared folder.
        return None
    for host_prefix, container_prefix in _HOST_PREFIX_MAP:
        if host_path == host_prefix.rstrip("/") or host_path.startswith(host_prefix):
            mapped = container_prefix + host_path[len(host_prefix) :]
            return Path(mapped)
    # Already a container path?
    p = Path(host_path)
    if p.is_dir():
        return p
    return None


def _parse_samba_bool(value: str) -> bool:
    return value.strip().lower() in {"yes", "true", "1", "on"}


def _expand_samba_path(path: str, username: str, home: str | None) -> str:
    """Expand Samba %H / %u / %U templates for a NAS user."""
    out = path or ""
    if home:
        out = out.replace("%H", home)
    out = out.replace("%u", username).replace("%U", username)
    return out


def _parse_smbshare_conf(
    text: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    """
    Parse UGOS smbshare.conf into
    ({share_name: {path, valid_users, write_list, writeable}}, home_template).
    """
    shares: dict[str, dict[str, Any]] = {}
    home_sections: dict[str, dict[str, Any]] = {}
    section: str | None = None
    current: dict[str, Any] = {}

    def flush() -> None:
        nonlocal section, current
        if section and current.get("path"):
            key = section.lower()
            if key in _HOME_SECTIONS:
                home_sections[key] = current
            elif key not in _SKIP_SECTIONS:
                shares[section] = current
        section = None
        current = {}

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        m = re.match(r"^\[(.+)\]\s*$", line)
        if m:
            flush()
            section = m.group(1).strip()
            current = {
                "path": "",
                "valid_users": "",
                "write_list": "",
                "writeable": True,
                "browseable": True,
            }
            continue
        if section is None or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().lower()
        val = val.strip()
        if key == "path":
            current["path"] = val
        elif key == "valid users":
            current["valid_users"] = val
        elif key == "write list":
            current["write_list"] = val
        elif key in ("writeable", "writable"):
            current["writeable"] = _parse_samba_bool(val)
        elif key == "browseable":
            current["browseable"] = _parse_samba_bool(val)

    flush()
    home_template = home_sections.get("personal_folder") or home_sections.get("homes")
    return shares, home_template


def _load_samba_shares() -> dict[str, dict[str, Any]]:
    """
    Cached Samba share table with container paths.

    Each value: {host_path, container_path: Path, valid_users, write_list, writeable, browseable}
    """
    settings = get_settings()
    conf_path = Path(settings.samba_shares_conf)
    try:
        mtime = conf_path.stat().st_mtime if conf_path.is_file() else None
    except OSError:
        mtime = None

    if _samba_cache["mtime"] == mtime and _samba_cache["shares"]:
        return _samba_cache["shares"]  # type: ignore[return-value]

    result: dict[str, dict[str, Any]] = {}
    home_template: dict[str, Any] | None = None
    if conf_path.is_file():
        try:
            text = conf_path.read_text(encoding="utf-8", errors="replace")
            parsed, home_template = _parse_smbshare_conf(text)
            for name, meta in parsed.items():
                mapped = _map_host_path(str(meta.get("path") or ""))
                if mapped is None:
                    continue
                result[name] = {
                    "host_path": meta["path"],
                    "container_path": mapped,
                    "valid_users": meta.get("valid_users") or "",
                    "write_list": meta.get("write_list") or "",
                    "writeable": bool(meta.get("writeable", True)),
                    "browseable": bool(meta.get("browseable", True)),
                }
        except OSError:
            result = {}
            home_template = None

    # Fallback: directory scan of mounted volumes if Samba conf missing/empty.
    if not result:
        for base in _volume_bases():
            if not base.is_dir():
                continue
            try:
                for child in base.iterdir():
                    if child.name.startswith("@") or not child.is_dir():
                        continue
                    if child.name in result:
                        continue
                    result[child.name] = {
                        "host_path": str(child),
                        "container_path": child,
                        "valid_users": "",
                        "write_list": "",
                        "writeable": True,
                        "browseable": True,
                    }
            except OSError:
                continue

    _samba_cache["mtime"] = mtime
    _samba_cache["shares"] = result
    _samba_cache["home_template"] = home_template
    return result


def _home_template() -> dict[str, Any] | None:
    _load_samba_shares()
    tmpl = _samba_cache.get("home_template")
    return tmpl if isinstance(tmpl, dict) else None


def _volume_bases() -> list[Path]:
    settings = get_settings()
    bases: list[Path] = []
    raw = (settings.shares_volumes or "").strip()
    if raw:
        for part in raw.split(","):
            part = part.strip()
            if part:
                bases.append(Path(part))
    # Legacy single-volume + extras
    bases.append(Path(settings.shares_volume))
    bases.append(Path(settings.extra_shares_dir))
    # Dedupe while preserving order
    seen: set[str] = set()
    out: list[Path] = []
    for b in bases:
        key = str(b)
        if key in seen:
            continue
        seen.add(key)
        out.append(b)
    return out


def _passwd_home(username: str) -> Path | None:
    try:
        pw = pwd.getpwnam(username)
    except KeyError:
        return None
    home = (pw.pw_dir or "").strip()
    return Path(home) if home else None


def _personal_home_candidates(username: str) -> list[Path]:
    """Possible container paths for a user's UGOS personal folder."""
    if not _PERSONAL_USER_RE.fullmatch(username or ""):
        return []
    pw_home = _passwd_home(username)
    raw: list[Path] = []
    tmpl = _home_template()
    if tmpl and tmpl.get("path"):
        expanded = _expand_samba_path(
            str(tmpl["path"]),
            username,
            str(pw_home) if pw_home else None,
        )
        if expanded and "%" not in expanded:
            mapped = _map_host_path(expanded)
            raw.append(mapped if mapped is not None else Path(expanded))
    if pw_home is not None:
        mapped = _map_host_path(str(pw_home))
        raw.append(mapped if mapped is not None else pw_home)
    for base in _volume_bases():
        raw.append(base / "home" / username)
        raw.append(base / "@home" / username)

    seen: set[str] = set()
    out: list[Path] = []
    for candidate in raw:
        if candidate.name != username:
            continue
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def _personal_home_path(username: str) -> Path | None:
    """Container path of a NAS user's UGOS personal folder, if it exists."""
    for candidate in _personal_home_candidates(username):
        try:
            if candidate.is_dir():
                return candidate.resolve()
        except OSError:
            continue
    return None


def _planned_personal_home(username: str) -> Path | None:
    """Where a missing personal folder should be created (parent must already exist)."""
    for candidate in _personal_home_candidates(username):
        try:
            parent = candidate.parent
            if parent.is_dir() and not candidate.exists():
                return candidate
        except OSError:
            continue
    return None


def _ensure_personal_home(username: str, uid: int, gid: int) -> Path | None:
    """Return this user's personal folder only if UGOS already created it."""
    del uid, gid
    return _personal_home_path(username)


def _personal_usernames(current: str) -> list[str]:
    """Current user first, then sibling home dirs that belong to NAS users."""
    names: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        if not _PERSONAL_USER_RE.fullmatch(name) or name in seen:
            return
        seen.add(name)
        names.append(name)

    add(current)
    home = _personal_home_path(current)
    parent = home.parent if home is not None else None
    if parent is None:
        return names
    try:
        children = list(parent.iterdir())
    except OSError:
        return names
    for child in children:
        try:
            if not child.is_dir() or child.name.startswith((".", "@")):
                continue
            pw = pwd.getpwnam(child.name)
        except (KeyError, OSError):
            continue
        if pw.pw_uid < 1000:
            continue
        add(child.name)
    names.sort(key=lambda n: (0 if n == current else 1, n.lower()))
    return names


def _personal_root(owner: str, status: dict[str, Any] | None) -> Path:
    """Container path of an unlocked (or unencrypted) personal folder."""
    path = _personal_home_path(owner)
    if path is not None:
        if status and status["encrypted"] and not _encrypted_home_mount(path):
            raise FSError("The unlocked NAS folder is not visible yet. Try again shortly.", 503)
        return path
    raise FSError(f"Share not found: {_personal_share_id(owner)}", 404)


def share_path(share: str) -> Path:
    """Absolute filesystem path for a named shared folder or ~user personal drive."""
    share = _sanitize_share_id(share)
    owner = personal_owner(share)
    if owner:
        status = None
        if personal_folders.configured():
            try:
                status = personal_folders.folders.status(owner)
            except personal_folders.OwnerSignInRequired as e:
                raise FSError(str(e), 428) from None
            except Exception as e:
                raise FSError("Personal-folder status unavailable", 503) from e
            if status["locked"]:
                raise FSError("Personal folder is locked. Enter its encryption key to unlock it.", 423)
        return _personal_root(owner, status)

    table = _load_samba_shares()
    if share in table:
        path = Path(table[share]["container_path"]).resolve()
        if path.is_dir():
            return path
        raise FSError(f"Share not found: {share}", 404)

    # Legacy lookup under volume mounts (share not in Samba conf yet).
    for base in _volume_bases():
        if not base.is_dir():
            continue
        candidate = (base / share).resolve()
        try:
            candidate.relative_to(base.resolve())
        except ValueError:
            continue
        if candidate.is_dir():
            return candidate

    raise FSError(f"Share not found: {share}", 404)


def discover_share_ids() -> list[str]:
    """All Samba (or volume) shared folder names (not filtered by ACL)."""
    table = _load_samba_shares()
    found = [name for name, meta in table.items() if meta.get("browseable", True)]
    preferred = get_settings().default_share
    found.sort(key=lambda n: (0 if n == preferred else 1, n.lower()))
    return found


def _token_matches_user(token: str, username: str, groups: set[str]) -> bool:
    """Match one Samba valid-users / write-list token to this NAS user."""
    token = token.strip().strip('"').strip("'")
    if not token:
        return False
    if token.startswith("@") or token.startswith("+"):
        return token[1:] in groups
    if token.startswith("&"):
        # Samba netgroup — treat as group name without &
        return token[1:] in groups
    return token == username


def _user_in_samba_list(spec: str, username: str, groups: set[str]) -> bool:
    if not (spec or "").strip():
        return False
    try:
        tokens = shlex.split(spec)
    except ValueError:
        tokens = spec.split()
    return any(_token_matches_user(t, username, groups) for t in tokens)


def _probe_share(
    share: str,
    uid: int,
    gid: int,
    username: str | None,
    groups: set[str],
    meta: dict[str, Any] | None,
    root: Path | None = None,
) -> dict[str, Any] | None:
    """
    Return share metadata if this user may open it.

    Prefer Samba valid users / write list (UGOS often uses skip smb perm = yes,
    so world-writable POSIX modes are *not* the real ACL). Fall back to a
    live listdir as the user when Samba has no valid users line.

    Pass `root` when the caller already resolved it (saves a UGOS status call).
    """
    if root is None:
        try:
            root = share_path(share)
        except FSError:
            return None

    uname = username or ""
    valid = (meta or {}).get("valid_users") or ""
    write_list = (meta or {}).get("write_list") or ""
    writeable_flag = bool((meta or {}).get("writeable", True))

    # Samba allow list
    if valid.strip():
        if not _user_in_samba_list(valid, uname, groups):
            return None
    # else: open to anyone who can list on disk

    # Must be able to enter+list for the web browser to work.
    with as_user(uid, gid, uname or None):
        try:
            if not root.is_dir():
                return None
            os.listdir(root)
        except (PermissionError, OSError, FileNotFoundError):
            return None

        can_write = False
        if write_list.strip():
            can_write = _user_in_samba_list(write_list, uname, groups)
        elif writeable_flag:
            try:
                can_write = os.access(root, os.W_OK, effective_ids=True)
            except TypeError:
                can_write = os.access(root, os.W_OK)
            except OSError:
                can_write = False

    return {
        "id": share,
        "name": share,
        "kind": "shared",
        "can_read": True,
        "can_write": bool(can_write),
    }


def _list_personal_shares(
    username: str,
    uid: int,
    gid: int,
    groups: set[str],
) -> list[dict[str, Any]]:
    """Personal folders this POSIX user can actually enter (never auto-created)."""
    owners = [username] if not is_nas_admin(username) else _personal_usernames(username)
    owners = [owner for owner in owners if owner]
    return _personal_share_entries(owners, uid, gid, username, groups)


def _personal_share_entries(
    owners: list[str],
    uid: int,
    gid: int,
    username: str,
    groups: set[str],
) -> list[dict[str, Any]]:
    """Share entries for these personal-folder owners (one UGOS status call for all)."""
    posix_meta = {"valid_users": "", "write_list": "", "writeable": True}
    statuses: dict[str, Any] = {}
    if owners and personal_folders.configured():
        try:
            statuses = personal_folders.folders.statuses(owners)
        except Exception as e:
            statuses = {owner: e for owner in owners}
    found: list[dict[str, Any]] = []
    for owner in owners:
        sid = _personal_share_id(owner)
        status = None
        if personal_folders.configured():
            status = statuses.get(owner)
            if isinstance(status, personal_folders.OwnerSignInRequired):
                status = {"locked": True, "encrypted": True, "expires_at": None, "needs_owner_signin": True}
            elif not isinstance(status, dict):
                # Keep the entry visible, but never fall through to the unmounted directory.
                status = {"locked": True, "encrypted": True, "expires_at": None}
            if status["locked"]:
                found.append({"id": sid, "name": owner, "kind": "personal", "can_read": False,
                              "can_write": False, **status})
                continue
        try:
            root = _personal_root(owner, status)
        except FSError:
            continue
        info = _probe_share(sid, uid, gid, username, groups, posix_meta, root)
        if not info:
            continue
        info["id"] = sid
        info["name"] = owner
        info["kind"] = "personal"
        if status:
            info.update(status)
        found.append(info)
    return found


def list_shares_for_user(username: str, uid: int, gid: int) -> list[dict[str, Any]]:
    """
    Shares this user may open in the web UI.

    Built from UGOS Samba definitions (volume1 + volume2 shares like Camp MAC),
    filtered by Samba valid users / groups and a real listdir probe, plus the
    user's personal folder (`~username`). NAS admins also see other users'
    personal folders they can list.
    """
    groups = set(user_group_names(username, gid))
    table = _load_samba_shares()
    shares: list[dict[str, Any]] = []

    for sid in discover_share_ids():
        meta = table.get(sid)
        info = _probe_share(sid, uid, gid, username, groups, meta)
        if info:
            info["kind"] = "shared"
            shares.append(info)

    shares.extend(_list_personal_shares(username, uid, gid, groups))
    return shares


def _share_listed_for_user(share: str, username: str, uid: int, gid: int) -> bool:
    """True if list_shares_for_user() would include `share`, checking only that share."""
    groups = set(user_group_names(username, gid))
    if share in discover_share_ids():
        return _probe_share(share, uid, gid, username, groups, _load_samba_shares().get(share)) is not None
    owner = personal_owner(share)
    if not owner:
        return False
    if owner != username and not (is_nas_admin(username) and owner in _personal_usernames(username)):
        return False
    return bool(_personal_share_entries([owner], uid, gid, username, groups))


def normalize_share_for_user(
    share: str | None,
    username: str,
    uid: int,
    gid: int,
) -> str:
    """Pick a valid active share for this user (session value or default)."""
    preferred = get_settings().default_share
    candidate = (share or "").strip() or preferred
    # Runs on every API request: check just the requested share, and only build
    # the full list (probes + UGOS status for every folder) to pick a fallback.
    if candidate and _share_listed_for_user(candidate, username, uid, gid):
        return candidate
    available = [s["id"] for s in list_shares_for_user(username, uid, gid) if s.get("can_read")]
    if candidate in available:
        return candidate
    if preferred in available:
        return preferred
    if not available:
        raise FSError("You don't have access to any shared folders on this NAS yet.", 404)
    return available[0]
