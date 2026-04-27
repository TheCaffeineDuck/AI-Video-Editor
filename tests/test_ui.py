"""UI construction tests + integration-style checks of the App controller.

All tests use the ``tk_root`` fixture from ``conftest.py`` which gives us a
hidden Tk root and tears it down on each test. The tests here exercise the
widget-construction paths and the App's queue-pump → render flow without
actually running ``mainloop()``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ui.components.drop_zone import DropZone
from ui.components.model_picker import ModelPicker
from ui.components.progress_card import ProgressCard
from ui.components.result_card import ResultCard
from ui.state import AppState, DoneEvent, ProgressEvent, SegmentEvent

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE = REPO_ROOT / "tests" / "fixtures" / "sample.wav"


# ---------------------------------------------------------------------------
# Component construction smoke tests
# ---------------------------------------------------------------------------


def test_drop_zone_constructs(tk_root):
    dz = DropZone(tk_root, on_file_selected=lambda _p: None)
    assert dz.winfo_exists()


def test_drop_zone_register_dnd_safe_on_plain_root(tk_root):
    """register_dnd should return False (not crash) on a non-DnD root."""
    dz = DropZone(tk_root, on_file_selected=lambda _p: None)
    assert dz.register_dnd() is False


def test_drop_zone_show_loaded_updates_labels(tk_root):
    dz = DropZone(tk_root, on_file_selected=lambda _p: None)
    dz.show_loaded(name="lecture.mp4", duration_seconds=125.5, size_bytes=1024 * 1024 * 2)
    assert dz._title_label.cget("text") == "lecture.mp4"


def test_drop_zone_invalid_extension_calls_invalid_handler(tk_root, tmp_path):
    invalid: list[str] = []
    selected: list[Path] = []
    dz = DropZone(
        tk_root,
        on_file_selected=selected.append,
        on_invalid_file=invalid.append,
    )
    bogus = tmp_path / "foo.txt"
    bogus.write_text("x")
    dz._accept(bogus)
    assert selected == []
    assert len(invalid) == 1


def test_drop_zone_valid_extension_fires_on_selected(tk_root):
    selected: list[Path] = []
    dz = DropZone(tk_root, on_file_selected=selected.append)
    dz._accept(SAMPLE)
    assert selected == [SAMPLE]


def test_model_picker_constructs(tk_root):
    mp = ModelPicker(tk_root)
    assert mp.value == "base"


def test_model_picker_initial_arg_respected(tk_root):
    mp = ModelPicker(tk_root, initial="tiny")
    assert mp.value == "tiny"


def test_model_picker_set_value_fires_callback(tk_root):
    seen: list[str] = []
    mp = ModelPicker(tk_root, on_change=seen.append)
    mp.set("small")
    assert mp.value == "small"
    assert seen == ["small"]


def test_model_picker_unknown_raises(tk_root):
    with pytest.raises(ValueError):
        ModelPicker(tk_root, initial="huge")


def test_progress_card_constructs(tk_root):
    pc = ProgressCard(tk_root, on_cancel=lambda: None)
    assert pc.winfo_exists()


def test_progress_card_set_progress_clamps(tk_root):
    pc = ProgressCard(tk_root, on_cancel=lambda: None)
    pc.reset()
    pc.set_progress(2.5)
    assert pc._bar.get() == 1.0
    pc.set_progress(-1.0)
    assert pc._bar.get() == 0.0


def test_progress_card_append_stream_caps_lines(tk_root):
    pc = ProgressCard(tk_root, on_cancel=lambda: None)
    pc.reset()
    for i in range(10):
        pc.append_stream(f"line {i}", max_lines=3)
    text = pc._read_stream_text()
    lines = [line for line in text.splitlines() if line]
    assert len(lines) == 3
    assert lines[-1] == "line 9"


def test_progress_card_cancel_fires_callback(tk_root):
    cancelled: list[bool] = []
    pc = ProgressCard(tk_root, on_cancel=lambda: cancelled.append(True))
    pc._handle_cancel()
    assert cancelled == [True]


def test_result_card_constructs_and_shows_result(tk_root, tmp_path):
    rc = ResultCard(tk_root, on_new_transcription=lambda: None)
    output = tmp_path / "clip.txt"
    output.write_text("hello world")
    rc.show_result(
        transcript="hello world",
        output_files={"txt": output},
        language="en",
        elapsed_seconds=1.2,
    )
    rc._textbox.configure(state="normal")
    assert rc._textbox.get("1.0", "end-1c") == "hello world"


def test_result_card_new_transcription_button_fires(tk_root):
    fired: list[bool] = []
    rc = ResultCard(tk_root, on_new_transcription=lambda: fired.append(True))
    rc._on_new()
    assert fired == [True]


# ---------------------------------------------------------------------------
# App controller — wiring & queue pump
# ---------------------------------------------------------------------------


@pytest.fixture
def app(tk_root):
    """Build an App against the bare Tk root (no DnD), avoiding mainloop()."""
    from ui.app import App

    instance = App(root=tk_root)
    yield instance
    # No explicit teardown needed; tk_root fixture destroys the root.


def test_app_starts_in_idle(app):
    assert app.state.state == AppState.IDLE


def test_app_load_file_transitions_to_file_loaded(app):
    app._handle_file_selected(SAMPLE)
    assert app.state.state == AppState.FILE_LOADED
    assert app.state.media_path == SAMPLE


def test_app_invalid_file_sets_transient_error(app, tmp_path):
    bad = tmp_path / "foo.txt"
    bad.write_text("x")
    app._handle_file_selected(bad)
    assert app.state.error_message is not None
    assert app.state.state == AppState.IDLE


def test_app_pump_applies_queue_events(app):
    """Manually invoking pump_once should drive segment/progress events into state."""
    app._handle_file_selected(SAMPLE)
    app.state.start_transcribing()
    app.event_queue.put(SegmentEvent(text="hello"))
    app.event_queue.put(ProgressEvent(fraction=0.5))
    n = app.pump_once()
    assert n == 2
    # The pump consumes streaming_text after rendering, but progress sticks.
    assert app.state.progress == 0.5
    assert app.state.state == AppState.TRANSCRIBING


def test_app_done_event_drives_complete(app, tmp_path):
    app._handle_file_selected(SAMPLE)
    app.state.start_transcribing()
    app.event_queue.put(
        DoneEvent(
            segments=[SimpleNamespace(text="hello world", start=0.0, end=1.0)],
            info=SimpleNamespace(language="en"),
            output_files={"txt": tmp_path / "x.txt"},
            elapsed=0.4,
        )
    )
    app.pump_once()
    assert app.state.state == AppState.COMPLETE


def test_app_cancel_returns_to_idle(app):
    app._handle_file_selected(SAMPLE)
    app.state.start_transcribing()
    app._handle_cancel()
    assert app.state.state == AppState.IDLE


def test_app_new_transcription_resets(app):
    app._handle_file_selected(SAMPLE)
    app.state.start_transcribing()
    app.state.apply_event(
        DoneEvent(
            segments=[SimpleNamespace(text="x", start=0, end=1)],
            info=SimpleNamespace(language="en"),
            output_files={"txt": Path("/tmp/x.txt")},
            elapsed=0.1,
        )
    )
    app._handle_new_transcription()
    assert app.state.state == AppState.IDLE
    assert app.state.media_path is None


# ---------------------------------------------------------------------------
# Phase 4f-2 — Document JSON cache (source_hash)
# ---------------------------------------------------------------------------


def _write_cache_file(
    *,
    media_path: Path,
    cache_path: Path,
    source_hash: str | None,
    segment_text: str = "cached hello",
) -> None:
    """Write a synthetic Document JSON sidecar that the cache lookup will find."""
    import json as _json

    payload = {
        "schema_version": 1,
        "media_path": str(media_path),
        "duration": 1.0,
        "language": "en",
        "model_name": "tiny",
        "created_at": "2026-04-26T10:00:00",
        "segments": [
            {"text": segment_text, "start": 0.0, "end": 1.0, "words": []}
        ],
        "cuts": [],
    }
    if source_hash is not None:
        payload["source_hash"] = source_hash
    cache_path.write_text(_json.dumps(payload), encoding="utf-8")


def _media_fixture(tmp_path: Path) -> Path:
    media = tmp_path / "media.wav"
    media.write_bytes(b"fake media bytes")
    return media


def test_try_load_cached_document_returns_none_when_no_file(app, tmp_path):
    media = _media_fixture(tmp_path)
    # Force the lookup to land in tmp_path even though the default settings
    # would write next to the source — they're equal here, but be explicit.
    app.settings.output_dir = str(tmp_path)
    assert app._try_load_cached_document(media) is None


def test_try_load_cached_document_returns_doc_when_hash_matches(app, tmp_path):
    from core.cache import cache_key

    media = _media_fixture(tmp_path)
    app.settings.output_dir = str(tmp_path)
    key = cache_key(media)
    _write_cache_file(
        media_path=media,
        cache_path=app._candidate_cache_path(media),
        source_hash=key,
    )
    doc = app._try_load_cached_document(media)
    assert doc is not None
    assert doc.source_hash == key
    assert doc.segments[0].text == "cached hello"


def test_try_load_cached_document_misses_on_hash_mismatch(app, tmp_path):
    media = _media_fixture(tmp_path)
    app.settings.output_dir = str(tmp_path)
    _write_cache_file(
        media_path=media,
        cache_path=app._candidate_cache_path(media),
        source_hash="0" * 64,  # not the real hash
    )
    assert app._try_load_cached_document(media) is None


def test_try_load_cached_document_misses_when_source_hash_absent(app, tmp_path):
    """Pre-Phase-4f-2 cache files have no ``source_hash`` and must miss."""
    media = _media_fixture(tmp_path)
    app.settings.output_dir = str(tmp_path)
    _write_cache_file(
        media_path=media,
        cache_path=app._candidate_cache_path(media),
        source_hash=None,
    )
    assert app._try_load_cached_document(media) is None


def test_try_load_cached_document_misses_on_corrupt_json(app, tmp_path):
    media = _media_fixture(tmp_path)
    app.settings.output_dir = str(tmp_path)
    app._candidate_cache_path(media).write_text(
        "{ this is not valid json", encoding="utf-8"
    )
    assert app._try_load_cached_document(media) is None


def test_run_transcription_cache_hit_skips_inference(app, tmp_path, monkeypatch):
    """Cache hit must not construct Transcriber or call download_model;
    the worker emits a DoneEvent built from the cached Document."""
    from core.cache import cache_key
    from ui import app as app_module

    media = _media_fixture(tmp_path)
    app.settings.output_dir = str(tmp_path)
    key = cache_key(media)
    _write_cache_file(
        media_path=media,
        cache_path=app._candidate_cache_path(media),
        source_hash=key,
    )

    def _boom_transcriber(*a, **kw):
        raise AssertionError("Transcriber must not be constructed on cache hit")

    def _boom_download(*a, **kw):
        raise AssertionError("download_model must not be called on cache hit")

    monkeypatch.setattr(app_module, "Transcriber", _boom_transcriber)
    monkeypatch.setattr(app_module, "download_model", _boom_download)
    monkeypatch.setattr(app_module, "is_downloaded", lambda *_a, **_kw: True)

    app._run_transcription(media, "tiny", "en", ["txt"])

    events = []
    while not app.event_queue.empty():
        events.append(app.event_queue.get_nowait())
    progress = [e for e in events if isinstance(e, ProgressEvent)]
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done) == 1
    assert any("cached" in (e.label or "").lower() for e in progress)
    assert done[0].segments[0].text == "cached hello"
    # Derivative output (.txt) was written; the cache file is unchanged.
    assert (tmp_path / "media.txt").is_file()


def test_run_transcription_cache_hit_reports_existing_json_path(
    app, tmp_path, monkeypatch
):
    """When the user requests ``json``, the cache hit reports the existing
    sidecar path rather than writing a numbered-suffix duplicate."""
    from core.cache import cache_key
    from ui import app as app_module

    media = _media_fixture(tmp_path)
    app.settings.output_dir = str(tmp_path)
    key = cache_key(media)
    cache_path = app._candidate_cache_path(media)
    _write_cache_file(media_path=media, cache_path=cache_path, source_hash=key)
    cache_bytes_before = cache_path.read_bytes()

    monkeypatch.setattr(
        app_module,
        "Transcriber",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("Transcriber must not run")
        ),
    )
    monkeypatch.setattr(app_module, "is_downloaded", lambda *_a, **_kw: True)

    app._run_transcription(media, "tiny", "en", ["txt", "json"])

    events = list(_drain_queue(app.event_queue))
    done = next(e for e in events if isinstance(e, DoneEvent))
    assert done.output_files["json"] == cache_path
    # Cache file was not rewritten or duplicated.
    assert cache_path.read_bytes() == cache_bytes_before
    assert not (tmp_path / "media_1.transcribe.json").exists()


def test_run_transcription_mtime_change_invalidates_cache(
    app, tmp_path, monkeypatch
):
    """Bumping the file's mtime changes the cache key, so a stale cache is
    not used and a fresh transcription path runs."""
    import os as _os

    from core.cache import cache_key
    from ui import app as app_module

    media = _media_fixture(tmp_path)
    app.settings.output_dir = str(tmp_path)
    stale_key = cache_key(media)
    _write_cache_file(
        media_path=media,
        cache_path=app._candidate_cache_path(media),
        source_hash=stale_key,
    )

    # Bump mtime: cache key now differs from what's stored.
    st = media.stat()
    _os.utime(media, (st.st_atime, st.st_mtime + 100.0))
    assert cache_key(media) != stale_key

    transcriber_calls: list[str] = []

    class _FakeTranscriber:
        def __init__(self, *_a, **_kw):
            transcriber_calls.append("init")

        def transcribe(self, _media, **_kw):
            transcriber_calls.append("transcribe")
            from core.document import Segment

            return [Segment(text="fresh", start=0.0, end=1.0)], SimpleNamespace(
                language="en", duration=1.0
            )

        def cancel(self) -> None:
            pass

    monkeypatch.setattr(app_module, "Transcriber", _FakeTranscriber)
    monkeypatch.setattr(app_module, "is_downloaded", lambda *_a, **_kw: True)

    app._run_transcription(media, "tiny", "en", ["txt"])

    assert transcriber_calls == ["init", "transcribe"]


def _drain_queue(q):
    while not q.empty():
        yield q.get_nowait()
