"""Audio helpers: ffmpeg path resolution, duration probing, WAV extraction."""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

# Repo root in dev mode is one level up from this file (core/audio.py -> ../).
_DEV_REPO_ROOT = Path(__file__).resolve().parent.parent
_BIN_NAMES = {
    "Darwin": "ffmpeg-mac",
    "Linux": "ffmpeg-linux",
    "Windows": "ffmpeg-win.exe",
}


def _bundled_root() -> Path:
    """Return the directory that contains ``resources/``.

    When running under PyInstaller the bundle exposes its data dir via
    ``sys._MEIPASS``. In dev we use the repo root.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return _DEV_REPO_ROOT


def get_ffmpeg_path() -> Path:
    """Return the absolute path to the bundled ffmpeg binary for this platform."""
    system = platform.system()
    try:
        name = _BIN_NAMES[system]
    except KeyError as e:
        raise RuntimeError(f"unsupported platform: {system}") from e
    return _bundled_root() / "resources" / "bin" / name


class FFmpegError(RuntimeError):
    """Raised when ffmpeg/ffprobe-equivalent calls fail."""


def get_duration(media_path: Path) -> float:
    """Return media duration in seconds via ``ffmpeg -i`` JSON-of-format probe.

    We don't ship ffprobe (only ffmpeg) so we use ``-show_format`` style isn't
    available. Instead we let ffmpeg parse the file with no output muxer and
    read the ``Duration`` line from stderr.
    """
    ffmpeg = get_ffmpeg_path()
    if not media_path.is_file():
        raise FileNotFoundError(media_path)
    result = subprocess.run(
        [str(ffmpeg), "-i", str(media_path), "-f", "null", "-"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    # ffmpeg writes everything informational to stderr regardless of success.
    return _parse_duration_from_stderr(result.stderr, media_path)


def _parse_duration_from_stderr(stderr: str, media_path: Path) -> float:
    for line in stderr.splitlines():
        stripped = line.strip()
        if stripped.startswith("Duration:"):
            ts = stripped.split(",", 1)[0].split(":", 1)[1].strip()
            if ts.upper() == "N/A":
                raise FFmpegError(f"ffmpeg could not determine duration for {media_path}")
            try:
                hh, mm, ss = ts.split(":")
                return int(hh) * 3600 + int(mm) * 60 + float(ss)
            except ValueError as e:
                raise FFmpegError(f"could not parse duration {ts!r}") from e
    raise FFmpegError(f"ffmpeg produced no Duration line for {media_path}")


def extract_wav_16k_mono(input_path: Path, output_path: Path) -> Path:
    """Extract a 16 kHz mono PCM WAV from ``input_path`` into ``output_path``.

    Returns the output path. Overwrites any existing file at ``output_path``.
    """
    ffmpeg = get_ffmpeg_path()
    if not input_path.is_file():
        raise FileNotFoundError(input_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(ffmpeg),
        "-y",
        "-i",
        str(input_path),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-acodec",
        "pcm_s16le",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise FFmpegError(
            f"ffmpeg failed (rc={result.returncode}) extracting {input_path}: "
            f"{result.stderr[-400:]}"
        )
    return output_path


def probe(media_path: Path) -> dict[str, object]:
    """Return a small dict of useful metadata. Currently just duration.

    Kept as a dict so we can extend it (codec, sample rate, channels) without
    breaking callers.
    """
    return {
        "path": str(media_path),
        "duration": get_duration(media_path),
    }
