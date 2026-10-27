"""UGOS RequestEncrypt (desktop HTTP client) helpers."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from ugos_api import (  # noqa: E402
    UgosClient,
    UgosError,
    decode_en_public_key,
    decrypt_aes_gcm,
    encrypt_aes_gcm,
    encrypt_password,
    hash_md5,
    hash_sha256,
    json_compact,
    nas_password_login,
    new_aes_key,
)


def _rsa_pair():
    key = rsa.generate_private_key(65537, 2048, default_backend())
    pem = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    return key, pem.decode("utf-8")


def test_aes_gcm_roundtrip():
    key = new_aes_key()
    assert len(key) == 32
    blob = encrypt_aes_gcm(key, "hello")
    assert decrypt_aes_gcm(key, blob) == "hello"


def test_decode_en_public_key_atob():
    pem = "-----BEGIN PUBLIC KEY-----\nabc\n-----END PUBLIC KEY-----"
    wrapped = base64.b64encode(pem.encode("utf-8")).decode("ascii")
    assert decode_en_public_key(wrapped) == pem
    assert decode_en_public_key(pem) == pem


def test_http_wrap_encrypts_query_and_body():
    priv, pem = _rsa_pair()
    client = UgosClient("http://127.0.0.1:9999", "nasadmin", "x")
    client._token = "sess-token"
    client._en_public_key = pem
    url, headers, body, aes_key = client._wrap_request(
        "/ugreen/v1/user/list?reverse=false",
        headers={},
        json_body={"username": "kid"},
    )
    assert aes_key
    assert headers["X-Ugreen-Security-Key"] == hash_md5("sess-token")
    wrapped = headers["X-Ugreen-Security-Code"]
    aes_from_rsa = priv.decrypt(base64.b64decode(wrapped), padding.PKCS1v15()).decode("utf-8")
    assert aes_from_rsa == aes_key

    parsed = urlparse(url)
    assert parsed.path == "/ugreen/v1/user/list"
    enc_q = parse_qs(parsed.query)["encrypt_query"][0]
    assert "token=" not in parsed.query
    assert decrypt_aes_gcm(aes_key, enc_q) == "token=sess-token&reverse=false"

    assert body is not None
    plain = json_compact({"username": "kid"})
    assert body["req_body_sha256"] == hash_sha256(plain)
    assert json.loads(decrypt_aes_gcm(aes_key, body["encrypt_req_body"])) == {"username": "kid"}
    client.close()


def _fake_httpx(monkeypatch, handler):
    import ugos_api as ua

    transport = httpx.MockTransport(handler)

    class Fake(httpx.Client):
        def __init__(self, **kwargs):
            kwargs["transport"] = transport
            super().__init__(**kwargs)

    monkeypatch.setattr(ua.httpx, "Client", Fake)


def test_service_login_uses_current_firmware_flags(monkeypatch):
    _, pem = _rsa_pair()
    def handler(request):
        if request.url.path.endswith('/verify/check'):
            return httpx.Response(200, json={'code': 200}, headers={'x-rsa-token': pem})
        body = json.loads(request.content)
        assert body['otp'] is True
        assert body['is_simple'] is False
        return httpx.Response(200, json={'code': 200, 'data': {'token': 'service'}})
    _fake_httpx(monkeypatch, handler)
    with UgosClient('https://nas', 'service', 'password') as client:
        client.login()
        assert client._token == 'service'


def test_unlock_encrypts_key_and_uses_native_path(monkeypatch):
    priv, pem = _rsa_pair()
    def handler(request):
        if request.url.path.endswith('/verify/check'):
            return httpx.Response(200, json={'code': 200}, headers={'x-rsa-token': pem})
        assert request.url.path.endswith('/unlockEncryptedDir')
        body = json.loads(request.content)
        assert body['path'] == '/home/nasadmin'
        assert priv.decrypt(base64.b64decode(body['password']), padding.PKCS1v15()) == b'folder-key'
        return httpx.Response(200, json={'code': 200})
    _fake_httpx(monkeypatch, handler)
    with UgosClient('https://nas', 'service', 'password') as client:
        client._token = 'service'
        client.unlock_personal('/home/nasadmin', 'folder-key')


def test_nas_password_login_success(monkeypatch):
    _, pem = _rsa_pair()
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/verify/check"):
            return httpx.Response(
                200,
                json={"code": 200, "data": {}},
                headers={"x-rsa-token": pem},
            )
        if request.url.path.endswith("/verify/login"):
            seen["login"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"code": 200, "data": {"token": "sess"}})
        return httpx.Response(404)

    _fake_httpx(monkeypatch, handler)
    out = nas_password_login("http://127.0.0.1:9999", "st10003", "Secret1!")
    assert out["ok"] is True
    assert out.get("need_otp") is False
    assert seen["login"]["otp"] is True
    assert seen["login"]["is_simple"] is False


def test_nas_password_login_firmware_9406(monkeypatch):
    _, pem = _rsa_pair()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/verify/check"):
            return httpx.Response(
                200,
                json={"code": 200, "data": {}},
                headers={"x-rsa-token": pem},
            )
        return httpx.Response(
            200,
            json={
                "code": 9406,
                "msg": "The client is not compatible with the firmware, please update the client！",
                "data": {},
            },
        )

    _fake_httpx(monkeypatch, handler)
    out = nas_password_login("http://127.0.0.1:9999", "nasadmin", "Secret1!")
    assert out["ok"] is False
    assert out.get("code") == 9406


def test_nas_password_login_otp_challenge(monkeypatch):
    _, pem = _rsa_pair()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/verify/check"):
            return httpx.Response(
                200,
                json={"code": 200, "data": {}},
                headers={"x-rsa-token": pem},
            )
        return httpx.Response(
            200, json={"code": 200, "data": {"enable_otp": True, "token_id": "otp-1"}}
        )

    _fake_httpx(monkeypatch, handler)
    out = nas_password_login("http://127.0.0.1:9999", "st10003", "Secret1!")
    assert out["ok"] is True
    assert out["need_otp"] is True
    assert out["token_id"] == "otp-1"


def test_https_skips_encrypt():
    client = UgosClient("https://ugos.example", "nasadmin", "x")
    client._token = "sess-token"
    url, headers, body, aes_key = client._wrap_request(
        "/ugreen/v1/user/create",
        headers={},
        json_body={"username": "kid"},
    )
    assert aes_key is None
    assert "encrypt_query" not in url
    assert "token=sess-token" in url
    assert body == {"username": "kid"}
    assert "X-Ugreen-Security-Code" not in headers
    client.close()


def test_encrypt_password_rsa():
    priv, pem = _rsa_pair()
    blob = encrypt_password(pem, "Secret1!")
    assert priv.decrypt(base64.b64decode(blob), padding.PKCS1v15()) == b"Secret1!"


def test_full_login_response_is_opt_in_for_sso(monkeypatch):
    _, pem = _rsa_pair()
    data = {"token": "native-session", "uid": 1001, "username": "student", "role": 2, "public_key": "key"}
    def handler(request):
        if request.url.path.endswith("/verify/check"):
            return httpx.Response(200, json={"code": 200}, headers={"x-rsa-token": pem})
        return httpx.Response(200, json={"code": 200, "data": data})
    _fake_httpx(monkeypatch, handler)
    assert "login_data" not in nas_password_login("https://nas", "student", "ticket")
    assert nas_password_login("https://nas", "student", "ticket", return_login_data=True)["login_data"] == data
    from ugos_api import nas_otp_login
    assert nas_otp_login("https://nas", code="123456", token_id="challenge", return_login_data=True)["login_data"] == data


def test_personal_statuses_batches_paths(monkeypatch):
    seen = []
    def handler(request):
        assert request.url.path.endswith('/encryptedDirStatus')
        seen.append(json.loads(request.content)['paths'])
        return httpx.Response(200, json={'code': 200, 'data': {'list': [
            {'path': '/home/alice', 'status': 3}, {'path': '/home/nasadmin', 'status': 4}]}})
    _fake_httpx(monkeypatch, handler)
    with UgosClient('https://nas', 'service', 'password') as client:
        client._token = 'service'
        assert client.personal_statuses(['/home/nasadmin', '/home/alice']) == {
            '/home/nasadmin': 4, '/home/alice': 3}
        assert client.personal_status('/home/nasadmin') == 4
    assert seen == [['/home/nasadmin', '/home/alice'], ['/home/nasadmin']]


def test_personal_statuses_first_row_wins_and_missing_raises_for_single(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={'code': 200, 'data': {'list': [
            {'path': '/home/alice', 'status': 3}, {'path': '/home/alice', 'status': 4}]}})
    _fake_httpx(monkeypatch, handler)
    with UgosClient('https://nas', 'service', 'password') as client:
        client._token = 'service'
        assert client.personal_statuses(['/home/alice', '/home/bob']) == {'/home/alice': 3}
        with pytest.raises(UgosError):
            client.personal_status('/home/bob')
