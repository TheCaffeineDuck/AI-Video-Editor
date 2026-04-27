"""Smoke-test QMediaPlayer against a directory of media files.

Usage::

    .venv/bin/python scripts/qt_codec_smoke.py /path/to/corpus

For each file under the directory whose extension is in
``SUPPORTED_EXTENSIONS`` from :mod:`ui.state`, instantiate a
:class:`QMediaPlayer`, point it at the file, and run a local
:class:`QEventLoop` until ``mediaStatusChanged`` reaches
``LoadedMedia``, ``InvalidMedia``, or ``EndOfMedia`` (or 10 s timeout).

Output is one line per file::

    PASS /abs/path/to/file.mp4
    FAIL /abs/path/to/file.mov: <errorString>

This is the evidence input to Decision 9 — whether QMediaPlayer is
sufficient or we need to swap in ``python-vlc``. Do not ship a
fallback in 5b; capture the data here and decide in 5c.

Exit code is 0 when at least one file was tested, regardless of
PASS/FAIL count — the caller is reading the output, not the status.
Returns 2 if the directory is empty or absent.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as ``python scripts/qt_codec_smoke.py`` from the repo root
# without a `pip install -e .` first.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PySide6.QtCore import QEventLoop, QTimer, QUrl  # noqa: E402
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui.state import SUPPORTED_EXTENSIONS  # noqa: E402

# Only video/audio containers Qt is plausibly responsible for.
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".m4a", ".mp3", ".wav"}

_TIMEOUT_MS = 10_000


def _scan(root: Path) -> list[Path]:
    """Return supported media files under ``root`` (recursive), sorted."""
    if not root.is_dir():
        return []
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() in (SUPPORTED_EXTENSIONS & _VIDEO_EXTENSIONS):
            out.append(p)
    return sorted(out)


def _probe(path: Path) -> tuple[bool, str]:
    """Return ``(loaded, message)`` for a single file."""
    player = QMediaPlayer()
    audio = QAudioOutput()
    player.setAudioOutput(audio)

    loop = QEventLoop()
    result: dict[str, object] = {"status": None, "error": ""}

    def on_status_changed(status: QMediaPlayer.MediaStatus) -> None:
        # End once we've reached a terminal state.
        terminal = (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.InvalidMedia,
            QMediaPlayer.MediaStatus.NoMedia,
            QMediaPlayer.MediaStatus.EndOfMedia,
        )
        if status in terminal:
            result["status"] = status
            loop.quit()

    def on_error(error: QMediaPlayer.Error, message: str) -> None:
        if error != QMediaPlayer.Error.NoError:
            result["error"] = message or str(error)

    player.mediaStatusChanged.connect(on_status_changed)
    player.errorOccurred.connect(on_error)

    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(_TIMEOUT_MS)

    player.setSource(QUrl.fromLocalFile(str(path)))

    loop.exec()
    timer.stop()

    status = result["status"]
    if status == QMediaPlayer.MediaStatus.LoadedMedia:
        outcome: tuple[bool, str] = (True, "")
    elif status is None:
        outcome = (False, f"timeout after {_TIMEOUT_MS // 1000} s (status never settled)")
    else:
        msg = str(result["error"]) or f"non-loaded status: {status.name}"
        outcome = (False, msg)

    player.stop()
    player.setSource(QUrl())
    player.setVideoOutput(None)
    player.setAudioOutput(None)
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", type=Path, help="folder to scan recursively")
    args = parser.parse_args(argv)

    files = _scan(args.directory)
    if not files:
        print(f"no media files under {args.directory}", file=sys.stderr)
        return 2

    # Need a QApplication for QMediaPlayer + QEventLoop to function.
    app = QApplication.instance() or QApplication([])
    _ = app  # silence unused

    pass_count = 0
    fail_count = 0
    for path in files:
        ok, msg = _probe(path)
        if ok:
            print(f"PASS {path}")
            pass_count += 1
        else:
            print(f"FAIL {path}: {msg}")
            fail_count += 1

    print(f"--- {pass_count} pass, {fail_count} fail, {len(files)} total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
