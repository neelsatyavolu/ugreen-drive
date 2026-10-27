import re
import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
import main  # noqa: E402

UPLOAD_BODY_ROUTES = ("/api/upload/chunk", "/api/speedtest/upload")


def _lifespan_starts_worker(monkeypatch) -> bool:
    started = []
    monkeypatch.setattr(main.personal_folders, "start_worker", lambda: started.append(True))
    monkeypatch.setattr(main.personal_folders, "stop_worker", lambda: None)
    monkeypatch.setattr(main, "require_strong_session_secret", lambda secret: None)
    with TestClient(main.app):
        pass
    return bool(started)


def test_main_process_runs_background_jobs(monkeypatch):
    monkeypatch.delenv("IFD_UPLOAD_WORKER", raising=False)
    assert _lifespan_starts_worker(monkeypatch)


def test_upload_worker_skips_background_jobs(monkeypatch):
    # Upload workers only take chunk bodies; the main process owns the jobs.
    monkeypatch.setenv("IFD_UPLOAD_WORKER", "1")
    assert not _lifespan_starts_worker(monkeypatch)


def test_gateway_sends_upload_bodies_to_worker_pool():
    conf = (ROOT / "docker" / "nginx.conf.template").read_text()
    pool = re.search(r"upstream ugd_upload \{(.*?)\}", conf, re.S)
    assert pool, "missing ugd_upload upstream"
    assert re.search(r"server unix:/run/ugreen-drive/app\.sock backup;", pool.group(1)), "main app must stay the fallback"
    location = re.search(r"location ~ \^/api/\(([^)]*upload/chunk[^)]*)\)\$ \{(.*?)\n    \}", conf, re.S)
    assert location and "proxy_pass http://ugd_upload;" in location.group(2)
    for route in UPLOAD_BODY_ROUTES:
        assert re.fullmatch(f"^/api/({location.group(1)})$", route), route
    assert "proxy_request_buffering off;" in location.group(2)
