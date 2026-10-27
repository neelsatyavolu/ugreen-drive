import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from config import get_settings  # noqa: E402

# Set at import time too: main reads settings when it is first imported.
_CONFIG_DIR = tempfile.mkdtemp(prefix="ugreen-drive-test-config-")
TEST_ENV = {
    "PUBLIC_BASE_URL": "https://drive.example.com",
    "LAN_ORIGIN": "http://192.168.1.50:8790",
    "UGOS_ADMIN_PATH": "http://192.168.1.50:9999/",
    "CONFIG_DIR": _CONFIG_DIR,
    "EMAIL_SIGN_IN_DB_PATH": str(Path(_CONFIG_DIR) / "email_sign_in.sqlite3"),
}
for _key, _value in TEST_ENV.items():
    os.environ.setdefault(_key, _value)


@pytest.fixture(autouse=True)
def deployment_env(monkeypatch):
    for key, value in TEST_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
