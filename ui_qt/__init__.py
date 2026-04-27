"""PySide6 UI for Whisper Transcriber.

Sibling to :mod:`ui` (the legacy customtkinter UI). Phase 5a is feature-
parity with the customtkinter app: drop a media file, choose model /
language / output formats, transcribe, see result paths. The editor view
arrives in Phase 5b. Both UIs run against the same :mod:`workers`
backend and share :mod:`core.settings` on disk.
"""

from __future__ import annotations
