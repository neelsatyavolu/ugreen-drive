import base64
import hashlib
import pwd
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import cli_tokens  # noqa: E402
import fsops  # noqa: E402
import main  # noqa: E402

ORIGIN = "https://drive.example.com"
VERIFIER = "v" * 43
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
STATE = "s" * 32
USER = {"email": "alex@example.org", "name": "Alex", "picture": None,
        "username": "alex", "uid": 1001, "gid": 100}


@pytest.fixture
def web_user():
    return {"user": dict(USER)}


@pytest.fixture
def client(tmp_path, monkeypatch, web_user):
    monkeypatch.setenv("PUBLIC_BASE_URL", ORIGIN)
    monkeypatch.setenv("CLI_TOKENS_DB_PATH", str(tmp_path / "cli.sqlite3"))
    main.get_settings.cache_clear()
    monkeypatch.setattr(main.settings, "public_base_url", ORIGIN)

    def getpwnam(name):
        if name != "alex":
            raise KeyError(name)
        return pwd.struct_passwd((name, "x", 1001, 100, "", "/", "/bin/sh"))

    monkeypatch.setattr(cli_tokens.pwd, "getpwnam", getpwnam)

    def session_user(request):
        if web_user["user"] is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return web_user["user"]

    monkeypatch.setattr(main, "_require_session_user", session_user)
    share_root = tmp_path / "share"
    share_root.mkdir()
    (share_root / "notes.txt").write_text("hello")
    monkeypatch.setattr(main, "share_path", lambda share: share_root)
    monkeypatch.setattr(main, "normalize_share_for_user", lambda requested, *a: requested or "Documents")
    monkeypatch.setattr(main, "list_shares_for_user", lambda *a: ["Documents"])
    monkeypatch.setattr(main, "is_nas_admin", lambda name: False)
    monkeypatch.setattr(main, "user_group_names", lambda *a: [])
    main._nas_login_hits.clear()
    main._nas_user_fails.clear()
    main._nas_otp_attempts.clear()
    main._cli_token_failures.clear()
    test_client = TestClient(main.app, base_url=ORIGIN)
    test_client.share_root = share_root
    return test_client


def authorize(client, headers=None, **overrides):
    body = {"port": 49152, "state": STATE, "challenge": CHALLENGE, "device": "Test Mac", "allow": "1"}
    body.update(overrides)
    return client.post("/api/cli/authorize", data=body, headers={"Origin": ORIGIN, **(headers or {})},
                       follow_redirects=False)


def redirect_of(response):
    assert response.status_code == 303, response.text
    return response.headers["location"]


def login(client):
    redirect = redirect_of(authorize(client))
    code = parse_qs(urlparse(redirect).query)["code"][0]
    response = client.post("/api/cli/token", json={"code": code, "verifier": VERIFIER})
    assert response.status_code == 200
    return response.json()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_consent_redirects_only_to_loopback(client):
    response = authorize(client)
    assert "code=" not in response.text  # never readable by page script, only via the 303
    redirect = urlparse(redirect_of(response))
    assert (redirect.scheme, redirect.hostname, redirect.port, redirect.path) == ("http", "127.0.0.1", 49152, "/callback")
    query = parse_qs(redirect.query)
    assert query["state"] == [STATE] and query["code"][0]


def test_deny_redirects_with_error_and_no_code(client):
    query = parse_qs(urlparse(redirect_of(authorize(client, allow="0"))).query)
    assert query == {"error": ["access_denied"], "state": [STATE]}


@pytest.mark.parametrize("field,value", [
    ("port", 80), ("port", 70000), ("state", "short"), ("state", "bad state with spaces!!"),
    ("challenge", "nope"),
])
def test_consent_rejects_bad_parameters(client, field, value):
    assert authorize(client, **{field: value}).status_code == 400


def test_consent_requires_same_origin_and_web_session(client, web_user):
    body = {"port": 49152, "state": STATE, "challenge": CHALLENGE, "device": "Mac", "allow": "1"}
    assert client.post("/api/cli/authorize", data=body).status_code == 403
    assert client.post("/api/cli/authorize", data=body, headers={"Origin": "https://evil.example"}).status_code == 403
    web_user["user"] = None
    assert authorize(client).status_code == 401


def test_full_flow_gives_bearer_access_without_cookies(client):
    issued = login(client)
    assert issued["token"].startswith("ugd_") and issued["username"] == "alex"
    listing = client.get("/api/files", headers=bearer(issued["token"]))
    assert listing.status_code == 200
    assert [item["name"] for item in listing.json()["items"]] == ["notes.txt"]
    assert "set-cookie" not in listing.headers
    me = client.get("/api/me", headers=bearer(issued["token"]))
    assert me.json()["authenticated"] is True and me.json()["nas_username"] == "alex"
    assert "set-cookie" not in me.headers


def test_token_endpoint_rejects_wrong_verifier(client):
    redirect = redirect_of(authorize(client))
    code = parse_qs(urlparse(redirect).query)["code"][0]
    assert client.post("/api/cli/token", json={"code": code, "verifier": "w" * 43}).status_code == 401
    assert client.post("/api/cli/token", json={"code": code, "verifier": VERIFIER}).status_code == 401


def test_token_endpoint_limits_failures_not_successful_logins(client):
    for _ in range(main._CLI_TOKEN_MAX_FAILURES + 5):
        login(client)  # a classroom behind one NAT signing in together
    for _ in range(main._CLI_TOKEN_MAX_FAILURES):
        assert client.post("/api/cli/token", json={"code": "x", "verifier": VERIFIER}).status_code == 401
    assert client.post("/api/cli/token", json={"code": "x", "verifier": VERIFIER}).status_code == 429


def test_invalid_bearer_is_401_and_never_falls_back(client):
    response = client.get("/api/files", headers=bearer("ugd_forged"))
    assert response.status_code == 401
    assert "Sign in again" in response.json()["detail"]


def test_bearer_cannot_mint_tokens_or_browser_sessions(client, web_user):
    token = login(client)["token"]
    web_user["user"] = None  # only the bearer is presented
    assert authorize(client, headers=bearer(token)).status_code == 401
    assert client.post("/api/lan-handoff", headers=bearer(token)).status_code == 401


def test_share_header_is_used_for_bearer(client, monkeypatch):
    seen = []
    monkeypatch.setattr(main, "normalize_share_for_user", lambda requested, *a: seen.append(requested) or "Documents")
    token = login(client)["token"]
    client.get("/api/files", headers={**bearer(token), "X-Drive-Share": "Photos"})
    assert seen[-1] == "Photos"


def test_sessions_list_revoke_and_logout(client):
    first, second = login(client), login(client)
    listed = client.get("/api/cli/sessions", headers=bearer(first["token"])).json()["sessions"]
    assert {row["id"] for row in listed} == {first["id"], second["id"]}
    assert [row["current"] for row in listed if row["id"] == first["id"]] == [True]
    assert client.delete("/api/cli/sessions/unknown", headers=bearer(first["token"])).status_code == 404
    assert client.delete(f"/api/cli/sessions/{second['id']}", headers=bearer(first["token"])).status_code == 200
    assert client.get("/api/files", headers=bearer(second["token"])).status_code == 401
    assert client.post("/api/cli/logout", headers=bearer(first["token"])).status_code == 200
    assert client.get("/api/files", headers=bearer(first["token"])).status_code == 401


def test_web_session_can_list_and_revoke(client):
    issued = login(client)
    listed = client.get("/api/cli/sessions").json()["sessions"]
    assert [row["device"] for row in listed] == ["Test Mac"]
    assert client.delete(f"/api/cli/sessions/{issued['id']}").status_code == 200


def test_upload_conflict_guard(client):
    token = login(client)["token"]
    current = client.get("/api/files", headers=bearer(token)).json()["items"][0]["mtime_ns"]

    def put(expect, name="notes.txt"):
        return client.post("/api/upload", headers=bearer(token),
                           data={"path": "", "expect_mtime_ns": str(expect)},
                           files={"file": (name, b"new text")})

    assert put(current - 1).status_code == 409
    assert put(-1).status_code == 409
    assert (client.share_root / "notes.txt").read_text() == "hello"
    assert put(current).status_code == 200
    assert put(-1, "fresh.txt").status_code == 200
    assert (client.share_root / "notes.txt").read_text() == "new text"


@pytest.mark.parametrize("url", [
    f"/cli/authorize?port=49152&state={STATE}&challenge={CHALLENGE}&device=Mac",
    "/assets/cli-authorize.html",
])
def test_consent_page_cannot_be_framed(client, url):
    response = client.get(url)
    assert response.status_code == 200 and "text/html" in response.headers["content-type"]
    csp = response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "form-action 'self' http://127.0.0.1:*" in csp
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "same-origin"
    assert "no-store" in response.headers["cache-control"]


def test_mac_install_script_embeds_server_url(client):
    response = client.get("/mac/install.sh")
    assert response.status_code == 200
    assert f"'{ORIGIN}'" in response.text
    assert "__UGD_SERVER__" not in response.text


def test_write_upload_stream_expect_mtime(tmp_path):
    (tmp_path / "a.txt").write_text("one")
    mtime = (tmp_path / "a.txt").stat().st_mtime_ns
    with fsops.use_share_root(tmp_path):
        with pytest.raises(fsops.FSError) as exc:
            fsops.write_upload_stream("", "a.txt", iter([b"two"]), 1001, 100, expect_mtime_ns=mtime + 1)
        assert exc.value.status == 409
        fsops.write_upload_stream("", "a.txt", iter([b"two"]), 1001, 100, expect_mtime_ns=mtime)
    assert (tmp_path / "a.txt").read_text() == "two"
    assert not list(tmp_path.glob(".*.partial"))


def test_must_not_exist_upload_never_replaces_a_file_that_appears_after_the_check(tmp_path, monkeypatch):
    real_check = fsops._check_expected_mtime
    calls = []

    def check_then_someone_creates_it(dest, expect):
        real_check(dest, expect)
        calls.append(dest)
        if len(calls) == 2:  # the final check before the rename
            dest.write_text("theirs")  # lands between that check and the rename

    monkeypatch.setattr(fsops, "_check_expected_mtime", check_then_someone_creates_it)
    with fsops.use_share_root(tmp_path), pytest.raises(fsops.FSError) as exc:
        fsops.write_upload_stream("", "race.txt", iter([b"mine"]), 1001, 100, expect_mtime_ns=-1)
    assert exc.value.status == 409
    assert (tmp_path / "race.txt").read_text() == "theirs"
    assert not list(tmp_path.glob(".*.partial"))


# Personal folders: the Mac app unlocks them with its sign-in token, exactly
# like the web app (same owner check, HTTPS, shared attempt limit).

@pytest.fixture
def personal(monkeypatch):
    monkeypatch.setattr(main.personal_folders, "configured", lambda: True)
    saved = []
    monkeypatch.setattr(main.personal_folders, "save_owner_session", lambda owner, session: saved.append((owner, session)))
    return saved


def test_bearer_unlocks_only_its_own_personal_folder(client, web_user, monkeypatch, personal):
    token = login(client)["token"]
    web_user["user"] = None
    calls = []

    def unlock(owner, key, *, key_file):
        calls.append((owner, key, key_file))
        return {"encrypted": True, "locked": False, "expires_at": 86400}

    monkeypatch.setattr(main.personal_folders.folders, "unlock", unlock)
    response = client.post("/api/personal/unlock", headers=bearer(token),
                           data={"owner": "alex", "key": "secret-key", "key_file": "true"})
    assert response.status_code == 200 and response.json()["locked"] is False
    assert "secret-key" not in response.text
    assert calls == [("alex", "secret-key", True)]
    other = client.post("/api/personal/unlock", headers=bearer(token), data={"owner": "sam", "key": "x"})
    assert other.status_code == 403 and len(calls) == 1
    assert "set-cookie" not in response.headers


def test_bearer_unlock_shares_the_attempt_limit(client, web_user, monkeypatch, personal):
    token = login(client)["token"]
    web_user["user"] = None
    monkeypatch.setattr(main.personal_folders.folders, "unlock", lambda *a, **kw: {"locked": False})
    codes = [client.post("/api/personal/unlock", headers=bearer(token),
                         data={"owner": "alex", "key": "x"}).status_code for _ in range(13)]
    assert codes[-1] == 429


def test_bearer_owner_sign_in_with_otp_uses_a_bound_pending_token(client, web_user, monkeypatch, personal):
    first_token = login(client)["token"]
    other_token = login(client)["token"]
    web_user["user"] = None
    monkeypatch.setattr(main, "nas_password_login",
                        lambda *a, **kw: {"ok": True, "need_otp": True, "token_id": "ugos-challenge"})
    seen = []

    def otp(base, *, code, token_id, return_session):
        seen.append((code, token_id))
        return {"ok": True, "session": {"token": "owner-token", "public_key": "k"}}

    monkeypatch.setattr(main, "nas_otp_login", otp)
    first = client.post("/api/personal/auth", headers=bearer(first_token),
                        data={"owner": "alex", "password": "nas-password"})
    body = first.json()
    assert body["need_otp"] is True and body["pending"]
    assert "ugos-challenge" not in first.text and "nas-password" not in first.text
    assert "set-cookie" not in first.headers

    def finish(token, pending, owner="alex"):
        return client.post("/api/personal/auth", headers=bearer(token),
                           data={"owner": owner, "code": "123456", "pending": pending})

    # 410 = start over with the NAS password (the app goes back a step).
    assert finish(first_token, body["pending"][:-4] + "AAAA").status_code == 410  # tampered
    assert finish(other_token, body["pending"]).status_code == 410  # another sign-in's token
    assert finish(first_token, body["pending"], owner="sam").status_code == 403
    assert seen == [] and personal == []
    done = finish(first_token, body["pending"])
    assert done.json() == {"ok": True}
    assert seen == [("123456", "ugos-challenge")]
    assert personal == [("alex", {"token": "owner-token", "public_key": "k"})]
    assert finish(first_token, body["pending"]).status_code == 410  # single use
    assert len(seen) == 1


def test_bearer_owner_sign_in_without_otp(client, web_user, monkeypatch, personal):
    token = login(client)["token"]
    web_user["user"] = None
    monkeypatch.setattr(main, "nas_password_login",
                        lambda *a, **kw: {"ok": True, "session": {"token": "t", "public_key": ""}})
    response = client.post("/api/personal/auth", headers=bearer(token), data={"owner": "alex", "password": "p"})
    assert response.json() == {"ok": True} and personal == [("alex", {"token": "t", "public_key": ""})]


def test_personal_auth_never_echoes_a_rejected_password(client, web_user, personal):
    token = login(client)["token"]
    web_user["user"] = None
    secret = "x" * 300 + "-my-nas-password"
    response = client.post("/api/personal/auth", headers=bearer(token), data={"owner": "alex", "password": secret})
    assert response.status_code == 422
    assert "my-nas-password" not in response.text


def test_personal_auth_counts_failures_against_the_account(client, web_user, monkeypatch, personal):
    token = login(client)["token"]
    web_user["user"] = None
    tries = []
    monkeypatch.setattr(main, "nas_password_login",
                        lambda *a, **kw: tries.append(1) or {"ok": False, "error": "Incorrect password"})
    codes = [client.post("/api/personal/auth", headers=bearer(token),
                         data={"owner": "alex", "password": "guess"}).status_code for _ in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 429
    assert len(tries) == 5  # the locked attempt never reached the NAS


def test_personal_auth_limits_codes_per_challenge(client, web_user, monkeypatch, personal):
    token = login(client)["token"]
    web_user["user"] = None
    monkeypatch.setattr(main, "nas_password_login",
                        lambda *a, **kw: {"ok": True, "need_otp": True, "token_id": "challenge-2"})
    monkeypatch.setattr(main, "nas_otp_login", lambda *a, **kw: {"ok": False, "error": "Wrong code"})
    pending = client.post("/api/personal/auth", headers=bearer(token),
                          data={"owner": "alex", "password": "p"}).json()["pending"]
    codes = [client.post("/api/personal/auth", headers=bearer(token),
                         data={"owner": "alex", "code": "000000", "pending": pending}).status_code
             for _ in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 410


def test_personal_auth_requires_a_password_or_code(client, web_user, monkeypatch, personal):
    token = login(client)["token"]
    web_user["user"] = None
    monkeypatch.setattr(main, "nas_password_login", lambda *a, **kw: pytest.fail("must not call the NAS"))
    response = client.post("/api/personal/auth", headers=bearer(token), data={"owner": "alex"})
    assert response.status_code == 400


# LAN route for the Mac helper: prove the LAN server is this Drive, then use a
# short-lived LAN-only token so the long-lived token never crosses the LAN.
LAN = "http://lan.test:8790"


@pytest.fixture
def lan(client, monkeypatch):
    monkeypatch.setattr(main.settings, "lan_origin", LAN)
    return TestClient(main.app, base_url=LAN)


def test_lan_proof_only_answers_direct_lan_requests(client, lan):
    nonce = "n" * 40
    direct = lan.get("/api/lan/proof", params={"nonce": nonce})
    assert direct.status_code == 200 and direct.json()["proof"]
    assert client.get("/api/lan/proof", params={"nonce": nonce}).status_code == 404  # public host
    relayed = lan.get("/api/lan/proof", params={"nonce": nonce}, headers={"CF-Ray": "abc"})
    assert relayed.status_code == 404  # came through Cloudflare: refuse (no relaying)
    assert lan.get("/api/lan/proof", params={"nonce": "short"}).status_code == 400


def test_lan_token_requires_a_genuine_proof_and_only_works_on_the_lan(client, web_user, lan):
    token = login(client)["token"]
    web_user["user"] = None
    nonce = "x" * 40
    proof = lan.get("/api/lan/proof", params={"nonce": nonce}).json()["proof"]
    bad = client.post("/api/cli/lan-token", headers=bearer(token), data={"nonce": nonce, "proof": "0" * 64})
    assert bad.status_code == 403
    issued = client.post("/api/cli/lan-token", headers=bearer(token), data={"nonce": nonce, "proof": proof})
    assert issued.status_code == 200
    lan_token = issued.json()["token"]
    assert lan_token.startswith("ugl_") and issued.json()["lan_origin"] == LAN
    assert lan.get("/api/files", headers=bearer(lan_token)).status_code == 200
    assert client.get("/api/files", headers=bearer(lan_token)).status_code == 401  # not via the internet
    assert lan.get("/api/files", headers={**bearer(lan_token), "CF-Ray": "abc"}).status_code == 401
    # A LAN token can't mint more LAN tokens.
    assert lan.post("/api/cli/lan-token", headers=bearer(lan_token), data={"nonce": nonce, "proof": proof}).status_code == 403
    # Signing out the parent sign-in kills its LAN tokens.
    assert client.post("/api/cli/logout", headers=bearer(token)).status_code == 200
    assert lan.get("/api/files", headers=bearer(lan_token)).status_code == 401


def test_lan_token_expires(client, web_user, lan, monkeypatch):
    token = login(client)["token"]
    web_user["user"] = None
    nonce = "y" * 40
    proof = lan.get("/api/lan/proof", params={"nonce": nonce}).json()["proof"]
    lan_token = client.post("/api/cli/lan-token", headers=bearer(token), data={"nonce": nonce, "proof": proof}).json()["token"]
    monkeypatch.setattr(main, "_LAN_TOKEN_TTL_S", -1)
    assert lan.get("/api/files", headers=bearer(lan_token)).status_code == 401


def test_lan_token_not_offered_without_lan_origin(client, web_user, monkeypatch):
    monkeypatch.setattr(main.settings, "lan_origin", "")
    token = login(client)["token"]
    web_user["user"] = None
    assert client.post("/api/cli/lan-token", headers=bearer(token), data={"nonce": "z" * 40, "proof": "a"}).status_code == 404
