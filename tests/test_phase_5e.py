"""Phase 5e — autosave, splitter persistence, settings dialog tabs.

These tests exercise the editor pane wiring that 5e adds on top of 5c
and 5d: autosave QTimer behaviour, splitter ``saveState``/``restoreState``
round-trip, and the new tabbed Settings dialog.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

if os.environ.get("WHISPER_NO_QT"):  # pragma: no cover - opt-in headless
    pytest.skip("WHISPER_NO_QT set", allow_module_level=True)

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtCore import Qt  # noqa: E402

from core.document import Document, MediaSource, Range, Segment, Word  # noqa: E402
from core.settings import Settings, load_settings, save_settings  # noqa: E402
from ui_qt.components.settings_panel import SettingsDialog  # noqa: E402
from ui_qt.editor_pane import EditorPane  # noqa: E402

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


def _set_selection(pane: EditorPane, start: int, end: int) -> None:
    view = pane.transcript_view
    view._selection_anchor = None
    view._set_selection(start, end)


@pytest.fixture
def editor(qtbot, tmp_path, monkeypatch):
    """An editor whose saves go to ``tmp_path`` (not ``tests/fixtures/``)."""
    monkeypatch.setenv("WHISPER_SETTINGS_DIR", str(tmp_path))
    settings = Settings(output_dir=str(tmp_path))
    pane = EditorPane(_make_doc(), settings=settings)
    qtbot.addWidget(pane)
    yield pane
    pane.release()


# ---------------------------------------------------------------------------
# Settings round-trip — bytes fields survive base64 encoding
# ---------------------------------------------------------------------------


def test_settings_round_trip_with_splitter_bytes(tmp_path):
    """Bytes splitter state encodes to base64 on disk and decodes back."""
    p = tmp_path / "settings.json"
    blob_outer = b"\x01\x02\x03outer-binary-data\xff"
    blob_inner = b"\x07\x08inner\x00\xab"
    s = Settings(
        editor_splitter_state=blob_outer,
        transcript_splitter_state=blob_inner,
    )
    save_settings(s, path=p)
    raw = json.loads(p.read_text())
    # On-disk values are strings (base64), not lists or null.
    assert isinstance(raw["editor_splitter_state"], str)
    assert isinstance(raw["transcript_splitter_state"], str)
    # And reload reproduces the original bytes.
    loaded = load_settings(path=p)
    assert loaded.editor_splitter_state == blob_outer
    assert loaded.transcript_splitter_state == blob_inner


def test_settings_decodes_garbled_splitter_state_to_none(tmp_path):
    """A non-base64 string in the splitter field falls back to None."""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"editor_splitter_state": "not-base64-!!"}))
    loaded = load_settings(path=p)
    assert loaded.editor_splitter_state is None


def test_settings_phase_5_field_round_trip(tmp_path):
    p = tmp_path / "settings.json"
    s = Settings(
        layout="video_left",
        default_pad_lead=0.25,
        default_pad_trail=0.30,
        default_audio_fade_ms=45,
        autosave_interval_s=120,
    )
    save_settings(s, path=p)
    loaded = load_settings(path=p)
    assert loaded.layout == "video_left"
    assert loaded.default_pad_lead == pytest.approx(0.25)
    assert loaded.default_pad_trail == pytest.approx(0.30)
    assert loaded.default_audio_fade_ms == 45
    assert loaded.autosave_interval_s == 120


# ---------------------------------------------------------------------------
# Autosave behaviour
# ---------------------------------------------------------------------------


def test_autosave_does_nothing_when_clean(editor, qtbot, monkeypatch):
    calls = {"writes": 0}
    real_write = editor._write_document_to

    def _spy(path):
        calls["writes"] += 1
        return real_write(path)

    monkeypatch.setattr(editor, "_write_document_to", _spy)
    # Force a tick by calling the slot directly — the QTimer schedule
    # is incidental here; we just need the slot to fire while clean.
    editor._handle_autosave_tick()
    assert calls["writes"] == 0


def test_autosave_writes_when_dirty_and_clears_dirty(editor, qtbot):
    _set_selection(editor, 0, 1)
    editor._cut_action.trigger()
    assert editor.session.is_dirty is True

    editor._handle_autosave_tick()
    assert editor.session.is_dirty is False
    save_path = editor._save_path()
    assert save_path is not None and save_path.is_file()


def test_autosave_failure_stays_dirty_silently(editor, qtbot, monkeypatch):
    _set_selection(editor, 0, 1)
    editor._cut_action.trigger()
    assert editor.session.is_dirty is True

    def _boom(_path):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(editor, "_write_document_to", _boom)
    # If a modal popped up we'd block here forever. The test passing
    # in finite time is itself the silence assertion; the stay-dirty
    # check is the explicit one.
    editor._handle_autosave_tick()
    assert editor.session.is_dirty is True


def test_apply_autosave_interval_starts_and_stops_timer(editor, qtbot):
    editor._apply_autosave_interval(30)
    assert editor._autosave_timer.isActive()
    assert editor._autosave_timer.interval() == 30_000
    editor._apply_autosave_interval(0)
    assert not editor._autosave_timer.isActive()


# ---------------------------------------------------------------------------
# Splitter persistence
# ---------------------------------------------------------------------------


def test_splitter_persist_then_restore_round_trip(qtbot, tmp_path, monkeypatch):
    """Move splitter, persist, recreate pane → splitter sizes restored."""
    monkeypatch.setenv("WHISPER_SETTINGS_DIR", str(tmp_path))
    settings = Settings(output_dir=str(tmp_path))
    first = EditorPane(_make_doc(), settings=settings)
    qtbot.addWidget(first)
    try:
        # Set explicit, easily-recognisable sizes and fire the persist
        # path manually (bypassing the 200ms debounce timer).
        first.outer_splitter.setSizes([300, 700])
        first.inner_splitter.setSizes([500, 80])
        first._persist_splitter_state()
        outer_blob = first.settings.editor_splitter_state
        inner_blob = first.settings.transcript_splitter_state
        assert isinstance(outer_blob, bytes) and len(outer_blob) > 0
        assert isinstance(inner_blob, bytes) and len(inner_blob) > 0
    finally:
        first.release()

    # New pane, freshly loaded settings: restoreState should land.
    fresh_settings = load_settings()
    second = EditorPane(_make_doc(), settings=fresh_settings)
    qtbot.addWidget(second)
    try:
        # restoreState inside __init__ should have produced non-default
        # sizes — the literal pixel values may vary by Qt internals, so
        # we assert state-equality rather than size-equality.
        assert (
            bytes(second.outer_splitter.saveState())
            == fresh_settings.editor_splitter_state
        )
        assert (
            bytes(second.inner_splitter.saveState())
            == fresh_settings.transcript_splitter_state
        )
    finally:
        second.release()


def test_layout_toggle_clears_outer_splitter_state(qtbot, tmp_path, monkeypatch):
    """Layout toggle wipes the outer-splitter blob (per-orientation persistence).

    The "accept the loss on toggle" contract: same orientation across
    sessions = sizes preserved; flipping layout = fresh split.
    Implemented by clearing ``editor_splitter_state`` in
    ``_handle_layout_toggle``.
    """
    monkeypatch.setenv("WHISPER_SETTINGS_DIR", str(tmp_path))
    settings = Settings(output_dir=str(tmp_path))
    pane = EditorPane(_make_doc(), settings=settings)
    qtbot.addWidget(pane)
    try:
        # Plant a non-empty blob (any bytes will do — we only care that
        # the toggle wipes it).
        pane.settings.editor_splitter_state = b"some-prior-blob"
        pane.settings.transcript_splitter_state = b"transcript-blob"
        pane._handle_layout_toggle()
        assert pane.settings.editor_splitter_state is None
        # Inner-splitter blob is preserved across the toggle (its
        # orientation is always vertical, so it never gets stale).
        assert pane.settings.transcript_splitter_state == b"transcript-blob"
    finally:
        pane.release()


# ---------------------------------------------------------------------------
# Settings dialog — tabs and field wiring
# ---------------------------------------------------------------------------


def test_settings_dialog_has_three_tabs(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_SETTINGS_DIR", str(tmp_path))
    dlg = SettingsDialog(current=Settings())
    qtbot.addWidget(dlg)
    titles = [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())]
    assert titles == ["Transcription", "Editor", "Advanced"]


def test_settings_dialog_layout_combo_initialised_from_current(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_SETTINGS_DIR", str(tmp_path))
    dlg = SettingsDialog(current=Settings(layout="video_left"))
    qtbot.addWidget(dlg)
    assert dlg.layout_combo.currentData() == "video_left"


def test_settings_dialog_save_emits_phase5_fields(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_SETTINGS_DIR", str(tmp_path))
    dlg = SettingsDialog(current=Settings())
    qtbot.addWidget(dlg)
    dlg.layout_combo.setCurrentIndex(dlg.layout_combo.findData("video_left"))
    dlg.pad_lead_spin.setValue(0.25)
    dlg.pad_trail_spin.setValue(0.20)
    dlg.audio_fade_spin.setValue(45)
    dlg.autosave_spin.setValue(120)

    captured: list[Settings] = []
    dlg.settings_saved.connect(captured.append)
    dlg._save()

    assert len(captured) == 1
    s = captured[0]
    assert s.layout == "video_left"
    assert s.default_pad_lead == pytest.approx(0.25)
    assert s.default_pad_trail == pytest.approx(0.20)
    assert s.default_audio_fade_ms == 45
    assert s.autosave_interval_s == 120


def test_settings_dialog_autosave_special_text_for_zero(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_SETTINGS_DIR", str(tmp_path))
    dlg = SettingsDialog(current=Settings(autosave_interval_s=0))
    qtbot.addWidget(dlg)
    assert dlg.autosave_spin.value() == 0
    assert dlg.autosave_spin.specialValueText() == "Off"


# ---------------------------------------------------------------------------
# Live propagation: settings → editor pane
# ---------------------------------------------------------------------------


def test_apply_settings_flips_layout_live(editor, qtbot):
    """A new Settings with flipped layout flips the outer splitter immediately."""
    assert editor.outer_splitter.orientation() == Qt.Orientation.Vertical
    new = Settings(**{**editor.settings.to_dict(), "layout": "video_left"})
    new = Settings.from_dict(new.to_dict())
    editor.apply_settings(new)
    assert editor.outer_splitter.orientation() == Qt.Orientation.Horizontal
    assert editor.settings.layout == "video_left"


def test_apply_settings_updates_autosave_interval_live(editor, qtbot):
    new = Settings(**{**editor.settings.to_dict(), "autosave_interval_s": 60})
    new = Settings.from_dict(new.to_dict())
    editor.apply_settings(new)
    assert editor._autosave_timer.isActive()
    assert editor._autosave_timer.interval() == 60_000

    # Disabling autosave stops the timer.
    new2 = Settings(**{**editor.settings.to_dict(), "autosave_interval_s": 0})
    new2 = Settings.from_dict(new2.to_dict())
    editor.apply_settings(new2)
    assert not editor._autosave_timer.isActive()


# ---------------------------------------------------------------------------
# Peaks-cache schema_version regen rule (5d carry-over)
# ---------------------------------------------------------------------------


def test_peaks_cache_with_old_schema_version_is_ignored(tmp_path):
    """Plant a .peaks.npz with schema_version=0 → load returns None."""
    import shutil

    from workers.waveform import load_peaks_cache, peaks_path

    media = tmp_path / "media.wav"
    shutil.copy(SAMPLE_WAV, media)
    target = peaks_path(media)
    np.savez(
        target,
        peaks=np.zeros((100, 2), dtype=np.float32),
        meta=np.array(
            {
                "source_hash": "irrelevant",
                "duration_s": 1.0,
                "bucket_count": 100,
                "schema_version": 0,
            },
            dtype=object,
        ),
    )
    assert load_peaks_cache(media) is None
