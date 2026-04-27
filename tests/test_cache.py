"""Tests for ``core.cache.cache_key``."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.cache import cache_key


def test_cache_key_is_stable_for_unchanged_file(tmp_path: Path):
    media = tmp_path / "a.wav"
    media.write_bytes(b"hello")
    k1 = cache_key(media)
    k2 = cache_key(media)
    assert k1 == k2
    # sha256 hex is 64 chars.
    assert len(k1) == 64
    assert all(c in "0123456789abcdef" for c in k1)


def test_cache_key_changes_when_path_changes(tmp_path: Path):
    a = tmp_path / "a.wav"
    a.write_bytes(b"hello")
    b = tmp_path / "b.wav"
    b.write_bytes(b"hello")
    # Same content + same size + (typically) same mtime, but different path
    # → keys differ. Path is part of the key intentionally so a moved or
    # renamed file invalidates the cache.
    assert cache_key(a) != cache_key(b)


def test_cache_key_changes_when_size_changes(tmp_path: Path):
    media = tmp_path / "a.wav"
    media.write_bytes(b"hello")
    k1 = cache_key(media)
    media.write_bytes(b"hello world")
    # Set mtime back to the original to isolate the size component.
    st = media.stat()
    os.utime(media, (st.st_atime, st.st_mtime - 10))  # hold mtime constant
    media.write_bytes(b"hello world!!")
    os.utime(media, (st.st_atime, st.st_mtime - 10))
    k2 = cache_key(media)
    assert k1 != k2


def test_cache_key_changes_when_mtime_changes(tmp_path: Path):
    media = tmp_path / "a.wav"
    media.write_bytes(b"hello")
    k1 = cache_key(media)
    # Bump mtime by ten seconds; size and path stay the same.
    st = media.stat()
    os.utime(media, (st.st_atime, st.st_mtime + 10.0))
    k2 = cache_key(media)
    assert k1 != k2


def test_cache_key_raises_on_missing_path(tmp_path: Path):
    missing = tmp_path / "does_not_exist.wav"
    with pytest.raises(FileNotFoundError):
        cache_key(missing)
