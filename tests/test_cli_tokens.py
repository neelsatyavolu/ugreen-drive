import base64
import hashlib
import pwd
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import cli_tokens  # noqa: E402

VERIFIER = "v" * 43


def challenge_for(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("CLI_TOKENS_DB_PATH", str(tmp_path / "cli.sqlite3"))
    cli_tokens.get_settings.cache_clear()
    cli_tokens._forget_all()
    users = {"alex": (1001, 100), "sam": (1002, 100), "daemon": (2, 2)}

    def getpwnam(name):
        if name not in users:
            raise KeyError(name)
        uid, gid = users[name]
        return pwd.struct_passwd((name, "x", uid, gid, "", "/", "/bin/sh"))

    monkeypatch.setattr(cli_tokens.pwd, "getpwnam", getpwnam)
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(cli_tokens.time, "time", lambda: clock["now"])
    return {"users": users, "clock": clock}


def sign_in(username="alex", device="Test Mac"):
    code = cli_tokens.issue_code(username, "alex@example.org", device, challenge_for(VERIFIER))
    return cli_tokens.redeem_code(code, VERIFIER)


def test_code_redeems_once_for_token_with_fresh_user_ids():
    issued = sign_in()
    assert issued["token"].startswith("ugd_") and issued["username"] == "alex"
    user = cli_tokens.lookup(issued["token"])
    assert user["username"] == "alex" and user["uid"] == 1001 and user["gid"] == 100
    assert user["email"] == "alex@example.org" and user["cli_token_id"] == issued["id"]


def test_code_is_single_use():
    code = cli_tokens.issue_code("alex", "", "Mac", challenge_for(VERIFIER))
    assert cli_tokens.redeem_code(code, VERIFIER)
    assert cli_tokens.redeem_code(code, VERIFIER) is None


def test_wrong_verifier_burns_the_code():
    code = cli_tokens.issue_code("alex", "", "Mac", challenge_for(VERIFIER))
    assert cli_tokens.redeem_code(code, "w" * 43) is None
    assert cli_tokens.redeem_code(code, VERIFIER) is None


def test_code_expires_after_a_minute(store):
    code = cli_tokens.issue_code("alex", "", "Mac", challenge_for(VERIFIER))
    store["clock"]["now"] += 61
    assert cli_tokens.redeem_code(code, VERIFIER) is None


def test_unknown_or_malformed_values_are_rejected():
    assert cli_tokens.redeem_code("nope", VERIFIER) is None
    assert cli_tokens.lookup("ugd_nope") is None
    assert cli_tokens.lookup("") is None
    with pytest.raises(ValueError):
        cli_tokens.issue_code("alex", "", "Mac", "short")


def test_only_hashes_are_stored(tmp_path):
    issued = sign_in()
    raw = (tmp_path / "cli.sqlite3").read_bytes()
    assert issued["token"].encode() not in raw


def test_idle_expiry_and_sliding_window(store):
    issued = sign_in()
    clock = store["clock"]
    clock["now"] += 29 * 86400
    assert cli_tokens.lookup(issued["token"])
    clock["now"] += 29 * 86400
    assert cli_tokens.lookup(issued["token"])  # use slid the window
    clock["now"] += 31 * 86400
    assert cli_tokens.lookup(issued["token"]) is None


def test_hard_cap_at_ninety_days(store):
    issued = sign_in()
    found = None
    for _ in range(4):
        store["clock"]["now"] += 25 * 86400
        found = cli_tokens.lookup(issued["token"])
    assert found is None  # day 100


def test_deleted_or_system_user_loses_access(store):
    issued = sign_in()
    del store["users"]["alex"]
    assert cli_tokens.lookup(issued["token"]) is None
    with pytest.raises(ValueError):
        cli_tokens.issue_code("daemon", "", "Mac", challenge_for(VERIFIER))


def test_list_and_revoke_are_per_user():
    mine = sign_in("alex", "Mac A")
    other = sign_in("sam", "Mac B")
    listed = cli_tokens.list_for("alex")
    assert [row["device"] for row in listed] == ["Mac A"]
    assert "token" not in listed[0] and "token_hash" not in listed[0]
    assert cli_tokens.revoke(other["id"], "alex") is False
    assert cli_tokens.lookup(other["token"])
    assert cli_tokens.revoke(mine["id"], "alex") is True
    assert cli_tokens.lookup(mine["token"]) is None
    assert cli_tokens.list_for("alex") == []


def test_revoke_token_and_revoke_all():
    first, second = sign_in(), sign_in()
    assert cli_tokens.revoke_token(first["token"]) is True
    assert cli_tokens.lookup(first["token"]) is None
    assert cli_tokens.revoke_all("alex") == 1
    assert cli_tokens.lookup(second["token"]) is None


def test_device_name_is_cleaned():
    assert cli_tokens.clean_device("  My\x00 Mac\n  ") == "My Mac"
    assert cli_tokens.clean_device("x" * 200) == "x" * 64
    assert cli_tokens.clean_device("") == "Unnamed device"


def test_last_used_is_throttled(store, tmp_path):
    issued = sign_in()
    cli_tokens.lookup(issued["token"])
    store["clock"]["now"] += 30
    cli_tokens.lookup(issued["token"])
    conn = sqlite3.connect(tmp_path / "cli.sqlite3")
    (last_used,) = conn.execute("SELECT last_used_at FROM cli_tokens").fetchone()
    assert last_used == 1_000_000.0


def test_recreated_user_with_same_name_does_not_inherit_tokens(store):
    issued = sign_in()
    store["users"]["alex"] = (1050, 100)  # deleted, then re-added with a new uid
    assert cli_tokens.lookup(issued["token"]) is None


def test_database_access_holds_the_credential_lock(monkeypatch):
    from contextlib import contextmanager
    entered = []

    @contextmanager
    def fake_as_root():
        entered.append(True)
        yield

    monkeypatch.setattr(cli_tokens, "as_root", fake_as_root)
    issued = sign_in()
    cli_tokens.lookup(issued["token"])
    assert len(entered) >= 3  # issue, redeem, lookup


def count_database_use(monkeypatch):
    opened = []
    real = cli_tokens._database

    def counting():
        opened.append(True)
        return real()

    monkeypatch.setattr(cli_tokens, "_database", counting)
    return opened


def test_lookups_in_a_burst_reuse_the_answer(store, monkeypatch):
    """A Finder copy sends many parallel requests: only the first waits for the
    credential lock and SQLite."""
    issued = sign_in()
    opened = count_database_use(monkeypatch)
    for _ in range(5):
        assert cli_tokens.lookup(issued["token"])["uid"] == 1001
    assert len(opened) == 1
    store["clock"]["now"] += 16
    assert cli_tokens.lookup(issued["token"])
    assert len(opened) == 2


def test_lan_token_parent_lookups_are_reused(store, monkeypatch):
    issued = sign_in()
    store["clock"]["now"] += 3600  # on the LAN all day: the parent isn't touched
    opened = count_database_use(monkeypatch)
    for _ in range(3):
        assert cli_tokens.lookup_id(issued["id"])["username"] == "alex"
    assert len(opened) == 1


def test_revoking_is_immediate_despite_reuse():
    first, second, third = sign_in(), sign_in(), sign_in()
    for issued in (first, second, third):
        assert cli_tokens.lookup(issued["token"]) and cli_tokens.lookup_id(issued["id"])
    assert cli_tokens.revoke_token(first["token"]) is True
    assert cli_tokens.lookup(first["token"]) is None and cli_tokens.lookup_id(first["id"]) is None
    assert cli_tokens.revoke(second["id"], "alex") is True
    assert cli_tokens.lookup(second["token"]) is None and cli_tokens.lookup_id(second["id"]) is None
    assert cli_tokens.revoke_all("alex") == 1
    assert cli_tokens.lookup(third["token"]) is None and cli_tokens.lookup_id(third["id"]) is None


def test_reused_answer_still_checks_the_nas_account(store):
    issued = sign_in()
    assert cli_tokens.lookup(issued["token"])
    del store["users"]["alex"]
    assert cli_tokens.lookup(issued["token"]) is None
    assert cli_tokens.lookup_id(issued["id"]) is None
