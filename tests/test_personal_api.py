import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'app'))
import main


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, '_require_session_user', lambda request: {'username': 'alice', 'uid': 1001, 'gid': 100})
    monkeypatch.setattr(main, 'is_nas_admin', lambda name: False)
    monkeypatch.setattr(main.personal_folders, 'configured', lambda: True)
    main._nas_login_hits.clear()
    main._nas_user_fails.clear()
    main._personal_key_fails.clear()
    yield TestClient(main.app, base_url='https://drive.test')
    main._nas_login_hits.clear()
    main._nas_user_fails.clear()
    main._personal_key_fails.clear()


def test_cannot_unlock_someone_elses_folder(client, monkeypatch):
    monkeypatch.setattr(main.personal_folders.folders, 'unlock', lambda *a, **kw: pytest.fail('must not call UGOS'))
    assert client.post('/api/personal/unlock', data={'owner': 'nasadmin', 'key': 'x'}).status_code == 403


def test_owner_can_submit_key_but_it_is_not_echoed(client, monkeypatch):
    def unlock(owner, key, *, key_file):
        assert (owner, key, key_file) == ('alice', 'private-key', True)
        return {'encrypted': True, 'locked': False, 'expires_at': 86400}
    monkeypatch.setattr(main.personal_folders.folders, 'unlock', unlock)
    response = client.post('/api/personal/unlock', data={'owner': 'alice', 'key': 'private-key', 'key_file': 'true'})
    assert response.status_code == 200
    assert 'private-key' not in response.text


def test_bad_encryption_key_locks_after_five(client, monkeypatch):
    calls = []

    def unlock(owner, key, *, key_file):
        calls.append(key)
        raise main.personal_folders.UgosError('bad key')

    monkeypatch.setattr(main.personal_folders.folders, 'unlock', unlock)
    for _ in range(5):
        assert client.post('/api/personal/unlock', data={'owner': 'alice', 'key': 'nope'}).status_code == 400
    monkeypatch.setattr(
        main.personal_folders.folders, 'unlock', lambda *a, **kw: pytest.fail('locked key must not be tried')
    )
    locked = client.post('/api/personal/unlock', data={'owner': 'alice', 'key': 'nope'})
    assert locked.status_code == 429
    assert len(calls) == 5
    # A wrong folder key does not lock the NAS account password.
    assert 'alice' not in main._nas_user_fails


def test_ugos_processing_does_not_burn_a_key_guess(client, monkeypatch):
    def unlock(owner, key, *, key_file):
        raise ValueError('UGOS is processing this folder. Try again shortly.')

    monkeypatch.setattr(main.personal_folders.folders, 'unlock', unlock)
    for _ in range(6):
        res = client.post('/api/personal/unlock', data={'owner': 'alice', 'key': 'x'})
        assert res.status_code == 400


def test_owner_signin_required_is_not_a_failed_key(client, monkeypatch):
    def unlock(owner, key, *, key_file):
        raise main.personal_folders.OwnerSignInRequired()

    monkeypatch.setattr(main.personal_folders.folders, 'unlock', unlock)
    for _ in range(6):
        assert client.post('/api/personal/unlock', data={'owner': 'alice', 'key': 'x'}).status_code == 428


def test_unlock_rate_limit(client, monkeypatch):
    monkeypatch.setattr(main.personal_folders.folders, 'unlock', lambda *a, **kw: {})
    for _ in range(12):
        assert client.post('/api/personal/unlock', data={'owner': 'alice', 'key': 'x'}).status_code == 200
    assert client.post('/api/personal/unlock', data={'owner': 'alice', 'key': 'x'}).status_code == 429


def test_reject_plain_http(client, monkeypatch):
    monkeypatch.setattr(main.personal_folders.folders, 'unlock', lambda *a, **kw: pytest.fail('must use HTTPS'))
    response = client.post('http://drive.test/api/personal/unlock', data={'owner': 'alice', 'key': 'x'})
    assert response.status_code == 426


def test_private_owner_signin_requires_otp_before_saving_session(client, monkeypatch):
    saved = []
    monkeypatch.setattr(main.personal_folders, 'save_owner_session', lambda owner, session: saved.append((owner, session)))
    monkeypatch.setattr(main, 'nas_password_login', lambda *a, **kw: {'ok': True, 'need_otp': True, 'token_id': 'challenge'})
    def otp(base, *, code, token_id, return_session):
        assert code == '123456' and token_id == 'challenge' and return_session
        return {'ok': True, 'session': {'token': 'private-token', 'public_key': 'key'}}
    monkeypatch.setattr(main, 'nas_otp_login', otp)
    first = client.post('/api/personal/auth', data={'owner': 'alice', 'password': 'not-stored'})
    assert first.json() == {'need_otp': True}
    assert saved == []
    second = client.post('/api/personal/auth', data={'owner': 'alice', 'code': '123456'})
    assert second.json() == {'ok': True}
    assert saved == [('alice', {'token': 'private-token', 'public_key': 'key'})]
    assert 'private-token' not in second.text
    assert client.post('/api/personal/auth', data={'owner': 'alice', 'code': '123456'}).status_code == 401


def test_admin_cannot_reuse_someone_elses_owner_session(client, monkeypatch):
    monkeypatch.setattr(main, 'is_nas_admin', lambda name: True)
    monkeypatch.setattr(main.personal_folders.folders, 'unlock', lambda *a, **kw: pytest.fail('must not reuse owner session'))
    for endpoint in ('auth', 'unlock'):
        assert client.post(f'/api/personal/{endpoint}', data={'owner': 'nasadmin', 'password': 'x', 'key': 'x'}).status_code == 403
