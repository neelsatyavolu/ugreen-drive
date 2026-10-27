"""Mac app sign-in rules that ugreen-drive adds on top of the ported CLI tests."""
from types import SimpleNamespace

from test_cli_api import ORIGIN, STATE, USER, bearer, client, login, web_user  # noqa: F401
import main


def test_confirmation_code_matches_the_app():
    # Same vector as cli/internal/auth: SHA-256("ugd-verify:" + state), XXXX-XXXX.
    assert main.cli_confirmation_code("s" + "0" * 31) == "2603-F3C6"


def test_confirm_code_endpoint(client):
    assert client.get(f"/api/cli/confirm-code?state={STATE}").json() == {"code": main.cli_confirmation_code(STATE)}
    assert client.get("/api/cli/confirm-code?state=bad state").status_code == 400


def test_consent_page_shows_the_drive_name(client, monkeypatch):
    monkeypatch.setattr(main.settings, "drive_name", "Home <drive>")
    page = client.get(f"/cli/authorize?state={STATE}").text
    assert "<title>Home &lt;drive&gt;</title>" in page


def test_password_approved_token_ignores_email_links(client, monkeypatch):
    # A NAS password session can carry the account's UGOS email; that's not a link.
    monkeypatch.setattr(main, "resolve_nas_user", lambda email: None)
    token = login(client)["token"]
    assert client.get("/api/files", headers=bearer(token)).status_code == 200


def test_unlinking_an_email_revokes_tokens_it_approved(client, web_user, monkeypatch):
    links = {"alex@example.org": "alex"}
    monkeypatch.setattr(main, "resolve_nas_user", lambda email: SimpleNamespace(username=links[email], uid=1001)
                        if email in links else None)
    web_user["user"] = {**USER, "via": "email"}
    token = login(client)["token"]
    assert client.get("/api/files", headers=bearer(token)).status_code == 200
    links.clear()
    assert client.get("/api/files", headers=bearer(token)).status_code == 401


def test_bearer_writes_skip_the_cookie_origin_check(client):
    token = login(client)["token"]
    response = client.post("/api/mkdir", headers={**bearer(token), "Sec-Fetch-Site": "cross-site"},
                           data={"path": "New"})
    assert response.status_code != 403


def test_app_requests_never_set_the_share_cookie(client):
    token = login(client)["token"]
    client.cookies.clear()
    response = client.get("/api/files", headers={**bearer(token), "X-Drive-Share": "Documents"})
    assert response.status_code == 200 and "set-cookie" not in response.headers
