"""Phase 0 smoke tests: project structure, deps, ffmpeg binary, sample fixture."""

from __future__ import annotations

import os
import struct
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FFMPEG_PATH = REPO_ROOT / "resources" / "bin" / "ffmpeg-mac"
SAMPLE_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "sample.wav"


def test_third_party_imports():
    """Pinned third-party deps are importable."""
    import customtkinter  # noqa: F401
    import faster_whisper  # noqa: F401
    import huggingface_hub  # noqa: F401
    import tkinterdnd2  # noqa: F401


def test_planned_module_paths_importable_when_present():
    """Smoke-test every planned module path; skip with importorskip if not yet implemented."""
    pytest.importorskip("core")
    pytest.importorskip("ui")
    # The following modules are implemented in later phases. Use importorskip so
    # this test stays green during Phase 0 but starts asserting once they exist.
    pytest.importorskip("core.exporters")
    pytest.importorskip("core.models")
    pytest.importorskip("core.audio")
    pytest.importorskip("core.transcriber")
    pytest.importorskip("ui.theme")
    pytest.importorskip("ui.app")
    pytest.importorskip("ui.components.drop_zone")
    pytest.importorskip("ui.components.model_picker")
    pytest.importorskip("ui.components.progress_card")
    pytest.importorskip("ui.components.result_card")


def test_ffmpeg_binary_exists():
    assert FFMPEG_PATH.is_file(), f"ffmpeg binary missing at {FFMPEG_PATH}"


def test_ffmpeg_binary_is_executable():
    assert os.access(FFMPEG_PATH, os.X_OK), f"ffmpeg binary not executable: {FFMPEG_PATH}"


def test_ffmpeg_runs():
    result = subprocess.run(
        [str(FFMPEG_PATH), "-version"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "ffmpeg version" in result.stdout.lower()


def test_sample_fixture_exists():
    assert SAMPLE_FIXTURE.is_file(), f"Sample fixture missing at {SAMPLE_FIXTURE}"


def test_sample_fixture_is_valid_wav():
    """Verify the fixture has a RIFF/WAVE header."""
    with SAMPLE_FIXTURE.open("rb") as f:
        riff = f.read(4)
        f.read(4)  # skip chunk size
        wave = f.read(4)
    assert riff == b"RIFF"
    assert wave == b"WAVE"


def test_sample_fixture_size_under_200kb():
    size = SAMPLE_FIXTURE.stat().st_size
    assert size < 200 * 1024, f"Fixture is {size} bytes, expected < 200 KB"


def test_sample_fixture_is_16khz_mono():
    """Parse the fmt chunk and check sample rate / channel count."""
    with SAMPLE_FIXTURE.open("rb") as f:
        header = f.read(44)
    # WAV fmt chunk layout (PCM): bytes 22-23 = num channels, 24-27 = sample rate.
    num_channels = struct.unpack("<H", header[22:24])[0]
    sample_rate = struct.unpack("<I", header[24:28])[0]
    assert num_channels == 1, f"expected mono, got {num_channels} channels"
    assert sample_rate == 16000, f"expected 16000 Hz, got {sample_rate}"
