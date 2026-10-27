"""Only a UGOS encrypted-home mount reports its personal quota via statfs."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import fsops


def test_encrypted_home_uses_quota_scope(monkeypatch):
    monkeypatch.setattr(fsops, "drive_root", lambda: Path("/data/home/nasadmin"))
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw:
        "1 0 8:1 / /data/home rw - btrfs /dev/disk rw\n"
        "2 1 0:2 / /data/home/nasadmin rw - fuse.uggocryptfs /home/@nasadmin@ rw\n")
    monkeypatch.setattr(fsops.shutil, "disk_usage", lambda p: (200, 50, 150))
    assert fsops.disk_usage(personal=True) == {
        "total": 200, "used": 50, "free": 150, "scope": "personal",
    }


def test_backing_volume_is_not_mislabeled_as_personal_quota(monkeypatch):
    monkeypatch.setattr(fsops, "drive_root", lambda: Path("/data/home/nasadmin"))
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw:
        "1 0 8:1 / /data/home rw - btrfs /dev/disk rw\n"
        "2 1 0:2 / /data/home/nasadmin-other rw - fuse.uggocryptfs none rw\n")
    monkeypatch.setattr(fsops.shutil, "disk_usage", lambda p: (800, 50, 750))
    assert fsops.disk_usage(personal=True)["scope"] == "volume"


def test_shared_storage_stays_volume_scope(monkeypatch):
    monkeypatch.setattr(fsops, "drive_root", lambda: Path("/data/share"))
    monkeypatch.setattr(fsops.shutil, "disk_usage", lambda p: (800, 50, 750))
    assert fsops.disk_usage()["scope"] == "volume"
