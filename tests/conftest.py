"""Shared pytest fixtures."""

from __future__ import annotations

import os
import subprocess
import tkinter as tk
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
FFMPEG = REPO_ROOT / "resources" / "bin" / "ffmpeg-mac"


def _can_open_display() -> bool:
    """Return True if a Tk display can actually be opened.

    On macOS this is almost always True under a normal user session; in
    headless CI it isn't. Skip UI tests cleanly when no display is available.
    """
    if os.environ.get("WHISPER_NO_TK"):
        return False
    try:
        root = tk.Tk()
    except tk.TclError:
        return False
    root.destroy()
    return True


@pytest.fixture
def tk_root():
    """Hidden Tk root for UI construction tests.

    Yields a withdrawn root, destroys it on teardown so each test starts clean.
    Skips the test (with a clear message) when no display is available.
    """
    if not _can_open_display():
        pytest.skip("no Tk display available")
    root = tk.Tk()
    root.withdraw()
    try:
        yield root
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


# ---------------------------------------------------------------------------
# Synthetic video fixture for core.render tests (Phase 4d).
#
# Generated once per developer's machine on first use, cached at
# tests/fixtures/synthetic.mp4 (gitignored). Regen takes ~1–2s.
# Tests that consume this fixture should be marked @pytest.mark.slow.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def synthetic_video() -> Path:
    """30s, 640x480, 30fps, h264+aac mp4 with stepped audio tones.

    Visual: ffmpeg's ``testsrc`` pattern, which includes a moving
    counter — so a misaligned cut is visible by eye on playback.
    Audio: 440Hz / 880Hz / 1320Hz tones at 0–10 / 10–20 / 20–30s.
    The tone change every 10s makes mis-cuts audible, and a tone-detect
    on a slice of the output can verify the right segment landed where
    it should.
    """
    cache = FIXTURE_DIR / "synthetic.mp4"
    if cache.exists():
        return cache
    if not FFMPEG.exists():
        pytest.skip(f"ffmpeg not found at {FFMPEG}")
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(FFMPEG), "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=duration=30:size=640x480:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
        "-f", "lavfi", "-i", "sine=frequency=880:duration=10",
        "-f", "lavfi", "-i", "sine=frequency=1320:duration=10",
        "-filter_complex", "[1:a][2:a][3:a]concat=n=3:v=0:a=1[a]",
        "-map", "0:v", "-map", "[a]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        str(cache),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return cache


def _probe_duration(path: Path) -> float:
    """Read a media file's duration via PyAV (already a transitive dep)."""
    import av

    with av.open(str(path)) as container:
        return float(container.duration) / av.time_base


def _is_playable(path: Path) -> bool:
    """Run ``ffmpeg -i path -f null -`` and report whether decode succeeds."""
    if not FFMPEG.exists():
        raise RuntimeError(f"ffmpeg not found at {FFMPEG}")
    proc = subprocess.run(
        [str(FFMPEG), "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True,
    )
    return proc.returncode == 0


@pytest.fixture(scope="session")
def probe_duration():
    return _probe_duration


@pytest.fixture(scope="session")
def is_playable():
    return _is_playable


# ---------------------------------------------------------------------------
# Tiny fixture mp4 for QMediaPlayer wiring tests (Phase 5b).
#
# 2 s, 64x64, h264 video + silent aac audio. Generated on demand into the
# session tmp dir so it never lives in git. Tests skip cleanly if ffmpeg
# isn't on disk.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def tiny_mp4(tmp_path_factory) -> Path:
    """Smallest .mp4 that exercises QMediaPlayer's video + audio path."""
    if not FFMPEG.exists():
        pytest.skip(f"ffmpeg not found at {FFMPEG}")
    out = tmp_path_factory.mktemp("media") / "tiny.mp4"
    cmd = [
        str(FFMPEG), "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=duration=2:size=64x64:rate=10",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
        "-t", "2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "32k",
        "-shortest",
        str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return out
