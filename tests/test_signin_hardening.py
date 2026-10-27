"""Sign-in checks that fail closed: UGOS 2FA replies, linked-email sessions, redirects, admins."""
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import main  # noqa: E402
import shares  # noqa: E402
import ugos_api  # noqa: E402


def _ugos_replies(monkeypatch, body):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    original = httpx.Client
    monkeypatch.setattr(ugos_api.httpx, "Client", lambda **kw: original(transport=transport, **kw))


@pytest.mark.parametrize("body", [{}, {"msg": "wrong code"}, {"code": 200, "data": {}}, {"code": 1001}])
def test_otp_without_an_explicit_success_and_token_is_rejected(monkeypatch, body):
    _ugos_replies(monkeypatch, body)
    assert ugos_api.nas_otp_login("http://ugos.test", code="123456", token_id="t")["ok"] is False


def test_otp_success_needs_a_token(monkeypatch):
    _ugos_replies(monkeypatch, {"code": 200, "data": {"token": "abc"}})
    assert ugos_api.nas_otp_login("http://ugos.test", code="123456", token_id="t") == {"ok": True}


def test_password_login_without_code_200_is_rejected(monkeypatch):
    _ugos_replies(monkeypatch, {"data": {"token": "abc"}})
    # The RSA step fails first on this fake body; either way it must not succeed.
    assert ugos_api.nas_password_login("http://ugos.test", "admin", "pw")["ok"] is False


@pytest.mark.parametrize("raw,expected", [
    ("/folder?x=1", "/folder?x=1"),
    ("#/?share=Photos", "/#/?share=Photos"),
    ("//evil.example", "/"),
    ("/\\evil.example", "/"),
    ("/\t/evil.example", "/"),
    ("https://evil.example", "/"),
    ("", "/"),
])
def test_next_url_stays_on_this_site(raw, expected):
    assert main._safe_next_url(raw) == expected


@pytest.fixture
def linked(monkeypatch):
    links = {"me@example.com": "admin"}
    monkeypatch.setattr(main, "resolve_nas_user", lambda email: SimpleNamespace(username=links[email], uid=1001)
                        if email in links else None)
    return links


def _request_with(user):
    return SimpleNamespace(session={"user": user})


def test_unlinking_an_email_ends_its_session(linked):
    user = {"email": "me@example.com", "username": "admin", "uid": 1001, "gid": 100, "via": "email"}
    req = _request_with(dict(user))
    assert main._require_session_user(req)["username"] == "admin"
    linked.clear()
    req = _request_with(dict(user))
    with pytest.raises(main.HTTPException) as caught:
        main._require_session_user(req)
    assert caught.value.status_code == 401
    assert req.session == {}


def test_relinking_to_another_user_ends_the_old_session(linked):
    user = {"email": "me@example.com", "username": "admin", "uid": 1001, "gid": 100, "via": "google"}
    linked["me@example.com"] = "family"
    with pytest.raises(main.HTTPException):
        main._require_session_user(_request_with(user))


def test_password_sessions_are_not_rechecked(linked):
    linked.clear()
    user = {"email": "", "username": "admin", "uid": 1001, "gid": 100}
    assert main._require_session_user(_request_with(user))["username"] == "admin"


def test_admin_comes_from_the_host_group_file(monkeypatch):
    monkeypatch.setattr(shares, "_host_group_rows", lambda: [("admin", 10, ["boss"]), ("users", 100, ["kid"])])
    monkeypatch.setattr(shares, "_pwd_user", lambda name: SimpleNamespace(pw_gid=100))
    assert shares.is_nas_admin("boss")
    assert not shares.is_nas_admin("kid")
    monkeypatch.setattr(shares, "_host_group_rows", lambda: [("users", 100, ["boss"])])
    assert not shares.is_nas_admin("boss")  # removed from admin in UGOS: no stale container copy


def test_google_sign_in_on_the_lan_address_does_not_loop(monkeypatch):
    monkeypatch.setattr(main.settings, "google_client_id", "id")
    monkeypatch.setattr(main.settings, "google_client_secret", "secret")
    monkeypatch.setattr(main.settings, "public_base_url", "http://192.168.1.50:8790")
    monkeypatch.setattr(main.settings, "lan_origin", "http://192.168.1.50:8790")
    client = TestClient(main.app, base_url="http://192.168.1.50:8790")
    res = client.get("/auth/login", follow_redirects=False)
    assert res.status_code in (302, 307)
    assert res.headers["location"].startswith("https://accounts.google.com/")
