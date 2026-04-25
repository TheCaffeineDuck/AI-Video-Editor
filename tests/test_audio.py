"""Tests for core.audio."""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

import pytest

from core import audio

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE = REPO_ROOT / "tests" / "fixtures" / "sample.wav"


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_get_ffmpeg_path_resolves_in_dev_mode(monkeypatch):
    # Ensure no _MEIPASS leak from a previous test.
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    path = audio.get_ffmpeg_path()
    assert path == REPO_ROOT / "resources" / "bin" / "ffmpeg-mac"
    assert path.is_file()
    assert os.access(path, os.X_OK)


def test_get_ffmpeg_path_uses_meipass_when_set(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    path = audio.get_ffmpeg_path()
    assert path == tmp_path / "resources" / "bin" / "ffmpeg-mac"


def test_get_ffmpeg_path_unsupported_platform(monkeypatch):
    monkeypatch.setattr(audio.platform, "system", lambda: "Plan9")
    with pytest.raises(RuntimeError):
        audio.get_ffmpeg_path()


# ---------------------------------------------------------------------------
# Duration probing
# ---------------------------------------------------------------------------


def test_get_duration_on_fixture():
    duration = audio.get_duration(SAMPLE)
    # Fixture is 6.10s per ffmpeg; allow ±0.2s tolerance per spec.
    assert duration == pytest.approx(6.10, abs=0.2)


def test_get_duration_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        audio.get_duration(tmp_path / "does_not_exist.wav")


def test_parse_duration_from_stderr_raw():
    stderr = (
        "Input #0, wav, from 'foo':\n"
        "  Duration: 00:01:02.50, start: 0.000000, bitrate: 256 kb/s\n"
        "    Stream #0:0: Audio\n"
    )
    assert audio._parse_duration_from_stderr(stderr, Path("foo")) == pytest.approx(62.5)


def test_parse_duration_no_line_raises():
    with pytest.raises(audio.FFmpegError):
        audio._parse_duration_from_stderr("nothing useful here\n", Path("foo"))


def test_parse_duration_n_a_raises():
    stderr = "  Duration: N/A, start: 0.0, bitrate: N/A\n"
    with pytest.raises(audio.FFmpegError):
        audio._parse_duration_from_stderr(stderr, Path("foo"))


# ---------------------------------------------------------------------------
# WAV extraction
# ---------------------------------------------------------------------------


def test_extract_wav_16k_mono_produces_valid_wav(tmp_path: Path):
    out = tmp_path / "out.wav"
    audio.extract_wav_16k_mono(SAMPLE, out)
    assert out.is_file()
    with out.open("rb") as f:
        header = f.read(44)
    assert header[0:4] == b"RIFF"
    assert header[8:12] == b"WAVE"
    num_channels = struct.unpack("<H", header[22:24])[0]
    sample_rate = struct.unpack("<I", header[24:28])[0]
    assert num_channels == 1
    assert sample_rate == 16000


def test_extract_wav_creates_parent_dirs(tmp_path: Path):
    out = tmp_path / "nested" / "child" / "out.wav"
    audio.extract_wav_16k_mono(SAMPLE, out)
    assert out.is_file()


def test_extract_wav_missing_input_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        audio.extract_wav_16k_mono(tmp_path / "missing.mp4", tmp_path / "out.wav")


def test_extract_wav_overwrites_existing_output(tmp_path: Path):
    out = tmp_path / "out.wav"
    out.write_bytes(b"old")
    audio.extract_wav_16k_mono(SAMPLE, out)
    assert out.read_bytes()[:4] == b"RIFF"


def test_probe_returns_duration():
    info = audio.probe(SAMPLE)
    assert "duration" in info
    assert info["duration"] == pytest.approx(6.10, abs=0.2)
    assert info["path"] == str(SAMPLE)
