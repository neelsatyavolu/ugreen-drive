import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import thumbs  # noqa: E402


def test_snap_size_clamps_to_allowed_set():
    assert thumbs.snap_size(256) == 256
    assert thumbs.snap_size(1) == 64
    assert thumbs.snap_size(300) == 256
    assert thumbs.snap_size(9999) == 512


def test_thumb_kind_by_extension():
    assert thumbs.thumb_kind("photo.JPG") == "image"
    assert thumbs.thumb_kind("clip.mp4") == "video"
    assert thumbs.thumb_kind("vector.svg") is None
    assert thumbs.thumb_kind("notes.txt") is None


def test_cache_key_changes_with_mtime_and_size():
    a = thumbs.cache_key("/x/a.jpg", 100, 5, 256)
    assert a != thumbs.cache_key("/x/a.jpg", 101, 5, 256)
    assert a != thumbs.cache_key("/x/a.jpg", 100, 6, 256)
    assert a != thumbs.cache_key("/x/a.jpg", 100, 5, 512)
    assert a == thumbs.cache_key("/x/a.jpg", 100, 5, 256)


def test_image_thumb_created_and_cached(tmp_path, monkeypatch):
    from PIL import Image

    monkeypatch.setenv("IFD_THUMB_CACHE_DIR", str(tmp_path / "cache"))
    src = tmp_path / "big.png"
    Image.new("RGB", (800, 600), "red").save(src)

    out = thumbs.get_or_create(src, 256)
    assert out is not None and out.exists()
    with Image.open(out) as im:
        assert im.format == "JPEG"
        assert max(im.size) <= 256

    first_stat = out.stat()
    again = thumbs.get_or_create(src, 256)
    assert again == out
    assert again.stat().st_mtime_ns == first_stat.st_mtime_ns  # served from cache


def test_large_jpeg_thumb_via_draft_decode(tmp_path, monkeypatch):
    from PIL import Image

    monkeypatch.setenv("IFD_THUMB_CACHE_DIR", str(tmp_path / "cache"))
    src = tmp_path / "photo.jpg"
    Image.new("RGB", (3200, 2400), "green").save(src, "JPEG")

    out = thumbs.get_or_create(src, 256)
    assert out is not None
    with Image.open(out) as im:
        assert im.format == "JPEG"
        assert max(im.size) <= 256
        # Aspect ratio preserved despite the DCT-scaled draft decode.
        assert abs(im.size[0] / im.size[1] - 3200 / 2400) < 0.05


def test_warm_paths_generates_in_background(tmp_path, monkeypatch):
    from PIL import Image

    cache = tmp_path / "cache"
    monkeypatch.setenv("IFD_THUMB_CACHE_DIR", str(cache))
    srcs = []
    for i in range(3):
        p = tmp_path / f"img{i}.png"
        Image.new("RGB", (400, 300), "blue").save(p)
        srcs.append(p)

    queued = thumbs.warm_paths(srcs, 256)
    assert queued == 3

    deadline = time.time() + 10
    while time.time() < deadline:
        if cache.exists() and len(list(cache.glob("*.jpg"))) == 3:
            break
        time.sleep(0.05)
    assert len(list(cache.glob("*.jpg"))) == 3

    # Unsupported paths queue a job but produce nothing (and don't crash).
    txt = tmp_path / "no.txt"
    txt.write_text("hi")
    assert thumbs.warm_paths([txt], 256) == 1


def test_video_thumb_passes_the_authorized_fd(tmp_path, monkeypatch):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"not-a-real-video")
    fd = os.open(src, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["pass_fds"] = kwargs.get("pass_fds")
        Path(cmd[-1]).write_bytes(b"jpeg-bytes")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setenv("IFD_THUMB_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(thumbs.subprocess, "run", fake_run)
    try:
        out = thumbs.get_or_create(
            src, 64, read_path=Path(f"/dev/fd/{fd}"), stat_result=os.fstat(fd)
        )
    finally:
        os.close(fd)
    assert seen["pass_fds"] == (fd,)
    assert out is not None and out.read_bytes() == b"jpeg-bytes"


def test_unsupported_and_missing_return_none(tmp_path, monkeypatch):
    monkeypatch.setenv("IFD_THUMB_CACHE_DIR", str(tmp_path / "cache"))
    txt = tmp_path / "a.txt"
    txt.write_text("hi")
    assert thumbs.get_or_create(txt, 256) is None
    assert thumbs.get_or_create(tmp_path / "missing.jpg", 256) is None


def test_corrupt_image_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("IFD_THUMB_CACHE_DIR", str(tmp_path / "cache"))
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    assert thumbs.get_or_create(bad, 256) is None


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_video_thumb(tmp_path, monkeypatch):
    monkeypatch.setenv("IFD_THUMB_CACHE_DIR", str(tmp_path / "cache"))
    src = tmp_path / "clip.mp4"
    os.system(
        f"ffmpeg -loglevel error -f lavfi -i testsrc=duration=2:size=320x240:rate=10 {src}"
    )
    out = thumbs.get_or_create(src, 256)
    assert out is not None and out.exists() and out.stat().st_size > 0
