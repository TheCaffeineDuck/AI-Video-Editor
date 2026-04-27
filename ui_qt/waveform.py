"""Waveform strip placeholder.

Phase 5b reserves vertical space for the waveform-and-cut-region strip
that lands in 5d (Decision 5: navigation + silence-spotting only,
click-to-seek, no drag handles). The body here is intentionally minimal
— `paintEvent` fills the rect with a palette mid-tone so it reads as
"placeholder, not unrendered".

5d will replace this widget; the EditorPane API contract is just
``setMinimumHeight(64)`` and "is a QWidget" — anything else is internal.
"""

from __future__ import annotations

from PySide6.QtGui import QPainter, QPaintEvent
from PySide6.QtWidgets import QWidget


class WaveformPlaceholder(QWidget):
    """Placeholder strip occupying the slot reserved for the waveform widget."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(64)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().mid())
        painter.end()
