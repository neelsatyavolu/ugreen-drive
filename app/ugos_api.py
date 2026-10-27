"""UGOS control-panel API client (create users the same way the UI does)."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import uuid
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import (
    load_der_public_key,
    load_pem_public_key,
)

log = logging.getLogger("ugos_api")


def _load_public_key(material: str):
    raw = (material or "").strip()
    if not raw:
        raise ValueError("empty UGOS RSA public key")
    if "BEGIN" in raw:
        return load_pem_public_key(raw.encode("utf-8"), default_backend())
    decoded = base64.b64decode(raw)
    if b"BEGIN" in decoded:
        return load_pem_public_key(decoded, default_backend())
    return load_der_public_key(decoded, default_backend())


def encrypt_password(public_key_material: str, password: str) -> str:
    """RSA PKCS1v15 + base64 — same as JSEncrypt.encrypt / encryptLong for short strings."""
    pub = _load_public_key(public_key_material)
    encrypted = pub.encrypt(password.encode("utf-8"), padding.PKCS1v15())
    return base64.b64encode(encrypted).decode("ascii")


def decode_en_public_key(material: str) -> str:
    """Login `public_key` is base64(PEM); desktop does atob() before JSEncrypt."""
    raw = (material or "").strip()
    if not raw:
        return ""
    if "BEGIN" in raw:
        return raw
    try:
        decoded = base64.b64decode(raw).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return raw
    return decoded if "BEGIN" in decoded else raw


def new_aes_key() -> str:
    """32 ASCII hex chars — Node treats this UTF-8 string as the AES-256 key."""
    return uuid.uuid4().hex


def encrypt_aes_gcm(key: str, plaintext: str) -> str:
    iv = os.urandom(12)
    ct = AESGCM(key.encode("utf-8")).encrypt(iv, plaintext.encode("utf-8"), None)
    return base64.b64encode(iv + ct).decode("ascii")


def decrypt_aes_gcm(key: str, blob: str) -> str:
    raw = base64.b64decode(blob)
    pt = AESGCM(key.encode("utf-8")).decrypt(raw[:12], raw[12:], None)
    return pt.decode("utf-8")


def json_compact(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True)


def hash_md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def hash_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class UgosError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None):
        super().__init__(message)
        self.code = code


def _ug_body(resp: httpx.Response) -> dict[str, Any]:
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise UgosError("UGOS returned a non-object JSON body")
    return data


def _otp_token_id(data: dict[str, Any]) -> str:
    for key in ("token_id", "tokenId", "otp_id", "id", "tmp_token"):
        val = data.get(key)
        if val is not None and str(val).strip() and key != "id":
            return str(val).strip()
    # `id` last — often a user id, not the OTP challenge.
    val = data.get("id")
    return str(val).strip() if val is not None and str(val).strip() else ""


def nas_password_login(base_url: str, username: str, password: str, *, return_session: bool = False, return_login_data: bool = False) -> dict[str, Any]:
    """UGOS username/password. Returns ok / need_otp / error. Never stores the password."""
    base = (base_url or "").rstrip("/")
    if not base or not username or not password:
        return {"ok": False, "error": "Username and password are required"}
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True) as http:
            check = http.post(
                f"{base}/ugreen/v1/verify/check?token=",
                json={"username": username},
            )
            body = _ug_body(check)
            if body.get("code") not in (200, None):
                return {"ok": False, "error": "Invalid username or password"}
            data_obj = body.get("data") if isinstance(body.get("data"), dict) else {}
            rsa = (
                check.headers.get("x-rsa-token")
                or check.headers.get("X-Rsa-Token")
                or data_obj.get("public_key")
            )
            if not rsa:
                return {"ok": False, "error": "Sign-in is temporarily unavailable"}
            # Header is base64(PEM). Official client atob()s it first.
            rsa_mat = decode_en_public_key(str(rsa)) or str(rsa)
            enc = encrypt_password(rsa_mat, password)
            login = http.post(
                f"{base}/ugreen/v1/verify/login",
                json={
                    # Firmware 9406 if otp is omitted/false — old clients without 2FA.
                    "username": username,
                    "password": enc,
                    "keepalive": return_session,
                    "otp": True,
                    "is_simple": False,
                },
            )
            parsed = _ug_body(login)
    except (httpx.HTTPError, ValueError, UgosError) as e:
        log.warning("NAS password login failed: %s", type(e).__name__)
        return {"ok": False, "error": "Invalid username or password"}
    if parsed.get("code") not in (200, None):
        log.warning("NAS UGOS login code=%s msg=%s", parsed.get("code"), parsed.get("msg"))
        return {
            "ok": False,
            "error": "Invalid username or password",
            "code": parsed.get("code"),
        }
    data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
    token = data.get("token") or data.get("static_token")
    if token:
        result = {"ok": True, "need_otp": False}
        if return_session:
            result["session"] = {"token": token, "public_key": decode_en_public_key(str(data.get("public_key") or ""))}
        if return_login_data:
            result["login_data"] = data
        return result
    if data.get("enable_otp"):
        tid = _otp_token_id(data)
        if not tid:
            return {"ok": False, "error": "Two-factor sign-in is required but the NAS did not start a challenge"}
        result = {"ok": True, "need_otp": True, "token_id": tid}
        if return_login_data:
            result["can_email_otp"] = bool(data.get("urgent_email"))
        return result
    return {"ok": False, "error": "Invalid username or password"}


def nas_otp_login(base_url: str, *, code: str, token_id: str, otp_type: int = 1, return_session: bool = False, return_login_data: bool = False) -> dict[str, Any]:
    """Complete UGOS 2FA. type 1 = authenticator, 2 = email code."""
    base = (base_url or "").rstrip("/")
    if not base or not code or not token_id:
        return {"ok": False, "error": "Enter the authentication code"}
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True) as http:
            resp = http.post(
                f"{base}/ugreen/v1/verify/code/login",
                json={
                    "code": code.strip(),
                    "type": int(otp_type) if otp_type in (1, 2) else 1,
                    "token_id": token_id,
                    "trust": False,
                    "trust_info": {"client_type": "web", "system": "Drive", "dev_name": "ugreen-drive"},
                },
            )
            parsed = _ug_body(resp)
    except (httpx.HTTPError, ValueError, UgosError) as e:
        log.warning("NAS OTP login failed: %s", type(e).__name__)
        return {"ok": False, "error": "Invalid or expired authentication code"}
    if parsed.get("code") not in (200, None):
        return {"ok": False, "error": "Invalid or expired authentication code"}
    data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
    if return_session or return_login_data:
        token = data.get("token") or data.get("static_token")
        if not token:
            return {"ok": False, "error": "NAS sign-in did not return a session"}
        result = {"ok": True}
        if return_session:
            result["session"] = {"token": token, "public_key": decode_en_public_key(str(data.get("public_key") or ""))}
        if return_login_data:
            result["login_data"] = data
        return result
    if data.get("token") or data.get("static_token") or parsed.get("code") in (200, None):
        return {"ok": True}
    return {"ok": False, "error": "Invalid or expired authentication code"}


def nas_send_otp_email(base_url: str, username: str) -> bool:
    """Ask UGOS to send its own recovery code to the account's bound address."""
    try:
        with httpx.Client(timeout=15) as client:
            response = client.post(
                base_url.rstrip("/") + "/ugreen/v1/otp/mail/send/code",
                json={"username": username}, headers={"UG-Client-Id": "ugreen-drive"},
            )
            return _ug_body(response).get("code") == 200
    except (httpx.HTTPError, ValueError, UgosError):
        return False


class UgosClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: float = 30.0):
        self.base_url = (base_url or "").rstrip("/")
        self.username = username
        self.password = password
        self._token: str | None = None
        self._rsa_material: str | None = None
        self._en_public_key: str | None = None
        self._http = httpx.Client(timeout=timeout, follow_redirects=True)

    def _http_encrypts(self) -> bool:
        # Desktop RequestEncrypt skips body encryption on https.
        return not self.base_url.lower().startswith("https://")

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> UgosClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _url(self, path: str, token: bool = False) -> str:
        path = path if path.startswith("/") else f"/{path}"
        url = f"{self.base_url}{path}"
        if token and self._token:
            sep = "&" if "?" in path else "?"
            url = f"{url}{sep}token={self._token}"
        return url

    def _check_body(self, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise UgosError("UGOS returned a non-object JSON body")
        code = data.get("code")
        if code not in (200, None):
            raise UgosError(str(data.get("msg") or data.get("message") or code), code=int(code or 0))
        return data

    def _parse(self, resp: httpx.Response) -> dict[str, Any]:
        resp.raise_for_status()
        return self._check_body(resp.json())

    def login(self, otp_code: str | None = None) -> None:
        if not self.password:
            raise UgosError("NAS owner sign-in required", code=401)
        check = self._http.post(
            self._url("/ugreen/v1/verify/check?token="),
            json={"username": self.username},
        )
        body = self._parse(check)
        data_obj = body.get("data") if isinstance(body.get("data"), dict) else {}
        rsa = (
            check.headers.get("x-rsa-token")
            or check.headers.get("X-Rsa-Token")
            or data_obj.get("public_key")
        )
        if not rsa:
            # Some firmware puts the key only in the login response after a
            # plaintext attempt; prefer header.
            raise UgosError("UGOS verify/check did not return an RSA public key")
        self._rsa_material = str(rsa)
        enc = encrypt_password(self._rsa_material, self.password)
        login = self._http.post(
            self._url("/ugreen/v1/verify/login"),
            json={
                "is_simple": False,
                "keepalive": True,
                "otp": True,
                "username": self.username,
                "password": enc,
            },
        )
        data = self._parse(login).get("data") or {}
        if data.get("enable_otp"):
            if not otp_code:
                raise UgosError("UGOS requires two-factor authentication")
            verified = self._http.post(
                self._url("/ugreen/v1/verify/code/login"),
                json={"code": otp_code, "type": 1, "token_id": data["token_id"], "trust": False,
                      "trust_info": {"client_type": "web", "system": "Drive", "dev_name": "ugreen-drive"}},
            )
            data = self._parse(verified).get("data") or {}
        token = data.get("token") or data.get("static_token")
        if not token:
            raise UgosError("UGOS login did not return a session token")
        self._token = str(token)
        if data.get("public_key"):
            pem = decode_en_public_key(str(data["public_key"]))
            self._en_public_key = pem or None
            # Keep check's x-rsa-token for password fields; fall back to login key.
            if not self._rsa_material:
                self._rsa_material = pem

    def _wrap_request(
        self, path: str, *, headers: dict[str, str], json_body: Any | None
    ) -> tuple[str, dict[str, str], dict[str, Any] | None, str | None]:
        """Apply desktop RequestEncrypt (HTTP only). Returns url, headers, json, aes_key."""
        if not self._token:
            raise UgosError("UGOS client is not logged in")
        if not self._http_encrypts():
            return self._url(path, token=True), headers, json_body, None
        pub = self._en_public_key or self._rsa_material
        if not pub:
            raise UgosError("UGOS login did not return a request-encryption public key")

        split = urlsplit(path if path.startswith("/") else f"/{path}")
        params: list[tuple[str, str]] = [("token", self._token)]
        params.extend((k, v) for k, v in parse_qsl(split.query, keep_blank_values=True) if k != "token")
        query_plain = urlencode(params)
        aes_key = new_aes_key()
        headers = dict(headers)
        headers["X-Ugreen-Security-Key"] = hash_md5(self._token)
        headers["X-Ugreen-Security-Code"] = encrypt_password(pub, aes_key)
        enc_query = encrypt_aes_gcm(aes_key, query_plain)
        url = f"{self.base_url}{split.path}?{urlencode({'encrypt_query': enc_query})}"
        enc_json = None
        if json_body is not None:
            plain = json_compact(json_body)
            enc_json = {
                "encrypt_req_body": encrypt_aes_gcm(aes_key, plain),
                "req_body_sha256": hash_sha256(plain),
            }
        return url, headers, enc_json, aes_key

    def _parse_maybe_encrypted(self, resp: httpx.Response, aes_key: str | None) -> dict[str, Any]:
        data = self._parse(resp)
        if aes_key and isinstance(data, dict) and data.get("encrypt_resp_body"):
            try:
                inner = json.loads(decrypt_aes_gcm(aes_key, str(data["encrypt_resp_body"])))
            except (ValueError, json.JSONDecodeError) as e:
                raise UgosError(f"UGOS encrypted response could not be decoded: {e}") from e
            return self._check_body(inner)
        return data

    def _authed(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if not self._token:
            self.login()
        headers = dict(kwargs.pop("headers", {}) or {})
        json_body = kwargs.pop("json", None)
        extra = kwargs

        def once() -> dict[str, Any]:
            url, hdrs, body, aes_key = self._wrap_request(
                path, headers=headers, json_body=json_body
            )
            resp = self._http.request(method, url, headers=hdrs, json=body, **extra)
            return self._parse_maybe_encrypted(resp, aes_key)

        try:
            return once()
        except UgosError as e:
            if e.code in (1024, 401, 403, 1001, 1002):
                self._token = None
                self.login()
                return once()
            raise

    def list_users(self) -> list[dict[str, Any]]:
        data = self._authed("GET", "/ugreen/v1/user/list?reverse=false")
        payload = data.get("data") or {}
        if isinstance(payload, list):
            return [u for u in payload if isinstance(u, dict)]
        for key in ("list", "users", "items"):
            raw = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(raw, list):
                return [u for u in raw if isinstance(u, dict)]
        return []

    def personal_status(self, path: str) -> int:
        found = self.personal_statuses([path])
        if path not in found:
            raise UgosError("Personal-folder status unavailable")
        return found[path]

    def personal_statuses(self, paths: list[str]) -> dict[str, int]:
        """Encryption state of several personal folders in one UGOS round trip.

        Paths UGOS did not report are left out; the first row for a path wins.
        """
        data = self._authed("POST", "/ugreen/v1/filemgr/encryptedDirStatus", json={"paths": list(paths)})
        found: dict[str, int] = {}
        for row in (data.get("data") or {}).get("list", []):
            path = row.get("path")
            if path in paths and path not in found:
                found[path] = int(row["status"])
        return found

    def unlock_personal(self, path: str, key: str, *, key_file: bool = False) -> None:
        # UGOS refreshes this RSA key for each encryption credential submission.
        check = self._http.post(self._url("/ugreen/v1/verify/check"), json={"username": self.username})
        self._parse(check)
        material = check.headers.get("x-rsa-token")
        if not material:
            raise UgosError("UGOS did not provide an encryption public key")
        pub = _load_public_key(material)
        raw = key.encode("utf-8")
        size = pub.key_size // 8 - 11
        encrypted = b"".join(pub.encrypt(raw[i:i + size], padding.PKCS1v15()) for i in range(0, len(raw), size))
        field = "raw_key_content" if key_file else "password"
        self._authed("POST", "/ugreen/v1/filemgr/unlockEncryptedDir", json={
            "path": path, field: base64.b64encode(encrypted).decode("ascii"),
        })

    def lock_personal(self, path: str) -> None:
        self._authed("POST", "/ugreen/v1/filemgr/lockEncryptedDir", json={"path": path})

    def existing_usernames(self) -> set[str]:
        names: set[str] = set()
        for row in self.list_users():
            for key in ("username", "name", "user_name"):
                val = row.get(key)
                if isinstance(val, str) and val.strip():
                    names.add(val.strip())
                    break
        return names

    def existing_emails(self) -> dict[str, str]:
        """email lower → username for accounts UGOS already has."""
        out: dict[str, str] = {}
        for row in self.list_users():
            email = str(row.get("email") or "").strip().lower()
            uname = str(row.get("username") or row.get("name") or "").strip()
            if email and "@" in email and uname:
                out[email] = uname
        return out

    def create_user(
        self,
        *,
        username: str,
        password: str,
        email: str,
        description: str,
        groups: list[str],
        role: str = "users",
        enable_home_dir: bool = True,
    ) -> dict[str, Any]:
        if not self._rsa_material:
            self.login()
        assert self._rsa_material is not None
        payload = {
            "username": username,
            "email": email or "",
            "description": (description or "")[:255],
            "role": role or "users",
            "groups": [{"name": g} for g in groups if g],
            "enable_home_dir": bool(enable_home_dir),
            "deny_change_pwd": False,
            "dir_quota": 0,
            "unit": 1,
            "password": encrypt_password(self._rsa_material, password),
            "expire_time": -1,
            "expire_date": "",
        }
        return self._authed("POST", "/ugreen/v1/user/create", json=payload)
