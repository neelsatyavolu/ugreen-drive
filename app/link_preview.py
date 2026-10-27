"""Open Graph tags so a pasted Drive link previews in iMessage, Slack, etc."""

from __future__ import annotations

import re
from html import escape
from pathlib import PurePosixPath

ICON = "/assets/apple-touch-icon.png"
MAX_NAME = 200

_KIND_LABELS = {"image": "Image", "video": "Video", "audio": "Audio", "pdf": "PDF", "text": "Document"}
_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")
_TITLE_RE = re.compile(r"<title>.*?</title>", re.S)


def human_size(size: int) -> str:
    """Same format as format.js formatSize, e.g. 4.71 GB / 212 MB / 38 KB."""
    if size < 1024:
        return f"{size} B"
    value, unit = float(size), 0
    while value >= 1024 and unit < len(_UNITS) - 1:
        value /= 1024
        unit += 1
    decimals = 0 if value >= 100 else 1 if value >= 10 else 2
    return f"{value:.{decimals}f} {_UNITS[unit]}"


def kind_label(kind: str | None) -> str:
    return _KIND_LABELS.get(kind or "", "File")


def item_count(count: int) -> str:
    return f"{count} item" if count == 1 else f"{count} items"


def base_name(path: str) -> str:
    """Last segment of a share-relative path ('' for the share root)."""
    return PurePosixPath("/" + (path or "").strip("/")).name[:MAX_NAME]


def parent_name(path: str) -> str:
    return base_name(str(PurePosixPath("/" + (path or "").strip("/")).parent))


def inject(page: str, *, title: str, description: str, url: str, image: str, site: str) -> str:
    """Return `page` with its <title> replaced and Open Graph tags added to <head>."""
    tags = {
        "og:site_name": site,
        "og:type": "website",
        "og:title": title,
        "og:description": description,
        "og:url": url,
        "og:image": image,
    }
    meta = "".join(
        f'  <meta property="{prop}" content="{escape(value)}" />\n' for prop, value in tags.items()
    )
    meta += f'  <meta name="description" content="{escape(description)}" />\n'
    page_title = f"<title>{escape(title if title == site else f'{title} · {site}')}</title>"
    page = _TITLE_RE.sub(lambda _match: page_title, page, count=1)
    return page.replace("</head>", meta + "</head>", 1)
