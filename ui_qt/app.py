"""PySide6 main window. Owns settings, state machine, worker, and pane swap.

Phase 5b restructured the window: the transcribe-flow widgets moved
into :class:`ui_qt.transcribe_pane.TranscribePane`, and the new
:class:`ui_qt.editor_pane.EditorPane` lives behind ``show_editor()``.
The window swaps between the two via ``setCentralWidget`` (Decision 4
— single window, state swap, no second window).

Phase 5f layered macOS-native polish on top of that: a real menu bar
(File / Edit / View / Window / Help plus the auto-routed application
menu via ``QAction.MenuRole``); a quit-when-dirty guard in
:meth:`closeEvent`; an app icon loaded via :meth:`QApplication.setWindowIcon`;
an About dialog; and a non-modal render progress strip in the status
bar so the editor stays interactive while a render is in flight.

Editor actions live on MainWindow rather than EditorPane so they
survive pane swaps. EditorPane is given the same :class:`EditorActions`
bundle every time it's constructed; it wires the actions in
``_wire_actions`` and disconnects in ``release`` to prevent stale
double-handling. The toolbar buttons inside the pane stay — they call
the same handlers directly, so users get both menu shortcuts and an
in-pane button row.

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

from PySide6.QtCore import QEvent, QTimer
from PySide6.QtGui import QAction, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMainWindow,
    QMenu,
    QMessageBox,
    QStatusBar,
    QWidget,
)

from core.document import Document, UnsupportedSchemaError
from core.settings import Settings, load_settings
from ui.state import (
    AppState,
    AppStateMachine,
    pump_queue,
)
from ui_qt import __version__
from ui_qt.components.about_dialog import AboutDialog
from ui_qt.components.settings_panel import SettingsDialog
from ui_qt.components.status_widgets import (
    AUTOSAVE_DIRTY,
    AUTOSAVE_SAVED,
    AUTOSAVE_SAVING,
    AutosaveStatusLabel,
    RenderStatusWidget,
)
from ui_qt.editor_pane import EditorActions, EditorPane
from ui_qt.style import (
    WINDOW_DEFAULT_SIZE,
    WINDOW_MIN_SIZE,
    WINDOW_TITLE,
)
from ui_qt.transcribe_pane import TranscribePane
from workers.transcription import TranscriptionWorker

PUMP_INTERVAL_MS = 100
_LOG = logging.getLogger(__name__)

# App icon — committed at ``resources/icons/transcribe.icns`` and
# regenerated via ``scripts/make_icon.py``. Resolved relative to the
# repo root because the app is currently launched as
# ``python main_qt.py``; bundled-app launching (post-Phase-5) will
# pick the icon up via the .app's Info.plist anyway.
ICON_PATH = Path(__file__).resolve().parent.parent / "resources" / "icons" / "transcribe.icns"


class MainWindow(QMainWindow):
    """Top-level window. Owns settings, state, the worker, and the pane swap."""

    def __init__(self, *, settings: Settings | None = None) -> None:
        super().__init__()
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(*WINDOW_DEFAULT_SIZE)
        self.setMinimumSize(*WINDOW_MIN_SIZE)
        if ICON_PATH.is_file():
            self.setWindowIcon(QIcon(str(ICON_PATH)))

        self.settings = settings or load_settings()
        self.state = AppStateMachine()
        self.event_queue: queue.Queue = queue.Queue()
        self._worker: TranscriptionWorker | None = None
        self._worker_thread: threading.Thread | None = None
        self._cancel_requested = threading.Event()

        self._transcribe_pane: TranscribePane | None = None
        self._editor_pane: EditorPane | None = None
        self._force_close: bool = False  # set after the user picks Discard or saves successfully

        # Actions outlive any single EditorPane — they're built once
        # here and handed down to every EditorPane this window spawns.
        self._build_actions()
        self._build_menu_bar()
        self._setup_status_bar()

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

    @property
    def editor_actions(self) -> EditorActions:
        return self._editor_actions

    # ----- menu bar / actions -----

    def _build_actions(self) -> None:
        """Construct the editor's :class:`EditorActions` bundle plus app-menu actions.

        The editor actions stay disabled until ``show_editor`` swaps a
        document in. Re-enable on swap, disable on dispose. Same QAction
        instances are shared across all editor panes — the spec's
        "no duplicates → no ambiguous-shortcut warnings" guideline.
        """
        # Editor actions — shared with every EditorPane.
        save = QAction("Save", self)
        save.setShortcut(QKeySequence.StandardKey.Save)
        export_ = QAction("Export…", self)
        export_.setShortcuts([QKeySequence("Ctrl+E"), QKeySequence("Meta+E")])
        undo = QAction("Undo", self)
        undo.setShortcut(QKeySequence.StandardKey.Undo)
        redo = QAction("Redo", self)
        redo.setShortcut(QKeySequence.StandardKey.Redo)
        cut = QAction("Cut", self)
        cut.setShortcuts([QKeySequence.StandardKey.Cut, QKeySequence("Backspace")])
        restore = QAction("Restore Cuts", self)
        restore.setShortcuts(
            [QKeySequence("Ctrl+Shift+Backspace"), QKeySequence("Meta+Backspace")]
        )
        delete = QAction("Delete", self)
        delete.setShortcut(QKeySequence.StandardKey.Delete)
        for a in (save, export_, undo, redo, cut, restore, delete):
            a.setEnabled(False)
        self._editor_actions = EditorActions(
            save=save, export_=export_, undo=undo, redo=redo,
            cut=cut, restore=restore, delete=delete,
        )

        # Application menu actions — set the right ``MenuRole`` so macOS
        # routes them to the correct menu regardless of which menu we
        # add them to.
        self._about_action = QAction("About Transcribe", self)
        self._about_action.setMenuRole(QAction.MenuRole.AboutRole)
        self._about_action.triggered.connect(self._show_about)

        self._prefs_action = QAction("Settings…", self)
        self._prefs_action.setMenuRole(QAction.MenuRole.PreferencesRole)
        self._prefs_action.setShortcut(QKeySequence("Ctrl+,"))
        self._prefs_action.triggered.connect(self._open_settings)

        self._quit_action = QAction("Quit Transcribe", self)
        self._quit_action.setMenuRole(QAction.MenuRole.QuitRole)
        self._quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        self._quit_action.triggered.connect(self.close)

        # File-menu actions.
        self._open_action = QAction("Open…", self)
        self._open_action.setShortcut(QKeySequence.StandardKey.Open)
        self._open_action.triggered.connect(self._handle_open_project)

        self._close_window_action = QAction("Close Window", self)
        self._close_window_action.setShortcut(QKeySequence.StandardKey.Close)
        self._close_window_action.triggered.connect(self.close)

        # View — toggle layout. Label updates via ``_refresh_layout_action_label``
        # any time the layout changes.
        self._layout_action = QAction("", self)
        self._layout_action.triggered.connect(self._handle_toggle_layout)
        self._refresh_layout_action_label()

        # Window menu — Qt populates Minimize/Bring-All-to-Front via the
        # role system, but Cmd-M also needs a regular QAction so the
        # shortcut works without the user opening the menu. Standard
        # ``StandardKey.Minimize`` doesn't exist; bind the literal.
        self._minimize_action = QAction("Minimize", self)
        self._minimize_action.setShortcut(QKeySequence("Ctrl+M"))
        self._minimize_action.triggered.connect(self.showMinimized)

    def _build_menu_bar(self) -> None:
        mb = self.menuBar()
        mb.setNativeMenuBar(True)  # macOS-native; no-op on other platforms.

        # File
        file_menu: QMenu = mb.addMenu("File")
        file_menu.addAction(self._open_action)
        file_menu.addSeparator()
        file_menu.addAction(self._editor_actions.save)
        file_menu.addAction(self._editor_actions.export_)
        file_menu.addSeparator()
        file_menu.addAction(self._close_window_action)

        # Edit
        edit_menu: QMenu = mb.addMenu("Edit")
        edit_menu.addAction(self._editor_actions.undo)
        edit_menu.addAction(self._editor_actions.redo)
        edit_menu.addSeparator()
        edit_menu.addAction(self._editor_actions.cut)
        edit_menu.addAction(self._editor_actions.restore)

        # View
        view_menu: QMenu = mb.addMenu("View")
        view_menu.addAction(self._layout_action)

        # Window
        window_menu: QMenu = mb.addMenu("Window")
        window_menu.addAction(self._minimize_action)

        # Help — Qt auto-populates the macOS Search field; an explicit
        # placeholder action keeps the menu visible even when empty.
        mb.addMenu("Help")

        # Application-menu actions. macOS re-routes these to the bold
        # "Transcribe" menu via their ``MenuRole``; on other platforms
        # they appear under the menu we added them to.
        edit_menu.addSeparator()
        edit_menu.addAction(self._prefs_action)
        edit_menu.addAction(self._about_action)
        edit_menu.addAction(self._quit_action)

    def _refresh_layout_action_label(self) -> None:
        """Reflect the *next* layout in the menu label, since Toggle = "switch to."""
        if self.settings.layout == "video_left":
            self._layout_action.setText("Switch to Video on Top")
        else:
            self._layout_action.setText("Switch to Video on Left")

    def _set_editor_actions_enabled(self, on: bool) -> None:
        a = self._editor_actions
        a.save.setEnabled(on)
        a.export_.setEnabled(on)
        a.cut.setEnabled(on)
        a.restore.setEnabled(on)
        a.delete.setEnabled(on)
        if not on:
            a.undo.setEnabled(False)
            a.redo.setEnabled(False)
        # Layout toggle is always available, even from the transcribe
        # pane — flipping it there changes the editor's first orientation.
        self._layout_action.setEnabled(True)

    # ----- status bar -----

    def _setup_status_bar(self) -> None:
        bar = QStatusBar(self)
        self.setStatusBar(bar)
        self._autosave_label = AutosaveStatusLabel()
        bar.addWidget(self._autosave_label, 1)
        self._render_status = RenderStatusWidget()
        self._render_status.cancel_clicked.connect(self._handle_render_cancel_request)
        bar.addPermanentWidget(self._render_status)

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
        self._set_editor_actions_enabled(False)
        self._autosave_label.clear_indicator()

    def show_editor(self, document: Document) -> None:
        """Swap to a fresh EditorPane bound to ``document``.

        Disposing both panes first lets this method handle every entry
        path: from the transcribe pane (the normal case) and from a
        prior editor pane (open-project-while-editing, or tests that
        cycle ``show_editor``). Without the editor-to-editor dispose,
        the displaced pane's QAction connections would stay live and
        every ``Cmd-S`` would double-handle until DeferredDelete swept.
        """
        self._dispose_transcribe_pane()
        self._dispose_editor_pane()
        editor = EditorPane(document, settings=self.settings, actions=self._editor_actions)
        editor.back_to_transcribe.connect(self._handle_back_from_editor)
        editor.layout_changed.connect(self._apply_settings)
        editor.dirty_changed.connect(self._refresh_window_title)
        editor.dirty_changed.connect(self._handle_editor_dirty_changed)
        editor.document_saved.connect(self._handle_document_saved)
        editor.render_started.connect(self._handle_render_started)
        editor.render_progress.connect(self._handle_render_progress)
        editor.render_completed.connect(self._handle_render_completed)
        editor.render_failed.connect(self._handle_render_failed)
        editor.render_cancelled.connect(self._handle_render_cancelled)
        self._editor_pane = editor
        self.setCentralWidget(editor)
        self._refresh_window_title(False)
        self._set_editor_actions_enabled(True)
        self._refresh_layout_action_label()
        self._handle_editor_dirty_changed(False)

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
        self._render_status.finish()
        self._autosave_label.clear_indicator()
        self._set_editor_actions_enabled(False)

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
            "Transcribe project (*.transcribe.json);;Audio/video (*.mp4 *.mp3 *.m4a *.wav *.mov);;All files (*.*)",
        )
        if not chosen:
            return
        path = Path(chosen)
        # If the user picked a media file rather than a project, route
        # it through the transcribe pane's file-selected path. The
        # extension check is loose — Document.from_json will reject
        # anything that isn't valid project JSON anyway.
        if path.suffix.lower() != ".json":
            self._handle_file_selected(path)
            return
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

    def _handle_toggle_layout(self) -> None:
        """View → Toggle Layout. Forwards to the editor when one's open."""
        if self._editor_pane is not None:
            self._editor_pane._handle_layout_toggle()
        else:
            # No editor; toggle the persisted setting so the next editor
            # opens in the new orientation.
            new_layout = "video_left" if self.settings.layout == "video_top" else "video_top"
            self.settings.layout = new_layout
            self.settings.editor_splitter_state = None  # clear stale orientation blob
            from core.settings import save_settings

            save_settings(self.settings)
        self._refresh_layout_action_label()

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
        """Save just landed (manual or autosave). Reflect in the autosave label."""
        self._autosave_label.set_state(AUTOSAVE_SAVED)

    def _handle_editor_dirty_changed(self, dirty: bool) -> None:
        """Drive the autosave indicator off the editor's dirty state."""
        if self._editor_pane is None:
            self._autosave_label.clear_indicator()
            return
        autosave_on = self.settings.autosave_interval_s > 0
        if not dirty:
            self._autosave_label.set_state(AUTOSAVE_SAVED)
            return
        if autosave_on:
            # An autosave tick will fire shortly; mark the in-flight
            # state so the user sees momentum.
            self._autosave_label.set_state(AUTOSAVE_SAVING)
        else:
            self._autosave_label.set_state(AUTOSAVE_DIRTY)

    # ----- render status -----

    def _handle_render_started(self, _output: Path) -> None:
        self._render_status.start()
        self._editor_actions.export_.setEnabled(False)

    def _handle_render_progress(self, fraction: float) -> None:
        self._render_status.set_progress(fraction)

    def _handle_render_completed(self, output: Path) -> None:
        self._render_status.finish()
        if self._editor_pane is not None:
            self._editor_actions.export_.setEnabled(True)
        QMessageBox.information(
            self, "Export complete",
            f"Wrote {output.name}.",
        )

    def _handle_render_failed(self, message: str) -> None:
        self._render_status.finish()
        if self._editor_pane is not None:
            self._editor_actions.export_.setEnabled(True)
        QMessageBox.critical(self, "Export failed", message)

    def _handle_render_cancelled(self) -> None:
        self._render_status.finish()
        if self._editor_pane is not None:
            self._editor_actions.export_.setEnabled(True)

    def _handle_render_cancel_request(self) -> None:
        if self._editor_pane is not None and self._editor_pane.is_rendering:
            self._render_status.mark_cancelling()
            self._editor_pane.cancel_render()

    # ----- About / Settings -----

    def _show_about(self) -> None:
        AboutDialog(self).exec()

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self, current=self.settings)
        dlg.settings_saved.connect(self._apply_settings)
        dlg.exec()

    def _apply_settings(self, new: Settings) -> None:
        self.settings = new
        if self._transcribe_pane is not None:
            self._transcribe_pane.update_settings(new)
        if self._editor_pane is not None:
            # Layout flip and autosave-interval changes propagate live;
            # the rest take effect on the next render or save.
            self._editor_pane.apply_settings(new)
        self._refresh_layout_action_label()
        # Autosave-on-or-off transition might have flipped the dirty
        # label semantics ("Unsaved" ↔ "Saving…"); refresh once.
        if self._editor_pane is not None:
            self._handle_editor_dirty_changed(self._editor_pane.session.is_dirty)

    # ----- close handling -----

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        """Quit guard — prompt on dirty editor; cancel close on Save failure."""
        if not self._force_close and self._has_unsaved_changes():
            choice = self._prompt_unsaved()
            if choice == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            if choice == QMessageBox.StandardButton.Save:
                if not self._save_for_quit():
                    event.ignore()
                    return
            # Save succeeded or user picked Discard → fall through.
        self._cancel_requested.set()
        if self._worker is not None:
            self._worker.cancel()
        self._pump_timer.stop()
        self._dispose_editor_pane()
        super().closeEvent(event)

    def _has_unsaved_changes(self) -> bool:
        return (
            self._editor_pane is not None
            and self._editor_pane.session.is_dirty
        )

    def _prompt_unsaved(self) -> QMessageBox.StandardButton:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Unsaved changes")
        box.setText("You have unsaved changes to this document.")
        box.setInformativeText("Save before closing?")
        box.setStandardButtons(
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel
        )
        box.setDefaultButton(QMessageBox.StandardButton.Save)
        return QMessageBox.StandardButton(box.exec())

    def _save_for_quit(self) -> bool:
        """Drive the editor's save handler; surface failures and return success."""
        if self._editor_pane is None:
            return True
        path = self._editor_pane._save_path()
        if path is None:
            QMessageBox.critical(
                self, "Save failed",
                "Cannot save: no source media is associated with this document.",
            )
            return False
        try:
            self._editor_pane._write_document_to(path)
        except OSError as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return False
        self._editor_pane.session.mark_saved()
        return True

    # ----- application reopen on Dock click -----

    def event(self, evt) -> bool:  # noqa: N802 (Qt API)
        if evt.type() == QEvent.Type.ApplicationActivate:
            # Dock-icon click while the window was minimized — restore.
            if self.isMinimized():
                self.showNormal()
        return super().event(evt)

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
    app.setApplicationName("Transcribe")
    app.setApplicationDisplayName("Transcribe")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("Aaron Ramos")
    if ICON_PATH.is_file():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
    win = MainWindow()
    win.show()
    return app.exec()


# Kept available for tests / future use.
def _notify_error(parent: QWidget, message: str) -> None:  # pragma: no cover - tiny
    QMessageBox.critical(parent, "Transcription failed", message)
