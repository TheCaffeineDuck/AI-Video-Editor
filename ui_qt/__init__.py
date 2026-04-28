"""PySide6 UI for Whisper Transcriber.

Sibling to :mod:`ui` (the legacy customtkinter UI). Phase 5a is feature-
parity with the customtkinter app: drop a media file, choose model /
language / output formats, transcribe, see result paths. Phase 5f
ships the macOS-native polish — menu bar, quit guard, icon, About —
that turns "a Qt window that runs on Mac" into a proper Mac app.
"""

from __future__ import annotations

# Phase 5f — single source of truth for the version string. Surfaced in
# the About dialog and the Settings dialog's About label. Bump together
# with `pyproject.toml` on a real release.
__version__ = "0.5.0"
