"""Status-bar widgets for MainWindow (Phase 5f).

The pieces that used to live as modal dialogs on EditorPane move here
so the editor stays interactive during long operations:

- :class:`RenderStatusWidget` — non-modal "Rendering… [▓▓▓░░░] [Cancel]"
  surfaced while a render is in flight. Hidden when idle. Inserted as
  a permanent widget on :class:`QStatusBar` so it sits on the right.
- :class:`AutosaveStatusLabel` — a debounced :class:`QLabel` that flips
  between "Saved", "Saving…", "Unsaved changes", and "Autosave failed".
  Hidden when autosave is off and the document is clean.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QWidget,
)


class RenderStatusWidget(QWidget):
    """Non-modal render progress strip.

    Shown when a render starts; updates from ``setProgress`` calls in
    response to ``RenderProgress`` events; hidden on completion / error /
    cancel. The Cancel button emits :pyattr:`cancel_clicked`; the host
    is responsible for forwarding to the editor's ``cancel_render``.
    """

    cancel_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(8)

        self._label = QLabel("")
        self._label.setStyleSheet("color: #6B7280;")
        layout.addWidget(self._label)

        self._bar = QProgressBar()
        # Indeterminate by default — ``core.render.render_cut`` only
        # emits progress sporadically. ``setProgress`` flips it to a
        # real range on first numeric tick.
        self._bar.setRange(0, 0)
        self._bar.setMaximumWidth(160)
        self._bar.setMaximumHeight(14)
        self._bar.setTextVisible(False)
        layout.addWidget(self._bar)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setFlat(True)
        self._cancel_btn.clicked.connect(self.cancel_clicked.emit)
        layout.addWidget(self._cancel_btn)

        self.hide()

    def start(self, label: str = "Rendering…") -> None:
        self._label.setText(label)
        self._bar.setRange(0, 0)
        self._bar.setValue(0)
        self._cancel_btn.setEnabled(True)
        self._cancel_btn.setText("Cancel")
        self.show()

    def set_progress(self, fraction: float) -> None:
        if self._bar.maximum() == 0:
            self._bar.setRange(0, 100)
        self._bar.setValue(int(max(0.0, min(1.0, fraction)) * 100))

    def mark_cancelling(self) -> None:
        """Render-cancel was requested; lock the button while smartcut unwinds."""
        self._label.setText("Cancelling…")
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.setText("Cancelling…")

    def finish(self) -> None:
        self.hide()
        self._label.setText("")
        self._bar.setRange(0, 0)
        self._bar.setValue(0)


# Autosave indicator state codes — kept as bare strings to avoid pulling
# in an enum just for four labels.
AUTOSAVE_SAVED = "saved"
AUTOSAVE_SAVING = "saving"
AUTOSAVE_DIRTY = "dirty"
AUTOSAVE_FAILED = "failed"


class AutosaveStatusLabel(QLabel):
    """Compact autosave indicator that lives on the left of the status bar.

    States and copy:
    - ``saved``  → "Saved"  (or "Saved 2m ago" when ``stamp_age_minutes`` is set)
    - ``saving`` → "Saving…"
    - ``dirty``  → "Unsaved changes"  (only when autosave is off)
    - ``failed`` → "Autosave failed"

    The label is debounced to coalesce rapid state churn — flipping
    saving→saved→saving on a quick cut+autosave-tick burst would otherwise
    flicker. ``set_state`` records the desired state but defers actually
    repainting the text by 80 ms; another call inside that window
    replaces the pending state.
    """

    _DEBOUNCE_MS = 80

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        self.setStyleSheet("color: #6B7280; padding: 0 8px;")
        self._state: str = AUTOSAVE_SAVED
        self._pending: str | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self._DEBOUNCE_MS)
        self._timer.timeout.connect(self._flush)
        self.hide()

    @property
    def state(self) -> str:
        return self._state

    def set_state(self, state: str) -> None:
        self._pending = state
        self._timer.start()

    def _flush(self) -> None:
        target = self._pending
        if target is None:
            return
        self._pending = None
        self._state = target
        self._render()

    def _render(self) -> None:
        if self._state == AUTOSAVE_SAVED:
            self.setText("Saved")
            self.show()
        elif self._state == AUTOSAVE_SAVING:
            self.setText("Saving…")
            self.show()
        elif self._state == AUTOSAVE_DIRTY:
            self.setText("Unsaved changes")
            self.show()
        elif self._state == AUTOSAVE_FAILED:
            self.setText("Autosave failed")
            self.show()
        else:  # idle / unknown — hide
            self.clear()
            self.hide()

    def clear_indicator(self) -> None:
        """Cancel any pending state change and hide the label outright."""
        self._timer.stop()
        self._pending = None
        self._state = ""
        self.clear()
        self.hide()
