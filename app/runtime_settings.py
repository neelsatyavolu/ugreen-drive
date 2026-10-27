"""Settings an admin edits in the web UI, saved in the config folder.

Environment values are the defaults; `/config/settings.json` overrides them.
This module also creates the session secret on first start, so a fresh
install needs no `.env` at all.

The live `Settings` object is shared by every module (`get_settings()`), so
`apply()` writes the effective values onto it. Only the main process saves;
upload workers read the file once at import.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from config import SESSION_SECRET_MIN_LEN, Settings, get_settings

EDITABLE_FIELDS = (
    "drive_name",
    "public_base_url",
    "lan_origin",
    "default_share",
    "google_client_id",
    "google_client_secret",
    "resend_api_key",
    "resend_from_email",
)
SECRET_FIELDS = frozenset({"google_client_secret", "resend_api_key"})
_ORIGIN_FIELDS = frozenset({"public_base_url", "lan_origin"})
_MAX_LEN = 300
_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_USERNAME_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")

# Env defaults, captured before the first overlay so a cleared field falls back to them.
_env_defaults: dict[str, Any] = {}


class SettingsError(ValueError):
    """A value an admin entered that can't be saved."""


def _config_dir(s: Settings) -> Path:
    return Path(s.config_dir)


def settings_path(s: Settings | None = None) -> Path:
    return _config_dir(s or get_settings()) / "settings.json"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_file(s: Settings | None = None) -> dict[str, Any]:
    """Saved admin settings: {"values": {...}, "identities": {email: username}}."""
    path = settings_path(s)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"values": {}, "identities": {}}
    values = data.get("values") if isinstance(data.get("values"), dict) else {}
    ids = data.get("identities") if isinstance(data.get("identities"), dict) else {}
    return {
        "values": {k: str(v) for k, v in values.items() if k in EDITABLE_FIELDS},
        "identities": {str(k).lower(): str(v) for k, v in ids.items()},
    }


def ensure_session_secret(s: Settings) -> str:
    """The env secret if it's strong enough, else the generated one in the config folder.

    Several processes start at once (main app + upload workers), so the file is
    created with a hard link: exactly one writer wins and the rest read its value.
    """
    env = (s.session_secret or "").strip()
    if len(env) >= SESSION_SECRET_MIN_LEN:
        return env
    path = _config_dir(s) / "session_secret"
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if len(existing) >= SESSION_SECRET_MIN_LEN:
        return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".session_secret.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(secrets.token_hex(32))
        os.chmod(tmp, 0o600)
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass
    finally:
        Path(tmp).unlink(missing_ok=True)
    return path.read_text(encoding="utf-8").strip()


def detect_lan_ip() -> str:
    """The NAS's LAN address (host network), or "" when it can't be found."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # UDP connect sends nothing; it only picks the outgoing interface.
        sock.connect(("10.255.255.255", 1))
        ip = sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()
    return "" if ip.startswith("127.") else ip


def _clean_origin(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise SettingsError(f"{value} isn't an address like https://drive.example.com")
    if parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise SettingsError(f"{value} should be just the address, with no path")
    return f"{parsed.scheme}://{parsed.netloc.lower()}"


def apply(s: Settings | None = None) -> Settings:
    """Overlay saved values and first-run defaults onto the live settings."""
    s = s or get_settings()
    if not _env_defaults:
        _env_defaults.update({k: getattr(s, k) for k in EDITABLE_FIELDS})
    saved = load_file(s)["values"]
    for key in EDITABLE_FIELDS:
        # An empty saved value means "use the default" (env, then detection).
        setattr(s, key, saved.get(key) or _env_defaults[key])
    s.session_secret = ensure_session_secret(s)
    if not s.lan_origin.strip():
        ip = detect_lan_ip()
        s.lan_origin = f"http://{ip}:{s.web_port}" if ip else ""
    if not s.public_base_url.strip():
        s.public_base_url = s.lan_origin or f"http://localhost:{s.web_port}"
    if not s.ugos_admin_path.strip() and s.lan_origin:
        host = urlparse(s.lan_origin).hostname
        s.ugos_admin_path = f"http://{host}:9999/" if host else ""
    if not s.drive_name.strip():
        s.drive_name = "Drive"
    return s


def identities(s: Settings | None = None) -> dict[str, str]:
    """Linked sign-ins: email (lowercase) → NAS username."""
    return load_file(s)["identities"]


def _clean_values(values: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, raw in values.items():
        if key not in EDITABLE_FIELDS:
            raise SettingsError(f"Unknown setting: {key}")
        value = str(raw if raw is not None else "").strip()
        if len(value) > _MAX_LEN:
            raise SettingsError(f"{key} is too long")
        if key in _ORIGIN_FIELDS:
            value = _clean_origin(value)
        if key == "resend_from_email" and value and "@" not in value:
            raise SettingsError("The sender should be an email address, e.g. Drive <drive@example.com>")
        if key == "drive_name" and len(value) > 40:
            raise SettingsError("Keep the name under 40 characters")
        out[key] = value
    return out


def _clean_identities(ids: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for email, username in ids.items():
        email = str(email).strip().lower()
        username = str(username).strip()
        if not _EMAIL_RE.fullmatch(email) or len(email) > 254:
            raise SettingsError(f"{email or 'That'} isn't a valid email address")
        if not _USERNAME_RE.fullmatch(username):
            raise SettingsError(f"{username or 'That'} isn't a valid NAS username")
        out[email] = username
    return out


def update(
    values: dict[str, Any] | None = None,
    new_identities: dict[str, Any] | None = None,
    s: Settings | None = None,
) -> Settings:
    """Validate and save admin changes, then apply them.

    A secret field left blank keeps its saved value; list it in
    `values["clear"]` to remove it.
    """
    s = s or get_settings()
    current = load_file(s)
    merged_values = dict(current["values"])
    clear = set((values or {}).pop("clear", None) or [])
    for key, value in _clean_values(values or {}).items():
        if key in SECRET_FIELDS and not value:
            continue
        merged_values[key] = value
    for key in clear:
        if key not in EDITABLE_FIELDS:
            raise SettingsError(f"Unknown setting: {key}")
        merged_values[key] = ""
    merged_ids = current["identities"] if new_identities is None else _clean_identities(new_identities)
    _atomic_write(
        settings_path(s),
        json.dumps({"values": merged_values, "identities": merged_ids}, indent=2, sort_keys=True) + "\n",
    )
    return apply(s)


def admin_view(s: Settings | None = None) -> dict[str, Any]:
    """What the Settings screen shows: saved or env values, secrets only as set/unset."""
    s = s or get_settings()
    saved = load_file(s)
    view: dict[str, Any] = {}
    for key in EDITABLE_FIELDS:
        value = saved["values"].get(key) or _env_defaults.get(key, getattr(s, key))
        view[key] = {"set": bool(value)} if key in SECRET_FIELDS else value
    return {
        "values": view,
        "identities": saved["identities"],
        "effective": {
            "public_base_url": s.public_base_url,
            "lan_origin": s.lan_origin,
            "google_redirect_uri": s.public_base_url.rstrip("/") + "/auth/callback",
        },
        "methods": methods(s),
    }


def methods(s: Settings | None = None) -> dict[str, bool]:
    """Sign-in methods that are switched on. NAS password is always available."""
    s = s or get_settings()
    return {
        "password": True,
        "google": bool(s.google_client_id and s.google_client_secret),
        "email": bool(s.resend_api_key and s.resend_from_email),
    }
