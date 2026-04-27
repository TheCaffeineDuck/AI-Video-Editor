"""Drop-and-transcribe pane (the IDLE/FILE_LOADED/TRANSCRIBING/COMPLETE flow).

Phase 5b extracted this widget out of :class:`ui_qt.app.MainWindow` so the
top-level window can swap between this pane and :class:`ui_qt.editor_pane.EditorPane`
via ``setCentralWidget``. The pane is intentionally dumb: it owns the
visual structure and emits signals; MainWindow keeps the state machine,
the worker, and the queue pump.

User actions surface as signals (``transcribe_requested``,
``cancel_requested``, ``open_project_requested``, …). Render-for-state
is a public method that MainWindow calls after each pump. The pane
holds no state machine of its own — re-creating it on
``show_transcribe()`` is therefore fine.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core import audio
from core.settings import Settings
from ui.state import AppState, AppStateMachine
from ui_qt.components.drop_zone import DropZone
from ui_qt.components.language_picker import LanguagePicker
from ui_qt.components.model_picker import ModelPicker
from ui_qt.components.output_formats import OutputFormatPicker
from ui_qt.components.progress_card import ProgressCard
from ui_qt.components.result_card import ResultCard
from ui_qt.style import accent_button_qss


class TranscribePane(QWidget):
    """The drop-zone / progress / result widget tree.

    Constructed cheaply; deleted via ``deleteLater`` on swap-to-editor
    (Decision 4 — single window, state swap). Re-instantiating from
    fresh state is the right behaviour because returning to the
    transcribe pane means the user wants to start a new transcription.
    """

    # Drop / open
    file_selected = Signal(Path)
    invalid_file = Signal(str)
    open_project_requested = Signal()

    # Worker control
    transcribe_requested = Signal(Path, str, object, list)
    cancel_requested = Signal()

    # Result-pane actions
    new_transcription_requested = Signal()

    def __init__(
        self,
        *,
        settings: Settings,
        state: AppStateMachine,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._state = state

        self._build()

    # ----- accessors (kept for test compatibility & MainWindow wiring) -----

    @property
    def drop_zone(self) -> DropZone:
        return self._drop_zone

    @property
    def model_picker(self) -> ModelPicker:
        return self._model_picker

    @property
    def language_picker(self) -> LanguagePicker:
        return self._language_picker

    @property
    def output_picker(self) -> OutputFormatPicker:
        return self._output_picker

    @property
    def transcribe_btn(self) -> QPushButton:
        return self._transcribe_btn

    @property
    def progress_card(self) -> ProgressCard:
        return self._progress_card

    @property
    def result_card(self) -> ResultCard:
        return self._result_card

    # ----- layout -----

    def _build(self) -> None:
        outer = QVBoxLayout(self)

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

        # --- IDLE / FILE_LOADED / ERROR view -----------------------------
        idle = QWidget()
        idle_layout = QVBoxLayout(idle)

        # Top row: open-project link sits above the drop zone so users
        # who already have a .transcribe.json on disk don't have to
        # transcribe again.
        open_row = QHBoxLayout()
        open_row.addStretch()
        self._open_btn = QPushButton("Open project (.transcribe.json)…")
        self._open_btn.setFlat(True)
        self._open_btn.clicked.connect(self.open_project_requested.emit)
        open_row.addWidget(self._open_btn)
        idle_layout.addLayout(open_row)

        self._drop_zone = DropZone()
        self._drop_zone.file_selected.connect(self.file_selected.emit)
        self._drop_zone.invalid_file.connect(self.invalid_file.emit)
        idle_layout.addWidget(self._drop_zone, stretch=1)

        self._model_picker = ModelPicker(initial=self._settings.default_model)
        idle_layout.addWidget(self._model_picker)

        self._language_picker = LanguagePicker(
            initial_code=self._settings.default_language
        )
        idle_layout.addWidget(self._language_picker)

        self._output_picker = OutputFormatPicker(initial=self._settings.output_formats)
        self._output_picker.formats_changed.connect(self._handle_output_format_change)
        idle_layout.addWidget(self._output_picker)

        self._transcribe_btn = QPushButton("Transcribe")
        self._transcribe_btn.setStyleSheet(accent_button_qss())
        self._transcribe_btn.setMinimumHeight(44)
        self._transcribe_btn.clicked.connect(self._handle_transcribe_click)
        idle_layout.addWidget(self._transcribe_btn)

        self._stack.addWidget(idle)
        self._idle = idle

        # --- TRANSCRIBING view ------------------------------------------
        self._progress_card = ProgressCard()
        self._progress_card.cancel_requested.connect(self.cancel_requested.emit)
        self._stack.addWidget(self._progress_card)

        # --- COMPLETE view (kept as a fallback when DoneEvent has no doc) -
        self._result_card = ResultCard()
        self._result_card.new_transcription.connect(self.new_transcription_requested.emit)
        self._stack.addWidget(self._result_card)

    # ----- render -----

    def render_for_state(self, state: AppState) -> None:
        if self._state.error_message:
            self._error_banner.setText(f"⚠  {self._state.error_message}")
            self._error_banner.show()
        else:
            self._error_banner.hide()

        if state in (AppState.IDLE, AppState.FILE_LOADED, AppState.ERROR):
            self._stack.setCurrentWidget(self._idle)
            if state == AppState.FILE_LOADED and self._state.media_path:
                self._show_loaded_preview(self._state.media_path)
                self._refresh_transcribe_button("Transcribe")
            elif state == AppState.ERROR and self._state.media_path:
                self._show_loaded_preview(self._state.media_path)
                self._refresh_transcribe_button("Retry")
            else:
                self._drop_zone.show_idle()
                self._transcribe_btn.setText("Transcribe")
                self._transcribe_btn.setEnabled(False)
        elif state == AppState.TRANSCRIBING:
            self._stack.setCurrentWidget(self._progress_card)
            self._progress_card.set_progress(self._state.progress)
            for line in self._state.streaming_text:
                self._progress_card.append_stream(line)
            self._state.streaming_text = []
        elif state == AppState.COMPLETE:
            self._stack.setCurrentWidget(self._result_card)
            self._show_result()

    def show_progress_label(self, label: str) -> None:
        """Forwarded by MainWindow during the pump's progress refresh."""
        self._progress_card.set_progress(self._state.progress, label=label)

    def reset_progress(self) -> None:
        self._progress_card.reset()
        self._progress_card.set_label("Preparing…")

    def update_settings(self, settings: Settings) -> None:
        """Apply a freshly-saved Settings to the visible inputs."""
        self._settings = settings
        try:
            self._model_picker.set_value(settings.default_model)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._language_picker.set_code(settings.default_language)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._output_picker.set_formats(settings.output_formats)
        except Exception:  # noqa: BLE001
            pass

    # ----- internal -----

    def _refresh_transcribe_button(self, label: str) -> None:
        if self._output_picker.has_selection:
            self._transcribe_btn.setText(label)
            self._transcribe_btn.setEnabled(True)
        else:
            self._transcribe_btn.setText(f"{label} (pick an output format)")
            self._transcribe_btn.setEnabled(False)

    def _handle_output_format_change(self, _formats: Iterable[str]) -> None:
        if self._state.state in (AppState.FILE_LOADED, AppState.ERROR):
            label = "Retry" if self._state.state == AppState.ERROR else "Transcribe"
            self._refresh_transcribe_button(label)

    def _handle_transcribe_click(self) -> None:
        media_path = self._state.media_path
        if media_path is None:
            return
        if not self._output_picker.has_selection:
            self.invalid_file.emit("Select at least one output format.")
            return
        self.transcribe_requested.emit(
            media_path,
            self._model_picker.value,
            self._language_picker.selected_code,
            list(self._output_picker.formats),
        )

    def _show_loaded_preview(self, path: Path) -> None:
        try:
            duration = audio.get_duration(path)
        except Exception:  # noqa: BLE001
            duration = 0.0
        size = path.stat().st_size if path.is_file() else 0
        self._drop_zone.show_loaded(
            name=path.name, duration_seconds=duration, size_bytes=size
        )

    def _show_result(self) -> None:
        result = self._state.result
        if result is None:
            return
        transcript = " ".join(s.text.strip() for s in result.segments).strip()
        language = getattr(result.info, "language", "") or "?"
        self._result_card.show_result(
            transcript=transcript,
            output_files=result.output_files,
            language=language,
            elapsed_seconds=result.elapsed,
        )
