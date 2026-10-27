import hashlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'app'))
import fsops


def fingerprint(data):
    return hashlib.sha256(b''.join(hashlib.sha256(data[i:i + 8 * 1024 * 1024]).digest()
                                  for i in range(0, len(data), 8 * 1024 * 1024))).hexdigest()


@pytest.mark.parametrize('data', [b'', b'hello', b'x' * (8 * 1024 * 1024 + 1)], ids=['empty', 'small', 'multi-chunk'])
def test_fingerprint_matches_chunked_browser_algorithm(tmp_path, data):
    (tmp_path / 'file').write_bytes(data)
    with fsops.use_share_root(tmp_path):
        assert fsops.upload_fingerprint('file', len(data), os.getuid(), os.getgid()) == fingerprint(data)


def test_missing_or_different_size_needs_upload(tmp_path):
    (tmp_path / 'file').write_bytes(b'abc')
    with fsops.use_share_root(tmp_path):
        assert fsops.upload_fingerprint('missing', 3, os.getuid(), os.getgid()) is None
        assert fsops.upload_fingerprint('file', 2, os.getuid(), os.getgid()) is None


def test_same_size_content_changes_are_detected(tmp_path):
    p = tmp_path / 'file'
    p.write_bytes(b'abc')
    with fsops.use_share_root(tmp_path):
        before = fsops.upload_fingerprint('file', 3, os.getuid(), os.getgid())
        p.write_bytes(b'xyz')
        assert fsops.upload_fingerprint('file', 3, os.getuid(), os.getgid()) != before


def test_cannot_hash_outside_share(tmp_path):
    with fsops.use_share_root(tmp_path), pytest.raises(fsops.FSError):
        fsops.upload_fingerprint('../outside', 3, os.getuid(), os.getgid())


def test_fingerprint_api_checks_auth_share_and_size(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from fastapi.testclient import TestClient
    import main

    (tmp_path / 'file').write_bytes(b'abc')
    monkeypatch.setattr(main, '_require_user', lambda request: {'uid': os.getuid(), 'gid': os.getgid()})
    monkeypatch.setattr(main, '_active_share', lambda request, user: 'selected')
    def selected_root(share):
        assert share == 'selected'
        return tmp_path
    monkeypatch.setattr(main, 'share_path', selected_root)
    client = TestClient(main.app)
    response = client.post('/api/upload/fingerprint', data={'path': 'file', 'size': 3})
    assert response.status_code == 200
    assert response.json() == {'fingerprint': fingerprint(b'abc')}
    assert client.post('/api/upload/fingerprint', data={'path': 'file', 'size': -1}).status_code == 422
    assert client.post('/api/upload/fingerprint', data={'path': '../outside', 'size': 3}).status_code == 400
    def deny(request):
        raise HTTPException(401)
    monkeypatch.setattr(main, '_require_user', deny)
    assert client.post('/api/upload/fingerprint', data={'path': 'file', 'size': 3}).status_code == 401
