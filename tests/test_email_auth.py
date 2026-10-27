"""Email sign-in: real SQLite state, mocked mail and NAS identity."""
import sys
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from config import get_settings
import email_auth

EMAIL = "student@example.org"
ORIGIN = "https://drive.example.com"
may_request_code = email_auth.may_request_code
send_code = email_auth.send_code

@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_SECRET", "test-only-secret")
    monkeypatch.setenv("EMAIL_SIGN_IN_DB_PATH", str(tmp_path / "email.sqlite3"))
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "Drive <signin@example.test>")
    get_settings.cache_clear()
    monkeypatch.setattr(email_auth, "may_request_code", lambda email: email == EMAIL)
    monkeypatch.setattr(email_auth, "resolve_nas_user", lambda email: SimpleNamespace(username="student", uid=1001, gid=100))
    sent = []
    monkeypatch.setattr(email_auth, "send_code", lambda email, code: sent.append((email, code)))
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-only-secret")
    app.include_router(email_auth.router)
    @app.get("/session")
    def session(request: Request):
        return dict(request.session)
    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as c:
        c.sent = sent
        yield c
    get_settings.cache_clear()


def request_code(client):
    return client.post("/auth/email/request", json={"email": " Student@example.org "})


def verify(client, code):
    return client.post("/auth/email/verify", json={"email": EMAIL, "code": code})


def test_sign_in_existing_account_once(client):
    response = request_code(client)
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    code = client.sent[0][1]
    assert len(code) == 6 and code.isdigit()
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]
    assert verify(client, code).status_code == 200
    assert client.get("/session").json()["user"]["username"] == "student"
    assert verify(client, code).status_code == 400


def test_expiry_and_browser_binding(client, monkeypatch):
    request_code(client)
    code = client.sent[0][1]
    challenge = client.cookies.get(email_auth.COOKIE)
    client.cookies.clear()
    assert verify(client, code).status_code == 400
    client.cookies.set(email_auth.COOKIE, "wrong-browser")
    assert verify(client, code).status_code == 400
    client.cookies.set(email_auth.COOKIE, challenge)
    now = email_auth.time.time()
    monkeypatch.setattr(email_auth.time, "time", lambda: now + 601)
    assert verify(client, code).status_code == 400


def test_five_wrong_guesses_lock_code(client):
    request_code(client)
    code = client.sent[0][1]
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        assert verify(client, wrong).status_code == 400
    assert verify(client, code).status_code == 400


def test_resend_limits_and_invalidates_old_code(client, monkeypatch):
    request_code(client)
    old_code = client.sent[0][1]
    old_browser = client.cookies.get(email_auth.COOKIE)
    assert request_code(client).status_code == 429
    now = email_auth.time.time()
    for i in range(1, 5):
        monkeypatch.setattr(email_auth.time, "time", lambda i=i: now + 61 * i)
        assert request_code(client).status_code == 200
    monkeypatch.setattr(email_auth.time, "time", lambda: now + 305)
    assert request_code(client).status_code == 429
    assert not email_auth.consume_code(EMAIL, old_code, old_browser)
    monkeypatch.setattr(email_auth.time, "time", lambda: now + 3601)
    assert request_code(client).status_code == 200


def test_unknown_email_does_not_send(client):
    response = client.post("/auth/email/request", json={"email": "unknown@example.test"})
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert not client.sent


def test_access_rechecked_after_code(client, monkeypatch):
    request_code(client)
    monkeypatch.setattr(email_auth, "resolve_nas_user", lambda email: None)
    assert verify(client, client.sent[0][1]).status_code == 403
    assert "user" not in client.get("/session").json()


def test_cross_origin_and_bad_input_rejected(client):
    for endpoint in ["request", "verify"]:
        response = client.post(f"/auth/email/{endpoint}", json={"email": EMAIL, "code": "123456"}, headers={"Origin": "https://evil.test"})
        assert response.status_code == 403
    assert client.post("/auth/email/request", json={"email": "invalid"}).status_code == 422
    assert not client.sent


def test_missing_mail_configuration_fails_closed(client, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "")
    get_settings.cache_clear()
    assert request_code(client).status_code == 503
    assert not client.sent


def test_concurrent_requests_and_verification(client):
    with ThreadPoolExecutor(max_workers=8) as pool:
        requests = list(pool.map(lambda _: email_auth.issue_code(EMAIL, "browser"), range(8)))
    assert sum(code is not None for code in requests) == 1
    code = next(code for code in requests if code is not None)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: email_auth.consume_code(EMAIL, code, "browser"), range(8)))
    assert sum(results) == 1


def test_database_stores_only_hashes(client):
    request_code(client)
    with sqlite3.connect(get_settings().email_sign_in_db_path) as conn:
        code_hash, browser_hash = conn.execute("SELECT code_hash, browser_hash FROM email_codes").fetchone()
    assert len(code_hash) == len(browser_hash) == 64
    assert code_hash != client.sent[0][1]
    assert browser_hash != client.cookies.get(email_auth.COOKIE)


def test_request_policy_only_allows_linked_emails(client, monkeypatch):
    import runtime_settings
    import users
    monkeypatch.setattr(email_auth, "resolve_nas_user", users.resolve_nas_user)
    monkeypatch.setattr(runtime_settings, "identities", lambda s=None: {EMAIL: "student"})
    monkeypatch.setattr("users._pwd_user", lambda name: SimpleNamespace(
        pw_name=name, pw_uid=1001, pw_gid=100, pw_dir=f"/home/{name}") if name == "student" else None)
    assert may_request_code(EMAIL)
    # Same local part, different domain: no fallback to the NAS user named "student".
    assert not may_request_code("student@outside.test")
    assert not may_request_code("unlinked@example.org")


def test_concurrent_wrong_guesses_stop_at_five(client):
    code = email_auth.issue_code(EMAIL, "browser")
    wrong = "000000" if code != "000000" else "111111"
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert not any(pool.map(lambda _: email_auth.consume_code(EMAIL, wrong, "browser"), range(8)))
    with sqlite3.connect(get_settings().email_sign_in_db_path) as conn:
        assert conn.execute("SELECT attempts FROM email_codes").fetchone()[0] == 5
    assert not email_auth.consume_code(EMAIL, code, "browser")


def test_mail_failure_hides_provider_details(client, monkeypatch):
    import httpx
    from fastapi import HTTPException
    original_client = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(500, text="private provider detail"))
    monkeypatch.setattr(email_auth.httpx, "Client", lambda **kwargs: original_client(transport=transport, **kwargs))
    with pytest.raises(HTTPException) as caught:
        send_code(EMAIL, "123456")
    assert caught.value.status_code == 503
    assert "private provider detail" not in caught.value.detail
