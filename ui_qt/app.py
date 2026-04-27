"""PySide6 main window. Owns settings, state machine, worker, and pane swap.

Phase 5b restructured the window: the transcribe-flow widgets moved
into :class:`ui_qt.transcribe_pane.TranscribePane`, and the new
:class:`ui_qt.editor_pane.EditorPane` lives behind ``show_editor()``.
The window swaps between the two via ``setCentralWidget`` (Decision 4
— single window, state swap, no second window).

The pump is a QTimer rather than ``root.after`` (Qt has no ``.after``)
but the underlying queue and worker events are identical to the
customtkinter side. The worker emits :class:`workers.events.WorkerEvent`
values into a plain ``queue.Queue``; the timer drains it and
dispatches to the state machine and the visible pane.

State management piggy-backs on :class:`ui.state.AppStateMachine` —
framework-free; only the rendering side differs between the two UIs.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QToolBar,
    QWidget,
)

from core.document import Document, UnsupportedSchemaError
from core.settings import Settings, load_settings
from ui.state import (
    AppState,
    AppStateMachine,
    pump_queue,
)
from ui_qt.components.settings_panel import SettingsDialog
from ui_qt.editor_pane import EditorPane
from ui_qt.style import (
    WINDOW_DEFAULT_SIZE,
    WINDOW_MIN_SIZE,
    WINDOW_TITLE,
)
from ui_qt.transcribe_pane import TranscribePane
from workers.transcription import TranscriptionWorker

PUMP_INTERVAL_MS = 100
_LOG = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Top-level window. Owns settings, state, the worker, and the pane swap."""

    def __init__(self, *, settings: Settings | None = None) -> None:
        super().__init__()
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(*WINDOW_DEFAULT_SIZE)
        self.setMinimumSize(*WINDOW_MIN_SIZE)

        self.settings = settings or load_settings()
        self.state = AppStateMachine()
        self.event_queue: queue.Queue = queue.Queue()
        self._worker: TranscriptionWorker | None = None
        self._worker_thread: threading.Thread | None = None
        self._cancel_requested = threading.Event()

        self._transcribe_pane: TranscribePane | None = None
        self._editor_pane: EditorPane | None = None

        self._build_toolbar()
        self.setStatusBar(QStatusBar(self))

        self.show_transcribe()

        self.state.on_change(self._render_for_state)

        self._pump_timer = QTimer(self)
        self._pump_timer.setInterval(PUMP_INTERVAL_MS)
        self._pump_timer.timeout.connect(self.pump_once)
        self._pump_timer.start()

    # ----- accessors retained for tests + EditorPane swap detection -----

    @property
    def transcribe_pane(self) -> TranscribePane | None:
        return self._transcribe_pane

    @property
    def editor_pane(self) -> EditorPane | None:
        return self._editor_pane

    # ----- toolbar -----

    def _build_toolbar(self) -> None:
        bar = QToolBar()
        bar.setMovable(False)
        spacer = QWidget()
        spacer.setSizePolicy(spacer.sizePolicy().horizontalPolicy(),
                             spacer.sizePolicy().verticalPolicy())
        bar.addWidget(spacer)
        gear = QPushButton("Settings")
        gear.setFlat(True)
        gear.clicked.connect(self._open_settings)
        bar.addWidget(gear)
        self.addToolBar(bar)

    # ----- pane swap -----

    def show_transcribe(self) -> None:
        """Swap to a fresh TranscribePane. Releases any active editor first."""
        self._dispose_editor_pane()
        pane = TranscribePane(settings=self.settings, state=self.state)
        pane.file_selected.connect(self._handle_file_selected)
        pane.invalid_file.connect(self._handle_invalid_file)
        pane.open_project_requested.connect(self._handle_open_project)
        pane.transcribe_requested.connect(self._handle_transcribe_requested)
        pane.cancel_requested.connect(self._handle_cancel)
        pane.new_transcription_requested.connect(self._handle_new_transcription)
        self._transcribe_pane = pane
        self.setCentralWidget(pane)
        self._render_for_state(self.state.state)
        self.setWindowTitle(WINDOW_TITLE)

    def show_editor(self, document: Document) -> None:
        """Swap to a fresh EditorPane bound to ``document``."""
        self._dispose_transcribe_pane()
        editor = EditorPane(document, settings=self.settings)
        editor.back_to_transcribe.connect(self._handle_back_from_editor)
        editor.layout_changed.connect(self._apply_settings)
        editor.dirty_changed.connect(self._refresh_window_title)
        editor.document_saved.connect(self._handle_document_saved)
        self._editor_pane = editor
        self.setCentralWidget(editor)
        self._refresh_window_title(False)

    def _dispose_transcribe_pane(self) -> None:
        if self._transcribe_pane is None:
            return
        old = self._transcribe_pane
        self._transcribe_pane = None
        old.setParent(None)
        old.deleteLater()

    def _dispose_editor_pane(self) -> None:
        if self._editor_pane is None:
            return
        old = self._editor_pane
        self._editor_pane = None
        # release() stops the player and clears its source — important on
        # macOS to avoid a phantom video CALayer outliving the swap.
        try:
            old.release()
        except RuntimeError:
            pass
        old.setParent(None)
        old.deleteLater()

    # ----- state-driven rendering -----

    def _render_for_state(self, state: AppState) -> None:
        if self._transcribe_pane is not None:
            self._transcribe_pane.render_for_state(state)

    # ----- user actions -----

    def _handle_file_selected(self, path: Path) -> None:
        try:
            self.state.load_file(path)
        except ValueError as exc:
            self._handle_invalid_file(str(exc))

    def _handle_invalid_file(self, message: str) -> None:
        self.state.error_message = message
        self._render_for_state(self.state.state)
        QTimer.singleShot(2500, self._clear_transient_error)

    def _clear_transient_error(self) -> None:
        if self.state.state in (AppState.IDLE, AppState.FILE_LOADED):
            self.state.error_message = None
            self._render_for_state(self.state.state)

    def _handle_open_project(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "Open project",
            "",
            "Transcribe project (*.transcribe.json);;All files (*.*)",
        )
        if not chosen:
            return
        path = Path(chosen)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            doc = Document.from_json(data)
        except (OSError, json.JSONDecodeError, UnsupportedSchemaError, KeyError, ValueError) as exc:
            self._handle_invalid_file(f"Could not open {path.name}: {exc}")
            return
        self.show_editor(doc)

    def _handle_transcribe_requested(
        self,
        media_path: Path,
        model_name: str,
        language: str | None,
        formats: list,
    ) -> None:
        if self.state.state == AppState.ERROR:
            self.state.error_message = None
            if self.state.media_path:
                self.state.transition_to(AppState.FILE_LOADED)
        if self.state.state != AppState.FILE_LOADED:
            return
        self.state.start_transcribing()
        if self._transcribe_pane is not None:
            self._transcribe_pane.reset_progress()
        self._cancel_requested.clear()

        self._worker = TranscriptionWorker(
            settings=self.settings,
            media_path=media_path,
            model_name=model_name,
            language=language,
            formats=list(formats),
            on_event=self.event_queue.put,
            cancel_event=self._cancel_requested,
        )
        self._worker_thread = threading.Thread(
            target=self._worker.run, daemon=True
        )
        self._worker_thread.start()

    def _handle_cancel(self) -> None:
        self._cancel_requested.set()
        if self._worker is not None:
            self._worker.cancel()
        if self.state.state == AppState.TRANSCRIBING:
            self.state.cancel()

    def _handle_new_transcription(self) -> None:
        self.state.reset()

    def _handle_back_from_editor(self) -> None:
        self.state.reset()
        self.show_transcribe()

    def _refresh_window_title(self, dirty: bool) -> None:
        """Title bar tracks the editor's dirty state.

        Format: ``"● filename.ext — Whisper Transcriber"`` when dirty,
        ``"filename.ext — Whisper Transcriber"`` when clean. The
        leading dot is the textual modified indicator; macOS also
        paints a dot in the close button via ``setWindowModified`` for
        windows where Qt knows the title contains ``[*]`` — we use the
        explicit prefix instead because it's deterministic across
        backends.
        """
        if self._editor_pane is None:
            self.setWindowTitle(WINDOW_TITLE)
            return
        doc = self._editor_pane.document
        if doc.sources:
            primary = next(iter(doc.sources.values()))
            name = primary.path.name or "(unnamed)"
        else:
            name = "(no source)"
        marker = "● " if dirty else ""
        self.setWindowTitle(f"{marker}{name} — {WINDOW_TITLE}")
        self.setWindowModified(dirty)

    def _handle_document_saved(self, _path: Path) -> None:
        # mark_saved already fired dirty_changed → title updated.
        # Hook reserved for future use (recent files, status flash).
        pass

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self, current=self.settings)
        dlg.settings_saved.connect(self._apply_settings)
        dlg.exec()

    def _apply_settings(self, new: Settings) -> None:
        self.settings = new
        if self._transcribe_pane is not None:
            self._transcribe_pane.update_settings(new)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        self._cancel_requested.set()
        if self._worker is not None:
            self._worker.cancel()
        self._pump_timer.stop()
        self._dispose_editor_pane()
        super().closeEvent(event)

    # ----- queue pump -----

    def pump_once(self) -> int:
        """Drain pending events into the state machine; refresh views.

        On a DoneEvent carrying a :class:`Document`, swap to the editor
        pane. Synthetic DoneEvents in tests that pass ``document=None``
        leave the transcribe pane's COMPLETE view visible as a fallback.

        Public so tests can call it without spinning the QTimer event loop.
        """
        n = pump_queue(self.event_queue, self.state)
        if n > 0:
            self._render_for_state(self.state.state)
            if self.state.state == AppState.TRANSCRIBING and self._transcribe_pane is not None:
                self._transcribe_pane.show_progress_label(
                    self.state.progress_label or "Transcribing…"
                )
            if self.state.state == AppState.COMPLETE and self.state.result is not None:
                doc = getattr(self.state.result, "document", None)
                if doc is not None:
                    self.show_editor(doc)
        return n


def run() -> int:
    """Convenience entry point used by ``main_qt.py`` and tests."""
    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.show()
    return app.exec()


# Kept available for tests / future use.
def _notify_error(parent: QWidget, message: str) -> None:  # pragma: no cover - tiny
    QMessageBox.critical(parent, "Transcription failed", message)
