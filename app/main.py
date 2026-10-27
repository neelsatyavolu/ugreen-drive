from __future__ import annotations

from html import escape as html_escape
import mimetypes
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote, urlencode, urlparse

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.middleware.sessions import SessionMiddleware

from chunk_upload import (
    CHUNK_SIZE_DEFAULT,
    MAX_UPLOAD_BYTES,
    abort_session,
    complete_session,
    create_session,
    status as chunk_upload_status,
    write_chunk,
)
from config import get_settings, require_strong_session_secret
import personal_folders
import runtime_settings
from email_auth import router as email_auth_router
from file_links import (
    DEFAULT_DAYS,
    MAX_DAYS,
    MIN_DAYS,
    LinkError,
    is_folder as is_folder_link,
    iso_utc,
    mint as mint_file_link,
    preview_kind,
    public_url as file_link_public_url,
    verify as verify_file_link,
)
from fsops import (
    RECYCLE_NAME,
    AuthorizedFile,
    FSError,
    as_user,
    collect_zip_entries,
    delete,
    disk_usage,
    empty_recycle,
    ensure_dir,
    folder_sizes,
    has_recycle_bin,
    is_under_recycle,
    list_dir,
    mkdir,
    move_item,
    open_authorized_path,
    open_for_download,
    open_zip_member,
    zip_member_size,
    rename,
    resolve_rel,
    resolve_within,
    search,
    use_share_root,
    upload_fingerprint,
    write_upload_stream,
)
import link_preview
import thumbs
from zipstream import stream_zip_store, zip_store_content_length
from shares import (
    DEFAULT_SHARE,
    is_nas_admin,
    list_shares_for_user,
    normalize_share_for_user,
    personal_owner,
    share_path,
    user_group_names,
)
from ugos_api import nas_otp_login, nas_password_login
from users import _pwd_user, linux_user_email, list_nas_usernames, resolve_nas_user
settings = runtime_settings.apply(get_settings())


@asynccontextmanager
async def lifespan(app):
    require_strong_session_secret(settings.session_secret)
    # Upload workers (scripts/entrypoint.sh) only take chunk bodies; the main
    # process owns background jobs.
    if os.environ.get("IFD_UPLOAD_WORKER") != "1":
        personal_folders.start_worker()
    try:
        yield
    finally:
        personal_folders.stop_worker()


app = FastAPI(title="ugreen-drive", docs_url=None, redoc_url=None, lifespan=lifespan)
app.include_router(email_auth_router)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie=settings.session_cookie,
    max_age=settings.session_max_age,
    same_site="lax",
    # Tunnel terminates TLS at Cloudflare; backend is plain HTTP
    https_only=False,
)

STATIC = Path(__file__).parent / "static"
app.mount("/assets", StaticFiles(directory=STATIC), name="assets")


@app.middleware("http")
async def asset_cache_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Cache policy: immutable versioned assets; never cache auth/API (share-aware)."""
    response = await call_next(request)
    path = request.url.path
    if path == "/api/thumbnail":
        # Thumb URLs carry the file mtime (?mt=), so they're safe to cache per-user.
        # CDN-Cache-Control keeps Cloudflare from storing private JPEGs at the edge.
        response.headers["Cache-Control"] = "private, max-age=604800"
        response.headers["CDN-Cache-Control"] = "no-store"
    elif path.startswith(("/api/", "/auth/")):
        # Listings differ by X-Drive-Share / ?share= — must not be shared-cached.
        response.headers["Cache-Control"] = "private, no-store, no-cache, must-revalidate"
        response.headers["CDN-Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    elif path.startswith("/assets/"):
        # Deploy cache-bust is the ?v= query on index.html / module imports.
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        response.headers["CDN-Cache-Control"] = "public, max-age=31536000, immutable"
    elif path in ("/", "/index.html") or path.startswith("/s/"):
        # Short edge cache so a new index.html / share.html (new ?v=) shows up quickly.
        response.headers["Cache-Control"] = "public, max-age=60, must-revalidate"
        response.headers["CDN-Cache-Control"] = "public, max-age=60, must-revalidate"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    # Clickjacking only. A full script-src would block the theme boot script,
    # pdf.js and the analytics tag. Routes that already set a CSP keep theirs.
    if "content-security-policy" not in response.headers:
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
    return response


def _origin_allowed(request: Request, origin: str) -> bool:
    """True when a browser POST's Origin is this Drive, not another site."""
    allowed = {settings.public_base_url.rstrip("/"), (settings.lan_origin or "").rstrip("/")} - {""}
    if origin.rstrip("/") in allowed:
        return True
    host = (request.headers.get("host") or request.url.netloc or "").lower()
    try:
        from urllib.parse import urlparse

        parsed = urlparse(origin)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and parsed.netloc.lower() == host


@app.middleware("http")
async def block_cross_site_writes(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Cookie mutations must come from this site."""
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        site = (request.headers.get("sec-fetch-site") or "").lower()
        origin = request.headers.get("origin")
        if site == "cross-site" or (origin is not None and not _origin_allowed(request, origin)):
            return JSONResponse(status_code=403, content={"detail": "Cross-site request blocked"})
    return await call_next(request)

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"


def _state_ser() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret, salt="oauth-state")


def _lan_handoff_ser() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret, salt="lan-handoff")


def _public_config(request: Request | None = None) -> dict[str, Any]:
    """Origins the client needs before / after auth (LAN prefer, OAuth host)."""
    lan = (settings.lan_origin or "").strip().rstrip("/")
    public = settings.public_base_url.rstrip("/")
    host = ""
    if request is not None:
        host = (request.headers.get("host") or request.url.netloc or "").split(":")[0].lower()
    via_lan = False
    if lan and host:
        try:
            from urllib.parse import urlparse

            lan_host = (urlparse(lan).hostname or "").lower()
            via_lan = bool(lan_host and host == lan_host)
        except Exception:
            via_lan = False
    return {
        "drive_name": settings.drive_name,
        "public_base_url": public,
        "lan_origin": lan,
        "lan_prefer": bool(settings.lan_prefer and lan),
        "via_lan": via_lan,
        "sign_in": runtime_settings.methods(settings),
    }


def _safe_next_url(raw: str | None) -> str:
    """Only allow same-site relative redirects (path or hash)."""
    next_url = (raw or "").strip() or "/"
    # Browsers read "/\\x", "/\tx" and friends as "//x" (another site).
    if "\\" in next_url or any(ord(c) < 0x20 or ord(c) == 0x7F for c in next_url):
        return "/"
    if next_url.startswith("#"):
        return f"/{next_url}"
    parsed = urlparse(next_url)
    if next_url.startswith("/") and not next_url.startswith("//") and not parsed.scheme and not parsed.netloc:
        return next_url
    return "/"


def _require_session_user(request: Request) -> dict[str, Any]:
    user = request.session.get("user")
    if not user or "uid" not in user or not _regular_uid(user):
        raise HTTPException(status_code=401, detail="Not authenticated")
    if user.get("via") in ("google", "email") and not _link_still_valid(user):
        # The admin unlinked (or relinked) this email: end the session now.
        request.session.clear()
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def _link_still_valid(user: dict[str, Any]) -> bool:
    nas = resolve_nas_user(str(user.get("email") or ""))
    return nas is not None and nas.username == user.get("username") and nas.uid == user.get("uid")


def _require_user(request: Request) -> dict[str, Any]:
    """Every route signs in with the browser session (there is no token API)."""
    return _require_session_user(request)


def _require_admin(request: Request) -> dict[str, Any]:
    user = _require_session_user(request)
    if not is_nas_admin(str(user.get("username") or "")):
        raise HTTPException(status_code=403, detail="Only NAS administrators can change settings")
    return user


def _regular_uid(user: dict[str, Any]) -> bool:
    """Every sign-in path only issues sessions for NAS users (uid >= 1000)."""
    try:
        return int(user["uid"]) >= 1000
    except (KeyError, TypeError, ValueError):
        return False


def _active_share(request: Request, user: dict[str, Any]) -> str:
    """Resolve share from query / header / session; pin to session.

    Query wins (downloads via <a href>), then explicit header, then session.
    /api/me should omit the share header so a stale client default does not
    wipe a previously chosen share on reload.
    """
    requested = (
        request.query_params.get("share")
        or request.headers.get("X-Drive-Share")
        or request.session.get("share")
    )
    try:
        share = normalize_share_for_user(
            requested,
            str(user.get("username") or ""),
            int(user["uid"]),
            int(user["gid"]),
        )
    except FSError as e:
        raise _fs_http(e) from None
    request.session["share"] = share
    return share


def _fs_http(e: FSError) -> HTTPException:
    return HTTPException(status_code=e.status, detail=str(e))


@app.get("/api/health")
async def health() -> dict[str, str]:
    """Liveness probe — must stay async so it never waits on the as_user thread pool.

    A hung sync health endpoint (all workers blocked on the process-wide FS lock)
    made the public site look fully down even though uvicorn was still running.
    """
    return {"status": "ok"}


@app.get("/api/config")
def api_public_config(request: Request) -> dict[str, Any]:
    """Unauthenticated boot config (LAN prefer origins)."""
    return _public_config(request)


@app.post("/api/lan-handoff")
def api_lan_handoff(request: Request) -> dict[str, Any]:
    """Mint a short-lived token so the browser can open the LAN origin signed-in."""
    user = _require_session_user(request)
    if not (settings.lan_origin or "").strip():
        raise HTTPException(status_code=404, detail="LAN origin not configured")
    payload = {
        "email": user.get("email"),
        "name": user.get("name"),
        "picture": user.get("picture"),
        "username": user.get("username"),
        "uid": user.get("uid"),
        "gid": user.get("gid"),
        "via": user.get("via"),
        "share": request.session.get("share") or DEFAULT_SHARE,
        "nonce": secrets.token_urlsafe(8),
    }
    token = _lan_handoff_ser().dumps(payload)
    return {
        "token": token,
        "lan_origin": settings.lan_origin.rstrip("/"),
        "expires_in": 90,
    }


@app.get("/auth/lan-handoff")
def auth_lan_handoff(
    request: Request,
    token: str = Query(""),
    next: str = Query("/"),
) -> RedirectResponse:
    """Consume a handoff token on the LAN host and establish a session cookie there."""
    if not token:
        raise HTTPException(status_code=400, detail="Missing handoff token")
    try:
        payload = _lan_handoff_ser().loads(token, max_age=90)
    except SignatureExpired as e:
        raise HTTPException(status_code=400, detail="Handoff expired — open Drive again") from e
    except BadSignature as e:
        raise HTTPException(status_code=400, detail="Invalid handoff token") from e

    username = str(payload.get("username") or "")
    try:
        uid = int(payload["uid"])
        gid = int(payload["gid"])
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail="Invalid handoff payload") from e

    request.session["user"] = {
        "email": payload.get("email"),
        "name": payload.get("name"),
        "picture": payload.get("picture"),
        "username": username,
        "uid": uid,
        "gid": gid,
        "via": payload.get("via"),
    }
    share = str(payload.get("share") or DEFAULT_SHARE)
    request.session["share"] = share
    return RedirectResponse(_safe_next_url(next), status_code=302)


@app.get("/api/me")
def me(request: Request) -> dict[str, Any]:
    cfg = _public_config(request)
    try:
        user = _require_user(request)
    except HTTPException:
        return {"authenticated": False, **cfg}
    smb_host = urlparse(settings.lan_origin).hostname or ""
    username = str(user.get("username") or "")
    uid = int(user["uid"])
    gid = int(user["gid"])
    admin = is_nas_admin(username)
    shares = list_shares_for_user(username, uid, gid)
    try:
        active = _active_share(request, user)
    except HTTPException:
        active = ""
    return {
        "authenticated": True,
        "email": user.get("email"),
        "name": user.get("name"),
        "picture": user.get("picture"),
        "nas_username": username,
        "is_admin": admin,
        "groups": user_group_names(username, gid),
        "share": active,
        "shares": shares,
        "ugos_admin_path": settings.ugos_admin_path if admin else "",
        **cfg,
        # Native SMB for Finder/Explorer (NAS password, LAN only); docs/FINDER-NETWORK-DRIVE.md
        "network_drive": {
            "protocol": "smb",
            "host": smb_host,
            "server_url": f"smb://{smb_host}" if smb_host else "",
            "note": "Use your NAS username and password. Works on the same network as the NAS.",
        },
    }


@app.get("/api/admin/settings")
def api_admin_settings(request: Request) -> dict[str, Any]:
    _require_admin(request)
    return {**runtime_settings.admin_view(settings), "nas_users": list_nas_usernames()}


@app.put("/api/admin/settings")
async def api_admin_save_settings(request: Request) -> dict[str, Any]:
    _require_admin(request)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Send the settings as JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Send the settings as JSON")
    values = body.get("values")
    ids = body.get("identities")
    if values is not None and not isinstance(values, dict):
        raise HTTPException(status_code=400, detail="values must be an object")
    if ids is not None and not isinstance(ids, dict):
        raise HTTPException(status_code=400, detail="identities must be an object")
    known = set(list_nas_usernames())
    for email, username in (ids or {}).items():
        if str(username).strip() not in known:
            raise HTTPException(status_code=400, detail=f"{username} isn't a NAS user. Create it in UGOS first.")
    try:
        runtime_settings.update(dict(values) if values else None, ids, settings)
    except runtime_settings.SettingsError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    return {**runtime_settings.admin_view(settings), "nas_users": sorted(known)}


@app.post("/api/share")
async def api_set_share(request: Request) -> dict[str, Any]:
    """Switch the active shared folder (NAS admins only see multiple options)."""
    user = _require_session_user(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    requested = str((body or {}).get("share") or "")
    share = normalize_share_for_user(
        requested,
        str(user.get("username") or ""),
        int(user["uid"]),
        int(user["gid"]),
    )
    request.session["share"] = share
    return {
        "share": share,
        "shares": list_shares_for_user(
            str(user.get("username") or ""),
            int(user["uid"]),
            int(user["gid"]),
        ),
    }


_PERSONAL_OTP_TTL_S = 300


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Like FastAPI's 422, minus the submitted values: a too-long password or
    key must never come back in the response."""
    errors = [{k: v for k, v in err.items() if k in ("type", "loc", "msg")} for err in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


@app.post("/api/personal/unlock")
def api_unlock_personal(request: Request, owner: str = Form(...), key: str = Form(..., max_length=65536),
                        key_file: bool = Form(False)) -> dict[str, Any]:
    user = _require_user(request)
    username = str(user.get("username") or "")
    if owner != username:
        raise HTTPException(status_code=403, detail="You cannot unlock this personal folder")
    if request.url.scheme != "https" and request.url.hostname not in ("localhost", "127.0.0.1", "testserver"):
        raise HTTPException(status_code=426, detail="Open Drive over HTTPS to enter your encryption key")
    if not _nas_login_allowed(f"personal:{user['uid']}"):
        raise HTTPException(status_code=429, detail="Too many unlock attempts. Try again in a few minutes.")
    # Separate from the NAS-password lockout: a wrong encryption key must not
    # lock the account password, and a UGOS outage must not burn a guess.
    if _personal_key_locked(owner):
        raise HTTPException(status_code=429, detail="Too many failed unlock attempts. Try again in 15 minutes.")
    if not personal_folders.configured():
        raise HTTPException(status_code=503, detail="Personal-folder unlocking is not configured")
    try:
        result = personal_folders.folders.unlock(owner, key, key_file=key_file)
    except personal_folders.OwnerSignInRequired as e:
        raise HTTPException(status_code=428, detail=str(e)) from None
    except personal_folders.UgosError:
        _personal_key_failed(owner)
        raise HTTPException(
            status_code=400, detail="Could not unlock the folder. Check the encryption key and try again."
        ) from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=400, detail="Could not unlock the folder. Check the encryption key and try again."
        ) from e
    _personal_key_fails.pop(owner.lower(), None)
    return result


@app.post("/api/personal/auth")
def api_personal_auth(request: Request, owner: str = Form(...),
                      password: str = Form("", max_length=256), code: str = Form("", max_length=12),
                      ) -> dict[str, Any]:
    user = _require_user(request)
    username = str(user.get("username") or "")
    if owner != username:
        raise HTTPException(status_code=403, detail="Sign in as the personal-folder owner")
    if request.url.scheme != "https" and request.url.hostname not in ("localhost", "127.0.0.1", "testserver"):
        raise HTTPException(status_code=426, detail="Open Drive over HTTPS to sign in")
    code = code.strip()
    if not code and not password:
        raise HTTPException(status_code=400, detail="Enter your NAS account password.")
    if not _nas_login_allowed(f"personal-auth:{user['uid']}"):
        raise HTTPException(status_code=429, detail="Too many sign-in attempts. Try again in a few minutes.")
    if code:
        step = request.session.get("personal_otp") or {}
        expires = float(step.get("expires", 0))
        if step.get("owner") != owner or expires < time.time():
            raise HTTPException(status_code=401, detail="Sign-in expired. Enter your NAS password again.")
        # At most 5 codes per challenge, and a challenge that worked is used up.
        attempt_key = f"personal\0{owner}\0{step['token_id']}"
        if not _nas_otp_take_attempt(attempt_key, expires):
            request.session.pop("personal_otp", None)
            raise HTTPException(status_code=401, detail="This sign-in can't take more codes. Enter your NAS password again.")
        result = nas_otp_login(settings.ugos_api_url, code=code, token_id=step["token_id"], return_session=True)
        if result.get("ok"):
            _nas_otp_attempts[attempt_key] = (_NAS_OTP_MAX_ATTEMPTS, expires)
    else:
        request.session.pop("personal_otp", None)
        if _nas_user_locked(owner):
            raise HTTPException(status_code=429, detail="Too many failed sign-ins for this account. Try again in 15 minutes.")
        result = nas_password_login(settings.ugos_api_url, owner, password, return_session=True)
        if result.get("ok"):
            _nas_user_fails.pop(owner.lower(), None)
        else:
            _nas_user_failed(owner)
    if not result.get("ok"):
        raise HTTPException(status_code=401, detail=result.get("error", "NAS sign-in failed"))
    if result.get("need_otp"):
        request.session["personal_otp"] = {"owner": owner, "token_id": result["token_id"],
                                           "expires": time.time() + _PERSONAL_OTP_TTL_S}
        return {"need_otp": True}
    personal_folders.save_owner_session(owner, result["session"])
    request.session.pop("personal_otp", None)
    return {"ok": True}


@app.get("/auth/login")
def auth_login(request: Request) -> RedirectResponse:
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(
            status_code=503,
            detail="Google sign-in isn't turned on. An admin can set it up in Settings.",
        )
    # Signed state (not session-only): Google always returns to PUBLIC_BASE_URL, but
    # LAN prefer may start login on the LAN origin — a different cookie jar.
    cfg = _public_config(request)
    public_host = (urlparse(settings.public_base_url).hostname or "").lower()
    here = (request.headers.get("host") or "").split(":")[0].lower()
    on_public = bool(public_host) and here == public_host
    return_lan = (bool(cfg.get("via_lan")) and not on_public) or request.query_params.get("return_lan") == "1"
    if cfg.get("via_lan") and not on_public:
        # Establish the OAuth browser cookie on the same public origin that
        # receives Google's callback, including sign-ins begun on the LAN.
        return RedirectResponse(settings.public_base_url.rstrip("/") + "/auth/login?" + urlencode({
            "return_lan": "1", "next": _safe_next_url(request.query_params.get("next")),
        }))
    browser_nonce = secrets.token_urlsafe(32)
    request.session["oauth_browser_nonce"] = browser_nonce
    state = _state_ser().dumps(
        {
            "n": secrets.token_urlsafe(16),
            "browser_nonce": browser_nonce,
            "lan": return_lan,
            "next": _safe_next_url(request.query_params.get("next")),
        }
    )
    redirect_uri = f"{settings.public_base_url.rstrip('/')}/auth/callback"
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "online",
        "include_granted_scopes": "true",
        "prompt": "select_account",
        "state": state,
    }
    return RedirectResponse(f"{GOOGLE_AUTH}?{urlencode(params)}")


_NAS_LOGIN_WINDOW_S = 300.0
_NAS_LOGIN_MAX = 12
_nas_login_hits: dict[str, list[float]] = {}


def _nas_login_allowed(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _nas_login_hits.get(ip, []) if now - t < _NAS_LOGIN_WINDOW_S]
    if len(hits) >= _NAS_LOGIN_MAX:
        _nas_login_hits[ip] = hits
        return False
    hits.append(now)
    _nas_login_hits[ip] = hits
    return True


# Per-username lockout on top of the per-IP limit (stops distributed guessing).
_NAS_USER_MAX_FAILS = 5
_NAS_USER_LOCK_S = 900.0
_nas_user_fails: dict[str, list[float]] = {}


def _nas_user_locked(username: str) -> bool:
    key = username.lower()
    now = time.time()
    fails = [t for t in _nas_user_fails.get(key, []) if now - t < _NAS_USER_LOCK_S]
    if fails:
        _nas_user_fails[key] = fails
    else:
        _nas_user_fails.pop(key, None)
    return len(fails) >= _NAS_USER_MAX_FAILS


def _nas_user_failed(username: str) -> None:
    _nas_user_fails.setdefault(username.lower(), []).append(time.time())


# Encryption-key guesses. Not the NAS password counter: the two secrets differ.
_personal_key_fails: dict[str, list[float]] = {}


def _personal_key_locked(username: str) -> bool:
    key = username.lower()
    now = time.time()
    fails = [t for t in _personal_key_fails.get(key, []) if now - t < _NAS_USER_LOCK_S]
    if fails:
        _personal_key_fails[key] = fails
    else:
        _personal_key_fails.pop(key, None)
    return len(fails) >= _NAS_USER_MAX_FAILS


def _personal_key_failed(username: str) -> None:
    _personal_key_fails.setdefault(username.lower(), []).append(time.time())


# Server-side so replaying an older session cookie can't reset the count.
_NAS_OTP_MAX_ATTEMPTS = 5
_nas_otp_attempts: dict[str, tuple[int, float]] = {}


def _nas_otp_take_attempt(key: str, expires: float) -> bool:
    now = time.time()
    for k in [k for k, (_n, exp) in _nas_otp_attempts.items() if exp < now]:
        del _nas_otp_attempts[k]
    used, _exp = _nas_otp_attempts.get(key, (0, expires))
    if used >= _NAS_OTP_MAX_ATTEMPTS:
        return False
    _nas_otp_attempts[key] = (used + 1, expires)
    return True


def _session_nas_user(request: Request, username: str) -> None:
    pw = _pwd_user(username)
    if pw is None or pw.pw_uid < 1000:
        raise HTTPException(status_code=403, detail="This NAS account can't sign in here")
    email = linux_user_email(username) or ""
    request.session.pop("nas_otp", None)
    request.session["user"] = {
        "email": email,
        "name": username,
        "picture": None,
        "username": pw.pw_name,
        "uid": pw.pw_uid,
        "gid": pw.pw_gid,
    }
    request.session["share"] = DEFAULT_SHARE


@app.post("/auth/nas")
async def auth_nas(request: Request) -> dict[str, Any]:
    ip = request.client.host if request.client else "unknown"
    if not _nas_login_allowed(ip):
        raise HTTPException(status_code=429, detail="Too many sign-in attempts. Try again in a few minutes.")
    body = await request.json()
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password are required")
    if _nas_user_locked(username):
        raise HTTPException(status_code=429, detail="Too many failed sign-ins for this account. Try again in 15 minutes.")
    # UGOS checks the password (and starts its 2FA challenge when the account has one).
    result = nas_password_login(settings.ugos_api_url, username, password)
    if not result.get("ok"):
        _nas_user_failed(username)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    _nas_user_fails.pop(username.lower(), None)
    if result.get("need_otp"):
        request.session["nas_otp"] = {
            "u": username,
            "tid": result.get("token_id") or "",
            "exp": time.time() + 300,
        }
        return {"ok": True, "need_otp": True}
    _session_nas_user(request, username)
    return {"ok": True, "need_otp": False}


@app.post("/auth/nas/otp")
async def auth_nas_otp(request: Request) -> dict[str, Any]:
    pending = request.session.get("nas_otp")
    if not isinstance(pending, dict) or time.time() > float(pending.get("exp") or 0):
        request.session.pop("nas_otp", None)
        raise HTTPException(status_code=401, detail="Sign-in expired. Enter your password again.")
    attempt_key = f"{pending.get('u') or ''}\0{pending.get('tid') or ''}"
    if not _nas_otp_take_attempt(attempt_key, float(pending.get("exp") or 0)):
        request.session.pop("nas_otp", None)
        raise HTTPException(status_code=401, detail="Too many invalid codes. Enter your password again.")
    body = await request.json()
    code = str(body.get("code") or "").strip()
    otp_type = int(body.get("type") or 1)
    ugos = (settings.ugos_api_url or "").strip()
    result = nas_otp_login(
        ugos,
        code=code,
        token_id=str(pending.get("tid") or ""),
        otp_type=otp_type,
    )
    if not result.get("ok") and otp_type == 1:
        result = nas_otp_login(
            ugos,
            code=code,
            token_id=str(pending.get("tid") or ""),
            otp_type=2,
        )
    if not result.get("ok"):
        raise HTTPException(status_code=401, detail=str(result.get("error") or "Invalid authentication code"))
    _session_nas_user(request, str(pending.get("u") or ""))
    return {"ok": True}


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str | None = None, state: str | None = None) -> RedirectResponse:
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code/state")
    # Prefer signed state (works across public ↔ LAN hosts). Fall back to legacy
    # session state for any mid-deploy OAuth round-trips still in flight.
    state_payload: dict[str, Any] = {"lan": False, "next": "/"}
    try:
        loaded = _state_ser().loads(state, max_age=600)
        if isinstance(loaded, dict):
            state_payload = loaded
        elif isinstance(loaded, str) and loaded:
            # Older signed-string shape if any
            state_payload = {"lan": False, "next": "/", "n": loaded}
    except (BadSignature, SignatureExpired):
        if state != request.session.get("oauth_state"):
            raise HTTPException(status_code=400, detail="Invalid OAuth state")
    request.session.pop("oauth_state", None)

    browser_verified = False
    if state_payload.get("browser_nonce"):
        expected = request.session.pop("oauth_browser_nonce", "")
        if not expected or not secrets.compare_digest(str(state_payload["browser_nonce"]), str(expected)):
            raise HTTPException(status_code=400, detail="Google sign-in belongs to another browser or was already used")
        browser_verified = True

    redirect_uri = f"{settings.public_base_url.rstrip('/')}/auth/callback"
    async with httpx.AsyncClient(timeout=20) as client:
        token_res = await client.post(
            GOOGLE_TOKEN,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if token_res.status_code != 200:
            raise HTTPException(status_code=400, detail="Token exchange failed")
        access_token = token_res.json().get("access_token")
        info_res = await client.get(
            GOOGLE_USERINFO,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if info_res.status_code != 200:
            raise HTTPException(status_code=400, detail="Userinfo failed")
        info = info_res.json()

    email = (info.get("email") or "").lower()
    if info.get("email_verified") is not True:
        raise HTTPException(status_code=403, detail="Email not verified")

    next_url = _safe_next_url(str(state_payload.get("next") or "/"))
    return_lan = bool(state_payload.get("lan")) and bool((settings.lan_origin or "").strip())

    nas = resolve_nas_user(email)
    if nas is None:
        request.session.clear()
        # Pass the attempted address back so the UI can name the missing NAS user.
        denied = f"/?{urlencode({'error': 'not_authorized', 'email': email})}"
        if return_lan:
            # Stay on public for the denied message (no session to hand off).
            return RedirectResponse(denied)
        return RedirectResponse(denied)

    user = {
        "email": email,
        "name": info.get("name") or email,
        "picture": info.get("picture"),
        "username": nas.username,
        "uid": nas.uid,
        "gid": nas.gid,
    }
    user["via"] = "google"
    if browser_verified and info.get("sub"):
        user.update(auth_provider="google", google_sub=str(info["sub"]))
    request.session["user"] = user
    request.session["share"] = DEFAULT_SHARE

    # Login often starts on the LAN; Google always lands on public_base_url.
    # Hand the new session to the LAN origin so the user stays on the fast path.
    if return_lan:
        handoff = {
            "email": user["email"],
            "name": user["name"],
            "picture": user["picture"],
            "username": user["username"],
            "uid": user["uid"],
            "gid": user["gid"],
            "via": "google",
            "share": DEFAULT_SHARE,
            "nonce": secrets.token_urlsafe(8),
        }
        token = _lan_handoff_ser().dumps(handoff)
        lan = settings.lan_origin.rstrip("/")
        return RedirectResponse(
            f"{lan}/auth/lan-handoff?{urlencode({'token': token, 'next': next_url})}",
            status_code=302,
        )
    return RedirectResponse(next_url)


@app.post("/auth/logout")
def auth_logout(request: Request) -> JSONResponse:
    request.session.clear()
    return JSONResponse({"ok": True})


@app.get("/api/files")
def api_list(request: Request, path: str = "") -> dict[str, Any]:
    user = _require_user(request)
    share = _active_share(request, user)
    try:
        with use_share_root(share_path(share)):
            data = list_dir(path, user["uid"], user["gid"])
            data["has_recycle"] = has_recycle_bin()
            data["in_recycle"] = is_under_recycle(path or "")
        data["share"] = share
        return data
    except FSError as e:
        raise _fs_http(e) from e


@app.post("/api/upload/fingerprint")
def api_upload_fingerprint(request: Request, path: str = Form(...), size: int = Form(..., ge=0)) -> dict[str, Any]:
    user = _require_user(request)
    share = _active_share(request, user)
    try:
        with use_share_root(share_path(share)):
            return {"fingerprint": upload_fingerprint(path, size, user["uid"], user["gid"])}
    except FSError as e:
        raise _fs_http(e) from e


@app.get("/api/folder-sizes")
def api_folder_sizes(
    request: Request,
    path: list[str] = Query(default=[]),
) -> dict[str, Any]:
    """
    Recursive content size for one or more folders (du-like).

    Used by the UI after listing so the file list stays fast; results are
    TTL-cached server-side. Incomplete walks set incomplete=true (UI shows ~).
    """
    user = _require_user(request)
    share = _active_share(request, user)
    if not path:
        return {"sizes": {}, "share": share}
    try:
        with use_share_root(share_path(share)):
            data = folder_sizes(path, user["uid"], user["gid"])
        data["share"] = share
        return data
    except FSError as e:
        raise _fs_http(e) from e


@app.get("/api/search")
def api_search(
    request: Request,
    q: str = Query("", min_length=0, max_length=200),
    path: str = "",
    limit: int = Query(40, ge=1, le=80),
) -> dict[str, Any]:
    """Recursive smart search under `path` (empty = whole active share)."""
    user = _require_user(request)
    share = _active_share(request, user)
    try:
        with use_share_root(share_path(share)):
            data = search(q, path, user["uid"], user["gid"], limit=limit)
        data["share"] = share
        return data
    except FSError as e:
        raise _fs_http(e) from e


@app.get("/api/usage")
def api_usage(request: Request) -> dict[str, Any]:
    """Capacity of the active mount, including UGOS encrypted-home limits."""
    user = _require_user(request)
    share = _active_share(request, user)
    try:
        with use_share_root(share_path(share)):
            owner = personal_owner(share)
            usage = disk_usage(personal=owner is not None)
            if owner and personal_folders.configured():
                usage["expires_at"] = personal_folders.folders.status(owner)["expires_at"]
            return usage
    except (FSError, OSError) as e:
        raise HTTPException(status_code=503, detail="Storage usage unavailable") from e


@app.post("/api/mkdir")
def api_mkdir(request: Request, path: str = Form(""), name: str = Form(...)) -> dict[str, Any]:
    user = _require_user(request)
    share = _active_share(request, user)
    try:
        with use_share_root(share_path(share)):
            return mkdir(path, name, user["uid"], user["gid"])
    except FSError as e:
        raise _fs_http(e) from e


@app.post("/api/rename")
def api_rename(request: Request, path: str = Form(...), new_name: str = Form(...)) -> dict[str, Any]:
    user = _require_user(request)
    share = _active_share(request, user)
    try:
        with use_share_root(share_path(share)):
            return rename(path, new_name, user["uid"], user["gid"])
    except FSError as e:
        raise _fs_http(e) from e


@app.post("/api/move")
def api_move(request: Request, path: str = Form(...), dest: str = Form(...)) -> dict[str, Any]:
    user = _require_user(request)
    share = _active_share(request, user)
    try:
        with use_share_root(share_path(share)):
            return move_item(path, dest, user["uid"], user["gid"])
    except FSError as e:
        raise _fs_http(e) from e


@app.post("/api/delete")
def api_delete(request: Request, path: str = Form(...)) -> dict[str, Any]:
    """Soft-delete into `#recycle` when present (Samba/UGOS style); permanent inside recycle."""
    user = _require_user(request)
    share = _active_share(request, user)
    try:
        with use_share_root(share_path(share)):
            return delete(path, user["uid"], user["gid"])
    except FSError as e:
        raise _fs_http(e) from e


@app.post("/api/recycle/empty")
def api_empty_recycle(request: Request) -> dict[str, Any]:
    """Permanently wipe `#recycle` on the active share. NAS admins only."""
    user = _require_user(request)
    username = str(user.get("username") or "")
    if not is_nas_admin(username):
        raise HTTPException(
            status_code=403,
            detail="Only NAS admins can empty the Recycle bin for everyone",
        )
    share = _active_share(request, user)
    try:
        with use_share_root(share_path(share)):
            return empty_recycle(user["uid"], user["gid"])
    except FSError as e:
        raise _fs_http(e) from e


@app.post("/api/upload")
async def api_upload(
    request: Request,
    path: str = Form(""),
    file: UploadFile = File(...),
    expect_mtime_ns: int | None = Form(None),
) -> dict[str, Any]:
    """Stream upload to disk (no full-file RAM buffer) so large files stay fast/stable."""
    import asyncio
    import queue

    from anyio import to_thread

    user = _require_user(request)
    share = _active_share(request, user)
    share_root = share_path(share)
    max_bytes = MAX_UPLOAD_BYTES
    # Prefer declared multipart size when present so a truncated stream never
    # becomes the final file. Unknown size still refuses commit on pump errors.
    expected: int | None = None
    try:
        if file.size is not None and int(file.size) >= 0:
            expected = int(file.size)
            if expected > max_bytes:
                raise HTTPException(status_code=413, detail=f"File too large (max {max_bytes // 1024 ** 3}GB)")
    except (TypeError, ValueError):
        expected = None
    # Larger pieces + deeper queue keep the tunnel fed while disk writes catch up.
    _READ = 2 * 1024 * 1024
    chunk_q: queue.Queue[bytes | None] = queue.Queue(maxsize=32)
    pump_error: list[BaseException] = []

    async def _put(item: bytes | None) -> None:
        while True:
            try:
                chunk_q.put_nowait(item)
                return
            except queue.Full:
                await asyncio.sleep(0.002)

    async def pump() -> None:
        total = 0
        try:
            while True:
                piece = await file.read(_READ)
                if not piece:
                    break
                total += len(piece)
                if total > max_bytes:
                    raise HTTPException(status_code=413, detail=f"File too large (max {max_bytes // 1024 ** 3}GB)")
                await _put(piece)
        except BaseException as exc:  # noqa: BLE001 — surface to writer
            pump_error.append(exc)
        finally:
            await _put(None)

    def sync_chunks() -> Any:
        while True:
            item = chunk_q.get()
            if item is None:
                # Fail inside the writer so the temp file is discarded, not committed.
                if pump_error:
                    raise pump_error[0]
                break
            yield item

    def write_on_share() -> Any:
        with use_share_root(share_root):
            return write_upload_stream(
                path,
                file.filename or "upload.bin",
                sync_chunks(),
                user["uid"],
                user["gid"],
                max_bytes,
                expected_bytes=expected,
                expect_mtime_ns=expect_mtime_ns,
            )

    pump_task = asyncio.create_task(pump())
    try:
        result = await to_thread.run_sync(write_on_share)
        await pump_task
        if pump_error:
            raise pump_error[0]
        return result
    except FSError as e:
        pump_task.cancel()
        raise _fs_http(e) from e
    except HTTPException:
        pump_task.cancel()
        raise
    except Exception:
        pump_task.cancel()
        raise


# ---------------------------------------------------------------------------
# Chunked / resumable multi-stream upload (remote tunnel throughput)
# ---------------------------------------------------------------------------
@app.post("/api/upload/init")
def api_upload_init(
    request: Request,
    path: str = Form(""),
    name: str = Form(...),
    size: int = Form(...),
    chunk_size: int = Form(CHUNK_SIZE_DEFAULT),
) -> dict[str, Any]:
    """Start a chunked upload session. Client then PUTs pieces in parallel."""
    user = _require_user(request)
    share = _active_share(request, user)
    try:
        # Folder uploads pass nested paths (webkitRelativePath) that may not
        # exist yet — mirror write_upload_stream and mkdir -p the parents.
        with use_share_root(share_path(share)):
            parent = resolve_rel(path or "")
            if not parent.is_dir():
                rel = (path or "").strip().lstrip("/")
                if not rel:
                    raise FSError("Parent not found", 404)
                ensure_dir(rel, int(user["uid"]), int(user["gid"]))
                parent = resolve_rel(rel)
                if not parent.is_dir():
                    raise FSError("Parent not found", 404)
            # Chunks go straight into the destination folder, so completing
            # is a rename rather than another copy of every byte.
            return create_session(
                username=str(user.get("username") or ""),
                uid=int(user["uid"]),
                gid=int(user["gid"]),
                share=share,
                rel_dir=path or "",
                filename=name,
                size=int(size),
                chunk_size=int(chunk_size),
                dest_dir=parent,
            )
    except FSError as e:
        raise _fs_http(e) from e


@app.get("/api/upload/status")
def api_upload_status(request: Request, upload_id: str = Query(...)) -> dict[str, Any]:
    user = _require_user(request)
    try:
        return chunk_upload_status(
            upload_id,
            username=str(user.get("username") or ""),
            uid=int(user["uid"]),
        )
    except FSError as e:
        raise _fs_http(e) from e


@app.put("/api/upload/chunk")
async def api_upload_chunk(
    request: Request,
    upload_id: str = Query(...),
    index: int = Query(..., ge=0),
) -> dict[str, Any]:
    """Accept one chunk body (raw octets). Safe to retry; overwrites same index."""
    import asyncio
    import queue

    from anyio import to_thread

    user = _require_user(request)
    username = str(user.get("username") or "")
    uid = int(user["uid"])

    # Stream request body into a small queue so we don't hold multi-32MB in RAM twice.
    body_q: queue.Queue[bytes | None] = queue.Queue(maxsize=8)
    pump_error: list[BaseException] = []

    async def _put(item: bytes | None) -> None:
        while True:
            try:
                body_q.put_nowait(item)
                return
            except queue.Full:
                await asyncio.sleep(0.002)

    async def pump() -> None:
        try:
            async for piece in request.stream():
                await _put(piece)
        except BaseException as exc:  # noqa: BLE001
            pump_error.append(exc)
        finally:
            await _put(None)

    def sync_iter() -> Any:
        while True:
            item = body_q.get()
            if item is None:
                if pump_error:
                    raise pump_error[0]
                break
            yield item

    def do_write() -> dict[str, Any]:
        return write_chunk(
            upload_id,
            index,
            sync_iter(),
            username=username,
            uid=uid,
        )

    pump_task = asyncio.create_task(pump())
    try:
        result = await to_thread.run_sync(do_write)
        await pump_task
        if pump_error:
            raise pump_error[0]
        return result
    except FSError as e:
        pump_task.cancel()
        raise _fs_http(e) from e
    except Exception:
        pump_task.cancel()
        raise


@app.post("/api/upload/complete")
def api_upload_complete(
    request: Request,
    upload_id: str = Form(...),
    expect_mtime_ns: int | None = Form(None),
) -> dict[str, Any]:
    user = _require_user(request)
    share = _active_share(request, user)
    try:
        # complete_session writes via write_upload_stream; pin share root.
        from chunk_upload import get_session

        meta = get_session(
            upload_id,
            username=str(user.get("username") or ""),
            uid=int(user["uid"]),
        )
        if meta.get("share") and meta["share"] != share:
            # Prefer the share recorded at init (client header should match).
            share = str(meta["share"])
        with use_share_root(share_path(share)):
            return complete_session(
                upload_id,
                username=str(user.get("username") or ""),
                uid=int(user["uid"]),
                expect_mtime_ns=expect_mtime_ns,
            )
    except FSError as e:
        raise _fs_http(e) from e


@app.post("/api/upload/abort")
def api_upload_abort(request: Request, upload_id: str = Form(...)) -> dict[str, Any]:
    user = _require_user(request)
    try:
        return abort_session(
            upload_id,
            username=str(user.get("username") or ""),
            uid=int(user["uid"]),
        )
    except FSError as e:
        raise _fs_http(e) from e


# ---------------------------------------------------------------------------
# Speed test — measures tunnel + NAS path the browser actually uses.
# Data is synthetic (zeros); nothing is written to the drive share.
# ---------------------------------------------------------------------------
_SPEEDTEST_CHUNK = 2 * 1024 * 1024  # 2 MiB chunks for higher throughput
_SPEEDTEST_MAX = 512 * 1024 * 1024  # 512 MiB hard cap
# Per-request upload piece used by multi-stream client (avoids single 512MB XHR death)
_SPEEDTEST_PIECE_MAX = 32 * 1024 * 1024


# Random, so proxies (Cloudflare) can't compress the stream and overstate the
# speed. The test "file" repeats this block, so byte ranges are consistent.
_SPEEDTEST_BLOCK = secrets.token_bytes(_SPEEDTEST_CHUNK)
_RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)$")


@app.get("/api/speedtest/download")
async def speedtest_download(
    request: Request,
    size: int = Query(32 * 1024 * 1024, ge=64 * 1024, le=_SPEEDTEST_MAX),
) -> StreamingResponse:
    _require_user(request)
    start, end = 0, size - 1
    status = 200
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "X-Accel-Buffering": "no",
        "ETag": '"speedtest"',
    }
    if match := _RANGE_RE.match(request.headers.get("range", "").strip()):
        start = int(match.group(1))
        end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
        if start >= size or start > end:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(end - start + 1)

    async def generate() -> AsyncIterator[bytes]:
        pos = start
        while pos <= end:
            offset = pos % _SPEEDTEST_CHUNK
            n = min(_SPEEDTEST_CHUNK - offset, end - pos + 1)
            yield _SPEEDTEST_BLOCK[offset:offset + n]
            pos += n

    return StreamingResponse(generate(), status_code=status, media_type="application/octet-stream", headers=headers)


@app.post("/api/speedtest/upload")
async def speedtest_upload(request: Request) -> dict[str, int]:
    """Accept one piece of a multi-stream upload test (max 32 MiB per request)."""
    _require_user(request)
    total = 0
    async for part in request.stream():
        total += len(part)
        if total > _SPEEDTEST_PIECE_MAX:
            raise HTTPException(status_code=413, detail="Speed-test piece too large (max 32MB per stream)")
    return {"received": total}


_TEXTISH_EXTS = (".md", ".markdown", ".log", ".csv", ".tsv", ".json", ".xml", ".yml", ".yaml")
# Only these render inline on the app origin; anything else (html, svg, xml, js…)
# could run script as the signed-in user, so it is always a download.
_INLINE_SAFE_PREFIXES = ("image/", "video/", "audio/")
_INLINE_SAFE_TYPES = frozenset({"application/pdf", "text/plain"})
_INLINE_UNSAFE_TYPES = frozenset({"image/svg+xml"})


def _inline_safe(media_type: str) -> bool:
    base = media_type.split(";", 1)[0].strip().lower()
    if base in _INLINE_UNSAFE_TYPES:
        return False
    return base in _INLINE_SAFE_TYPES or base.startswith(_INLINE_SAFE_PREFIXES)


class _OpenedFileResponse(FileResponse):
    """FileResponse that reads an already-authorized fd (/dev/fd/N) and closes it."""

    def __init__(self, opened: AuthorizedFile, **kwargs: Any) -> None:
        self._opened = opened
        opened.rewind()
        filename = kwargs.pop("filename", opened.name)
        super().__init__(path=opened.dev_path(), stat_result=opened.stat, filename=filename, **kwargs)

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._opened.close()


def _thumbnail_for(opened: AuthorizedFile, size: int) -> Path | None:
    """Build a thumbnail from an authorized fd, then close it."""
    try:
        return thumbs.get_or_create(
            opened.path, size, read_path=Path(opened.dev_path()), stat_result=opened.stat
        )
    finally:
        opened.close()


def _user_file_response(opened: AuthorizedFile, inline: bool) -> FileResponse:
    """Serve a user-uploaded file without letting it execute on our origin."""
    name = opened.name
    media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    # Text-ish types without a registered MIME still preview better as plain text.
    if media_type in ("application/octet-stream", "text/markdown") and name.lower().endswith(_TEXTISH_EXTS):
        media_type = "text/plain; charset=utf-8"
    inline = inline and _inline_safe(media_type)
    headers = {
        "X-Accel-Buffering": "no",
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    # Browsers won't run their built-in PDF viewer in a sandboxed document.
    # The share page and the viewer fallback embed that PDF in a same-origin iframe.
    if inline and media_type == "application/pdf":
        headers["Content-Security-Policy"] = "frame-ancestors 'self'"
        headers["X-Frame-Options"] = "SAMEORIGIN"
    else:
        headers["Content-Security-Policy"] = "sandbox"
    return _OpenedFileResponse(
        opened,
        filename=name,
        media_type=media_type,
        content_disposition_type="inline" if inline else "attachment",
        headers=headers,
    )


@app.get("/api/download")
def api_download(
    request: Request,
    path: str,
    inline: bool = Query(False, description="Serve for in-browser preview when true"),
) -> FileResponse:
    user = _require_user(request)
    share = _active_share(request, user)
    try:
        with use_share_root(share_path(share)):
            opened = open_for_download(path, user["uid"], user["gid"])
    except FSError as e:
        raise _fs_http(e) from e
    return _user_file_response(opened, inline)


@app.get("/api/thumbnail")
def api_thumbnail(
    request: Request,
    path: str,
    size: int = Query(256, ge=16, le=1024),
    mt: str = Query("", description="File mtime for browser cache busting; unused server-side"),
) -> FileResponse:
    """Cached JPEG thumbnail for an image/video. 404 = no thumbnail available."""
    user = _require_user(request)
    share = _active_share(request, user)
    try:
        with use_share_root(share_path(share)):
            opened = open_for_download(path, user["uid"], user["gid"])
    except FSError as e:
        raise _fs_http(e) from e
    # Pixels come from the fd opened as this user. Sync endpoint → threadpool,
    # so Pillow/ffmpeg don't block the event loop.
    thumb_path = _thumbnail_for(opened, size)
    if thumb_path is None:
        raise HTTPException(status_code=404, detail="No thumbnail")
    return FileResponse(path=thumb_path, media_type="image/jpeg")


_THUMB_WARM_MAX = 300


@app.post("/api/thumbnail/warm")
def api_thumbnail_warm(
    request: Request,
    path: list[str] = Form(default=[]),
    size: int = Form(256),
) -> dict[str, Any]:
    """Pre-generate thumbnails for a folder listing so scrolling hits warm cache.
    Generation opens each file as this user; a path they cannot read is skipped."""
    user = _require_user(request)
    share = _active_share(request, user)
    targets: list[Path] = []
    try:
        with use_share_root(share_path(share)) as root:
            for rel in path[:_THUMB_WARM_MAX]:
                if thumbs.thumb_kind(rel) is None:
                    continue
                try:
                    targets.append(resolve_rel(rel))
                except FSError:
                    continue
    except FSError as e:
        raise _fs_http(e) from e

    def open_source(src: Path) -> AuthorizedFile:
        return open_authorized_path(src, root, int(user["uid"]), int(user["gid"]))

    queued = thumbs.warm_paths(targets, size, open_source=open_source)
    return {"ok": True, "queued": queued}


_ZIP_MAX_FILES = 10_000  # files after folder expansion
_ZIP_MAX_SELECTION = 500  # top-level paths in the query
_ZIP_MAX_TOTAL = 8 * 1024 * 1024 * 1024  # 8 GiB total payload cap


@app.get("/api/download/zip")
def api_download_zip(
    request: Request,
    path: list[str] = Query(default=[]),
) -> StreamingResponse:
    """Stream a STORE zip of files and/or folders. Query: ?path=a&path=b.

    Folders are expanded recursively (symlinks / #recycle / temp junk skipped).
    STORE method only — no recompress; streams under X-Accel-Buffering: no.
    """
    user = _require_user(request)
    share = _active_share(request, user)
    paths = [p for p in path if (p or "").strip()]
    if not paths:
        raise HTTPException(status_code=400, detail="No paths given")
    if len(paths) > _ZIP_MAX_SELECTION:
        raise HTTPException(
            status_code=400,
            detail=f"Too many items (max {_ZIP_MAX_SELECTION})",
        )

    try:
        with use_share_root(share_path(share)) as root:
            entries = collect_zip_entries(
                paths,
                user["uid"],
                user["gid"],
                max_files=_ZIP_MAX_FILES,
                max_total=_ZIP_MAX_TOTAL,
            )
    except FSError as e:
        raise _fs_http(e) from e

    # Name the archive after a single top-level selection when possible.
    if len(paths) == 1:
        base = Path(paths[0].rstrip("/")).name or "download"
        zip_name = f"{base}.zip"
    elif len(entries) == 1:
        zip_name = f"{Path(entries[0][0]).name}.zip"
    else:
        zip_name = f"{re.sub(r'[^A-Za-z0-9]+', '-', settings.drive_name).strip('-') or 'Drive'}-{len(paths)}-items.zip"
    return _zip_response(entries, zip_name, root=root, uid=int(user["uid"]), gid=int(user["gid"]))


def _zip_response(
    entries: list[tuple[str, Path]], zip_name: str, *, root: Path, uid: int, gid: int
) -> StreamingResponse:
    # Content-Disposition with RFC 5987 filename*
    cd = f"attachment; filename=\"{zip_name}\"; filename*=UTF-8''{quote(zip_name)}"

    def generate() -> Any:
        # Each member is opened as the NAS user. The body is read from that fd
        # after the credential lock is released, so a swapped symlink is not
        # served as root and the lock is not held across the download.
        yield from stream_zip_store(
            entries, open_file=lambda path: open_zip_member(path, root, uid, gid)
        )

    headers = {
        "Content-Disposition": cd,
        "Cache-Control": "private, no-store",
        "X-Accel-Buffering": "no",
    }
    try:
        headers["Content-Length"] = str(
            zip_store_content_length(entries, size_of=lambda path: zip_member_size(path, root, uid, gid))
        )
    except (OSError, FSError):
        pass

    return StreamingResponse(
        generate(),
        media_type="application/zip",
        headers=headers,
    )


@app.api_route("/", methods=["GET", "HEAD"])
def index() -> HTMLResponse:
    return _index_page()


_DRIVE_NAME_RE = re.compile(r"(data-drive-name>)Drive(<)")


def _with_drive_name(page: str) -> str:
    """Fill this install's name into a static page (title and wordmarks)."""
    name = html_escape(settings.drive_name)
    page = page.replace("<title>Drive</title>", f"<title>{name}</title>", 1)
    return _DRIVE_NAME_RE.sub(lambda m: f"{m.group(1)}{name}{m.group(2)}", page)


def _index_page() -> HTMLResponse:
    return HTMLResponse(_with_drive_name((STATIC / "index.html").read_text(encoding="utf-8")))


def _file_link_error(err: LinkError) -> JSONResponse:
    body: dict[str, Any] = {"error": err.code}
    if err.code == "expired" and err.payload:
        try:
            body["expires_at"] = iso_utc(int(err.payload["exp"]))
        except (KeyError, TypeError, ValueError):
            pass
        name = err.payload.get("name")
        if name:
            body["name"] = name
    return JSONResponse(status_code=404, content=body)


def _link_target(token: str, sub: str = "") -> tuple[dict[str, Any], Path, str, str]:
    """Verify a link → (payload, share root, target path under that root, sub path).

    A file link takes no sub path; a folder link's sub path never leaves the folder.
    """
    payload = verify_file_link(token)
    folder = is_folder_link(payload)
    if (sub or "").strip("/ ") and not folder:
        raise LinkError("unavailable", payload)
    # Personal folders stay behind sign-in. A link minted earlier must die too.
    if personal_owner(str(payload.get("share") or "")):
        raise LinkError("unavailable", payload)
    try:
        with use_share_root(share_path(str(payload["share"]))) as root:
            if folder:
                target, sub = resolve_within(str(payload["path"]), sub)
            else:
                target, sub = resolve_rel(str(payload["path"])), ""
            rel = target.relative_to(root).as_posix()
    except (FSError, TypeError, ValueError) as e:
        raise LinkError("unavailable", payload) from e
    return payload, root, rel, sub


def _link_file(payload: dict[str, Any], root: Path, rel: str) -> AuthorizedFile:
    try:
        with use_share_root(root):
            return open_for_download(rel, int(payload["uid"]), int(payload["gid"]))
    except (FSError, TypeError, ValueError) as e:
        raise LinkError("unavailable", payload) from e


def _link_listing(payload: dict[str, Any], root: Path, rel: str, sub: str) -> dict[str, Any] | None:
    """Public listing of a folder link's directory; None when `rel` is a file."""
    uid, gid = int(payload["uid"]), int(payload["gid"])
    try:
        with use_share_root(root):
            with as_user(uid, gid):
                if not resolve_rel(rel).is_dir():
                    return None
            listing = list_dir(rel, uid, gid)
    except FSError as e:
        raise LinkError("unavailable", payload) from e
    items = [
        {
            "name": item["name"],
            "path": f"{sub}/{item['name']}" if sub else item["name"],
            "is_dir": item["is_dir"],
            "size": item["size"],
            "mtime": item["mtime"],
        }
        for item in listing["items"]
        if not item["is_link"] and item["name"] != RECYCLE_NAME
    ]
    return {
        "is_dir": True,
        "name": Path(rel).name,
        "folder": payload.get("name"),
        "path": sub,
        "items": items,
        "expires_at": iso_utc(payload["exp"]),
        "error": None,
    }


@app.post("/api/file-link")
async def api_mint_file_link(request: Request) -> dict[str, Any]:
    """Mint a public, expiring link for a file or folder the user can read."""
    user = _require_user(request)
    share = _active_share(request, user)
    try:
        body = await request.json()
    except Exception:
        body = {}
    rel = str((body or {}).get("path") or "").strip()
    if not rel:
        raise HTTPException(status_code=400, detail="path required")
    raw_days = (body or {}).get("days", DEFAULT_DAYS)
    try:
        days = int(raw_days)
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail="days must be 1–30") from e
    if days < MIN_DAYS or days > MAX_DAYS:
        raise HTTPException(status_code=400, detail="days must be 1–30")
    uid, gid = int(user["uid"]), int(user["gid"])
    if personal_owner(share):
        raise HTTPException(status_code=403, detail="Personal folders can't be shared with a public link")
    try:
        with use_share_root(share_path(share)) as root:
            target = resolve_rel(rel)
            with as_user(uid, gid):
                is_dir = target.is_dir()
            if is_dir:
                canonical = target.relative_to(root)
                if not canonical.parts or canonical.parts[0] == RECYCLE_NAME:
                    raise FSError("This folder can't be shared", 400)
                list_dir(rel, uid, gid)  # the sharer must be able to open it
                # Store the resolved path so a symlink swapped in later can't redirect the link.
                rel, name = canonical.as_posix(), target.name
            else:
                opened = open_for_download(rel, uid, gid)
                try:
                    name = opened.name
                finally:
                    opened.close()
    except FSError as e:
        raise _fs_http(e) from e
    token = mint_file_link(
        share=share,
        path=rel,
        days=days,
        uid=uid,
        gid=gid,
        name=name,
        is_dir=is_dir,
    )
    payload = verify_file_link(token)
    return {"url": file_link_public_url(token), "expires_at": iso_utc(payload["exp"])}


@app.get("/api/s/{token}", response_model=None)
def api_file_link_meta(token: str, path: str = Query("", description="Item inside a folder link")):
    try:
        payload, root, rel, sub = _link_target(token, path)
        if is_folder_link(payload):
            listing = _link_listing(payload, root, rel, sub)
            if listing is not None:
                return listing
        opened = _link_file(payload, root, rel)
    except LinkError as e:
        return _file_link_error(e)
    try:
        stat = opened.stat
        name = opened.name
    finally:
        opened.close()
    kind = preview_kind(name)
    meta = {
        "is_dir": False,
        "name": name,
        "size": stat.st_size,
        "mtime": iso_utc(stat.st_mtime),
        "kind": kind,
        "previewable": kind is not None,
        "expires_at": iso_utc(payload["exp"]),
        "error": None,
    }
    if is_folder_link(payload):
        meta.update(folder=payload.get("name"), path=sub)
    return meta


@app.get("/api/s/{token}/file", response_model=None)
def api_file_link_file(
    token: str,
    path: str = Query("", description="File inside a folder link"),
    inline: bool = Query(False, description="Serve for in-browser preview when true"),
):
    try:
        payload, root, rel, _sub = _link_target(token, path)
        opened = _link_file(payload, root, rel)
    except LinkError as e:
        return _file_link_error(e)
    return _user_file_response(opened, inline)


@app.get("/api/s/{token}/zip", response_model=None)
def api_folder_link_zip(token: str, path: str = Query("", description="Subfolder inside a folder link")):
    try:
        payload, root, rel, _sub = _link_target(token, path)
        if not is_folder_link(payload):
            raise LinkError("unavailable", payload)
    except LinkError as e:
        return _file_link_error(e)
    try:
        with use_share_root(root):
            entries = collect_zip_entries(
                [rel],
                int(payload["uid"]),
                int(payload["gid"]),
                max_files=_ZIP_MAX_FILES,
                max_total=_ZIP_MAX_TOTAL,
            )
    except FSError as e:
        raise _fs_http(e) from e
    return _zip_response(
        entries, f"{Path(rel).name}.zip", root=root, uid=int(payload["uid"]), gid=int(payload["gid"])
    )


@app.get("/api/s/{token}/thumb", response_model=None)
def api_file_link_thumb(token: str):
    """JPEG preview image for a file link (og:image in iMessage, Slack, …)."""
    try:
        payload, root, rel, _sub = _link_target(token)
        opened = _link_file(payload, root, rel)
    except LinkError as e:
        return _file_link_error(e)
    thumb_path = _thumbnail_for(opened, 512)
    if thumb_path is None:
        raise HTTPException(status_code=404, detail="No thumbnail")
    return FileResponse(path=thumb_path, media_type="image/jpeg")


def _share_preview(token: str) -> tuple[str, str, str | None]:
    """(title, description, thumbnail path or None) for a share link's preview."""
    try:
        payload, root, rel, _sub = _link_target(token)
        if is_folder_link(payload):
            listing = _link_listing(payload, root, rel, "")
            if listing is not None:
                count = link_preview.item_count(len(listing["items"]))
                return listing["name"], f"Folder · {count} · Shared from {settings.drive_name}", None
        opened = _link_file(payload, root, rel)
        try:
            name = opened.name
            size = link_preview.human_size(opened.stat.st_size)
        finally:
            opened.close()
    except LinkError as e:
        title = "This link has expired" if e.code == "expired" else "Link not available"
        return title, settings.drive_name, None
    label = link_preview.kind_label(preview_kind(name))
    thumb = f"/api/s/{token}/thumb" if thumbs.thumb_kind(name) else None
    return name, f"{label} · {size} · Shared from {settings.drive_name}", thumb


@app.get("/s/{token}")
def share_page(token: str) -> HTMLResponse:
    base = (get_settings().public_base_url or "").rstrip("/")
    title, description, thumb = _share_preview(token)
    page = link_preview.inject(
        _with_drive_name((STATIC / "share.html").read_text(encoding="utf-8")),
        title=title,
        description=description,
        url=f"{base}/s/{token}",
        image=base + (thumb or link_preview.ICON),
        site=settings.drive_name,
    )
    return HTMLResponse(page)


@app.get("/open")
def open_link(
    share: str = Query("", description="Drive the item is on"),
    path: str = Query("", description="Folder to open"),
    file: str = Query("", description="File to open"),
) -> HTMLResponse:
    """Copy-link target: the Drive app, with a preview built only from the URL.

    Recipients sign in to see the item, so the preview never touches the disk —
    it can't confirm a path exists or leak anything the URL doesn't already say.
    """
    base = (get_settings().public_base_url or "").rstrip("/")
    query = urlencode({key: value for key, value in (("share", share), ("path", path), ("file", file)) if value})
    target = file or path
    title = link_preview.base_name(target) or settings.drive_name
    if file:
        what = link_preview.kind_label(preview_kind(title))
    else:
        what = "Folder" if target.strip("/") else "Drive"
    parent = link_preview.parent_name(target)
    description = f"{what} in {parent} · {settings.drive_name}" if parent else f"{what} · {settings.drive_name}"
    page = link_preview.inject(
        _with_drive_name((STATIC / "index.html").read_text(encoding="utf-8")),
        title=title,
        description=description,
        url=f"{base}/open?{query}",
        image=base + link_preview.ICON,
        site=settings.drive_name,
    )
    return HTMLResponse(page)


@app.get("/{full_path:path}")
def spa_fallback(full_path: str) -> Response:
    # Don't swallow API/auth/public share pages
    if full_path.startswith(("api/", "auth/", "assets/", "s/")):
        raise HTTPException(status_code=404)
    return _index_page()
