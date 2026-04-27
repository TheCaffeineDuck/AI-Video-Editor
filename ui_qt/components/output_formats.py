"""Checkbox row for selecting which output formats to write."""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ui_qt.style import muted_label_qss

# Order matters for display.
ALL_FORMATS: tuple[tuple[str, str], ...] = (
    ("txt", "Text (.txt)"),
    ("srt", "Subtitles (.srt)"),
    ("vtt", "WebVTT (.vtt)"),
    ("json", "Editable project (.transcribe.json)"),
)


class OutputFormatPicker(QWidget):
    """Row of checkboxes plus a small nudge label about the JSON format."""

    formats_changed = Signal(list)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        initial: Iterable[str] = ("txt", "srt", "json"),
    ) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        row = QHBoxLayout()
        row.addWidget(QLabel("Output:"))
        self._checkboxes: dict[str, QCheckBox] = {}
        chosen = set(initial)
        for fmt, display in ALL_FORMATS:
            cb = QCheckBox(display)
            cb.setChecked(fmt in chosen)
            cb.toggled.connect(self._fire)
            row.addWidget(cb)
            self._checkboxes[fmt] = cb
        row.addStretch()
        outer.addLayout(row)

        nudge = QLabel("JSON is required for the upcoming editor view.")
        nudge.setStyleSheet(muted_label_qss())
        outer.addWidget(nudge)

    def _fire(self) -> None:
        self.formats_changed.emit(self.formats)

    @property
    def formats(self) -> list[str]:
        return [fmt for fmt, cb in self._checkboxes.items() if cb.isChecked()]

    @property
    def has_selection(self) -> bool:
        return any(cb.isChecked() for cb in self._checkboxes.values())

    def set_formats(self, formats: Iterable[str]) -> None:
        chosen = set(formats)
        for fmt, cb in self._checkboxes.items():
            with _BlockedSignals(cb):
                cb.setChecked(fmt in chosen)
        self._fire()


class _BlockedSignals:
    """Context manager around ``QObject.blockSignals``."""

    def __init__(self, obj):
        self._obj = obj

    def __enter__(self):
        self._prev = self._obj.blockSignals(True)
        return self._obj

    def __exit__(self, *_exc):
        self._obj.blockSignals(self._prev)
