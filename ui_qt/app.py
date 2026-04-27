"""PySide6 main window. Wires components, the worker thread, and the event pump.

The pump is a QTimer rather than ``root.after`` (Qt has no ``.after``) but
the underlying queue and worker events are identical to the customtkinter
side. The worker emits :class:`workers.events.WorkerEvent` values into a
plain ``queue.Queue``; the timer drains it and dispatches to the state
machine and the visible UI panes.

State management piggy-backs on :class:`ui.state.AppStateMachine`. That
class is framework-free; only the rendering side differs between the
two UIs.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from core import audio
from core.settings import Settings, load_settings
from ui.state import (
    AppState,
    AppStateMachine,
    pump_queue,
)
from ui_qt.components.drop_zone import DropZone
from ui_qt.components.language_picker import LanguagePicker
from ui_qt.components.model_picker import ModelPicker
from ui_qt.components.output_formats import OutputFormatPicker
from ui_qt.components.progress_card import ProgressCard
from ui_qt.components.result_card import ResultCard
from ui_qt.components.settings_panel import SettingsDialog
from ui_qt.style import (
    WINDOW_DEFAULT_SIZE,
    WINDOW_MIN_SIZE,
    WINDOW_TITLE,
    accent_button_qss,
)
from workers.transcription import TranscriptionWorker

PUMP_INTERVAL_MS = 100


class MainWindow(QMainWindow):
    """Top-level window. Owns settings, state, the worker, and the queue pump."""

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

        self._build_toolbar()
        self._build_central()
        self.setStatusBar(QStatusBar(self))

        self.state.on_change(self._render_for_state)
        self._render_for_state(self.state.state)

        self._pump_timer = QTimer(self)
        self._pump_timer.setInterval(PUMP_INTERVAL_MS)
        self._pump_timer.timeout.connect(self.pump_once)
        self._pump_timer.start()

    # ----- layout -----

    def _build_toolbar(self) -> None:
        bar = QToolBar()
        bar.setMovable(False)
        bar.addWidget(_spacer())
        gear = QPushButton("Settings")
        gear.setFlat(True)
        gear.clicked.connect(self._open_settings)
        bar.addWidget(gear)
        self.addToolBar(bar)

    def _build_central(self) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)

        self._error_banner = QLabel("")
        self._error_banner.setWordWrap(True)
        self._error_banner.setStyleSheet(
            "background-color: #DC2626; color: white; padding: 8px;"
            "border-radius: 4px;"
        )
        self._error_banner.hide()
        outer.addWidget(self._error_banner)

        self._stack = QStackedWidget()
        outer.addWidget(self._stack, stretch=1)

        # Idle / file-loaded view.
        self._idle = QWidget()
        idle_layout = QVBoxLayout(self._idle)
        self.drop_zone = DropZone()
        self.drop_zone.file_selected.connect(self._handle_file_selected)
        self.drop_zone.invalid_file.connect(self._handle_invalid_file)
        idle_layout.addWidget(self.drop_zone, stretch=1)

        self.model_picker = ModelPicker(initial=self.settings.default_model)
        idle_layout.addWidget(self.model_picker)

        self.language_picker = LanguagePicker(
            initial_code=self.settings.default_language
        )
        idle_layout.addWidget(self.language_picker)

        self.output_picker = OutputFormatPicker(initial=self.settings.output_formats)
        self.output_picker.formats_changed.connect(self._handle_output_format_change)
        idle_layout.addWidget(self.output_picker)

        self.transcribe_btn = QPushButton("Transcribe")
        self.transcribe_btn.setStyleSheet(accent_button_qss())
        self.transcribe_btn.setMinimumHeight(44)
        self.transcribe_btn.clicked.connect(self._handle_transcribe_click)
        idle_layout.addWidget(self.transcribe_btn)

        self._stack.addWidget(self._idle)

        # Transcribing view.
        self.progress_card = ProgressCard()
        self.progress_card.cancel_requested.connect(self._handle_cancel)
        self._stack.addWidget(self.progress_card)

        # Complete view.
        self.result_card = ResultCard()
        self.result_card.new_transcription.connect(self._handle_new_transcription)
        self._stack.addWidget(self.result_card)

        self.setCentralWidget(central)

    # ----- state-driven rendering -----

    def _render_for_state(self, state: AppState) -> None:
        if self.state.error_message:
            self._error_banner.setText(f"⚠  {self.state.error_message}")
            self._error_banner.show()
        else:
            self._error_banner.hide()

        if state in (AppState.IDLE, AppState.FILE_LOADED, AppState.ERROR):
            self._stack.setCurrentWidget(self._idle)
            if state == AppState.FILE_LOADED and self.state.media_path:
                self._show_loaded_preview(self.state.media_path)
                self._refresh_transcribe_button("Transcribe")
            elif state == AppState.ERROR and self.state.media_path:
                self._show_loaded_preview(self.state.media_path)
                self._refresh_transcribe_button("Retry")
            else:
                self.drop_zone.show_idle()
                self.transcribe_btn.setText("Transcribe")
                self.transcribe_btn.setEnabled(False)
        elif state == AppState.TRANSCRIBING:
            self._stack.setCurrentWidget(self.progress_card)
            self.progress_card.set_progress(self.state.progress)
            for line in self.state.streaming_text:
                self.progress_card.append_stream(line)
            self.state.streaming_text = []
        elif state == AppState.COMPLETE:
            self._show_result()
            self._stack.setCurrentWidget(self.result_card)

    def _refresh_transcribe_button(self, label: str) -> None:
        if self.output_picker.has_selection:
            self.transcribe_btn.setText(label)
            self.transcribe_btn.setEnabled(True)
        else:
            self.transcribe_btn.setText(f"{label} (pick an output format)")
            self.transcribe_btn.setEnabled(False)

    def _handle_output_format_change(self, _formats: list[str]) -> None:
        if self.state.state in (AppState.FILE_LOADED, AppState.ERROR):
            label = "Retry" if self.state.state == AppState.ERROR else "Transcribe"
            self._refresh_transcribe_button(label)

    def _show_loaded_preview(self, path: Path) -> None:
        try:
            duration = audio.get_duration(path)
        except Exception:  # noqa: BLE001
            duration = 0.0
        size = path.stat().st_size if path.is_file() else 0
        self.drop_zone.show_loaded(
            name=path.name, duration_seconds=duration, size_bytes=size
        )

    def _show_result(self) -> None:
        result = self.state.result
        if result is None:
            return
        transcript = " ".join(s.text.strip() for s in result.segments).strip()
        language = getattr(result.info, "language", "") or "?"
        self.result_card.show_result(
            transcript=transcript,
            output_files=result.output_files,
            language=language,
            elapsed_seconds=result.elapsed,
        )

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

    def _handle_transcribe_click(self) -> None:
        if self.state.state == AppState.ERROR:
            self.state.error_message = None
            if self.state.media_path:
                self.state.state = AppState.FILE_LOADED
                self.state._emit()  # noqa: SLF001
        if self.state.state != AppState.FILE_LOADED:
            return
        if not self.output_picker.has_selection:
            self._handle_invalid_file("Select at least one output format.")
            return
        self.state.start_transcribing()
        self.progress_card.reset()
        self.progress_card.set_label("Preparing…")
        self._cancel_requested.clear()

        media_path = self.state.media_path
        assert media_path is not None
        self._worker = TranscriptionWorker(
            settings=self.settings,
            media_path=media_path,
            model_name=self.model_picker.value,
            language=self.language_picker.selected_code,
            formats=list(self.output_picker.formats),
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

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self, current=self.settings)
        dlg.settings_saved.connect(self._apply_settings)
        dlg.exec()

    def _apply_settings(self, new: Settings) -> None:
        self.settings = new
        try:
            self.model_picker.set_value(new.default_model)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.language_picker.set_code(new.default_language)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.output_picker.set_formats(new.output_formats)
        except Exception:  # noqa: BLE001
            pass

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        self._cancel_requested.set()
        if self._worker is not None:
            self._worker.cancel()
        self._pump_timer.stop()
        super().closeEvent(event)

    # ----- queue pump -----

    def pump_once(self) -> int:
        """Drain pending events into the state machine; refresh views.

        Public so tests can call it without spinning the QTimer event loop.
        """
        n = pump_queue(self.event_queue, self.state)
        if n > 0:
            self._render_for_state(self.state.state)
            if self.state.state == AppState.TRANSCRIBING:
                self.progress_card.set_progress(
                    self.state.progress,
                    label=self.state.progress_label or "Transcribing…",
                )
        return n


def _spacer() -> QWidget:
    """A horizontal stretchable spacer for the toolbar."""
    spacer = QWidget()
    spacer.setSizePolicy(
        spacer.sizePolicy().horizontalPolicy(),
        spacer.sizePolicy().verticalPolicy(),
    )
    spacer.setMinimumWidth(0)
    spacer.setStyleSheet("background: transparent;")
    return spacer


# Keep around in case tests want to assert on a notification path.
def _notify_error(parent: QWidget, message: str) -> None:  # pragma: no cover - tiny
    QMessageBox.critical(parent, "Transcription failed", message)


def run() -> int:
    """Convenience entry point used by ``main_qt.py`` and tests."""
    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.show()
    return app.exec()
