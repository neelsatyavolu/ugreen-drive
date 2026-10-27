"""Browser-bound, single-use email codes for emails linked to a NAS user in Settings."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

from config import get_settings
from shares import DEFAULT_SHARE
from users import resolve_nas_user

COOKIE = "ugreen_drive_email_code"
TTL = 600


def check_origin(request: Request) -> None:
    settings = get_settings()
    origins = {settings.public_base_url.rstrip("/"), settings.lan_origin.rstrip("/")} - {""}
    origin = request.headers.get("origin") or ""
    # Also this exact host (e.g. the tunnel address before Settings has it).
    same_host = origin.split("://", 1)[-1].lower() == (request.headers.get("host") or "").lower()
    if origin not in origins and not (origin.startswith(("http://", "https://")) and same_host):
        raise HTTPException(403, f"Sign in from the {settings.drive_name} page.")


router = APIRouter(prefix="/auth/email", dependencies=[Depends(check_origin)])


class EmailInput(BaseModel):
    email: str = Field(max_length=254)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Enter a valid email address.")
        return value


class CodeInput(EmailInput):
    code: str = Field(pattern=r"^[0-9]{6}$")


def digest(value: str) -> str:
    secret = get_settings().session_secret
    if not secret or secret.startswith("change-me"):
        raise HTTPException(503, "Email sign-in is not configured yet.")
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


@contextmanager
def database():
    path = Path(get_settings().email_sign_in_db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Persist outside the image so container restarts cannot reset attempt limits.
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS email_codes (
                email TEXT PRIMARY KEY, code_hash TEXT, browser_hash TEXT,
                expires_at REAL, sent_at REAL, attempts INTEGER,
                window_started_at REAL, send_count INTEGER
            )""")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
    finally:
        conn.close()


def issue_code(email: str, browser: str) -> str | None:
    now = time.time()
    with database() as conn:
        row = conn.execute("SELECT * FROM email_codes WHERE email = ?", (email,)).fetchone()
        new_window = not row or now - row["window_started_at"] >= 3600
        if row and (now - row["sent_at"] < 60 or (not new_window and row["send_count"] >= 5)):
            return None
        code = f"{secrets.randbelow(1_000_000):06d}"
        conn.execute("""INSERT OR REPLACE INTO email_codes
            (email, code_hash, browser_hash, expires_at, sent_at, attempts, window_started_at, send_count)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?)""", (
            email, digest(f"code:{email}:{browser}:{code}"), digest(f"browser:{browser}"),
            now + TTL, now, now if new_window else row["window_started_at"],
            1 if new_window else row["send_count"] + 1,
        ))
        return code


def consume_code(email: str, code: str, browser: str) -> bool:
    if not browser or not re.fullmatch(r"[0-9]{6}", code):
        return False
    with database() as conn:
        row = conn.execute("SELECT * FROM email_codes WHERE email = ?", (email,)).fetchone()
        if (not row or row["expires_at"] <= time.time() or row["attempts"] >= 5
                or not row["code_hash"] or not hmac.compare_digest(row["browser_hash"], digest(f"browser:{browser}"))):
            return False
        valid = hmac.compare_digest(row["code_hash"], digest(f"code:{email}:{browser}:{code}"))
        conn.execute("UPDATE email_codes SET attempts = attempts + 1, code_hash = ? WHERE email = ?",
                     ("" if valid else row["code_hash"], email))
        return valid


def may_request_code(email: str) -> bool:
    return resolve_nas_user(email) is not None


def send_code(email: str, code: str) -> None:
    settings = get_settings()
    try:
        with httpx.Client(timeout=20) as client:
            result = client.post("https://api.resend.com/emails", headers={
                "Authorization": f"Bearer {settings.resend_api_key}"
            }, json={
                "from": settings.resend_from_email,
                "to": [email],
                "subject": f"Your {settings.drive_name} sign-in code",
                "text": f"Your {settings.drive_name} sign-in code is {code}.\n\n"
                        "Enter it on the sign-in page where you requested it. "
                        "It expires in 10 minutes and works once.\n\n"
                        "If you didn't request this code, you can ignore this email.",
            })
            result.raise_for_status()
    except httpx.HTTPError:
        # Never surface provider responses, credentials, or codes in logs/errors.
        raise HTTPException(503, "Could not send your code. Wait a minute and try again.") from None


@router.post("/request")
def request_code(body: EmailInput, request: Request, response: Response):
    settings = get_settings()
    if not settings.resend_api_key or not settings.resend_from_email:
        raise HTTPException(503, "Email sign-in isn't turned on. Sign in with your NAS username and password.")
    digest("configuration-check")
    browser = secrets.token_urlsafe(32)
    if may_request_code(body.email):
        code = issue_code(body.email, browser)
        if code is None:
            raise HTTPException(429, "Wait a minute before resending. You can request up to 5 codes per hour.")
        send_code(body.email, code)
    response.set_cookie(COOKIE, browser, max_age=TTL, httponly=True, samesite="strict",
                        secure=request.headers.get("origin", "").startswith("https://"), path="/auth/email")
    return {"ok": True}


@router.post("/verify")
def verify_code(body: CodeInput, request: Request, response: Response):
    if not consume_code(body.email, body.code, request.cookies.get(COOKIE, "")):
        raise HTTPException(400, "This code is invalid or expired. Try again or request a new code.")
    nas = resolve_nas_user(body.email)
    if nas is None:
        raise HTTPException(403, "This email isn't linked to a NAS account. Ask the NAS admin to add it.")
    request.session.clear()
    request.session["user"] = {
        "email": body.email, "name": nas.username, "picture": None,
        "username": nas.username, "uid": nas.uid, "gid": nas.gid, "via": "email",
    }
    request.session["share"] = DEFAULT_SHARE
    response.delete_cookie(COOKIE, path="/auth/email", httponly=True, samesite="strict",
                           secure=request.headers.get("origin", "").startswith("https://"))
    return {"ok": True}
