"""Phase 5f — macOS polish: menu bar, quit guard, render UX, About, autosave indicator.

Tests are organised by section in the spec:

- §1 Menu bar — actions reparented onto MainWindow, enabled/disabled
  state mirrors the editor presence.
- §2 Quit-when-dirty — closeEvent prompt, three branches, save-failure
  cancels the close.
- §4 About dialog — opens and reports the right strings.
- §5 Non-modal render progress — status-bar widget shows during render,
  Export disabled while in flight, snapshot isolation across edits
  during render.
- §6 Autosave indicator — state transitions on dirty/save events.
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

if os.environ.get("WHISPER_NO_QT"):  # pragma: no cover - opt-in headless
    pytest.skip("WHISPER_NO_QT set", allow_module_level=True)

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtWidgets import QMessageBox  # noqa: E402

from core.document import Document, MediaSource, Range, Segment, Word  # noqa: E402
from core.settings import Settings  # noqa: E402
from ui_qt.app import MainWindow  # noqa: E402
from ui_qt.components.about_dialog import AboutDialog, about_text  # noqa: E402
from ui_qt.components.status_widgets import (  # noqa: E402
    AUTOSAVE_DIRTY,
    AUTOSAVE_FAILED,
    AUTOSAVE_SAVED,
    AUTOSAVE_SAVING,
    AutosaveStatusLabel,
    RenderStatusWidget,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_WAV = REPO_ROOT / "tests" / "fixtures" / "sample.wav"


def _make_doc() -> Document:
    seg = Segment(
        text="alpha beta",
        start=0.0,
        end=1.0,
        words=(
            Word(text="alpha", start=0.0, end=0.5),
            Word(text=" beta", start=0.5, end=1.0),
        ),
    )
    return Document(
        sources={"src0": MediaSource(id="src0", path=SAMPLE_WAV, duration=1.0)},
        segments=[seg],
        ranges=[Range(source_id="src0", start=0.0, end=1.0)],
        language="en",
        created_at=datetime.now(UTC),
        model_name="tiny",
        source_hash=None,
    )


def _set_selection(pane, start: int, end: int) -> None:
    view = pane.transcript_view
    view._selection_anchor = None
    view._set_selection(start, end)


@pytest.fixture
def main_window(qtbot, tmp_path, monkeypatch):
    """MainWindow with deterministic teardown.

    Two failure modes pytest-qt's default delete-on-end can hit when a
    test creates an :class:`EditorPane` via ``show_editor``:

    - ``QMediaPlayer`` set up by the pane keeps a private worker thread
      alive past the C++ widget destruction, which shows up as
      multi-second teardown stalls or "QThread destroyed while still
      running" aborts.
    - The waveform controller's peak-generation ``QThread`` similarly
      outlives a release that doesn't explicitly join it.

    Explicit ``_dispose_*_pane`` calls before yield-end give us the
    deterministic teardown each pane needs. ``_force_close`` short-
    circuits the dirty-prompt path so a test that left the document
    dirty doesn't block close on a modal with no responder.
    """
    monkeypatch.setenv("WHISPER_SETTINGS_DIR", str(tmp_path))
    win = MainWindow(settings=Settings(output_dir=str(tmp_path)))
    # pytest-qt's ``_close_widgets`` runs in ``pytest_runtest_teardown``
    # which on this plugin install fires *before* the fixture's
    # finalizer. That means a test that left the document dirty would
    # see ``closeEvent`` pop the unsaved-changes ``QMessageBox`` —
    # blocking forever, with no responder. Hand pytest-qt a hook that
    # flips the bypass flag immediately before the close, so close-time
    # is always non-blocking; tests that exercise the prompt path do
    # so explicitly via ``win.close()`` *during* the test body.
    qtbot.addWidget(
        win,
        before_close_func=lambda w: setattr(w, "_force_close", True),
    )
    win._pump_timer.stop()
    yield win
    win._dispose_editor_pane()
    win._dispose_transcribe_pane()


# ---------------------------------------------------------------------------
# §1 Menu bar — reparenting and enabled-state mirroring
# ---------------------------------------------------------------------------


def test_menu_bar_has_expected_top_level_menus(main_window):
    titles = [a.text() for a in main_window.menuBar().actions()]
    assert "File" in titles
    assert "Edit" in titles
    assert "View" in titles
    assert "Window" in titles
    assert "Help" in titles


def test_editor_actions_disabled_until_show_editor(main_window):
    a = main_window.editor_actions
    assert not a.save.isEnabled()
    assert not a.export_.isEnabled()
    assert not a.undo.isEnabled()
    assert not a.redo.isEnabled()
    assert not a.cut.isEnabled()


def test_editor_actions_enabled_after_show_editor(main_window):
    main_window.show_editor(_make_doc())
    a = main_window.editor_actions
    assert a.save.isEnabled()
    assert a.export_.isEnabled()
    assert a.cut.isEnabled()
    assert a.restore.isEnabled()
    # Undo/redo remain disabled until the user makes an edit.
    assert not a.undo.isEnabled()
    assert not a.redo.isEnabled()


def test_save_action_is_shared_with_editor_pane(main_window):
    """The same QAction instance lives on both MainWindow and EditorPane."""
    main_window.show_editor(_make_doc())
    assert main_window.editor_pane is not None
    assert main_window.editor_actions.save is main_window.editor_pane._save_action
    assert main_window.editor_actions.export_ is main_window.editor_pane._export_action
    assert main_window.editor_actions.undo is main_window.editor_pane._undo_action
    assert main_window.editor_actions.redo is main_window.editor_pane._redo_action
    assert main_window.editor_actions.cut is main_window.editor_pane._cut_action
    assert main_window.editor_actions.restore is main_window.editor_pane._restore_action


def test_undo_action_enables_after_first_edit(main_window):
    main_window.show_editor(_make_doc())
    _set_selection(main_window.editor_pane, 0, 1)
    main_window.editor_actions.cut.trigger()
    assert main_window.editor_actions.undo.isEnabled()


def test_actions_disable_again_on_back_to_transcribe(main_window):
    main_window.show_editor(_make_doc())
    main_window.editor_pane.back_to_transcribe.emit()
    a = main_window.editor_actions
    assert not a.save.isEnabled()
    assert not a.export_.isEnabled()
    assert not a.cut.isEnabled()


def test_layout_action_label_reflects_current_layout(main_window, monkeypatch, tmp_path):
    """Toggle Layout menu item shows the *destination* orientation."""
    main_window.settings.layout = "video_top"
    main_window._refresh_layout_action_label()
    assert "Left" in main_window._layout_action.text()

    main_window.settings.layout = "video_left"
    main_window._refresh_layout_action_label()
    assert "Top" in main_window._layout_action.text()


def test_settings_action_has_preferences_role(main_window):
    """macOS routes Cmd-, to the application menu via ``MenuRole.PreferencesRole``."""
    from PySide6.QtGui import QAction

    assert main_window._prefs_action.menuRole() == QAction.MenuRole.PreferencesRole
    assert main_window._about_action.menuRole() == QAction.MenuRole.AboutRole
    assert main_window._quit_action.menuRole() == QAction.MenuRole.QuitRole


# ---------------------------------------------------------------------------
# §2 Quit-when-dirty
# ---------------------------------------------------------------------------


def _make_dirty(window: MainWindow) -> None:
    window.show_editor(_make_doc())
    _set_selection(window.editor_pane, 0, 1)
    window.editor_actions.cut.trigger()
    assert window.editor_pane.session.is_dirty


def test_close_with_clean_editor_closes_immediately(main_window, monkeypatch):
    main_window.show_editor(_make_doc())
    called: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "exec",
        lambda self: called.append("prompted") or QMessageBox.StandardButton.Save.value,
    )
    accepted = main_window.close()
    assert accepted is True
    assert called == []  # clean editor never prompts


def test_close_with_dirty_editor_prompts(main_window, monkeypatch):
    _make_dirty(main_window)
    captured: list[QMessageBox] = []

    def _spy(self):
        captured.append(self)
        return int(QMessageBox.StandardButton.Cancel)

    monkeypatch.setattr(QMessageBox, "exec", _spy)
    accepted = main_window.close()
    assert captured, "expected a QMessageBox to be shown"
    assert accepted is False  # Cancel branch keeps the window open


def test_close_dirty_discard_branch_closes(main_window, monkeypatch):
    _make_dirty(main_window)
    monkeypatch.setattr(
        QMessageBox, "exec",
        lambda self: int(QMessageBox.StandardButton.Discard),
    )
    accepted = main_window.close()
    assert accepted is True


def test_close_dirty_save_branch_writes_and_closes(main_window, monkeypatch):
    _make_dirty(main_window)
    monkeypatch.setattr(
        QMessageBox, "exec",
        lambda self: int(QMessageBox.StandardButton.Save),
    )
    save_path = main_window.editor_pane._save_path()
    assert save_path is not None
    accepted = main_window.close()
    assert accepted is True
    assert save_path.is_file()


def test_close_dirty_save_failure_cancels_close(main_window, monkeypatch):
    """If the save raises, the close is cancelled and the error surfaces."""
    _make_dirty(main_window)

    # First the unsaved-changes prompt → user picks Save.
    # Then the "Save failed" critical → user OKs.
    answers = iter([
        int(QMessageBox.StandardButton.Save),
        int(QMessageBox.StandardButton.Ok),
    ])
    monkeypatch.setattr(QMessageBox, "exec", lambda self: next(answers))
    monkeypatch.setattr(
        QMessageBox, "critical",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Ok,
    )

    def _boom(_path):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(main_window.editor_pane, "_write_document_to", _boom)
    accepted = main_window.close()
    assert accepted is False
    # And the editor is still alive and still dirty.
    assert main_window.editor_pane is not None
    assert main_window.editor_pane.session.is_dirty


# ---------------------------------------------------------------------------
# §4 About dialog
# ---------------------------------------------------------------------------


def test_about_dialog_opens(qtbot, main_window):
    dlg = AboutDialog(main_window)
    qtbot.addWidget(dlg)
    # Just opening shouldn't crash; close immediately.
    dlg.show()
    dlg.close()


def test_about_text_contains_version_and_runtime():
    text = about_text()
    assert "Whisper Transcriber" in text
    assert "v0.5.0" in text
    assert "Python " in text
    assert "PySide6" in text


# ---------------------------------------------------------------------------
# §5 Render progress — non-modal status bar
# ---------------------------------------------------------------------------


def test_render_status_widget_lifecycle(qtbot):
    w = RenderStatusWidget()
    qtbot.addWidget(w)
    assert not w.isVisible()
    w.start("Rendering…")
    assert w.isVisible()
    w.set_progress(0.5)
    w.finish()
    assert not w.isVisible()


def test_render_status_cancel_emits_signal(qtbot):
    w = RenderStatusWidget()
    qtbot.addWidget(w)
    w.start()
    received = []
    w.cancel_clicked.connect(lambda: received.append(True))
    w._cancel_btn.click()
    assert received == [True]


def test_render_started_shows_status_widget(main_window, tmp_path):
    main_window.show_editor(_make_doc())
    out = tmp_path / "out.mp4"
    main_window.editor_pane.render_started.emit(out)
    # ``isVisible`` is False unless the parent window is also shown; in
    # tests we don't ``show()`` the MainWindow, so check ``isHidden`` —
    # the widget's own visibility flag — instead. Same effective contract.
    assert not main_window._render_status.isHidden()
    # Export action is disabled while a render is in flight.
    assert not main_window.editor_actions.export_.isEnabled()


def test_render_completed_hides_status_widget(main_window, tmp_path, monkeypatch):
    main_window.show_editor(_make_doc())
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    out = tmp_path / "out.mp4"
    main_window.editor_pane.render_started.emit(out)
    main_window.editor_pane.render_completed.emit(out)
    assert main_window._render_status.isHidden()
    assert main_window.editor_actions.export_.isEnabled()


def test_render_failed_re_enables_export(main_window, tmp_path, monkeypatch):
    main_window.show_editor(_make_doc())
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)
    out = tmp_path / "out.mp4"
    main_window.editor_pane.render_started.emit(out)
    main_window.editor_pane.render_failed.emit("ffmpeg blew up")
    assert main_window._render_status.isHidden()
    assert main_window.editor_actions.export_.isEnabled()


def test_render_cancel_request_drives_widget_to_cancelling(main_window, tmp_path):
    """Clicking Cancel on the status widget locks it and asks the editor to cancel."""
    main_window.show_editor(_make_doc())
    main_window.editor_pane.render_started.emit(tmp_path / "out.mp4")

    cancel_event = threading.Event()
    main_window.editor_pane._render_cancel_event = cancel_event
    # Stub the render-thread surface ``release()`` touches: ``isRunning``
    # for is_rendering, plus ``quit``/``wait`` for the dispose path Qt
    # walks through during fixture teardown. Avoids spinning a real worker.
    stub = SimpleNamespace(
        isRunning=lambda: True,
        quit=lambda: None,
        wait=lambda _ms=None: True,
    )
    main_window.editor_pane._render_thread = stub

    main_window._render_status.cancel_clicked.emit()
    assert cancel_event.is_set()
    # Cancel button has been disabled while the worker unwinds.
    assert not main_window._render_status._cancel_btn.isEnabled()
    # Reset so teardown's release() doesn't try to quit our stub.
    main_window.editor_pane._render_thread = None


def test_render_during_edit_uses_pre_render_snapshot(main_window, tmp_path, monkeypatch):
    """Edits made during a render don't reach the rendered output.

    The contract: ``RenderWorker`` snapshots the document at construction
    (it stores ``self.document`` once) and ``core.render.render_cut``
    consumes it synchronously. Mid-render ``DocumentSession`` mutations
    flow into a *new* Document object on the session — the worker's
    snapshot is unaffected.
    """
    from workers.render import RenderWorker

    main_window.show_editor(_make_doc())
    snapshot_doc = main_window.editor_pane.document

    captured = {}

    def _fake_render(*args, **kwargs):
        captured["doc"] = args[0]
        captured["ranges"] = list(args[0].ranges)

    monkeypatch.setattr("workers.render.render_cut", _fake_render)
    worker = RenderWorker(
        document=snapshot_doc,
        output_path=tmp_path / "out.mp4",
        settings=main_window.settings,
        on_event=lambda _e: None,
    )
    # Mutate the *session* between worker construction and worker run.
    _set_selection(main_window.editor_pane, 0, 1)
    main_window.editor_actions.cut.trigger()
    assert main_window.editor_pane.document is not snapshot_doc

    worker.run()
    # The worker rendered the original ranges, not the post-cut ones.
    assert captured["doc"] is snapshot_doc
    assert captured["ranges"] == list(snapshot_doc.ranges)


# ---------------------------------------------------------------------------
# §6 Autosave indicator
# ---------------------------------------------------------------------------


def test_autosave_label_state_transitions(qtbot):
    label = AutosaveStatusLabel()
    qtbot.addWidget(label)
    label.set_state(AUTOSAVE_SAVING)
    label._flush()  # bypass debounce
    assert label.text() == "Saving…"
    label.set_state(AUTOSAVE_SAVED)
    label._flush()
    assert label.text() == "Saved"
    label.set_state(AUTOSAVE_DIRTY)
    label._flush()
    assert label.text() == "Unsaved changes"
    label.set_state(AUTOSAVE_FAILED)
    label._flush()
    assert label.text() == "Autosave failed"


def test_autosave_label_debounce_coalesces_quick_flips(qtbot):
    """Rapid set_state calls within debounce window settle on the last value."""
    label = AutosaveStatusLabel()
    qtbot.addWidget(label)
    label.set_state(AUTOSAVE_SAVING)
    label.set_state(AUTOSAVE_SAVED)
    label.set_state(AUTOSAVE_DIRTY)
    label._flush()
    assert label.state == AUTOSAVE_DIRTY


def test_dirty_changed_with_autosave_off_shows_unsaved(main_window):
    main_window.settings.autosave_interval_s = 0
    main_window.show_editor(_make_doc())
    _set_selection(main_window.editor_pane, 0, 1)
    main_window.editor_actions.cut.trigger()
    main_window._autosave_label._flush()
    assert main_window._autosave_label.state == AUTOSAVE_DIRTY


def test_dirty_changed_with_autosave_on_shows_saving(main_window):
    main_window.settings.autosave_interval_s = 30
    main_window.show_editor(_make_doc())
    _set_selection(main_window.editor_pane, 0, 1)
    main_window.editor_actions.cut.trigger()
    main_window._autosave_label._flush()
    assert main_window._autosave_label.state == AUTOSAVE_SAVING


def test_document_saved_marks_label_saved(main_window, tmp_path):
    main_window.show_editor(_make_doc())
    _set_selection(main_window.editor_pane, 0, 1)
    main_window.editor_actions.cut.trigger()
    # Programmatic save round-trip.
    main_window.editor_actions.save.trigger()
    main_window._autosave_label._flush()
    assert main_window._autosave_label.state == AUTOSAVE_SAVED


# ---------------------------------------------------------------------------
# Pane-swap sanity — actions disconnect cleanly
# ---------------------------------------------------------------------------


def test_action_connections_disconnect_on_release(qtbot, tmp_path, monkeypatch):
    """Two consecutive editor pane swaps don't leave double-fired handlers."""
    monkeypatch.setenv("WHISPER_SETTINGS_DIR", str(tmp_path))
    win = MainWindow(settings=Settings(output_dir=str(tmp_path)))
    qtbot.addWidget(win)
    win._pump_timer.stop()
    try:
        win.show_editor(_make_doc())
        first = win.editor_pane
        win.show_editor(_make_doc())
        second = win.editor_pane
        assert first is not second
        # The first pane's action_connections were cleared on release.
        assert first._action_connections == []
        # The second pane has live connections.
        assert len(second._action_connections) == 7
    finally:
        win.close()
