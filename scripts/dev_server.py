"""Run the drive on a laptop to work on the UI. Never use this on a NAS.

It serves a temporary folder of sample files and fakes the UGOS sign-in, so any
username works with the password `dev`. Run:

    .venv/bin/python scripts/dev_server.py   # then open http://127.0.0.1:8787
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(tempfile.mkdtemp(prefix="ugreen-drive-dev-"))
for share in ("Photos", "Documents", "Media"):
    (DATA / "volume1" / share).mkdir(parents=True)
(DATA / "volume1" / "Documents" / "notes.md").write_text("# Notes\n\nHello from the dev server.\n")
(DATA / "volume1" / "Documents" / "Taxes").mkdir()
(DATA / "volume1" / "Photos" / "2026").mkdir()

os.environ.update(
    CONFIG_DIR=str(DATA / "config"),
    EMAIL_SIGN_IN_DB_PATH=str(DATA / "config" / "email.sqlite3"),
    CLI_TOKENS_DB_PATH=str(DATA / "config" / "cli_tokens.sqlite3"),
    DRIVE_ROOT=str(DATA / "volume1"),
    SHARES_VOLUME=str(DATA / "volume1"),
    SHARES_VOLUMES=str(DATA / "volume1"),
    SAMBA_SHARES_CONF=str(DATA / "missing-smbshare.conf"),
    LAN_ORIGIN="http://127.0.0.1:8787",
    PUBLIC_BASE_URL="http://127.0.0.1:8787",
    UGOS_ADMIN_PATH="http://127.0.0.1:9999/",
)
sys.path.insert(0, str(ROOT / "app"))

import uvicorn  # noqa: E402

import cli_tokens  # noqa: E402
import main  # noqa: E402
import users  # noqa: E402

_me = users._Pw(pw_name="admin", pw_uid=1001, pw_gid=1001, pw_dir=str(DATA / "home"))
main._pwd_user = lambda name: _me if name == "admin" else None
users._pwd_user = main._pwd_user
main.nas_password_login = lambda base, u, p, **kw: {"ok": p == "dev" and u == "admin", "need_otp": False}
main.is_nas_admin = lambda name: name == "admin"
main.list_nas_usernames = lambda: ["admin", "family"]
# The Mac app and `ugd login` work against this server too.
cli_tokens._nas_account = lambda name, uid=None: _me if name == "admin" and uid in (None, _me.pw_uid) else None

if __name__ == "__main__":
    print(f"Sign in as admin / dev. Sample files: {DATA}")
    uvicorn.run(main.app, host="127.0.0.1", port=8787)
