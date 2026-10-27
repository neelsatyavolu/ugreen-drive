"""UGOS-managed personal encryption with persistent, non-sliding 24-hour leases."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet

from config import get_settings
from ugos_api import UgosClient, UgosError

SERVICE_USERNAME = "ugreen-drive-svc"
UNLOCK_SECONDS = 24 * 60 * 60
# UGOS status answers are reused this long: every request inside a personal
# folder (and every /api/me) needs one, and UGOS can take seconds to answer.
STATUS_TTL_S = 10
log = logging.getLogger(__name__)


class OwnerSignInRequired(RuntimeError):
    def __init__(self):
        super().__init__("This folder is private. Sign in as its NAS owner to unlock it and enable automatic relocking.")


def credential_cipher(secret: str) -> Fernet:
    key = hashlib.sha256(b"personal-folder-service\0" + secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


class PersonalFolders:
    def __init__(self, db_path: Path, client_factory, *, owner_client_factory=None, clock=time.time):
        self.db_path = db_path
        self.client_factory = client_factory
        self.clock = clock
        self.owner_client_factory = owner_client_factory
        self.lock = threading.RLock()
        # owner -> (clock time, status): UGOS answers reused for STATUS_TTL_S
        self._cache = {}
        self._cache_lock = threading.Lock()

    @contextmanager
    def _db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Owner UGOS sessions live in this file. Refuse to open it if other
        # users can read it. O_NOFOLLOW: a planted symlink is not the database.
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.db_path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            if os.fstat(fd).st_mode & 0o077:
                raise OSError("personal folder state is readable by other users")
        finally:
            os.close(fd)
        db = sqlite3.connect(self.db_path)
        try:
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS unlocks (owner TEXT PRIMARY KEY, expires REAL NOT NULL)")
                yield db
        finally:
            db.close()

    @staticmethod
    def _path(owner):
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}", owner):
            raise ValueError("Invalid personal-folder owner")
        return f"/home/{owner}"

    def _cached(self, owner):
        """A status UGOS gave in the last STATUS_TTL_S, unless its lease has run out.

        Safe to reuse: a locked answer only denies, and an unlocked one is still
        checked against the live mount table before any file is touched
        (shares._personal_root).
        """
        with self._cache_lock:
            hit = self._cache.get(owner)
        if hit is None:
            return None
        at, status = hit
        now = self.clock()
        if not 0 <= now - at < STATUS_TTL_S:
            return None
        if status["expires_at"] is not None and now >= status["expires_at"]:
            return None  # the lease ran out: settle (relock) it now
        return dict(status)

    def _remember(self, owner, status):
        with self._cache_lock:
            if status is None:
                self._cache.pop(owner, None)
            else:
                self._cache[owner] = (self.clock(), dict(status))

    def status(self, owner):
        path = self._path(owner)
        cached = self._cached(owner)
        if cached is not None:
            return cached
        return self._live_status(owner, path)

    def _live_status(self, owner, path=None):
        path = path or self._path(owner)
        # Remember inside the lock: a slower check can't overwrite a newer answer.
        with self.lock:
            try:
                client = self.client_factory()
                state = client.personal_status(path)
                with self._db() as db:
                    result = self._settle(db, client, owner, path, state)
            except Exception:
                self._remember(owner, None)
                raise
            self._remember(owner, result)
            return result

    def statuses(self, owners):
        """Like status() for many owners, with one UGOS round trip instead of one each.

        Maps each owner to its status dict, or to the exception settling that
        owner raised, so one folder failing to relock can't hide the others.
        """
        results = {}
        paths = {}
        for owner in owners:
            try:
                path = self._path(owner)
            except ValueError as e:
                results[owner] = e
                continue
            cached = self._cached(owner)
            if cached is not None:
                results[owner] = cached
            else:
                paths[owner] = path
        if not paths:
            return results
        with self.lock:
            client = self.client_factory()
            states = client.personal_statuses(list(paths.values()))
            for owner, path in paths.items():
                try:
                    # A path missing from the batch is asked for alone, as before.
                    state = states[path] if path in states else client.personal_status(path)
                    with self._db() as db:
                        results[owner] = self._settle(db, client, owner, path, state)
                    self._remember(owner, results[owner])
                except Exception as e:
                    self._remember(owner, None)
                    results[owner] = e
            return results

    def _settle(self, db, client, owner, path, state):
        """Apply the 24-hour lease to a UGOS state and describe the folder."""
        row = db.execute("SELECT expires FROM unlocks WHERE owner=?", (owner,)).fetchone()
        expires = row[0] if row else None
        if state == 4:
            if expires is None:
                expires = self.clock() + UNLOCK_SECONDS
                db.execute("INSERT INTO unlocks VALUES (?, ?)", (owner, expires))
            elif self.clock() >= expires:
                # Never renew an expired lease, even if UGOS refuses a busy unmount.
                self._operate(owner, "lock_personal", path)
                state = client.personal_status(path)
                if state != 3:
                    raise RuntimeError("Personal folder is waiting to relock")
        if state in (0, 3):
            db.execute("DELETE FROM unlocks WHERE owner=?", (owner,))
            expires = None
        return {"encrypted": state != 0, "locked": state not in (0, 4),
                "expires_at": expires, "state": state}

    def unlock(self, owner, key, *, key_file=False):
        path = self._path(owner)
        if not key or len(key.encode()) > 65536:
            raise ValueError("Enter an encryption password or key file (up to 64 KB)")
        with self.lock:
            current = self._live_status(owner, path)
            if not current["locked"]:
                return current
            if current["state"] != 3:
                raise ValueError("UGOS is processing this folder. Try again shortly.")
            self._operate(owner, "unlock_personal", path, key, key_file=key_file)
            return self._live_status(owner, path)

    def _operate(self, owner, method, *args, **kwargs):
        own_client = self.owner_client_factory(owner) if self.owner_client_factory else None
        client = own_client or self.client_factory()
        try:
            return getattr(client, method)(*args, **kwargs)
        except UgosError as e:
            if e.code == 40015 or (own_client and e.code in (401, 403, 1001, 1002, 1024)):
                raise OwnerSignInRequired() from None
            raise
        finally:
            if own_client and hasattr(own_client, "close"):
                own_client.close()

    def expire(self):
        with self.lock, self._db() as db:
            owners = [row[0] for row in db.execute("SELECT owner FROM unlocks WHERE expires<=?", (self.clock(),))]
        for owner in owners:
            try:
                self._live_status(owner)
            except Exception:
                # Credentials and UGOS response bodies must never appear in logs.
                log.warning("Personal-folder relock failed; will retry")

    def keepalive(self):
        if not self.owner_client_factory:
            return
        with self.lock, self._db() as db:
            owners = [row[0] for row in db.execute("SELECT owner FROM unlocks WHERE expires>?", (self.clock(),))]
        for owner in owners:
            client = self.owner_client_factory(owner)
            if client:
                try:
                    client.personal_status(self._path(owner))
                except Exception:
                    log.warning("Personal-folder owner session needs renewed sign-in")
                finally:
                    client.close()


_client = None


def _service_client():
    global _client
    if _client is None:
        s = get_settings()
        raw = Path(s.personal_credential_path).read_bytes()
        data = json.loads(credential_cipher(s.session_secret).decrypt(raw))
        if data["username"] != SERVICE_USERNAME:
            raise ValueError("Unexpected personal-folder service account")
        _client = UgosClient(s.ugos_api_url, data["username"], data["password"], timeout=15)
    return _client


def configured():
    return Path(get_settings().personal_credential_path).is_file()


def save_owner_session(owner, session):
    PersonalFolders._path(owner)
    if not session.get("token"):
        raise OwnerSignInRequired()
    encrypted = credential_cipher(get_settings().session_secret).encrypt(
        json.dumps({"owner": owner, "token": session["token"], "public_key": session.get("public_key", "")}).encode())
    with folders.lock, folders._db() as db:
        db.execute("CREATE TABLE IF NOT EXISTS owner_sessions (owner TEXT PRIMARY KEY, session BLOB NOT NULL)")
        db.execute("INSERT OR REPLACE INTO owner_sessions VALUES (?, ?)", (owner, encrypted))


def _owner_client(owner):
    with folders.lock, folders._db() as db:
        db.execute("CREATE TABLE IF NOT EXISTS owner_sessions (owner TEXT PRIMARY KEY, session BLOB NOT NULL)")
        row = db.execute("SELECT session FROM owner_sessions WHERE owner=?", (owner,)).fetchone()
    if not row:
        return None
    s = get_settings()
    data = json.loads(credential_cipher(s.session_secret).decrypt(row[0]))
    if data["owner"] != owner:
        raise OwnerSignInRequired()
    client = UgosClient(s.ugos_api_url, owner, "", timeout=15)
    client._token = data["token"]
    client._en_public_key = data["public_key"]
    return client


folders = PersonalFolders(Path(get_settings().personal_state_path), _service_client, owner_client_factory=_owner_client)
_stop = threading.Event()
_worker = None


def start_worker():
    global _worker
    if not configured():
        return
    _stop.clear()
    def run():
        last_keepalive = 0
        while not _stop.is_set():
            try:
                folders.expire()
                if time.monotonic() - last_keepalive >= 300:
                    folders.keepalive()
                    last_keepalive = time.monotonic()
            except Exception:
                log.warning("Personal-folder expiry check failed; will retry")
            _stop.wait(15)
    _worker = threading.Thread(target=run, name="personal-folder-relock", daemon=True)
    _worker.start()


def stop_worker():
    _stop.set()
    if _worker:
        _worker.join(timeout=20)
