"""Framework-agnostic background work shared by both UIs.

The package was created in Phase 5a to unblock the second (PySide6) UI:
the transcription worker that previously lived as a method on
:class:`ui.app.App` is here as :class:`workers.transcription.TranscriptionWorker`,
and the worker→UI event dataclasses are here as :mod:`workers.events`.

Neither module imports anything UI-framework-specific. The tkinter and
Qt UIs each provide their own thin adapter (callback or signal) around
the same worker.
"""

from __future__ import annotations
