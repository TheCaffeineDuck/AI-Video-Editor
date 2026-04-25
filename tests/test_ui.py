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
