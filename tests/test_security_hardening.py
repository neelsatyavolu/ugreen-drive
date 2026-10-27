"""Security hardening: session secret, inline file serving, NAS sign-in throttles."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import config  # noqa: E402
import main  # noqa: E402

STRONG_SECRET = "s3cure-test-secret-0123456789abcdef-0123456789abcdef"


# --- 1. SESSION_SECRET ------------------------------------------------------

@pytest.mark.parametrize(
    "weak",
    [
        "",
        "   ",
        "change-me-in-production",
        "change-me",
        "Change-Me-please-this-is-long-enough-to-pass-length",
        "generate-a-long-random-string",
        "x" * 31,
    ],
)
def test_weak_session_secret_rejected(weak):
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        config.require_strong_session_secret(weak)


def test_strong_session_secret_accepted():
    config.require_strong_session_secret(STRONG_SECRET)
    config.require_strong_session_secret("a" * 32)


def test_app_refuses_to_start_with_default_secret(monkeypatch):
    monkeypatch.setattr(main.settings, "session_secret", "change-me-in-production")
    started = []
    monkeypatch.setattr(main.personal_folders, "start_worker", lambda: started.append(1))
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        with TestClient(main.app):
            pass
    assert started == []


def _fake_request(user):
    return SimpleNamespace(
        session={"user": user},
        headers={},
        url=SimpleNamespace(netloc="drive.test"),
        query_params={},
    )


@pytest.mark.parametrize("uid", [0, 1, 999])
def test_session_with_system_uid_is_rejected(uid):
    with pytest.raises(HTTPException) as exc:
        main._require_user(_fake_request({"username": "root", "uid": uid, "gid": 0}))
    assert exc.value.status_code == 401


def test_session_with_regular_uid_is_accepted():
    user = {"username": "alice", "uid": 1001, "gid": 100}
    assert main._require_user(_fake_request(user)) == user


def test_me_treats_system_uid_session_as_signed_out():
    body = main.me(_fake_request({"username": "root", "uid": 0, "gid": 0}))
    assert body["authenticated"] is False


# --- 2. Inline file serving (stored XSS) --------------------------------------

@pytest.fixture()
def files_root(tmp_path):
    root = tmp_path / "Drive"
    root.mkdir()
    for name, data in {
        "evil.html": b"<script>alert(1)</script>",
        "evil.svg": b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
        "evil.xml": b"<x/>",
        "evil.js": b"alert(1)",
        "photo.png": b"\x89PNG\r\n\x1a\n",
        "clip.mp4": b"mp4",
        "song.mp3": b"mp3",
        "doc.pdf": b"%PDF-1.4",
        "notes.md": b"# hi",
        "notes.txt": b"hi",
    }.items():
        (root / name).write_bytes(data)
    return root


@pytest.fixture()
def files_client(files_root, monkeypatch):
    uid, gid = os.getuid(), os.getgid()
    monkeypatch.setattr(main, "_require_user", lambda request: {"uid": uid, "gid": gid, "username": "tester"})
    monkeypatch.setattr(main, "_active_share", lambda request, user: "Drive")
    monkeypatch.setattr(main, "share_path", lambda share: files_root)
    return TestClient(main.app)


def _link(name):
    return main.mint_file_link(share="Drive", path=name, days=1, uid=os.getuid(), gid=os.getgid(), name=name)


def _get_both(client, name):
    token = _link(name)
    return [
        client.get("/api/download", params={"path": name, "inline": "1"}),
        client.get(f"/api/s/{token}/file", params={"inline": "1"}),
    ]


@pytest.mark.parametrize("name", ["evil.html", "evil.svg", "evil.xml", "evil.js"])
def test_active_content_never_served_inline(files_client, name):
    for res in _get_both(files_client, name):
        assert res.status_code == 200, res.text
        assert res.headers["content-disposition"].startswith("attachment"), name
        assert res.headers["x-content-type-options"] == "nosniff"
        assert res.headers["content-security-policy"] == "sandbox"


@pytest.mark.parametrize("name", ["photo.png", "clip.mp4", "song.mp3", "notes.txt"])
def test_safe_media_still_inline(files_client, name):
    for res in _get_both(files_client, name):
        assert res.status_code == 200, res.text
        assert res.headers["content-disposition"].startswith("inline"), name
        assert res.headers["x-content-type-options"] == "nosniff"
        assert res.headers["content-security-policy"] == "sandbox"


def test_markdown_previews_as_plain_text(files_client):
    for res in _get_both(files_client, "notes.md"):
        assert res.headers["content-disposition"].startswith("inline")
        assert res.headers["content-type"].startswith("text/plain")
        assert res.text == "# hi"


def test_pdf_inline_keeps_native_viewer(files_client):
    # `sandbox` disables the built-in PDF viewer. Clickjacking directives do not.
    for res in _get_both(files_client, "doc.pdf"):
        assert res.headers["content-disposition"].startswith("inline")
        assert res.headers["x-content-type-options"] == "nosniff"
        csp = res.headers["content-security-policy"]
        assert "sandbox" not in csp
        assert "frame-ancestors 'self'" in csp
        assert res.headers["x-frame-options"] == "SAMEORIGIN"


def test_attachment_download_gets_hardening_headers(files_client):
    res = files_client.get("/api/download", params={"path": "evil.html"})
    assert res.headers["content-disposition"].startswith("attachment")
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["content-security-policy"] == "sandbox"


# --- 3/4. NAS password sign-in throttles ------------------------------------

@pytest.fixture()
def nas_client(monkeypatch):
    main._nas_login_hits.clear()
    main._nas_user_fails.clear()
    main._nas_otp_attempts.clear()
    monkeypatch.setattr(main, "_session_nas_user", lambda request, username: request.session.update(user={"username": username}))
    yield TestClient(main.app, base_url="https://drive.test")
    main._nas_login_hits.clear()
    main._nas_user_fails.clear()
    main._nas_otp_attempts.clear()


def _login(client, username="alice", password="pw"):
    return client.post("/auth/nas", json={"username": username, "password": password})


def test_username_locked_after_five_failures(nas_client, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "nas_password_login", lambda base, u, p: calls.append(u) or {"ok": False})
    for _ in range(5):
        assert _login(nas_client).status_code == 401
    # Correct password no longer reaches UGOS while locked.
    monkeypatch.setattr(main, "nas_password_login", lambda *a, **kw: pytest.fail("locked user must not be checked"))
    assert _login(nas_client).status_code == 429
    assert _login(nas_client, username="ALICE").status_code == 429
    assert len(calls) == 5


def test_lockout_is_per_username(nas_client, monkeypatch):
    monkeypatch.setattr(main, "nas_password_login", lambda *a, **kw: {"ok": False})
    for _ in range(5):
        _login(nas_client)
    monkeypatch.setattr(main, "nas_password_login", lambda *a, **kw: {"ok": True, "need_otp": False})
    assert _login(nas_client, username="bob").status_code == 200


def test_lockout_expires(nas_client, monkeypatch):
    monkeypatch.setattr(main, "nas_password_login", lambda *a, **kw: {"ok": False})
    for _ in range(5):
        _login(nas_client)
    main._nas_user_fails["alice"] = [t - main._NAS_USER_LOCK_S - 1 for t in main._nas_user_fails["alice"]]
    main._nas_login_hits.clear()
    monkeypatch.setattr(main, "nas_password_login", lambda *a, **kw: {"ok": True, "need_otp": False})
    assert _login(nas_client).status_code == 200


def test_success_clears_failures(nas_client, monkeypatch):
    monkeypatch.setattr(main, "nas_password_login", lambda *a, **kw: {"ok": False})
    for _ in range(4):
        _login(nas_client)
    monkeypatch.setattr(main, "nas_password_login", lambda *a, **kw: {"ok": True, "need_otp": False})
    assert _login(nas_client).status_code == 200
    assert "alice" not in main._nas_user_fails


def _start_otp(client, monkeypatch, token_id="tid-1"):
    monkeypatch.setattr(main.settings, "ugos_api_url", "http://ugos.test")
    monkeypatch.setattr(main, "nas_password_login", lambda *a, **kw: {"ok": True, "need_otp": True, "token_id": token_id})
    res = _login(client)
    assert res.json() == {"ok": True, "need_otp": True}


def test_otp_attempts_capped_per_challenge(nas_client, monkeypatch):
    _start_otp(nas_client, monkeypatch)
    pending_cookies = dict(nas_client.cookies)
    calls = []
    monkeypatch.setattr(main, "nas_otp_login", lambda *a, **kw: calls.append(kw) or {"ok": False, "error": "bad"})
    for _ in range(5):
        assert nas_client.post("/auth/nas/otp", json={"code": "000000", "type": 2}).status_code == 401
    assert len(calls) == 5
    res = nas_client.post("/auth/nas/otp", json={"code": "000000", "type": 2})
    assert res.status_code == 401
    assert len(calls) == 5
    # Challenge cleared from the session.
    res = nas_client.post("/auth/nas/otp", json={"code": "123456", "type": 2})
    assert "expired" in res.json()["detail"].lower()
    # Replaying the pre-lockout session cookie does not reset the counter.
    replay = TestClient(main.app, base_url="https://drive.test", cookies=pending_cookies)
    monkeypatch.setattr(main, "nas_otp_login", lambda *a, **kw: pytest.fail("exhausted challenge must not reach UGOS"))
    assert replay.post("/auth/nas/otp", json={"code": "123456", "type": 2}).status_code == 401


def test_otp_success_within_cap(nas_client, monkeypatch):
    _start_otp(nas_client, monkeypatch, token_id="tid-2")
    results = iter([{"ok": False}] * 4 + [{"ok": True}])
    monkeypatch.setattr(main, "nas_otp_login", lambda *a, **kw: next(results))
    for _ in range(4):
        assert nas_client.post("/auth/nas/otp", json={"code": "000000", "type": 2}).status_code == 401
    assert nas_client.post("/auth/nas/otp", json={"code": "123456", "type": 2}).status_code == 200


# --- File bytes stay on the fd opened as the NAS user -----------------------

def test_download_returns_file_bytes(files_client):
    res = files_client.get("/api/download", params={"path": "notes.txt"})
    assert res.status_code == 200
    assert res.content == b"hi"
    ranged = files_client.get("/api/download", params={"path": "notes.txt"}, headers={"Range": "bytes=1-1"})
    assert ranged.status_code == 206
    assert ranged.content == b"i"


def test_symlink_outside_the_share_is_not_served(files_client, files_root, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"SECRET-OUTSIDE-SHARE")
    (files_root / "link.txt").symlink_to(secret)
    res = files_client.get("/api/download", params={"path": "link.txt"})
    assert res.status_code in (400, 404)
    assert b"SECRET-OUTSIDE-SHARE" not in res.content


def test_opened_fd_ignores_a_later_symlink_swap(files_root, tmp_path):
    import fsops

    target = files_root / "notes.txt"
    original = target.read_bytes()
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"SECRET-OUTSIDE-SHARE")
    opened = fsops.open_authorized_path(target, files_root, os.getuid(), os.getgid())
    try:
        target.unlink()
        target.symlink_to(secret)
        assert os.read(opened.fd, 100) == original
        # /dev/fd/N shares this fd's offset, so rewind before reading it again.
        opened.rewind()
        assert Path(opened.dev_path()).read_bytes() == original
    finally:
        opened.close()


def test_upload_does_not_write_through_a_symlink_outside_the_share(files_root, tmp_path):
    import fsops

    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"SECRET-OUTSIDE-SHARE")
    link = files_root / "drop.txt"
    link.symlink_to(secret)
    with fsops.use_share_root(files_root), pytest.raises(fsops.FSError) as exc:
        fsops.write_upload_stream("", "drop.txt", iter([b"uploaded"]), os.getuid(), os.getgid())
    assert exc.value.status == 400
    assert secret.read_bytes() == b"SECRET-OUTSIDE-SHARE"
    assert link.is_symlink()


def test_image_thumbnail_reads_the_authorized_fd(files_root, tmp_path, monkeypatch):
    from PIL import Image

    import fsops
    import thumbs

    monkeypatch.setenv("IFD_THUMB_CACHE_DIR", str(tmp_path / "thumbs"))
    Image.new("RGB", (8, 8), (10, 20, 30)).save(files_root / "photo.png")
    opened = fsops.open_authorized_path(files_root / "photo.png", files_root, os.getuid(), os.getgid())
    try:
        thumb = thumbs.get_or_create(
            opened.path, 64, read_path=Path(opened.dev_path()), stat_result=opened.stat
        )
    finally:
        opened.close()
    assert thumb is not None and thumb.is_file() and thumb.stat().st_size > 0


# --- Cookie writes must come from this site ----------------------------------

def test_cross_site_write_is_blocked(files_client, files_root):
    res = files_client.post(
        "/api/mkdir",
        data={"name": "nope"},
        headers={"Origin": "https://evil.test", "Sec-Fetch-Site": "cross-site"},
    )
    assert res.status_code == 403
    assert res.json()["detail"] == "Cross-site request blocked"
    assert not (files_root / "nope").exists()


def test_sec_fetch_site_cross_site_is_blocked(files_client):
    res = files_client.post("/api/mkdir", data={"name": "nope"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert res.status_code == 403


def test_null_origin_is_blocked(files_client):
    res = files_client.post("/api/mkdir", data={"name": "nope"}, headers={"Origin": "null"})
    assert res.status_code == 403


def test_same_host_origin_can_write(files_client, files_root):
    res = files_client.post("/api/mkdir", data={"name": "okdir"}, headers={"Origin": "http://testserver"})
    assert res.status_code == 200, res.text
    assert (files_root / "okdir").is_dir()


def test_missing_origin_can_write(files_client, files_root):
    res = files_client.post("/api/mkdir", data={"name": "curl-dir"})
    assert res.status_code == 200, res.text
    assert (files_root / "curl-dir").is_dir()


def test_configured_public_origin_can_write(files_client, files_root, monkeypatch):
    monkeypatch.setattr(main.settings, "public_base_url", "https://drive.example.com")
    res = files_client.post(
        "/api/mkdir", data={"name": "public-dir"}, headers={"Origin": "https://drive.example.com"}
    )
    assert res.status_code == 200, res.text
    assert (files_root / "public-dir").is_dir()


def test_cross_site_get_still_downloads(files_client):
    res = files_client.get(
        "/api/download",
        params={"path": "notes.txt"},
        headers={"Origin": "https://evil.test", "Sec-Fetch-Site": "cross-site"},
    )
    assert res.status_code == 200
    assert res.content == b"hi"


def test_home_page_denies_framing(files_client):
    res = files_client.get("/")
    assert res.status_code == 200
    assert b"<!doctype html" in res.content.lower() or b"<html" in res.content.lower()
    assert res.headers["x-frame-options"] == "DENY"
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["referrer-policy"] == "same-origin"
    assert "frame-ancestors 'none'" in res.headers["content-security-policy"]


def test_download_keeps_its_sandbox_csp(files_client):
    res = files_client.get("/api/download", params={"path": "notes.txt", "inline": "1"})
    assert res.headers["content-security-policy"] == "sandbox"
    assert res.content == b"hi"
