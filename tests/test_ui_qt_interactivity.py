"""Phase 5c — pytest-qt tests for transcript interactivity.

Behavioural coverage of:

- :class:`ui_qt.document_session.DocumentSession` — apply / undo / redo
  / dirty tracking against fixture commands (no Qt GUI needed).
- :class:`ui_qt.components.transcript_view.TranscriptView` — word
  click → seek, drag-select, selection clearing, playhead-follow.
- :class:`ui_qt.editor_pane.EditorPane` — Cmd-X / Cmd-Z / Cmd-S
  shortcuts via QAction trigger; cut + restore + undo + save round
  trip; title-bar dirty marker.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

if os.environ.get("WHISPER_NO_QT"):  # pragma: no cover - opt-in headless
    pytest.skip("WHISPER_NO_QT set", allow_module_level=True)

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QFont, QMouseEvent  # noqa: E402

from core.document import Document, MediaSource, Range, Segment, Word  # noqa: E402
from core.editing import AddCut  # noqa: E402
from core.settings import Settings  # noqa: E402
from ui_qt.app import MainWindow  # noqa: E402
from ui_qt.document_session import DocumentSession  # noqa: E402
from ui_qt.editor_pane import EditorPane  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_WAV = REPO_ROOT / "tests" / "fixtures" / "sample.wav"


# ---------------------------------------------------------------------------
# Fixture document — flat single segment for crisp word-index assertions
# ---------------------------------------------------------------------------


def _make_doc(media_path: Path = SAMPLE_WAV, *, with_cut: bool = False) -> Document:
    """Six contiguous words. Optionally pre-cut words 2–3 (1.0–2.0)."""
    words = (
        Word(text="alpha", start=0.0, end=0.5),
        Word(text=" beta", start=0.5, end=1.0),
        Word(text=" gamma", start=1.0, end=1.5),
        Word(text=" delta", start=1.5, end=2.0),
        Word(text=" epsilon", start=2.0, end=2.5),
        Word(text=" zeta", start=2.5, end=3.0),
    )
    seg = Segment(text="alpha beta gamma delta epsilon zeta", start=0.0, end=3.0, words=words)
    ranges: list[Range] = (
        [
            Range(source_id="src0", start=0.0, end=1.0),
            Range(source_id="src0", start=2.0, end=3.0),
        ]
        if with_cut
        else [Range(source_id="src0", start=0.0, end=3.0)]
    )
    return Document(
        sources={"src0": MediaSource(id="src0", path=media_path, duration=3.0)},
        segments=[seg],
        ranges=ranges,
        language="en",
        created_at=datetime.now(UTC),
        model_name="tiny",
        source_hash=None,
    )


# ---------------------------------------------------------------------------
# DocumentSession — pure-state behavior
# ---------------------------------------------------------------------------


def test_session_apply_pushes_and_changes_document(qtbot):
    sess = DocumentSession(_make_doc())
    before = sess.document
    changed = sess.apply(AddCut(start=1.0, end=2.0))
    assert changed is True
    assert sess.document is not before
    assert sess.can_undo is True
    assert sess.can_redo is False
    assert sess.is_dirty is True


def test_session_apply_noop_returns_false_and_doesnt_push(qtbot):
    """Applying a cut that produces equal ranges should not add to undo stack."""
    doc = _make_doc(with_cut=True)
    sess = DocumentSession(doc)
    # Cutting the already-cut interval is a no-op via subtract_interval.
    changed = sess.apply(AddCut(start=1.0, end=2.0))
    assert changed is False
    assert sess.can_undo is False
    assert sess.is_dirty is False


def test_session_undo_redo_round_trip(qtbot):
    sess = DocumentSession(_make_doc())
    initial = sess.document
    sess.apply(AddCut(start=1.0, end=2.0))
    after = sess.document
    sess.undo()
    assert sess.document is initial
    assert sess.can_undo is False
    assert sess.can_redo is True
    sess.redo()
    assert sess.document is after
    assert sess.can_undo is True
    assert sess.can_redo is False


def test_session_dirty_clears_on_undo_back_to_pristine(qtbot):
    """The undo-back-to-pristine clears-dirty contract from spec §5."""
    sess = DocumentSession(_make_doc())
    sess.apply(AddCut(start=1.0, end=2.0))
    assert sess.is_dirty is True
    sess.undo()
    assert sess.is_dirty is False  # back to the document we started with


def test_session_dirty_after_save_clears(qtbot):
    sess = DocumentSession(_make_doc())
    sess.apply(AddCut(start=1.0, end=2.0))
    assert sess.is_dirty is True
    sess.mark_saved()
    assert sess.is_dirty is False


def test_session_dirty_after_save_then_edit(qtbot):
    sess = DocumentSession(_make_doc())
    sess.apply(AddCut(start=1.0, end=2.0))
    sess.mark_saved()
    sess.apply(AddCut(start=2.0, end=2.5))
    assert sess.is_dirty is True


def test_session_fork_after_save_unreachable_pristine_stays_dirty(qtbot):
    """save A → undo to B → edit to C: A is unreachable, must stay dirty."""
    sess = DocumentSession(_make_doc())
    sess.apply(AddCut(start=0.5, end=1.0))   # A
    sess.mark_saved()                         # saved at A
    sess.undo()                               # at original (B)
    assert sess.is_dirty is True              # B != A
    sess.apply(AddCut(start=2.0, end=2.5))   # fork → C; A's redo entry is gone
    assert sess.is_dirty is True              # current is C, saved is A; dirty


def test_session_signals_fire(qtbot):
    sess = DocumentSession(_make_doc())
    docs: list = []
    dirty: list[bool] = []
    sess.document_changed.connect(docs.append)
    sess.dirty_changed.connect(dirty.append)
    sess.apply(AddCut(start=1.0, end=2.0))
    assert len(docs) == 1
    assert dirty == [True]
    sess.undo()
    assert len(docs) == 2
    assert dirty == [True, False]


# ---------------------------------------------------------------------------
# EditorPane / TranscriptView — interactive behaviour
# ---------------------------------------------------------------------------


@pytest.fixture
def editor(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_SETTINGS_DIR", str(tmp_path))
    # Redirect Document save destination away from the repo's
    # tests/fixtures/ — without ``output_dir`` the save resolves to
    # ``media_path.parent`` (sample.wav's directory) and pollutes the
    # tree with ``sample.transcribe.json``.
    settings = Settings(output_dir=str(tmp_path))
    pane = EditorPane(_make_doc(), settings=settings)
    qtbot.addWidget(pane)
    yield pane
    pane.release()


def _set_selection(pane: EditorPane, start: int, end: int) -> None:
    """Programmatically set the transcript selection by index."""
    view = pane.transcript_view
    view._selection_anchor = None
    view._set_selection(start, end)


def test_word_click_emits_signal_and_seek(editor, qtbot):
    """A click on a word's character run emits word_clicked + seek.

    We synthesize the press/release through the public mouse-event
    handlers, bypassing pixel geometry (which requires a shown window
    for ``cursorForPosition`` to return meaningful values). The handler
    contract — anchor word resolves to word_clicked + seek_requested
    on a release-without-drag — is what we're verifying.
    """
    view = editor.transcript_view
    seen_clicks: list[int] = []
    seen_seeks: list[int] = []
    view.word_clicked.connect(seen_clicks.append)
    view.seek_requested.connect(seen_seeks.append)

    # Stand in for the press: anchor on word 2, no drag.
    view._selection_anchor = 2
    view._selection_start = 2
    view._selection_end = 2
    view._dragged = False

    target = QPoint(0, 0)
    release = QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease, target, view.viewport().mapToGlobal(target),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
    )
    view.mouseReleaseEvent(release)

    assert seen_clicks == [2]
    # word 2 (" gamma") starts at 1.0 → 1000 ms.
    assert seen_seeks == [1000]


def test_drag_selection_sets_selection_signal(editor, qtbot):
    """Drag from word i to word j produces selection (i, j)."""
    view = editor.transcript_view
    spy: list = []
    view.selection_changed.connect(spy.append)
    _set_selection(editor, 1, 3)
    assert view.selection == (1, 3)
    assert spy[-1] == (1, 3)


def test_click_without_drag_clears_prior_selection(editor, qtbot):
    """Bare click clears any previous selection."""
    view = editor.transcript_view
    _set_selection(editor, 1, 3)
    assert view.selection == (1, 3)

    cursor = view._cursor_for_word(0)
    rect = view.cursorRect(cursor)
    target = QPoint(rect.left() + 4, rect.top() + rect.height() // 2)
    press = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress, target, view.viewport().mapToGlobal(target),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    release = QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease, target, view.viewport().mapToGlobal(target),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
    )
    view.mousePressEvent(press)
    view.mouseReleaseEvent(release)

    assert view.selection is None


def test_cmd_x_on_selection_pushes_addcut(editor, qtbot):
    """Cmd-X on a selection cuts the corresponding time interval."""
    _set_selection(editor, 2, 3)  # " gamma" + " delta" → 1.0–2.0
    before = editor.document
    editor._cut_action.trigger()
    after = editor.document
    # Re-render replaces the doc; ranges should now exclude 1.0–2.0.
    expected = [
        Range(source_id="src0", start=0.0, end=1.0),
        Range(source_id="src0", start=2.0, end=3.0),
    ]
    assert after.ranges == expected
    assert after is not before
    # Selection cleared after re-render.
    assert editor.transcript_view.selection is None


def test_cmd_x_on_no_selection_is_noop(editor, qtbot):
    """No selection → Cmd-X does nothing, no command pushed."""
    before = editor.document
    editor._cut_action.trigger()
    assert editor.document is before
    assert editor.session.can_undo is False


def test_cmd_x_on_already_cut_selection_pushes_restore(editor, qtbot):
    """All-already-cut selection routes through RestoreRange instead of AddCut."""
    # First cut words 2–3.
    _set_selection(editor, 2, 3)
    editor._cut_action.trigger()
    # Now both words are struck; selecting them again should restore.
    _set_selection(editor, 2, 3)
    editor._cut_action.trigger()
    final = editor.document
    # Ranges should be back to the original full coverage (or a single
    # union covering 0–3 in the canonical form).
    assert len(final.ranges) == 1
    assert final.ranges[0].start == pytest.approx(0.0)
    assert final.ranges[0].end == pytest.approx(3.0)


def test_undo_reverses_last_command(editor, qtbot):
    _set_selection(editor, 2, 3)
    editor._cut_action.trigger()
    after_cut = editor.document
    editor._undo_action.trigger()
    assert editor.document is not after_cut
    assert editor.session.can_undo is False
    assert editor.session.can_redo is True


def test_redo_replays_undone_command(editor, qtbot):
    _set_selection(editor, 2, 3)
    editor._cut_action.trigger()
    after_cut = editor.document
    editor._undo_action.trigger()
    editor._redo_action.trigger()
    assert editor.document is after_cut


def test_save_writes_to_candidate_cache_path_and_round_trips(editor, qtbot, tmp_path):
    """Save writes the Document to disk; reload reproduces it."""
    from workers.transcription import candidate_cache_path

    # Pre-cut so saved doc differs from the freshly-built one.
    _set_selection(editor, 2, 3)
    editor._cut_action.trigger()

    expected_path = candidate_cache_path(editor.settings, SAMPLE_WAV)
    editor._save_action.trigger()

    assert expected_path.is_file()
    reloaded = Document.from_json(json.loads(expected_path.read_text()))
    assert reloaded.ranges == editor.document.ranges
    # After save the editor is no longer dirty.
    assert editor.session.is_dirty is False


def test_save_failure_keeps_document_dirty(editor, qtbot, monkeypatch):
    """Mock the write to raise; dirty flag stays set."""
    _set_selection(editor, 2, 3)
    editor._cut_action.trigger()
    assert editor.session.is_dirty is True

    def _boom(self, data, encoding="utf-8"):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(Path, "write_text", _boom)
    # Suppress the modal QMessageBox.critical so the test doesn't block.
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **kw: None)

    editor._save_action.trigger()
    assert editor.session.is_dirty is True


def test_undo_back_to_pristine_clears_dirty_in_editor(editor, qtbot):
    """End-to-end version of session-level test: editor exposes the flag."""
    seen_dirty: list[bool] = []
    editor.dirty_changed.connect(seen_dirty.append)
    _set_selection(editor, 2, 3)
    editor._cut_action.trigger()
    editor._undo_action.trigger()
    assert seen_dirty == [True, False]


def test_playhead_follow_advances_monotonically(editor, qtbot):
    """Feeding ascending positions sets the playhead word in monotonic order."""
    view = editor.transcript_view
    seen: list[int] = []
    # Sample positions inside each word.
    for ms in (250, 750, 1250, 1750, 2250, 2750):
        view.set_playhead_position(ms)
        seen.append(view.playhead_word)
    assert seen == [0, 1, 2, 3, 4, 5]


def test_playhead_in_gap_clears_highlight(editor, qtbot):
    """Position outside any word's [start, end) yields no highlight."""
    view = editor.transcript_view
    view.set_playhead_position(500)  # inside word 1 (" beta", 0.5–1.0)
    assert view.playhead_word == 1
    view.set_playhead_position(10_000)  # past end
    assert view.playhead_word == -1


def test_playhead_word_format_is_bold(editor, qtbot):
    view = editor.transcript_view
    view.set_playhead_position(1250)  # inside word 2 (" gamma", 1.0–1.5)
    cursor = view._cursor_for_word(2)
    assert cursor is not None
    weight = cursor.charFormat().fontWeight()
    assert weight == QFont.Weight.Bold


# ---------------------------------------------------------------------------
# MainWindow title-bar dirty marker
# ---------------------------------------------------------------------------


def test_title_bar_dirty_marker_lifecycle(qtbot, tmp_path, monkeypatch):
    """Title bar shows ●filename when dirty, filename when clean.

    Walks the full lifecycle: load → edit (dirty) → save (clean) →
    edit again (dirty) → undo back to saved (clean).
    """
    monkeypatch.setenv("WHISPER_SETTINGS_DIR", str(tmp_path))
    from core.settings import Settings, save_settings
    from workers.transcription import candidate_cache_path

    # Pre-write a settings file with output_dir → tmp_path so the save
    # path doesn't end up next to sample.wav in the repo.
    save_settings(Settings(output_dir=str(tmp_path)))

    win = MainWindow()
    qtbot.addWidget(win)
    win._pump_timer.stop()

    doc = _make_doc()
    win.show_editor(doc)
    editor = win.editor_pane

    # Title starts clean.
    assert "●" not in win.windowTitle()
    assert SAMPLE_WAV.name in win.windowTitle()

    # Edit → dirty.
    _set_selection(editor, 2, 3)
    editor._cut_action.trigger()
    assert win.windowTitle().startswith("●")

    # Save → clean.
    editor._save_action.trigger()
    assert "●" not in win.windowTitle()
    assert candidate_cache_path(editor.settings, SAMPLE_WAV).is_file()

    # Edit again → dirty.
    _set_selection(editor, 4, 5)
    editor._cut_action.trigger()
    assert win.windowTitle().startswith("●")

    # Undo back to saved doc → clean.
    editor._undo_action.trigger()
    assert "●" not in win.windowTitle()


def test_save_path_uses_candidate_cache_path(editor, qtbot, tmp_path):
    from workers.transcription import candidate_cache_path
    expected = candidate_cache_path(editor.settings, SAMPLE_WAV)
    assert editor._save_path() == expected


# ---------------------------------------------------------------------------
# AppStateMachine.transition_to — public method
# ---------------------------------------------------------------------------


def test_transition_to_validates_legal(qtbot):
    from ui.state import AppState, AppStateMachine, InvalidTransitionError
    sm = AppStateMachine()
    p = SAMPLE_WAV
    sm.load_file(p)
    sm.start_transcribing()
    # TRANSCRIBING → COMPLETE is legal.
    sm.transition_to(AppState.COMPLETE)
    assert sm.state == AppState.COMPLETE
    # Then COMPLETE → TRANSCRIBING is illegal.
    with pytest.raises(InvalidTransitionError):
        sm.transition_to(AppState.TRANSCRIBING)
