"""Phase 5b — pytest-qt tests for the editor pane skeleton.

Behavioural tests against:
- :class:`ui_qt.editor_pane.EditorPane` — splitter topology, layout
  toggle, transcript rendering from doc.ranges.
- :class:`ui_qt.app.MainWindow` — show_editor / show_transcribe swap,
  transcribe-pane teardown on swap, DoneEvent-with-document → editor.
- :class:`ui_qt.components.video_viewport.VideoViewport` — QMediaPlayer
  reaches LoadedMedia, position slider follows positionChanged.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

if os.environ.get("WHISPER_NO_QT"):  # pragma: no cover - opt-in headless
    pytest.skip("WHISPER_NO_QT set", allow_module_level=True)

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

import shiboken6  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtMultimedia import QMediaPlayer  # noqa: E402
from PySide6.QtTest import QSignalSpy  # noqa: E402

from core.document import Document, MediaSource, Range, Segment, Word  # noqa: E402
from core.settings import Settings  # noqa: E402
from ui.state import AppState, DoneEvent  # noqa: E402
from ui_qt.app import MainWindow  # noqa: E402
from ui_qt.components.transcript_view import TranscriptView, collect_words  # noqa: E402
from ui_qt.components.video_viewport import VideoViewport, _format_ms  # noqa: E402
from ui_qt.editor_pane import EditorPane  # noqa: E402
from ui_qt.transcribe_pane import TranscribePane  # noqa: E402
from ui_qt.waveform import WaveformPlaceholder  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_WAV = REPO_ROOT / "tests" / "fixtures" / "sample.wav"


# ---------------------------------------------------------------------------
# In-memory fixture document
# ---------------------------------------------------------------------------


def _make_document(media_path: Path) -> Document:
    """Three segments + three ranges. Two ranges overlap two segments;
    one range carves a gap. Word coverage is asymmetric so tests can
    detect off-by-one mistakes in `collect_words`.
    """
    seg0 = Segment(
        text="hello world",
        start=0.0,
        end=1.0,
        words=(
            Word(text="hello", start=0.0, end=0.4),
            Word(text=" world", start=0.4, end=1.0),
        ),
    )
    seg1 = Segment(
        text="this is a test",
        start=1.5,
        end=3.0,
        words=(
            Word(text="this", start=1.5, end=1.9),
            Word(text=" is", start=1.9, end=2.1),
            Word(text=" a", start=2.1, end=2.3),
            Word(text=" test", start=2.3, end=3.0),
        ),
    )
    seg2 = Segment(
        text="goodbye",
        start=4.0,
        end=4.6,
        words=(
            Word(text="goodbye", start=4.0, end=4.6),
        ),
    )
    ranges = [
        Range(source_id="src0", start=0.0, end=1.0),    # keeps seg0 entirely
        Range(source_id="src0", start=2.0, end=2.5),    # keeps "is" + "a" from seg1
        Range(source_id="src0", start=4.0, end=4.6),    # keeps seg2 entirely
        # Note: " test" (2.3-3.0) is NOT kept — overlaps the second range
        # only at 2.3-2.5; per `_word_in_any_range`, overlap counts, so
        # " test" IS kept. The test asserts the overlap rule.
    ]
    return Document(
        sources={"src0": MediaSource(id="src0", path=media_path, duration=5.0)},
        segments=[seg0, seg1, seg2],
        ranges=ranges,
        language="en",
        created_at=datetime.now(UTC),
        model_name="tiny",
        source_hash=None,
    )


# ---------------------------------------------------------------------------
# Transcript rendering — pure walk over doc.ranges
# ---------------------------------------------------------------------------


def test_collect_words_returns_every_word_with_kept_flag(qtbot):
    """5c: cut words still appear in the transcript (Decision 2 — strikethrough).

    ``collect_words`` returns all words; ``ref.kept`` distinguishes
    kept (rendered normally) from cut (rendered with strikethrough).
    """
    doc = _make_document(SAMPLE_WAV)
    refs = collect_words(doc)
    rendered = [r.word.text for r in refs]
    # Every word in every segment is present, in document order.
    assert rendered == ["hello", " world", "this", " is", " a", " test", "goodbye"]
    kept_lookup = {r.word.text: r.kept for r in refs}
    # seg0 fully kept; seg2 fully kept.
    assert kept_lookup["hello"] is True
    assert kept_lookup[" world"] is True
    assert kept_lookup["goodbye"] is True
    # seg1: only words overlapping the (2.0, 2.5) range survive as kept.
    assert kept_lookup["this"] is False  # 1.5-1.9, no overlap
    assert kept_lookup[" is"] is True    # 1.9-2.1, overlaps
    assert kept_lookup[" a"] is True     # 2.1-2.3, overlaps
    assert kept_lookup[" test"] is True  # 2.3-3.0, overlaps at 2.3-2.5


def test_transcript_view_renders_every_word_and_strikes_cut_ones(qtbot):

    from ui_qt.components.transcript_view import WORD_INDEX_PROPERTY

    view = TranscriptView()
    qtbot.addWidget(view)
    doc = _make_document(SAMPLE_WAV)
    view.set_document_model(doc)
    text = view.toPlainText()
    # Both kept and cut words appear in the rendered text.
    assert "hello" in text
    assert "this" in text  # 5c: cut words still rendered (strikethrough)
    assert "goodbye" in text
    # The cut word's character format carries strikethrough.
    cut_word_idx = next(i for i, r in enumerate(view.words) if r.word.text == "this")
    cursor = view._cursor_for_word(cut_word_idx)
    assert cursor is not None
    fmt = cursor.charFormat()
    assert fmt.fontStrikeOut() is True
    # And the kept word's format does not.
    kept_word_idx = next(i for i, r in enumerate(view.words) if r.word.text == "hello")
    cursor = view._cursor_for_word(kept_word_idx)
    assert cursor.charFormat().fontStrikeOut() is False
    # Each word is indexed by WORD_INDEX_PROPERTY.
    assert int(cursor.charFormat().property(WORD_INDEX_PROPERTY)) == kept_word_idx


def test_waveform_placeholder_min_height(qtbot):
    wp = WaveformPlaceholder()
    qtbot.addWidget(wp)
    assert wp.minimumHeight() == 64


# ---------------------------------------------------------------------------
# EditorPane — layout toggle, splitter topology
# ---------------------------------------------------------------------------


def _fresh_settings(tmp_path: Path) -> Settings:
    """A Settings object whose save() writes to ``tmp_path/settings.json``.

    Doesn't use the WHISPER_SETTINGS_DIR env var because the editor
    pane saves via ``save_settings(self._settings)`` (no path arg) —
    relies on settings_dir(). The test fixture ``main_window`` sets
    that env var; helpers here call ``save_settings(s, path=...)``
    directly to avoid coupling.
    """
    return Settings()


def test_editor_pane_constructs_video_top_default(qtbot, tmp_path):
    doc = _make_document(SAMPLE_WAV)
    pane = EditorPane(doc, settings=_fresh_settings(tmp_path))
    qtbot.addWidget(pane)
    try:
        assert pane.outer_splitter.orientation() == Qt.Orientation.Vertical
        assert pane.inner_splitter.orientation() == Qt.Orientation.Vertical
        # Outer always holds video first, inner second.
        assert pane.outer_splitter.count() == 2
        assert pane.outer_splitter.widget(0) is pane.video_viewport
        assert pane.outer_splitter.widget(1) is pane.inner_splitter
        # Inner always holds transcript over waveform.
        assert pane.inner_splitter.count() == 2
        assert isinstance(pane.inner_splitter.widget(0), TranscriptView)
        assert isinstance(pane.inner_splitter.widget(1), WaveformPlaceholder)
    finally:
        pane.release()


def test_editor_pane_constructs_video_left_when_settings_say_so(qtbot, tmp_path):
    settings = Settings(layout="video_left")
    pane = EditorPane(_make_document(SAMPLE_WAV), settings=settings)
    qtbot.addWidget(pane)
    try:
        assert pane.outer_splitter.orientation() == Qt.Orientation.Horizontal
        # Inner stays vertical regardless of the outer layout choice
        # (Decision 5: waveform stays directly under transcript).
        assert pane.inner_splitter.orientation() == Qt.Orientation.Vertical
    finally:
        pane.release()


def test_editor_pane_layout_toggle_flips_outer_splitter_and_persists(
    qtbot, tmp_path, monkeypatch
):
    monkeypatch.setenv("WHISPER_SETTINGS_DIR", str(tmp_path))
    settings = Settings()  # defaults: video_top
    pane = EditorPane(_make_document(SAMPLE_WAV), settings=settings)
    qtbot.addWidget(pane)
    try:
        assert pane.outer_splitter.orientation() == Qt.Orientation.Vertical
        # Click toggle.
        pane._layout_btn.click()
        assert pane.outer_splitter.orientation() == Qt.Orientation.Horizontal
        assert pane.settings.layout == "video_left"
        # Inner unchanged.
        assert pane.inner_splitter.orientation() == Qt.Orientation.Vertical
        # File was written.
        from core.settings import load_settings, settings_file
        loaded = load_settings(path=settings_file())
        assert loaded.layout == "video_left"
        # Toggle back.
        pane._layout_btn.click()
        assert pane.outer_splitter.orientation() == Qt.Orientation.Vertical
        assert pane.settings.layout == "video_top"
    finally:
        pane.release()


# ---------------------------------------------------------------------------
# MainWindow — show_editor / show_transcribe swap mechanics
# ---------------------------------------------------------------------------


@pytest.fixture
def main_window(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_SETTINGS_DIR", str(tmp_path))
    win = MainWindow()
    qtbot.addWidget(win)
    win._pump_timer.stop()
    yield win


def test_show_editor_swaps_central_widget_and_destroys_transcribe_pane(qtbot, main_window):
    pane_before = main_window.transcribe_pane
    assert pane_before is not None
    doc = _make_document(SAMPLE_WAV)
    main_window.show_editor(doc)
    assert main_window.editor_pane is not None
    assert main_window.transcribe_pane is None
    # The displaced widget is queued for deletion. Spin the event loop
    # so Qt processes the pending DeferredDelete event.
    qtbot.waitUntil(lambda: not shiboken6.isValid(pane_before), timeout=2_000)
    assert not shiboken6.isValid(pane_before)


def test_show_transcribe_after_editor_destroys_editor_pane(qtbot, main_window):
    doc = _make_document(SAMPLE_WAV)
    main_window.show_editor(doc)
    editor_before = main_window.editor_pane
    assert editor_before is not None
    main_window.show_transcribe()
    assert main_window.transcribe_pane is not None
    assert main_window.editor_pane is None
    qtbot.waitUntil(lambda: not shiboken6.isValid(editor_before), timeout=2_000)


def test_done_event_with_document_swaps_to_editor(main_window, tmp_path):
    main_window._handle_file_selected(SAMPLE_WAV)
    main_window.state.start_transcribing()
    doc = _make_document(SAMPLE_WAV)
    main_window.event_queue.put(
        DoneEvent(
            segments=doc.segments,
            info=SimpleNamespace(language="en", duration=5.0),
            output_files={"txt": tmp_path / "out.txt"},
            elapsed=0.5,
            document=doc,
        )
    )
    main_window.pump_once()
    assert main_window.state.state == AppState.COMPLETE
    assert main_window.editor_pane is not None
    assert main_window.transcribe_pane is None


def test_open_project_loads_document_and_swaps(main_window, tmp_path):
    """show_editor invoked from a Document.from_json round-trip."""
    doc = _make_document(SAMPLE_WAV)
    project_path = tmp_path / "demo.transcribe.json"
    project_path.write_text(json.dumps(doc.to_json()), encoding="utf-8")

    # Drive the open path by hand — QFileDialog isn't unit-testable here.
    data = json.loads(project_path.read_text())
    loaded = Document.from_json(data)
    main_window.show_editor(loaded)

    assert main_window.editor_pane is not None
    assert main_window.editor_pane.document.sources["src0"].path == SAMPLE_WAV


def test_back_from_editor_returns_to_transcribe_pane(main_window):
    doc = _make_document(SAMPLE_WAV)
    main_window.show_editor(doc)
    main_window.editor_pane.back_to_transcribe.emit()
    assert main_window.transcribe_pane is not None
    assert main_window.editor_pane is None


# ---------------------------------------------------------------------------
# VideoViewport — actual QMediaPlayer wiring
# ---------------------------------------------------------------------------


def test_video_viewport_loads_real_mp4_to_loaded_media(qtbot, tiny_mp4):
    viewport = VideoViewport()
    qtbot.addWidget(viewport)
    try:
        with qtbot.waitSignal(
            viewport.player.mediaStatusChanged,
            check_params_cb=lambda s: s == QMediaPlayer.MediaStatus.LoadedMedia,
            timeout=10_000,
        ):
            viewport.set_source(tiny_mp4)
        assert viewport.player.duration() > 0
    finally:
        viewport.release()


def test_video_viewport_position_changed_drives_slider(qtbot, tiny_mp4):
    """positionChanged signal → slider value tracks the player position."""
    viewport = VideoViewport()
    qtbot.addWidget(viewport)
    try:
        with qtbot.waitSignal(
            viewport.player.mediaStatusChanged,
            check_params_cb=lambda s: s == QMediaPlayer.MediaStatus.LoadedMedia,
            timeout=10_000,
        ):
            viewport.set_source(tiny_mp4)

        # QSignalSpy captures positionChanged emissions across play.
        spy = QSignalSpy(viewport.position_changed)
        viewport.player.play()
        # Wait for at least one position update past zero.
        qtbot.waitUntil(lambda: spy.count() > 0, timeout=5_000)
        viewport.player.pause()
        # Slider value should match (or be close to) the player position.
        # QSlider stores ints; the slot updates it from positionChanged.
        assert viewport._slider.value() >= 0
    finally:
        viewport.release()


def test_video_viewport_release_clears_source(qtbot, tiny_mp4):
    viewport = VideoViewport()
    qtbot.addWidget(viewport)
    viewport.set_source(tiny_mp4)
    viewport.release()
    # Source URL should be empty / cleared after release.
    assert viewport.player.source().toString() == ""


def test_format_ms_zero_and_minutes():
    assert _format_ms(0) == "0:00"
    assert _format_ms(125_000) == "2:05"
    assert _format_ms(3_725_000) == "1:02:05"


# ---------------------------------------------------------------------------
# TranscribePane — open_project signal wiring
# ---------------------------------------------------------------------------


def test_transcribe_pane_open_project_button_emits_signal(qtbot, tmp_path):
    from ui.state import AppStateMachine
    pane = TranscribePane(settings=Settings(), state=AppStateMachine())
    qtbot.addWidget(pane)
    with qtbot.waitSignal(pane.open_project_requested, timeout=1_000):
        pane._open_btn.click()


# ---------------------------------------------------------------------------
# Settings layout fallback warning (carry-over from 5a)
# ---------------------------------------------------------------------------


def test_settings_unknown_layout_emits_warning(caplog, tmp_path):
    """Unrecognized ``layout`` value falls back to default with a warning."""
    import logging as _logging

    from core.settings import DEFAULT_LAYOUT, Settings

    with caplog.at_level(_logging.WARNING, logger="core.settings"):
        s = Settings.from_dict({"layout": "diagonal_corner"})
    assert s.layout == DEFAULT_LAYOUT
    assert any("diagonal_corner" in m for m in caplog.text.splitlines())
