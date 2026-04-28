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

Phase 5c added the editing surface on top of 5b's skeleton:

- Transcript word click → seek video player to ``word.start``.
- Drag-select range; Cmd-X / Delete pushes :class:`AddCut` (mixed
  selection) or :class:`RestoreRange` (all-already-cut selection).
- Cmd-Z / Cmd-Shift-Z drive the existing :class:`CommandStack` via the
  :class:`ui_qt.document_session.DocumentSession` helper.
- Cmd-S writes :meth:`Document.to_json` to the cache path the
  transcribe flow originally used (via
  :func:`workers.transcription.candidate_cache_path`).
- Player ``positionChanged`` drives a now-playing bold highlight on
  the matching word, with viewport-edge auto-scroll.

Out of scope for 5c: render-time preview playback (player still plays
source-time, so a click on a struck word still seeks there); autosave
(Decision 8 says explicit only); waveform interaction (5d); menu-bar
shortcut wiring (5f). The shortcuts here are pane-local QActions —
they reparent cleanly to the menu bar when 5f arrives.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
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
from workers.transcription import candidate_cache_path

_LOG = logging.getLogger(__name__)


def _orientation_for(layout: str) -> Qt.Orientation:
    """Map a settings layout string to the outer splitter's orientation."""
    if layout == "video_left":
        return Qt.Orientation.Horizontal
    return Qt.Orientation.Vertical  # video_top (default)


def _toggle(layout: str) -> str:
    return "video_left" if layout == "video_top" else "video_top"


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
        """Tear down the embedded media player + waveform thread before destruction."""
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
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self._session.document.to_json(), indent=2)
            path.write_text(payload, encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self._session.mark_saved()
        self.document_saved.emit(path)
        _LOG.info("saved document to %s", path)

    def _handle_layout_toggle(self) -> None:
        new_layout = _toggle(self._settings.layout)
        if new_layout not in LAYOUT_CHOICES:
            new_layout = DEFAULT_LAYOUT
        self._settings.layout = new_layout
        save_settings(self._settings)
        self._outer.setOrientation(_orientation_for(new_layout))
        self._layout_btn.setText(self._layout_button_label(new_layout))
        self.layout_changed.emit(self._settings)

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
