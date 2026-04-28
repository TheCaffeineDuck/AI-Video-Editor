"""Editor pane: nested QSplitter holding video, transcript, waveform.

Topology (Decision 1 + Decision 5):

    EditorPane
    └── outer QSplitter   (Vertical for video_top, Horizontal for video_left)
        ├── VideoViewport
        └── inner QSplitter  (always Vertical)
            ├── TranscriptView
            └── WaveformStrip

Only the *outer* splitter flips with the layout toggle. The inner
splitter is always vertical so the waveform stays directly under the
transcript regardless of layout — the user-visible promise of
Decision 5 ("waveform is the strip below the transcript").

Phase 5c added the editing surface; Phase 5d added the waveform strip
and its async peak generator. Phase 5e closes the integration loop:

- **Cmd-E (Export…)** runs :class:`workers.render.RenderWorker` on a
  :class:`QThread` and surfaces an indeterminate :class:`QProgressDialog`
  with a Cancel button. Render-time progress is not exposed by
  ``core.render.render_cut`` in 5e; the dialog stays indeterminate
  rather than fudging a fake bar.
- **Autosave** ticks at ``Settings.autosave_interval_s``. When the
  document is dirty the same save path Cmd-S uses fires; failures are
  silent (modal autosave failures are infuriating per Decision 8) and
  leave the dirty marker in place.
- **Splitter sizes** persist between sessions via base64-encoded
  ``QSplitter.saveState()`` blobs in ``Settings``. The persistence is
  per-orientation: flipping the layout abandons the prior orientation's
  saved sizes and asks Qt for a fresh split. Round-tripping the same
  orientation across sessions restores the user's tuning.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from PySide6.QtCore import QByteArray, QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSplitter,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from core.document import Document
from core.editing import AddCut, RestoreRange
from core.settings import DEFAULT_LAYOUT, LAYOUT_CHOICES, Settings, save_settings
from ui_qt.components.transcript_view import TranscriptView
from ui_qt.components.video_viewport import VideoViewport
from ui_qt.document_session import DocumentSession
from ui_qt.waveform import WaveformStrip
from ui_qt.waveform_controller import WaveformController
from workers.render import (
    RenderCancelled,
    RenderComplete,
    RenderError,
    RenderEvent,
    RenderProgress,
    RenderStarted,
    RenderWorker,
)
from workers.transcription import candidate_cache_path

# Splitter saves debounce delay — `splitterMoved` fires per drag pixel
# so writing settings.json on every emit would burn the disk and lock-
# step the dragging UI to file I/O. 200 ms collects a full drag into
# one persisted blob.
_SPLITTER_SAVE_DEBOUNCE_MS = 200

_LOG = logging.getLogger(__name__)


def _orientation_for(layout: str) -> Qt.Orientation:
    """Map a settings layout string to the outer splitter's orientation."""
    if layout == "video_left":
        return Qt.Orientation.Horizontal
    return Qt.Orientation.Vertical  # video_top (default)


def _toggle(layout: str) -> str:
    return "video_left" if layout == "video_top" else "video_top"


class _RenderRunner(QObject):
    """Owns the :class:`RenderWorker` lifecycle on a :class:`QThread`.

    Adapter exists so the worker (a plain object, no Qt) can hand events
    back to the GUI thread via a Qt signal. Lives on the worker thread;
    its ``event_received`` signal is connected with the default
    auto-connection so the slot runs on the GUI thread.
    """

    event_received = Signal(object)
    finished = Signal()

    def __init__(self, worker: RenderWorker) -> None:
        super().__init__()
        self._worker = worker

    def run(self) -> None:
        try:
            self._worker.run()
        finally:
            self.finished.emit()


class EditorPane(QWidget):
    """The editor view: video + transcript + waveform-placeholder.

    Owns a :class:`DocumentSession` for document mutations + undo/redo
    + dirty tracking. The MainWindow listens for ``dirty_changed`` to
    update the title bar and ``back_to_transcribe`` for the manual
    escape hatch.

    Signals:
        back_to_transcribe: emitted when the user clicks the toolbar
            "Back" button (5b's escape hatch — full menu integration is
            5f's job).
        layout_changed(Settings): emitted after the layout toggle
            persists; carries the freshly-saved Settings object.
        dirty_changed(bool): forwarded from the embedded
            :class:`DocumentSession` — the editor is dirty iff the
            current Document differs from the one last saved.
        document_saved(Path): emitted after a successful Cmd-S write.
    """

    back_to_transcribe = Signal()
    layout_changed = Signal(Settings)
    dirty_changed = Signal(bool)
    document_saved = Signal(Path)
    render_started = Signal(Path)
    render_completed = Signal(Path)
    render_failed = Signal(str)
    render_cancelled = Signal()

    def __init__(
        self,
        document: Document,
        *,
        settings: Settings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._session = DocumentSession(document, parent=self)
        self._session.document_changed.connect(self._on_document_changed)
        self._session.dirty_changed.connect(self.dirty_changed.emit)

        self._build_toolbar()
        self._build_splitters()
        self._build_actions()
        self._wire_signals()

        self._waveform_controller = WaveformController(
            self._waveform, self._session, parent=self
        )
        self._waveform_controller.bind_player(self._video)
        self._waveform.seek_requested.connect(self._video.seek_ms)

        # Splitter persistence: restore stored states (per-orientation,
        # so a horizontal blob skips when we're vertical and vice
        # versa), then schedule debounced saves on splitterMoved.
        self._splitter_save_timer = QTimer(self)
        self._splitter_save_timer.setSingleShot(True)
        self._splitter_save_timer.setInterval(_SPLITTER_SAVE_DEBOUNCE_MS)
        self._splitter_save_timer.timeout.connect(self._persist_splitter_state)
        self._restore_splitter_state()
        self._outer.splitterMoved.connect(self._on_splitter_moved)
        self._inner.splitterMoved.connect(self._on_splitter_moved)

        # Render-worker plumbing — initialised lazily on the first export.
        self._render_thread: QThread | None = None
        self._render_worker: RenderWorker | None = None
        self._render_runner: _RenderRunner | None = None
        self._render_dialog: QProgressDialog | None = None
        self._render_cancel_event: threading.Event | None = None

        # Autosave: a single QTimer driven from the configured interval.
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(False)
        self._autosave_timer.timeout.connect(self._handle_autosave_tick)
        self._apply_autosave_interval(self._settings.autosave_interval_s)

        self._render_document()
        self._wire_video_source()

    # ----- public surface -----

    @property
    def document(self) -> Document:
        return self._session.document

    @property
    def session(self) -> DocumentSession:
        return self._session

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def video_viewport(self) -> VideoViewport:
        return self._video

    @property
    def transcript_view(self) -> TranscriptView:
        return self._transcript

    @property
    def outer_splitter(self) -> QSplitter:
        return self._outer

    @property
    def inner_splitter(self) -> QSplitter:
        return self._inner

    def release(self) -> None:
        """Tear down embedded threads + media player before destruction."""
        try:
            self._autosave_timer.stop()
        except RuntimeError:
            pass
        try:
            self._splitter_save_timer.stop()
        except RuntimeError:
            pass
        # Cancel any in-flight export. Same shape as the waveform
        # controller's shutdown — flip the cancel flag, ask the QThread
        # to quit, wait briefly for it to land. Leaving a render
        # QThread alive past widget destruction triggers Qt's "QThread
        # destroyed while still running" abort.
        if self._render_cancel_event is not None:
            self._render_cancel_event.set()
        if self._render_thread is not None and self._render_thread.isRunning():
            self._render_thread.quit()
            self._render_thread.wait(5_000)
        self._render_thread = None
        self._render_worker = None
        self._render_runner = None
        self._render_cancel_event = None
        try:
            self._waveform_controller.shutdown()
        except RuntimeError:
            pass
        try:
            self._video.release()
        except RuntimeError:
            pass

    # ----- layout / build -----

    def _build_toolbar(self) -> None:
        self._outer_layout = QVBoxLayout(self)
        self._outer_layout.setContentsMargins(0, 0, 0, 0)

        bar = QToolBar()
        bar.setMovable(False)

        self._back_btn = QPushButton("← Back to Transcribe")
        self._back_btn.setFlat(True)
        self._back_btn.clicked.connect(self.back_to_transcribe.emit)
        bar.addWidget(self._back_btn)

        spacer = QWidget()
        spacer.setSizePolicy(spacer.sizePolicy().horizontalPolicy(),
                             spacer.sizePolicy().verticalPolicy())
        bar.addWidget(spacer)

        self._save_btn = QPushButton("Save")
        self._save_btn.setFlat(True)
        self._save_btn.clicked.connect(self._handle_save)
        bar.addWidget(self._save_btn)

        self._export_btn = QPushButton("Export…")
        self._export_btn.setFlat(True)
        self._export_btn.clicked.connect(self._handle_export)
        bar.addWidget(self._export_btn)

        self._undo_btn = QPushButton("Undo")
        self._undo_btn.setFlat(True)
        self._undo_btn.setEnabled(False)
        self._undo_btn.clicked.connect(self._handle_undo)
        bar.addWidget(self._undo_btn)

        self._redo_btn = QPushButton("Redo")
        self._redo_btn.setFlat(True)
        self._redo_btn.setEnabled(False)
        self._redo_btn.clicked.connect(self._handle_redo)
        bar.addWidget(self._redo_btn)

        self._layout_btn = QPushButton(self._layout_button_label(self._settings.layout))
        self._layout_btn.setFlat(True)
        self._layout_btn.clicked.connect(self._handle_layout_toggle)
        bar.addWidget(self._layout_btn)

        self._outer_layout.addWidget(bar)

    def _build_splitters(self) -> None:
        self._outer = QSplitter(_orientation_for(self._settings.layout), self)
        self._video = VideoViewport(self._outer)

        self._inner = QSplitter(Qt.Orientation.Vertical, self._outer)
        self._transcript = TranscriptView(self._inner)
        self._waveform = WaveformStrip(self._inner)
        self._inner.addWidget(self._transcript)
        self._inner.addWidget(self._waveform)
        self._inner.setStretchFactor(0, 1)
        self._inner.setStretchFactor(1, 0)
        self._inner.setSizes([400, 80])

        self._outer.addWidget(self._video)
        self._outer.addWidget(self._inner)
        self._outer.setStretchFactor(0, 1)
        self._outer.setStretchFactor(1, 1)

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.addWidget(self._outer)
        self._outer_layout.addWidget(body, stretch=1)

        self._status = QLabel("")
        self._status.setStyleSheet("color: #6B7280; padding: 4px 8px;")
        self._outer_layout.addWidget(self._status)

    def _build_actions(self) -> None:
        """Pane-level QActions for the editor shortcuts.

        ``Qt.ApplicationShortcut`` context (rather than the default
        ``WindowShortcut``) — naive ``QShortcut`` on the pane fails in
        some macOS focus configurations, especially when a child
        widget like the TranscriptView has the active focus. Actions
        with application context fire regardless of which descendant
        has focus.

        These are owned by the editor pane in 5c. 5f reparents them to
        the menu bar (File → Save, Edit → Undo, etc.) by reusing the
        same QAction instances — no rebinding needed.
        """
        self._cut_action = self._make_action(
            "Cut",
            [QKeySequence.StandardKey.Cut, QKeySequence("Backspace")],
            self._handle_cut,
        )
        # Forward Delete (the dedicated key, not Backspace) is bound
        # separately because macOS has it as a distinct keysym and
        # QKeySequence("Delete") doesn't include both spellings.
        self._delete_action = self._make_action(
            "Delete",
            [QKeySequence(Qt.Key.Key_Delete)],
            self._handle_cut,
        )
        self._restore_action = self._make_action(
            "Restore",
            [QKeySequence("Ctrl+Shift+Backspace"), QKeySequence("Meta+Backspace")],
            self._handle_cut,  # same handler — branches on selection state
        )
        self._undo_action = self._make_action(
            "Undo", [QKeySequence.StandardKey.Undo], self._handle_undo
        )
        self._redo_action = self._make_action(
            "Redo", [QKeySequence.StandardKey.Redo], self._handle_redo
        )
        self._save_action = self._make_action(
            "Save", [QKeySequence.StandardKey.Save], self._handle_save
        )
        self._export_action = self._make_action(
            "Export", [QKeySequence("Ctrl+E"), QKeySequence("Meta+E")], self._handle_export
        )

    def _make_action(self, text, shortcuts, slot) -> QAction:
        action = QAction(text, self)
        if isinstance(shortcuts, list):
            action.setShortcuts(shortcuts)
        else:
            action.setShortcut(shortcuts)
        action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        action.triggered.connect(slot)
        self.addAction(action)
        return action

    def _wire_signals(self) -> None:
        self._transcript.cut_requested.connect(self._handle_cut_request)
        self._transcript.seek_requested.connect(self._video.seek_ms)
        self._video.position_changed.connect(self._transcript.set_playhead_position)

    # ----- behavior -----

    def _render_document(self) -> None:
        self._transcript.set_document_model(self._session.document)
        self._refresh_undo_redo_buttons()

    def _wire_video_source(self) -> None:
        if not self._session.document.sources:
            self._status.setText("(no source media on this document)")
            self._video.set_source(None)
            return
        primary = next(iter(self._session.document.sources.values()))
        self._status.setText(str(primary.path))
        if primary.path.is_file():
            self._video.set_source(primary.path)
        else:
            self._status.setText(f"(source missing: {primary.path})")
            self._video.set_source(None)

    def _on_document_changed(self, _doc: Document) -> None:
        self._render_document()

    def _refresh_undo_redo_buttons(self) -> None:
        self._undo_btn.setEnabled(self._session.can_undo)
        self._redo_btn.setEnabled(self._session.can_redo)

    # ----- shortcut handlers -----

    def _handle_cut(self) -> None:
        """Cmd-X / Delete on the live selection. No selection → no-op."""
        if not self._transcript.request_cut_for_selection():
            return  # silent no-op per spec

    def _handle_cut_request(self, start_idx: int, end_idx: int) -> None:
        """Apply cut-or-restore for selection word range ``[start, end]`` inclusive."""
        words = self._transcript.words
        if not (0 <= start_idx <= end_idx < len(words)):
            return
        span = words[start_idx:end_idx + 1]
        # Time interval covers the leftmost word's start through the
        # rightmost word's end. By construction these are word
        # boundaries, so the "Never cut inside a word" rule holds.
        interval_start = span[0].word.start
        interval_end = span[-1].word.end
        primary_source_id = self._primary_source_id()

        all_already_cut = all(not w.kept for w in span)
        if all_already_cut:
            command = RestoreRange(
                start=interval_start,
                end=interval_end,
                source_id=primary_source_id,
            )
        else:
            command = AddCut(
                start=interval_start,
                end=interval_end,
                source_id=primary_source_id,
            )
        self._session.apply(command)
        # _on_document_changed re-renders; that drops the selection.

    def _handle_undo(self) -> None:
        self._session.undo()

    def _handle_redo(self) -> None:
        self._session.redo()

    def _handle_save(self) -> None:
        path = self._save_path()
        if path is None:
            QMessageBox.critical(
                self, "Save failed",
                "Cannot save: no source media is associated with this document.",
            )
            return
        try:
            self._write_document_to(path)
        except OSError as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self._session.mark_saved()
        self.document_saved.emit(path)
        _LOG.info("saved document to %s", path)

    def _write_document_to(self, path: Path) -> None:
        """Persist the live Document JSON to ``path``. Raises on I/O error."""
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._session.document.to_json(), indent=2)
        path.write_text(payload, encoding="utf-8")

    def _handle_autosave_tick(self) -> None:
        """Fire on the autosave QTimer. No-op when clean.

        Failures are silent per Decision 8: stderr log, leave the dirty
        marker in place, retry on the next tick. Modal autosave failures
        are infuriating and would crowd out the actual user-driven save.
        """
        if not self._session.is_dirty:
            return
        path = self._save_path()
        if path is None:
            _LOG.warning("autosave skipped: no save path for the active document")
            return
        try:
            self._write_document_to(path)
        except OSError as exc:
            _LOG.error("autosave failed for %s: %s", path, exc)
            return
        self._session.mark_saved()
        self.document_saved.emit(path)
        _LOG.info("autosaved document to %s", path)

    def _apply_autosave_interval(self, seconds: int) -> None:
        """Reconfigure the autosave QTimer. ``0`` stops it."""
        if seconds <= 0:
            self._autosave_timer.stop()
            return
        # ``setInterval`` while running adjusts the next tick on Qt;
        # ``start`` is idempotent (it re-arms the timer cleanly).
        self._autosave_timer.setInterval(int(seconds * 1000))
        self._autosave_timer.start()

    def apply_settings(self, settings: Settings) -> None:
        """Apply a freshly-saved ``Settings`` to the running pane.

        Called by MainWindow after the settings dialog emits
        ``settings_saved``. Currently picks up:

        - ``layout`` (flips outer splitter orientation live)
        - ``autosave_interval_s`` (re-arms the autosave QTimer)
        - render-default fields (no live propagation needed; consumed by
          the next export)
        - ``output_dir`` (used by the next save's path calc)

        The splitter-state fields stay untouched here — those flow the
        opposite direction (pane → settings on user drag).
        """
        prior_layout = self._settings.layout
        # Carry the freshly-typed splitter state forward — the dialog
        # round-trips Settings, but the user's most-recent splitter
        # tuning lives on this pane's settings reference, not the dialog
        # snapshot. (The dialog preserves the existing values on Save.)
        new = Settings(**{**settings.to_dict(), **{
            "editor_splitter_state": settings.editor_splitter_state,
            "transcript_splitter_state": settings.transcript_splitter_state,
        }})
        # to_dict already encoded any bytes to base64; from_dict path
        # handles the round-trip for splitter state.
        new = Settings.from_dict(new.to_dict())
        self._settings = new

        if new.layout != prior_layout and new.layout in LAYOUT_CHOICES:
            self._outer.setOrientation(_orientation_for(new.layout))
            self._layout_btn.setText(self._layout_button_label(new.layout))

        self._apply_autosave_interval(new.autosave_interval_s)

    def _handle_layout_toggle(self) -> None:
        new_layout = _toggle(self._settings.layout)
        if new_layout not in LAYOUT_CHOICES:
            new_layout = DEFAULT_LAYOUT
        self._settings.layout = new_layout
        # Per-orientation persistence: a saved horizontal blob is wrong
        # for vertical and vice versa, so toggling resets the outer
        # splitter's saved state. The user's tuning for the *new*
        # orientation gets persisted on their next splitter drag.
        self._settings.editor_splitter_state = None
        save_settings(self._settings)
        self._outer.setOrientation(_orientation_for(new_layout))
        self._layout_btn.setText(self._layout_button_label(new_layout))
        self.layout_changed.emit(self._settings)

    # ----- splitter persistence -----

    def _restore_splitter_state(self) -> None:
        """Replay saved splitter sizes from settings.

        Saved state is QSplitter-orientation-specific in Qt's encoding.
        ``_handle_layout_toggle`` clears ``editor_splitter_state`` when
        the user flips the outer orientation so we never end up handing
        a horizontal blob to a now-vertical splitter (or vice versa).
        That makes "accept the loss on toggle" the persistence
        contract: same orientation across sessions = sizes preserved;
        toggle = fresh proportional split.
        """
        outer_state = self._settings.editor_splitter_state
        if outer_state:
            self._outer.restoreState(QByteArray(outer_state))
        inner_state = self._settings.transcript_splitter_state
        if inner_state:
            self._inner.restoreState(QByteArray(inner_state))

    def _on_splitter_moved(self, _pos: int, _index: int) -> None:
        # Coalesce burst events from a single drag into one save.
        self._splitter_save_timer.start()

    def _persist_splitter_state(self) -> None:
        outer_bytes = bytes(self._outer.saveState())
        inner_bytes = bytes(self._inner.saveState())
        self._settings.editor_splitter_state = outer_bytes
        self._settings.transcript_splitter_state = inner_bytes
        try:
            save_settings(self._settings)
        except OSError as exc:
            _LOG.warning("could not persist splitter state: %s", exc)

    # ----- export -----

    def _handle_export(self) -> None:
        if not self._session.document.ranges:
            QMessageBox.warning(
                self,
                "Nothing to export",
                "This document has no ranges to render.",
            )
            return
        if self._render_thread is not None and self._render_thread.isRunning():
            QMessageBox.information(
                self, "Render in progress",
                "Wait for the current export to finish before starting another.",
            )
            return
        primary = next(iter(self._session.document.sources.values()), None)
        if primary is None or not primary.path.is_file():
            QMessageBox.critical(
                self, "Export failed",
                "Cannot export: source media is missing.",
            )
            return

        suggested = self._suggested_export_path(primary.path)
        chosen, _ = QFileDialog.getSaveFileName(
            self,
            "Export edited video",
            str(suggested),
            "MP4 (*.mp4);;All files (*.*)",
        )
        if not chosen:
            return
        output_path = Path(chosen)
        self._launch_render(output_path)

    def _suggested_export_path(self, source_path: Path) -> Path:
        directory = (
            Path(self._settings.output_dir)
            if self._settings.output_dir
            else source_path.parent
        )
        return directory / f"{source_path.stem}_edited.mp4"

    def _launch_render(self, output_path: Path) -> None:
        cancel = threading.Event()
        worker = RenderWorker(
            document=self._session.document,
            output_path=output_path,
            settings=self._settings,
            on_event=lambda evt: self._render_runner.event_received.emit(evt),  # type: ignore[union-attr]
            cancel_event=cancel,
        )
        thread = QThread(self)
        runner = _RenderRunner(worker)
        runner.moveToThread(thread)
        thread.started.connect(runner.run)
        runner.event_received.connect(self._handle_render_event)
        runner.finished.connect(thread.quit)
        thread.finished.connect(runner.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self._render_cancel_event = cancel
        self._render_thread = thread
        self._render_worker = worker
        self._render_runner = runner

        # Indeterminate progress dialog: ``setRange(0, 0)`` paints a busy
        # bar. ``core.render.render_cut`` doesn't currently emit
        # progress to ``on_progress`` for the smartcut step; fudging a
        # fake bar would lie to the user. Spec §1 explicitly forbids
        # adding progress to core/.
        dlg = QProgressDialog("Rendering…", "Cancel", 0, 0, self)
        dlg.setWindowTitle("Export")
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.canceled.connect(self._handle_render_cancel)
        self._render_dialog = dlg
        dlg.show()

        thread.start()

    def _handle_render_event(self, event: RenderEvent) -> None:
        """Receive worker events on the GUI thread."""
        if isinstance(event, RenderStarted):
            self.render_started.emit(event.output_path)
        elif isinstance(event, RenderProgress):
            # render_cut may begin emitting progress in a future phase;
            # promote the dialog from indeterminate to a real bar when
            # we see real numbers.
            if self._render_dialog is not None:
                if self._render_dialog.maximum() == 0:
                    self._render_dialog.setRange(0, 100)
                self._render_dialog.setValue(int(event.fraction * 100))
        elif isinstance(event, RenderComplete):
            self._close_render_dialog()
            self.render_completed.emit(event.output_path)
            QMessageBox.information(
                self, "Export complete",
                f"Wrote {event.output_path.name} ({event.elapsed:.1f}s).",
            )
            self._render_thread = None
            self._render_worker = None
            self._render_runner = None
            self._render_cancel_event = None
        elif isinstance(event, RenderError):
            self._close_render_dialog()
            self.render_failed.emit(event.message)
            QMessageBox.critical(self, "Export failed", event.message)
            self._render_thread = None
            self._render_worker = None
            self._render_runner = None
            self._render_cancel_event = None
        elif isinstance(event, RenderCancelled):
            self._close_render_dialog()
            self.render_cancelled.emit()
            self._render_thread = None
            self._render_worker = None
            self._render_runner = None
            self._render_cancel_event = None

    def _handle_render_cancel(self) -> None:
        if self._render_cancel_event is not None:
            self._render_cancel_event.set()
        # Disable the Cancel button so a frantic user doesn't stack
        # cancel signals while the worker winds down.
        if self._render_dialog is not None:
            self._render_dialog.setLabelText("Cancelling…")
            self._render_dialog.setCancelButton(None)

    def _close_render_dialog(self) -> None:
        dlg = self._render_dialog
        self._render_dialog = None
        if dlg is None:
            return
        try:
            dlg.close()
        except RuntimeError:
            pass

    # ----- helpers -----

    def _primary_source_id(self) -> str:
        if not self._session.document.sources:
            return "src0"
        return next(iter(self._session.document.sources.keys()))

    def _save_path(self) -> Path | None:
        if not self._session.document.sources:
            return None
        primary = next(iter(self._session.document.sources.values()))
        return candidate_cache_path(self._settings, primary.path)

    @staticmethod
    def _layout_button_label(layout: str) -> str:
        if layout == "video_left":
            return "Layout: video left  ↔"
        return "Layout: video top  ↕"
