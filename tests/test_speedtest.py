import sys
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import main  # noqa: E402

MIB = 1024 * 1024


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "_require_user", lambda request: {"username": "student1", "uid": 1001, "gid": 100})
    return TestClient(main.app, base_url="https://drive.test")


def test_download_is_incompressible(client):
    # Zeros would let a proxy compress the stream and overstate the speed.
    body = client.get("/api/speedtest/download", params={"size": 4 * MIB}).content
    assert len(body) == 4 * MIB
    assert len(zlib.compress(body)) > 0.9 * len(body)


def test_download_supports_ranges(client):
    full = client.get("/api/speedtest/download", params={"size": 3 * MIB}).content
    r = client.get("/api/speedtest/download", params={"size": 3 * MIB}, headers={"Range": "bytes=1000-2999999"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 1000-2999999/{3 * MIB}"
    assert r.content == full[1000:3000000]
    tail = client.get("/api/speedtest/download", params={"size": 3 * MIB}, headers={"Range": f"bytes={3 * MIB - 10}-"})
    assert tail.status_code == 206 and tail.content == full[-10:]


def test_download_rejects_unsatisfiable_range(client):
    r = client.get("/api/speedtest/download", params={"size": MIB}, headers={"Range": f"bytes={MIB}-"})
    assert r.status_code == 416
