"""Range splitting for parallel downloads (mirrors api.js splitByteRanges)."""

from __future__ import annotations


def split_byte_ranges(total: int, max_streams: int = 8, min_chunk: int = 4 * 1024 * 1024) -> list[tuple[int, int]]:
    size = max(0, int(total or 0))
    if size <= 0:
        return []
    n = min(max(1, max_streams), max(1, (size + min_chunk - 1) // min_chunk))
    parts: list[tuple[int, int]] = []
    start = 0
    for i in range(n):
        remaining = n - i
        left = size - start
        take = left if i == n - 1 else left // remaining
        end = start + take - 1
        parts.append((start, end))
        start = end + 1
    return parts


def test_empty():
    assert split_byte_ranges(0) == []


def test_small_file_one_part():
    parts = split_byte_ranges(100)
    assert parts == [(0, 99)]


def test_covers_exactly():
    size = 25 * 1024 * 1024
    parts = split_byte_ranges(size)
    assert parts[0][0] == 0
    assert parts[-1][1] == size - 1
    covered = sum(end - start + 1 for start, end in parts)
    assert covered == size
    for i in range(1, len(parts)):
        assert parts[i][0] == parts[i - 1][1] + 1


def test_caps_at_max_streams():
    parts = split_byte_ranges(200 * 1024 * 1024, max_streams=8)
    assert len(parts) == 8
