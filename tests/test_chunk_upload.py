"""Chunked upload sessions must create staging files even under as_root."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import chunk_upload  # noqa: E402
import fsops  # noqa: E402


@pytest.fixture()
def staging(tmp_path, monkeypatch):
    root = tmp_path / "chunks"
    monkeypatch.setattr(chunk_upload, "SESSION_ROOT", root)
    chunk_upload._sessions.clear()
    return root


def test_create_session_writes_meta(staging):
    session = chunk_upload.create_session(
        username="st10001",
        uid=1067,
        gid=10,
        share="~st10001",
        rel_dir="EGLLFACT",
        filename="big.rar",
        size=64 * 1024 * 1024,
        chunk_size=32 * 1024 * 1024,
    )
    assert session["total_chunks"] == 2
    meta = staging / session["upload_id"] / "meta.json"
    assert meta.is_file()
    assert chunk_upload.peek_session(session["upload_id"])["name"] == "big.rar"


def test_write_chunk_and_complete(staging, tmp_path, monkeypatch):
    drive = tmp_path / "drive"
    drive.mkdir()
    monkeypatch.setenv("DRIVE_ROOT", str(drive))
    from config import get_settings

    get_settings.cache_clear()

    payload = b"abcdefgh" * 1024
    session = chunk_upload.create_session(
        username="nasadmin",
        uid=1001,
        gid=1001,
        share="Drive",
        rel_dir="",
        filename="clip.bin",
        size=len(payload),
        chunk_size=1024 * 1024,
    )
    chunk_upload.write_chunk(
        session["upload_id"],
        0,
        iter([payload]),
        username="nasadmin",
        uid=1001,
    )
    with fsops.use_share_root(drive):
        result = chunk_upload.complete_session(
            session["upload_id"],
            username="nasadmin",
            uid=1001,
        )
    assert (drive / "clip.bin").read_bytes() == payload
    assert result["name"] == "clip.bin"


def test_as_root_noop_without_privileges():
    with fsops.as_root():
        pass


@pytest.mark.parametrize("kind,gb", [("A-roll", 30), ("B-roll", 15)])
@pytest.mark.parametrize("extra", [0, 1])
def test_package_roll_upload_limit(staging, kind, gb, extra):
    kwargs = dict(
        username="nasadmin", uid=1001, gid=1001, share="Drive",
        rel_dir=f"Package Storage/Cycle 1/Tom cookbook/A-roll B-roll/{kind}",
        filename="clip.mp4", size=gb * 1024 ** 3 + extra,
    )
    if extra:
        with pytest.raises(fsops.FSError, match=f"max {gb}GB"):
            chunk_upload.create_session(**kwargs)
    else:
        session = chunk_upload.create_session(**kwargs)
        assert session["size"] == gb * 1024 ** 3


@pytest.mark.parametrize("extra", [0, 1])
def test_default_upload_limit_is_50gb(staging, extra):
    kwargs = dict(
        username="nasadmin", uid=1001, gid=1001, share="Drive",
        rel_dir="Footage", filename="raw.mov", size=50 * 1024 ** 3 + extra,
    )
    if extra:
        with pytest.raises(fsops.FSError, match="max 50GB"):
            chunk_upload.create_session(**kwargs)
    else:
        assert chunk_upload.create_session(**kwargs)["size"] == 50 * 1024 ** 3


@pytest.fixture()
def drive(tmp_path, monkeypatch):
    root = tmp_path / "drive"
    (root / "Shows").mkdir(parents=True)
    monkeypatch.setenv("DRIVE_ROOT", str(root))
    from config import get_settings

    get_settings.cache_clear()
    return root


def in_place_session(drive, size, name="clip.mov", chunk=1024 * 1024):
    return chunk_upload.create_session(
        username="nasadmin", uid=1001, gid=1001, share="Drive", rel_dir="Shows",
        filename=name, size=size, chunk_size=chunk, dest_dir=drive / "Shows",
    )


def partials(drive):
    return sorted(p.name for p in (drive / "Shows").iterdir() if p.name.endswith(".partial"))


def test_chunks_are_written_in_place_and_complete_is_a_rename(staging, drive):
    """Completing used to copy every byte again; now it only renames."""
    payload = bytes(range(256)) * 12 * 1024  # 3 MiB
    session = in_place_session(drive, len(payload))
    assert len(partials(drive)) == 1 and partials(drive)[0].startswith(".clip.mov.")
    upload_id = session["upload_id"]
    mib = 1024 * 1024
    for index in (2, 0, 1):  # parallel streams finish in any order
        chunk_upload.write_chunk(upload_id, index, iter([payload[index * mib:(index + 1) * mib]]),
                                 username="nasadmin", uid=1001)
    assert chunk_upload.received_indices(upload_id) == [0, 1, 2]
    staged = sum(p.stat().st_size for p in (staging / upload_id).iterdir() if p.name != "meta.json")
    assert staged == 0  # nothing kept twice
    with fsops.use_share_root(drive):
        result = chunk_upload.complete_session(upload_id, username="nasadmin", uid=1001)
    assert (drive / "Shows" / "clip.mov").read_bytes() == payload
    assert result["name"] == "clip.mov"
    assert partials(drive) == [] and not (staging / upload_id).exists()


def test_in_place_upload_missing_a_chunk_is_refused(staging, drive):
    session = in_place_session(drive, 2 * 1024 * 1024)
    chunk_upload.write_chunk(session["upload_id"], 0, iter([b"x" * 1024 * 1024]), username="nasadmin", uid=1001)
    with fsops.use_share_root(drive), pytest.raises(fsops.FSError, match="Missing chunks"):
        chunk_upload.complete_session(session["upload_id"], username="nasadmin", uid=1001)
    assert not (drive / "Shows" / "clip.mov").exists()


def test_in_place_retry_of_a_chunk_overwrites_it(staging, drive):
    session = in_place_session(drive, 1024 * 1024)
    upload_id = session["upload_id"]
    with pytest.raises(fsops.FSError):  # connection dropped mid-chunk
        chunk_upload.write_chunk(upload_id, 0, iter([b"a" * 1000]), username="nasadmin", uid=1001)
    assert chunk_upload.received_indices(upload_id) == []
    chunk_upload.write_chunk(upload_id, 0, iter([b"b" * 1024 * 1024]), username="nasadmin", uid=1001)
    with fsops.use_share_root(drive):
        chunk_upload.complete_session(upload_id, username="nasadmin", uid=1001)
    assert (drive / "Shows" / "clip.mov").read_bytes() == b"b" * 1024 * 1024


def test_aborted_or_expired_in_place_upload_leaves_nothing(staging, drive, monkeypatch):
    first = in_place_session(drive, 1024 * 1024, name="a.mov")
    chunk_upload.abort_session(first["upload_id"], username="nasadmin", uid=1001)
    second = in_place_session(drive, 1024 * 1024, name="b.mov")
    assert len(partials(drive)) == 1
    monkeypatch.setattr(chunk_upload, "_now", lambda: 10 ** 12)
    assert chunk_upload.cleanup_expired() == 1
    assert partials(drive) == [] and not (staging / second["upload_id"]).exists()


def test_in_place_create_only_never_replaces_a_file(staging, drive):
    (drive / "Shows" / "clip.mov").write_bytes(b"theirs")
    session = in_place_session(drive, 1024 * 1024)
    chunk_upload.write_chunk(session["upload_id"], 0, iter([b"m" * 1024 * 1024]), username="nasadmin", uid=1001)
    with fsops.use_share_root(drive), pytest.raises(fsops.FSError) as err:
        chunk_upload.complete_session(session["upload_id"], username="nasadmin", uid=1001, expect_mtime_ns=-1)
    assert err.value.status == 409
    assert (drive / "Shows" / "clip.mov").read_bytes() == b"theirs"
