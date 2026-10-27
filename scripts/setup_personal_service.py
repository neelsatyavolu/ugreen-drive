"""Run inside the NAS app container; admin OTP arrives on stdin, never in argv.

Creates only the dedicated account, then stores its generated password encrypted
with SESSION_SECRET. Does not retain the admin OTP or any folder encryption key.
"""
import base64
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

from cryptography.fernet import Fernet
from config import get_settings
from ugos_api import UgosClient

NAME = "ugreen-drive-svc"
s = get_settings()
if s.session_secret in ("", "change-me"):
    raise RuntimeError("Configure a strong session secret before storing a service credential")
target = Path(s.personal_credential_path)
cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(
    b"personal-folder-service\0" + s.session_secret.encode()).digest()))
if target.exists():
    data = json.loads(cipher.decrypt(target.read_bytes()))
    assert data["username"] == NAME
else:
    with UgosClient(s.ugos_api_url, s.ugos_admin_user, s.ugos_admin_password) as admin:
        admin.login(sys.stdin.read().strip())
        rows = admin.list_users()
        if any(r.get("username") == NAME for r in rows):
            raise RuntimeError("Service account already exists without its credential file; reconcile before continuing")
        role = next(r["role"] for r in rows if r.get("username") == s.ugos_admin_user)
        data = {"username": NAME, "password": secrets.token_urlsafe(18) + "aA1!"}
        # Persist recovery material before the API call, without overwriting an existing file.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(cipher.encrypt(json.dumps(data).encode()))
        admin.create_user(username=NAME, password=data["password"], email="",
                          description="ugreen-drive personal folder lock service", groups=["admin", "users"],
                          role=role, enable_home_dir=False)
with UgosClient(s.ugos_api_url, data["username"], data["password"]) as client:
    client.login()
    state = client.personal_status(f"/home/{s.ugos_admin_user}")
    print(f"Dedicated service account authenticated; personal-folder status={state}")
