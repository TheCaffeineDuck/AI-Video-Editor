"""About dialog (Phase 5f).

Triggered from the macOS application menu via a ``QAction`` with
``MenuRole.AboutRole``. Reports app name, version, a one-line
description, and the runtime versions of the main third-party deps
(Python, PySide6, faster-whisper) plus ffmpeg's first ``-version`` line.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

import ui_qt as _ui_qt_pkg

APP_VERSION = _ui_qt_pkg.__version__


def _pyside_version() -> str:
    try:
        import PySide6  # noqa: PLC0415

        return str(getattr(PySide6, "__version__", "?"))
    except Exception:  # pragma: no cover - import always succeeds in this app
        return "?"


def _faster_whisper_version() -> str:
    try:
        import faster_whisper  # noqa: PLC0415

        return str(getattr(faster_whisper, "__version__", "?"))
    except Exception:
        return "(unavailable)"


def _ffmpeg_version() -> str:
    """Parse ffmpeg's first ``-version`` line. Returns ``"(missing)"`` if absent."""
    binary = shutil.which("ffmpeg")
    if not binary:
        return "(missing)"
    try:
        out = subprocess.run(
            [binary, "-version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "(unreadable)"
    first_line = (out.stdout or out.stderr or "").splitlines()
    return first_line[0].strip() if first_line else "(empty)"


def about_text() -> str:
    """Compose the body text. Public so tests can sanity-check the format."""
    py = "{}.{}.{}".format(*sys.version_info[:3])
    return (
        f"Whisper Transcriber v{APP_VERSION}\n"
        f"\n"
        f"Offline desktop transcription and editing powered by\n"
        f"faster-whisper and smartcut.\n"
        f"\n"
        f"Python {py} on {platform.platform(terse=True)}\n"
        f"PySide6 {_pyside_version()}\n"
        f"faster-whisper {_faster_whisper_version()}\n"
        f"{_ffmpeg_version()}\n"
        f"\n"
        f"© Aaron Ramos. MIT-licensed."
    )


class AboutDialog(QDialog):
    """Modal "About Transcribe" sheet."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About Transcribe")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        body = QLabel(about_text())
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.setWordWrap(True)
        layout.addWidget(body)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
