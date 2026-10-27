"""Map Google email → local NAS (Linux) username / uid / gid."""

from __future__ import annotations

import pwd
from dataclasses import dataclass
from pathlib import Path

import runtime_settings


@dataclass(frozen=True)
class NasUser:
    username: str
    uid: int
    gid: int
    home: str
    email: str


def _load_overrides() -> dict[str, str]:
    """Linked sign-ins from Settings: email (lower) → NAS username."""
    return runtime_settings.identities()


# Directory bind of host /etc — file binds of /etc/passwd go stale after UGOS useradd
# (atomic rename). Opening /host-etc/passwd does a fresh directory lookup.
_HOST_PASSWD = Path("/host-etc/passwd")


@dataclass(frozen=True)
class _Pw:
    pw_name: str
    pw_uid: int
    pw_gid: int
    pw_dir: str
    pw_gecos: str = ""


def gecos_email(gecos: str) -> str | None:
    """Email stored on the NAS user (passwd GECOS), or None."""
    raw = (gecos or "").strip()
    if not raw:
        return None
    lower = raw.lower()
    if "<" in lower and ">" in lower:
        inner = lower.split("<", 1)[1].split(">", 1)[0].strip()
        if "@" in inner and " " not in inner:
            return inner
    token = lower.split(",")[0].strip()
    if "@" in token and " " not in token:
        return token
    return None


def _parse_passwd_line(line: str) -> _Pw | None:
    parts = line.split(":")
    if len(parts) < 6:
        return None
    try:
        uid, gid = int(parts[2]), int(parts[3])
    except ValueError:
        return None
    name = parts[0]
    return _Pw(
        pw_name=name,
        pw_uid=uid,
        pw_gid=gid,
        pw_dir=parts[5] or f"/home/{name}",
        pw_gecos=parts[4] if len(parts) > 4 else "",
    )


def _iter_passwd_file(path: Path) -> list[_Pw]:
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[_Pw] = []
    for line in text.splitlines():
        pw = _parse_passwd_line(line)
        if pw is not None:
            out.append(pw)
    return out


def _user_from_passwd_file(path: Path, name: str) -> _Pw | None:
    if not name:
        return None
    for pw in _iter_passwd_file(path):
        if pw.pw_name == name:
            return pw
    return None


def _user_from_passwd_email(path: Path, email: str) -> _Pw | None:
    want = (email or "").strip().lower()
    if not want or "@" not in want:
        return None
    for pw in _iter_passwd_file(path):
        if pw.pw_uid < 1000:
            continue
        if gecos_email(pw.pw_gecos) == want:
            return pw
    return None


def _pwd_user(name: str) -> pwd.struct_passwd | _Pw | None:
    host = _user_from_passwd_file(_HOST_PASSWD, name)
    if host is not None:
        return host
    try:
        return pwd.getpwnam(name)
    except KeyError:
        return None


def linux_user_email(name: str) -> str | None:
    pw = _pwd_user(name)
    if pw is None:
        return None
    return gecos_email(getattr(pw, "pw_gecos", "") or "")


def linux_user_by_email(email: str) -> _Pw | None:
    """NAS user whose stored email matches, else None. Never local-part fallback."""
    host = _user_from_passwd_email(_HOST_PASSWD, email)
    if host is not None:
        return host
    want = (email or "").strip().lower()
    if not want or "@" not in want:
        return None
    try:
        for pw in pwd.getpwall():
            if pw.pw_uid < 1000:
                continue
            if gecos_email(pw.pw_gecos) == want:
                return _Pw(
                    pw_name=pw.pw_name,
                    pw_uid=pw.pw_uid,
                    pw_gid=pw.pw_gid,
                    pw_dir=pw.pw_dir or f"/home/{pw.pw_name}",
                    pw_gecos=pw.pw_gecos or "",
                )
    except OSError:
        return None
    return None


def split_email(email: str) -> tuple[str, str] | None:
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return None
    local, domain = email.split("@", 1)
    if not local or not domain:
        return None
    return local, domain


def linux_user_exists(name: str) -> bool:
    pw = _pwd_user(name)
    return pw is not None and pw.pw_uid >= 1000


def _nas_from_pw(pw: pwd.struct_passwd | _Pw, email: str) -> NasUser | None:
    if pw.pw_uid < 1000:
        return None
    return NasUser(
        username=pw.pw_name,
        uid=pw.pw_uid,
        gid=pw.pw_gid,
        home=pw.pw_dir,
        email=email,
    )


def resolve_nas_user(email: str) -> NasUser | None:
    """NAS account for a Google or email-code sign-in.

    Only emails an admin linked in Settings → People sign in; there is no
    domain-wide or local-part fallback.
    """
    parts = split_email(email)
    if parts is None:
        return None
    email = "@".join(parts)
    username = _load_overrides().get(email)
    if not username:
        return None
    pw = _pwd_user(username)
    return _nas_from_pw(pw, email) if pw is not None else None


def list_nas_usernames() -> list[str]:
    """Regular NAS accounts (uid >= 1000), for linking sign-ins in Settings."""
    users = _iter_passwd_file(_HOST_PASSWD)
    if not users:
        try:
            users = [_Pw(p.pw_name, p.pw_uid, p.pw_gid, p.pw_dir) for p in pwd.getpwall()]
        except OSError:
            users = []
    return sorted({u.pw_name for u in users if 1000 <= u.pw_uid < 65534})
