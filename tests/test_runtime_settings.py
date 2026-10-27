"""Admin settings saved in the config folder, and first-run secrets."""
import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import main  # noqa: E402
import runtime_settings  # noqa: E402
from config import Settings  # noqa: E402


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_settings, "detect_lan_ip", lambda: "192.168.1.50")
    runtime_settings._env_defaults.clear()
    s = Settings(config_dir=str(tmp_path), session_secret="", public_base_url="", lan_origin="")
    yield s
    runtime_settings._env_defaults.clear()


def test_first_start_generates_and_keeps_a_secret(fresh, tmp_path):
    runtime_settings.apply(fresh)
    secret = fresh.session_secret
    assert len(secret) >= 32
    path = tmp_path / "session_secret"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    again = Settings(config_dir=str(tmp_path), session_secret="")
    assert runtime_settings.ensure_session_secret(again) == secret


def test_strong_env_secret_wins(fresh, tmp_path):
    fresh.session_secret = "x" * 40
    assert runtime_settings.ensure_session_secret(fresh) == "x" * 40
    assert not (tmp_path / "session_secret").exists()


def test_defaults_come_from_the_lan_address(fresh):
    runtime_settings.apply(fresh)
    assert fresh.lan_origin == "http://192.168.1.50:8790"
    assert fresh.public_base_url == "http://192.168.1.50:8790"
    assert fresh.ugos_admin_path == "http://192.168.1.50:9999/"


def test_saved_values_override_env_and_clearing_falls_back(fresh):
    fresh.drive_name = "Env Name"
    runtime_settings.apply(fresh)
    runtime_settings.update({"drive_name": "Home Drive", "public_base_url": "https://drive.example.com/"}, None, fresh)
    assert fresh.drive_name == "Home Drive"
    assert fresh.public_base_url == "https://drive.example.com"
    runtime_settings.update({"drive_name": ""}, None, fresh)
    assert fresh.drive_name == "Env Name"  # an empty saved value falls back to the env default


def test_secrets_keep_unless_cleared_and_are_never_shown(fresh):
    runtime_settings.apply(fresh)
    runtime_settings.update({"google_client_id": "id", "google_client_secret": "shh"}, None, fresh)
    runtime_settings.update({"google_client_secret": ""}, None, fresh)
    assert fresh.google_client_secret == "shh"
    view = runtime_settings.admin_view(fresh)
    assert view["values"]["google_client_secret"] == {"set": True}
    assert "shh" not in json.dumps(view)
    assert view["methods"]["google"] is True
    runtime_settings.update({"clear": ["google_client_secret"]}, None, fresh)
    assert fresh.google_client_secret == ""
    assert runtime_settings.methods(fresh)["google"] is False


@pytest.mark.parametrize("value", ["drive.example.com", "ftp://x.test", "https://x.test/path", "https://u@x.test"])
def test_bad_addresses_are_rejected(fresh, value):
    runtime_settings.apply(fresh)
    with pytest.raises(runtime_settings.SettingsError):
        runtime_settings.update({"public_base_url": value}, None, fresh)


def test_unknown_fields_and_bad_identities_are_rejected(fresh):
    runtime_settings.apply(fresh)
    with pytest.raises(runtime_settings.SettingsError):
        runtime_settings.update({"session_secret": "x"}, None, fresh)
    with pytest.raises(runtime_settings.SettingsError):
        runtime_settings.update(None, {"not-an-email": "admin"}, fresh)
    with pytest.raises(runtime_settings.SettingsError):
        runtime_settings.update(None, {"a@example.com": "bad name"}, fresh)


def test_identities_are_lowercased_and_saved(fresh):
    runtime_settings.apply(fresh)
    runtime_settings.update(None, {"Me@Example.com": "admin"}, fresh)
    assert runtime_settings.identities(fresh) == {"me@example.com": "admin"}


# --- /api/admin/settings ----------------------------------------------------

@pytest.fixture
def admin_client(monkeypatch, tmp_path):
    s = Settings(config_dir=str(tmp_path), session_secret="y" * 40,
                 public_base_url="https://drive.example.com", lan_origin="http://192.168.1.50:8790")
    runtime_settings._env_defaults.clear()
    runtime_settings.apply(s)
    monkeypatch.setattr(main, "settings", s)
    monkeypatch.setattr(main, "_require_session_user", lambda request: {"username": "admin", "uid": 1001, "gid": 100})
    monkeypatch.setattr(main, "is_nas_admin", lambda name: name == "admin")
    monkeypatch.setattr(main, "list_nas_usernames", lambda: ["admin", "family"])
    yield TestClient(main.app, base_url="https://drive.example.com")
    runtime_settings._env_defaults.clear()


def test_admin_can_read_and_save(admin_client):
    view = admin_client.get("/api/admin/settings").json()
    assert view["nas_users"] == ["admin", "family"]
    res = admin_client.put("/api/admin/settings", json={
        "values": {"drive_name": "Home"}, "identities": {"kid@example.com": "family"}})
    assert res.status_code == 200, res.text
    assert res.json()["identities"] == {"kid@example.com": "family"}
    assert admin_client.get("/api/config").json()["drive_name"] == "Home"


def test_linking_to_a_missing_nas_user_is_refused(admin_client):
    res = admin_client.put("/api/admin/settings", json={"identities": {"x@example.com": "ghost"}})
    assert res.status_code == 400
    assert "isn't a NAS user" in res.json()["detail"]


def test_non_admins_cannot_see_or_change_settings(admin_client, monkeypatch):
    monkeypatch.setattr(main, "_require_session_user", lambda request: {"username": "family", "uid": 1002, "gid": 100})
    assert admin_client.get("/api/admin/settings").status_code == 403
    assert admin_client.put("/api/admin/settings", json={"values": {"drive_name": "x"}}).status_code == 403


def test_cross_site_settings_write_is_blocked(admin_client):
    res = admin_client.put("/api/admin/settings", json={"values": {"drive_name": "x"}},
                           headers={"Origin": "https://evil.example"})
    assert res.status_code == 403


def test_password_sign_in_goes_through_ugos(admin_client, monkeypatch):
    seen = []
    monkeypatch.setattr(main, "nas_password_login", lambda base, u, p: seen.append((base, u)) or {"ok": True, "need_otp": False})
    monkeypatch.setattr(main, "_pwd_user", lambda name: SimpleNamespace(pw_name=name, pw_uid=1001, pw_gid=100))
    monkeypatch.setattr(main, "linux_user_email", lambda name: None)
    main._nas_login_hits.clear()
    res = admin_client.post("/auth/nas", json={"username": "admin", "password": "pw"})
    assert res.status_code == 200
    assert seen == [("http://127.0.0.1:9999", "admin")]
