"""Progress card shown while a transcription is running.

Same interaction model as :class:`ui.components.progress_card.ProgressCard`:
a label, a determinate bar, an elapsed/remaining timing line, a small
streaming-text preview, and a Cancel button. Signals out a single
``cancel_requested`` event on click.
"""

from __future__ import annotations

import time

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ui_qt.style import muted_label_qss

_MAX_STREAM_LINES = 3


class ProgressCard(QFrame):
    cancel_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        layout = QVBoxLayout(self)

        self._label = QLabel("Preparing…")
        font = self._label.font()
        font.setPointSize(16)
        font.setBold(True)
        self._label.setFont(font)
        layout.addWidget(self._label)

        self._bar = QProgressBar()
        self._bar.setRange(0, 1000)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        layout.addWidget(self._bar)

        self._timing = QLabel("")
        self._timing.setStyleSheet(muted_label_qss())
        layout.addWidget(self._timing)

        self._stream = QTextEdit()
        self._stream.setReadOnly(True)
        self._stream.setFixedHeight(80)
        layout.addWidget(self._stream)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self._handle_cancel)
        layout.addWidget(self._cancel_btn)

        layout.addStretch()

        self._start_time: float | None = None
        self._last_progress: float = 0.0

    def reset(self) -> None:
        self._start_time = time.monotonic()
        self._last_progress = 0.0
        self._label.setText("Preparing…")
        self._bar.setValue(0)
        self._timing.setText("")
        self._stream.setPlainText("")
        self._cancel_btn.setEnabled(True)
        self._cancel_btn.setText("Cancel")

    def set_label(self, text: str) -> None:
        self._label.setText(text)

    def set_progress(self, fraction: float, label: str | None = None) -> None:
        fraction = max(0.0, min(1.0, fraction))
        self._last_progress = fraction
        self._bar.setValue(int(fraction * 1000))
        if label:
            self._label.setText(label)
        self._update_timing()

    def append_stream(self, text: str) -> None:
        existing = self._stream.toPlainText().splitlines()
        existing.append(text.strip())
        existing = [line for line in existing if line][-_MAX_STREAM_LINES:]
        self._stream.setPlainText("\n".join(existing))

    def _handle_cancel(self) -> None:
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.setText("Cancelling…")
        self.cancel_requested.emit()

    def _update_timing(self) -> None:
        if self._start_time is None:
            return
        elapsed = time.monotonic() - self._start_time
        if self._last_progress > 0.01:
            total = elapsed / self._last_progress
            remaining = max(0.0, total - elapsed)
            self._timing.setText(
                f"Elapsed {elapsed:.0f}s   ·   ~{remaining:.0f}s remaining"
            )
        else:
            self._timing.setText(f"Elapsed {elapsed:.0f}s")
