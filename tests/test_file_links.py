"""Signed public file-share links."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from config import get_settings  # noqa: E402


@pytest.fixture()
def secret(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-file-link-secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://drive.example.com")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_mint_default_and_max_days(secret):
    import file_links

    token7 = file_links.mint(
        share="Drive", path="clip.mp4", days=7, uid=1001, gid=1001, name="clip.mp4"
    )
    payload = file_links.verify(token7)
    assert payload["v"] == 1
    assert payload["share"] == "Drive"
    assert payload["path"] == "clip.mp4"
    assert payload["name"] == "clip.mp4"
    assert payload["uid"] == 1001
    assert payload["gid"] == 1001
    assert payload["exp"] > time.time() + 6 * 86400
    assert payload["exp"] < time.time() + 8 * 86400

    token30 = file_links.mint(
        share="Drive", path="clip.mp4", days=30, uid=1001, gid=1001, name="clip.mp4"
    )
    p30 = file_links.verify(token30)
    assert p30["exp"] > time.time() + 29 * 86400
    assert p30["exp"] < time.time() + 31 * 86400


def test_mint_rejects_days_out_of_range(secret):
    import file_links

    with pytest.raises(ValueError):
        file_links.mint(share="Drive", path="a.txt", days=0, uid=1, gid=1, name="a.txt")
    with pytest.raises(ValueError):
        file_links.mint(share="Drive", path="a.txt", days=31, uid=1, gid=1, name="a.txt")


def test_tampered_token_is_invalid(secret):
    import file_links

    token = file_links.mint(
        share="Drive", path="a.txt", days=7, uid=1, gid=1, name="a.txt"
    )
    bad = token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1]
    with pytest.raises(file_links.LinkError) as ei:
        file_links.verify(bad)
    assert ei.value.code == "invalid"


def test_expired_token(secret):
    import file_links

    token = file_links._ser().dumps(
        {
            "v": 1,
            "share": "Drive",
            "path": "a.txt",
            "exp": int(time.time()) - 10,
            "name": "a.txt",
            "uid": 1,
            "gid": 1,
        }
    )
    with pytest.raises(file_links.LinkError) as ei:
        file_links.verify(token)
    assert ei.value.code == "expired"
    assert ei.value.payload["name"] == "a.txt"


def test_public_url(secret):
    import file_links

    url = file_links.public_url("abc.token")
    assert url == "https://drive.example.com/s/abc.token"


def test_preview_kind():
    import file_links

    assert file_links.preview_kind("clip.mp4") == "video"
    assert file_links.preview_kind("pic.png") == "image"
    assert file_links.preview_kind("notes.txt") == "text"
    assert file_links.preview_kind("pack.zip") is None


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------


@pytest.fixture()
def drive_root(tmp_path, monkeypatch, secret):
    root = tmp_path / "Drive"
    root.mkdir()
    (root / "clip.mp4").write_bytes(b"fake-mp4-bytes")
    nested = root / "Camp"
    nested.mkdir()
    (nested / "take.mp4").write_bytes(b"nested-bytes")
    (root / "notes.txt").write_text("hello share")
    monkeypatch.setenv("DRIVE_ROOT", str(root))
    get_settings.cache_clear()
    return root


@pytest.fixture()
def client(drive_root, monkeypatch):
    import main

    uid, gid = os.getuid(), os.getgid()

    monkeypatch.setattr(
        main,
        "_require_user",
        lambda request: {"uid": uid, "gid": gid, "username": "tester", "email": "t@example.org"},
    )
    monkeypatch.setattr(main, "_active_share", lambda request, user: "Drive")
    monkeypatch.setattr(main, "share_path", lambda share: drive_root)
    return TestClient(main.app)


def test_mint_refuses_a_personal_folder(client, monkeypatch):
    import main

    monkeypatch.setattr(main, "_active_share", lambda request, user: "~tester")
    res = client.post("/api/file-link", json={"path": "notes.txt", "days": 7})
    assert res.status_code == 403
    assert "personal" in res.json()["detail"].lower()


def test_old_personal_link_does_not_serve_the_file(client):
    import file_links

    token = file_links.mint(
        share="~tester",
        path="notes.txt",
        days=1,
        uid=os.getuid(),
        gid=os.getgid(),
        name="notes.txt",
    )
    body = client.get(f"/api/s/{token}/file")
    assert body.status_code == 404
    assert b"hello share" not in body.content
    assert client.get(f"/api/s/{token}").status_code == 404


def test_mint_and_public_get_without_session(client, drive_root):
    res = client.post("/api/file-link", json={"path": "clip.mp4", "days": 7})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["url"].startswith("https://drive.example.com/s/")
    assert "expires_at" in body
    token = body["url"].rsplit("/", 1)[-1]

    # No session cookie on a fresh client for public routes.
    public = TestClient(client.app)
    meta = public.get(f"/api/s/{token}")
    assert meta.status_code == 200, meta.text
    data = meta.json()
    assert data["name"] == "clip.mp4"
    assert data["size"] == len(b"fake-mp4-bytes")
    assert data["kind"] == "video"
    assert data["previewable"] is True
    assert data.get("error") in (None, "")
    assert "path" not in data
    assert "Camp" not in meta.text
    assert "tester" not in meta.text
    assert "uid" not in data

    page = public.get(f"/s/{token}")
    assert page.status_code == 200
    assert "share.js" in page.text
    assert "noindex" in page.text


def test_mint_rejects_bad_days(client):
    assert client.post("/api/file-link", json={"path": "clip.mp4", "days": 0}).status_code == 400
    assert client.post("/api/file-link", json={"path": "clip.mp4", "days": 31}).status_code == 400


def test_public_tampered_and_expired(client, secret):
    import file_links
    import main

    public = TestClient(main.app)
    token = file_links.mint(
        share="Drive", path="clip.mp4", days=7, uid=1, gid=1, name="clip.mp4"
    )
    bad = token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1]
    res = public.get(f"/api/s/{bad}")
    assert res.status_code == 404
    assert res.json()["error"] == "invalid"

    expired = file_links._ser().dumps(
        {
            "v": 1,
            "share": "Drive",
            "path": "clip.mp4",
            "exp": int(time.time()) - 5,
            "name": "clip.mp4",
            "uid": 1,
            "gid": 1,
        }
    )
    res = public.get(f"/api/s/{expired}")
    assert res.status_code == 404
    assert res.json()["error"] == "expired"


def test_missing_file_after_mint(client, drive_root):
    res = client.post("/api/file-link", json={"path": "clip.mp4", "days": 7})
    token = res.json()["url"].rsplit("/", 1)[-1]
    (drive_root / "clip.mp4").unlink()
    public = TestClient(client.app)
    meta = public.get(f"/api/s/{token}")
    assert meta.status_code == 404
    assert meta.json()["error"] == "unavailable"


def test_path_traversal_payload_rejected(client, secret, drive_root):
    import file_links

    uid, gid = os.getuid(), os.getgid()
    token = file_links._ser().dumps(
        {
            "v": 1,
            "share": "Drive",
            "path": "../etc/passwd",
            "exp": int(time.time()) + 86400,
            "name": "passwd",
            "uid": uid,
            "gid": gid,
        }
    )
    public = TestClient(client.app)
    res = public.get(f"/api/s/{token}")
    assert res.status_code in (400, 404)
    body = res.json()
    assert body.get("error") in ("unavailable", "invalid") or res.status_code == 400


def test_parent_path_not_in_public_json(client):
    res = client.post("/api/file-link", json={"path": "Camp/take.mp4", "days": 7})
    token = res.json()["url"].rsplit("/", 1)[-1]
    public = TestClient(client.app)
    data = public.get(f"/api/s/{token}").json()
    assert data["name"] == "take.mp4"
    assert "path" not in data
    assert "Camp" not in str(data)


def test_file_inline_vs_attachment(client):
    res = client.post("/api/file-link", json={"path": "notes.txt", "days": 7})
    token = res.json()["url"].rsplit("/", 1)[-1]
    public = TestClient(client.app)

    att = public.get(f"/api/s/{token}/file")
    assert att.status_code == 200
    assert att.content == b"hello share"
    disp = att.headers.get("content-disposition", "").lower()
    assert "attachment" in disp

    inline = public.get(f"/api/s/{token}/file", params={"inline": "1"})
    assert inline.status_code == 200
    disp = inline.headers.get("content-disposition", "").lower()
    assert "inline" in disp


# ---------------------------------------------------------------------------
# Folder links
# ---------------------------------------------------------------------------


def test_mint_marks_folder_links(secret):
    import file_links

    file_token = file_links.mint(
        share="Drive", path="a.txt", days=7, uid=1, gid=1, name="a.txt"
    )
    assert "dir" not in file_links.verify(file_token)
    dir_token = file_links.mint(
        share="Drive", path="Camp", days=7, uid=1, gid=1, name="Camp", is_dir=True
    )
    assert file_links.verify(dir_token)["dir"] is True


@pytest.fixture()
def folder_token(client, drive_root):
    camp = drive_root / "Camp"
    clips = camp / "clips"
    clips.mkdir()
    (clips / "b.txt").write_text("bee")
    (camp / ".DS_Store").write_bytes(b"junk")
    (camp / "#recycle").mkdir()
    (camp / "#recycle" / "gone.txt").write_text("gone")
    os.symlink(drive_root / "notes.txt", camp / "escape.txt")
    res = client.post("/api/file-link", json={"path": "Camp", "days": 7})
    assert res.status_code == 200, res.text
    return res.json()["url"].rsplit("/", 1)[-1]


def test_folder_link_lists_contents(client, folder_token):
    public = TestClient(client.app)
    res = public.get(f"/api/s/{folder_token}")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["is_dir"] is True
    assert data["name"] == "Camp"
    assert data["folder"] == "Camp"
    assert data["path"] == ""
    assert "expires_at" in data
    items = {i["name"]: i for i in data["items"]}
    # Junk, the recycle bin and symlinks never show up.
    assert set(items) == {"clips", "take.mp4"}
    assert items["clips"]["is_dir"] is True
    assert items["clips"]["path"] == "clips"
    assert items["take.mp4"]["size"] == len(b"nested-bytes")
    assert items["take.mp4"]["path"] == "take.mp4"
    assert "uid" not in res.text
    assert "mode" not in items["take.mp4"]


def test_folder_link_subfolder_and_files(client, folder_token):
    public = TestClient(client.app)
    sub = public.get(f"/api/s/{folder_token}", params={"path": "clips"}).json()
    assert sub["is_dir"] is True
    assert sub["name"] == "clips"
    assert sub["path"] == "clips"
    assert [i["path"] for i in sub["items"]] == ["clips/b.txt"]

    meta = public.get(f"/api/s/{folder_token}", params={"path": "clips/b.txt"}).json()
    assert meta["is_dir"] is False
    assert meta["name"] == "b.txt"
    assert meta["size"] == 3
    assert meta["kind"] == "text"
    assert meta["folder"] == "Camp"
    assert meta["path"] == "clips/b.txt"

    body = public.get(f"/api/s/{folder_token}/file", params={"path": "clips/b.txt"})
    assert body.status_code == 200
    assert body.content == b"bee"
    assert "attachment" in body.headers.get("content-disposition", "").lower()


@pytest.mark.parametrize(
    "sub",
    ["../clip.mp4", "../notes.txt", "escape.txt", "#recycle", "#recycle/gone.txt", "clips/../../notes.txt"],
)
def test_folder_link_stays_inside_folder(client, folder_token, sub):
    public = TestClient(client.app)
    meta = public.get(f"/api/s/{folder_token}", params={"path": sub})
    assert meta.status_code == 404, meta.text
    assert meta.json()["error"] == "unavailable"
    raw = public.get(f"/api/s/{folder_token}/file", params={"path": sub})
    assert raw.status_code == 404
    assert b"hello share" not in raw.content
    assert b"gone" not in raw.content


def test_folder_link_zip(client, folder_token):
    import io
    import zipfile

    public = TestClient(client.app)
    res = public.get(f"/api/s/{folder_token}/zip")
    assert res.status_code == 200, res.text
    assert 'filename="Camp.zip"' in res.headers["content-disposition"]
    names = sorted(zipfile.ZipFile(io.BytesIO(res.content)).namelist())
    assert names == ["Camp/clips/b.txt", "Camp/take.mp4"]

    sub = public.get(f"/api/s/{folder_token}/zip", params={"path": "clips"})
    assert sub.status_code == 200
    assert zipfile.ZipFile(io.BytesIO(sub.content)).namelist() == ["clips/b.txt"]

    outside = public.get(f"/api/s/{folder_token}/zip", params={"path": ".."})
    assert outside.status_code == 404


def test_file_link_takes_no_subpath_or_zip(client):
    res = client.post("/api/file-link", json={"path": "notes.txt", "days": 7})
    token = res.json()["url"].rsplit("/", 1)[-1]
    public = TestClient(client.app)
    assert public.get(f"/api/s/{token}").json()["is_dir"] is False
    assert public.get(f"/api/s/{token}", params={"path": "x"}).status_code == 404
    assert public.get(f"/api/s/{token}/file", params={"path": "x"}).status_code == 404
    assert public.get(f"/api/s/{token}/zip").status_code == 404


@pytest.mark.parametrize("path", [".", "/", "#recycle"])
def test_mint_refuses_share_root_and_recycle(client, drive_root, path):
    (drive_root / "#recycle").mkdir(exist_ok=True)
    assert client.post("/api/file-link", json={"path": path, "days": 7}).status_code == 400


def test_folder_link_refuses_symlink_swapped_in(client, drive_root, folder_token):
    # Someone with write access replaces the shared folder with a symlink to a
    # folder only the sharer can read: the link must not follow it.
    (drive_root / "Camp").rename(drive_root / "Camp-old")
    private = drive_root / "Private"
    private.mkdir()
    (private / "secret.txt").write_text("secret")
    os.symlink(private, drive_root / "Camp")
    public = TestClient(client.app)
    res = public.get(f"/api/s/{folder_token}")
    assert res.status_code == 404
    assert "secret" not in res.text
    assert public.get(f"/api/s/{folder_token}/file", params={"path": "secret.txt"}).status_code == 404
    assert public.get(f"/api/s/{folder_token}/zip").status_code == 404


# ---------------------------------------------------------------------------
# Link previews (Open Graph) for iMessage / Slack / etc.
# ---------------------------------------------------------------------------


def _og(html: str, prop: str) -> str:
    import re

    match = re.search(rf'<meta property="{prop}" content="([^"]*)"', html)
    assert match, f"missing {prop}"
    return match.group(1)


def test_share_page_previews_file(client):
    token = client.post("/api/file-link", json={"path": "clip.mp4", "days": 7}).json()["url"].rsplit("/", 1)[-1]
    page = TestClient(client.app).get(f"/s/{token}").text
    assert _og(page, "og:title") == "clip.mp4"
    assert _og(page, "og:description") == "Video · 14 B · Shared from Drive"
    assert _og(page, "og:image") == f"https://drive.example.com/api/s/{token}/thumb"
    assert _og(page, "og:url") == f"https://drive.example.com/s/{token}"
    assert "<title>clip.mp4 · Drive</title>" in page
    assert "share.js" in page


def test_share_page_without_thumbnail_uses_app_icon(client):
    token = client.post("/api/file-link", json={"path": "notes.txt", "days": 7}).json()["url"].rsplit("/", 1)[-1]
    page = TestClient(client.app).get(f"/s/{token}").text
    assert _og(page, "og:description") == "Document · 11 B · Shared from Drive"
    assert _og(page, "og:image").startswith("https://drive.example.com/assets/apple-touch-icon.png")


def test_share_page_previews_folder(client, folder_token):
    page = TestClient(client.app).get(f"/s/{folder_token}").text
    assert _og(page, "og:title") == "Camp"
    # take.mp4 + clips/ (hidden files, #recycle and symlinks aren't listed)
    assert _og(page, "og:description") == "Folder · 2 items · Shared from Drive"


def test_share_page_preview_escapes_names(client, drive_root):
    (drive_root / 'a"<b>&.txt').write_text("x")
    token = client.post("/api/file-link", json={"path": 'a"<b>&.txt', "days": 7}).json()["url"].rsplit("/", 1)[-1]
    page = TestClient(client.app).get(f"/s/{token}").text
    assert _og(page, "og:title") == "a&quot;&lt;b&gt;&amp;.txt"
    assert '<b>' not in page.split("<body", 1)[0]


def test_share_page_preview_for_bad_links(client, secret):
    import file_links
    import main

    public = TestClient(main.app)
    page = public.get("/s/not-a-token").text
    assert _og(page, "og:title") == "Link not available"
    assert "share.js" in page

    token = file_links.mint(share="Drive", path="clip.mp4", days=1, uid=1, gid=1, name="clip.mp4")
    payload = file_links.decode(token)
    payload["exp"] = int(time.time()) - 10
    expired = file_links._ser().dumps(payload)
    assert _og(public.get(f"/s/{expired}").text, "og:title") == "This link has expired"


def test_share_link_thumbnail(client, drive_root, tmp_path, monkeypatch):
    from PIL import Image

    monkeypatch.setenv("IFD_THUMB_CACHE_DIR", str(tmp_path / "thumbs"))
    Image.new("RGB", (40, 30), "red").save(drive_root / "pic.png")
    token = client.post("/api/file-link", json={"path": "pic.png", "days": 7}).json()["url"].rsplit("/", 1)[-1]
    public = TestClient(client.app)
    res = public.get(f"/api/s/{token}/thumb")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
    assert public.get("/api/s/not-a-token/thumb").status_code == 404


def test_open_link_previews_file_from_url_only(client):
    public = TestClient(client.app)
    page = public.get("/open", params={"share": "Drive", "file": "Camp/take.mp4"}).text
    assert _og(page, "og:title") == "take.mp4"
    assert _og(page, "og:description") == "Video in Camp · Drive"
    assert "<title>take.mp4 · Drive</title>" in page
    assert "app.js" in page


def test_open_link_previews_folder(client):
    public = TestClient(client.app)
    page = public.get("/open", params={"share": "Drive", "path": "Camp/clips"}).text
    assert _og(page, "og:title") == "clips"
    assert _og(page, "og:description") == "Folder in Camp · Drive"
    root = public.get("/open", params={"share": "Drive"}).text
    assert _og(root, "og:title") == "Drive"
