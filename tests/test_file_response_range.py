"""Starlette FileResponse honors Range — used by /api/download."""

from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import FileResponse
from starlette.routing import Route
from starlette.testclient import TestClient


def test_file_response_range(tmp_path: Path):
    blob = tmp_path / "clip.bin"
    blob.write_bytes(bytes(range(256)))

    def download(request):
        return FileResponse(path=blob, filename="clip.bin", media_type="application/octet-stream")

    app = Starlette(routes=[Route("/d", download)])
    client = TestClient(app)
    res = client.get("/d", headers={"Range": "bytes=10-19"})
    assert res.status_code == 206
    assert res.content == bytes(range(10, 20))
    assert "bytes 10-19/256" in (res.headers.get("content-range") or "")
    assert res.headers.get("accept-ranges") == "bytes"
