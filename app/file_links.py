"""Signed, unlisted public download links for a single file or a folder."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from itsdangerous import BadSignature, URLSafeSerializer

from config import get_settings

SALT = "file-share-v1"
MIN_DAYS = 1
MAX_DAYS = 30
DEFAULT_DAYS = 7

PREVIEW_IMAGE = frozenset({"png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "avif"})
PREVIEW_VIDEO = frozenset({"mp4", "webm", "m4v", "ogg", "ogv", "mov"})
PREVIEW_AUDIO = frozenset({"mp3", "wav", "ogg", "oga", "m4a", "aac", "flac"})
PREVIEW_PDF = frozenset({"pdf"})
PREVIEW_TEXT = frozenset({
    "txt", "md", "markdown", "csv", "tsv", "log", "json", "xml", "yml", "yaml",
    "rtf", "html", "htm", "css", "js", "ts", "py", "sh",
})


class LinkError(Exception):
    def __init__(self, code: str, payload: dict | None = None):
        super().__init__(code)
        self.code = code  # invalid | expired
        self.payload = payload or {}


def _ser() -> URLSafeSerializer:
    return URLSafeSerializer(get_settings().session_secret, salt=SALT)


def _ext(name: str) -> str:
    if not name or "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


def preview_kind(name: str) -> str | None:
    ext = _ext(name)
    if ext in PREVIEW_IMAGE:
        return "image"
    if ext in PREVIEW_VIDEO:
        return "video"
    if ext in PREVIEW_AUDIO:
        return "audio"
    if ext in PREVIEW_PDF:
        return "pdf"
    if ext in PREVIEW_TEXT:
        return "text"
    return None


def iso_utc(ts: int | float) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mint(
    *, share: str, path: str, days: int, uid: int, gid: int, name: str, is_dir: bool = False
) -> str:
    days = int(days)
    if days < MIN_DAYS or days > MAX_DAYS:
        raise ValueError("days must be 1–30")
    payload = {
        "v": 1,
        "share": share,
        "path": path,
        "exp": int(time.time()) + days * 86400,
        "name": Path(name).name,
        "uid": int(uid),
        "gid": int(gid),
    }
    if is_dir:
        payload["dir"] = True
    return _ser().dumps(payload)


def decode(token: str) -> dict:
    try:
        payload = _ser().loads(token)
    except (BadSignature, TypeError, ValueError) as e:
        raise LinkError("invalid") from e
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise LinkError("invalid")
    for key in ("share", "path", "exp", "uid", "gid"):
        if key not in payload:
            raise LinkError("invalid")
    return payload


def is_folder(payload: dict) -> bool:
    return payload.get("dir") is True


def verify(token: str) -> dict:
    payload = decode(token)
    try:
        exp = int(payload["exp"])
    except (TypeError, ValueError) as e:
        raise LinkError("invalid") from e
    if exp < int(time.time()):
        raise LinkError("expired", payload)
    return payload


def public_url(token: str) -> str:
    base = (get_settings().public_base_url or "").rstrip("/")
    return f"{base}/s/{token}"
