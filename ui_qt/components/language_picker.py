"""Searchable combo box for picking a transcription language."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QWidget

from core.languages import LANGUAGE_OPTIONS, name_for


class LanguagePicker(QWidget):
    """Label + editable combo box with type-to-search filtering.

    QComboBox's built-in completer handles substring search well enough
    that we don't need to roll a separate dialog (unlike the tkinter side,
    which had no native combo with completion).
    """

    language_changed = Signal(object)  # str | None

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        initial_code: str | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Language:"))

        self._combo = QComboBox()
        self._combo.setEditable(True)
        self._combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._combo.setMinimumWidth(220)
        self._codes: list[str | None] = []
        for code, display in LANGUAGE_OPTIONS:
            self._combo.addItem(display, userData=code)
            self._codes.append(code)
        completer = self._combo.completer()
        if completer is not None:
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.set_code(initial_code)
        self._combo.currentIndexChanged.connect(self._emit_change)
        layout.addWidget(self._combo)
        layout.addStretch()

    def _emit_change(self) -> None:
        self.language_changed.emit(self.selected_code)

    @property
    def selected_code(self) -> str | None:
        return self._combo.currentData()

    def set_code(self, code: str | None) -> None:
        for idx, c in enumerate(self._codes):
            if c == code:
                self._combo.setCurrentIndex(idx)
                return
        # Fall back to the auto-detect entry (code is None).
        self._combo.setEditText(name_for(code))
