"""Tests for ui.state — pure-logic state machine + worker event handling.

These tests do not require a Tk display.
"""

from __future__ import annotations

import queue
from pathlib import Path
from types import SimpleNamespace

import pytest

from ui.state import (
    AppState,
    AppStateMachine,
    CancelledEvent,
    DoneEvent,
    ErrorEvent,
    InvalidTransitionError,
    ProgressEvent,
    SegmentEvent,
    is_supported_media,
    pump_queue,
)

# ---------------------------------------------------------------------------
# is_supported_media — drop-zone whitelist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["a.mp4", "b.mov", "c.mkv", "d.mp3", "e.wav", "f.m4a", "G.MP4", "h.MoV"]
)
def test_is_supported_media_accepts_whitelisted(name):
    assert is_supported_media(name) is True


@pytest.mark.parametrize("name", ["x.txt", "y.exe", "z.docx", "noext", "movie.avi"])
def test_is_supported_media_rejects_other(name):
    assert is_supported_media(name) is False


# ---------------------------------------------------------------------------
# Transitions per spec 4.3
# ---------------------------------------------------------------------------


def media(tmp_path: Path) -> Path:
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"")
    return p


def test_idle_to_file_loaded(tmp_path):
    sm = AppStateMachine()
    sm.load_file(media(tmp_path))
    assert sm.state == AppState.FILE_LOADED


def test_file_loaded_to_transcribing_to_complete(tmp_path):
    sm = AppStateMachine()
    sm.load_file(media(tmp_path))
    sm.start_transcribing()
    assert sm.state == AppState.TRANSCRIBING
    sm.apply_event(
        DoneEvent(
            segments=[SimpleNamespace(text="hello", start=0.0, end=1.0)],
            info=SimpleNamespace(language="en", duration=1.0),
            output_files={"txt": tmp_path / "clip.txt"},
            elapsed=1.0,
        )
    )
    assert sm.state == AppState.COMPLETE
    assert sm.result is not None
    assert sm.result.elapsed == 1.0


def test_idle_to_file_loaded_to_error_to_idle(tmp_path):
    sm = AppStateMachine()
    sm.load_file(media(tmp_path))
    sm.apply_event(ErrorEvent(message="boom"))
    assert sm.state == AppState.ERROR
    assert sm.error_message == "boom"
    sm.reset()
    assert sm.state == AppState.IDLE
    assert sm.error_message is None


def test_transcribing_to_idle_via_cancel(tmp_path):
    sm = AppStateMachine()
    sm.load_file(media(tmp_path))
    sm.start_transcribing()
    sm.cancel()
    assert sm.state == AppState.IDLE
    assert sm.media_path is None


def test_complete_to_file_loaded_via_load_file(tmp_path):
    sm = AppStateMachine()
    sm.load_file(media(tmp_path))
    sm.start_transcribing()
    sm.apply_event(
        DoneEvent(
            segments=[SimpleNamespace(text="x", start=0, end=1)],
            info=SimpleNamespace(language="en"),
            output_files={"txt": tmp_path / "clip.txt"},
            elapsed=1.0,
        )
    )
    other = tmp_path / "other.wav"
    other.write_bytes(b"")
    sm.load_file(other)
    assert sm.state == AppState.FILE_LOADED
    assert sm.media_path == other


def test_load_unsupported_extension_raises(tmp_path):
    sm = AppStateMachine()
    bad = tmp_path / "foo.txt"
    bad.write_text("x")
    with pytest.raises(ValueError):
        sm.load_file(bad)


def test_cancel_only_legal_in_transcribing(tmp_path):
    sm = AppStateMachine()
    with pytest.raises(InvalidTransitionError):
        sm.cancel()
    sm.load_file(media(tmp_path))
    with pytest.raises(InvalidTransitionError):
        sm.cancel()


def test_start_transcribing_from_idle_raises():
    sm = AppStateMachine()
    with pytest.raises(InvalidTransitionError):
        sm.start_transcribing()


# ---------------------------------------------------------------------------
# Listener notifications
# ---------------------------------------------------------------------------


def test_listener_fires_on_state_change(tmp_path):
    sm = AppStateMachine()
    seen: list[AppState] = []
    sm.on_change(seen.append)
    sm.load_file(media(tmp_path))
    sm.start_transcribing()
    assert seen[0] == AppState.FILE_LOADED
    assert seen[-1] == AppState.TRANSCRIBING


# ---------------------------------------------------------------------------
# apply_event behaviors
# ---------------------------------------------------------------------------


def test_segment_event_appends_only_while_transcribing(tmp_path):
    sm = AppStateMachine()
    sm.apply_event(SegmentEvent(text="ignored when idle"))
    assert sm.streaming_text == []
    sm.load_file(media(tmp_path))
    sm.start_transcribing()
    sm.apply_event(SegmentEvent(text="hello"))
    sm.apply_event(SegmentEvent(text="world"))
    assert sm.streaming_text == ["hello", "world"]


def test_progress_event_clamps_and_records_label(tmp_path):
    sm = AppStateMachine()
    sm.load_file(media(tmp_path))
    sm.start_transcribing()
    sm.apply_event(ProgressEvent(fraction=2.0, label="Transcribing…"))
    assert sm.progress == 1.0
    assert sm.progress_label == "Transcribing…"
    sm.apply_event(ProgressEvent(fraction=-0.5))
    assert sm.progress == 0.0


def test_done_event_after_cancel_is_dropped(tmp_path):
    sm = AppStateMachine()
    sm.load_file(media(tmp_path))
    sm.start_transcribing()
    sm.cancel()
    sm.apply_event(
        DoneEvent(segments=[], info=None, output_files={}, elapsed=0.0)
    )
    # Stale event should not flip us back to COMPLETE.
    assert sm.state == AppState.IDLE
    assert sm.result is None


def test_error_event_sets_state_and_message(tmp_path):
    sm = AppStateMachine()
    sm.load_file(media(tmp_path))
    sm.start_transcribing()
    sm.apply_event(ErrorEvent(message="kaboom"))
    assert sm.state == AppState.ERROR
    assert sm.error_message == "kaboom"


def test_cancelled_event_is_a_noop_after_user_cancel(tmp_path):
    sm = AppStateMachine()
    sm.load_file(media(tmp_path))
    sm.start_transcribing()
    sm.cancel()
    sm.apply_event(CancelledEvent())
    assert sm.state == AppState.IDLE


def test_apply_event_unknown_type_raises(tmp_path):
    sm = AppStateMachine()
    sm.load_file(media(tmp_path))
    sm.start_transcribing()

    class Bogus:
        pass

    with pytest.raises(TypeError):
        sm.apply_event(Bogus())


# ---------------------------------------------------------------------------
# pump_queue plumbing
# ---------------------------------------------------------------------------


def test_pump_queue_drains_and_applies(tmp_path):
    sm = AppStateMachine()
    sm.load_file(media(tmp_path))
    sm.start_transcribing()
    q: queue.Queue = queue.Queue()
    q.put(SegmentEvent(text="one"))
    q.put(SegmentEvent(text="two"))
    q.put(ProgressEvent(fraction=0.5))
    n = pump_queue(q, sm)
    assert n == 3
    assert sm.streaming_text == ["one", "two"]
    assert sm.progress == 0.5
    assert q.empty()


def test_pump_queue_empty_returns_zero(tmp_path):
    sm = AppStateMachine()
    q: queue.Queue = queue.Queue()
    assert pump_queue(q, sm) == 0


def test_pump_queue_progress_drives_state_transition_to_complete(tmp_path):
    sm = AppStateMachine()
    sm.load_file(media(tmp_path))
    sm.start_transcribing()
    q: queue.Queue = queue.Queue()
    q.put(ProgressEvent(fraction=1.0))
    q.put(
        DoneEvent(
            segments=[SimpleNamespace(text="x", start=0, end=1)],
            info=SimpleNamespace(language="en"),
            output_files={"txt": tmp_path / "clip.txt"},
            elapsed=2.0,
        )
    )
    pump_queue(q, sm)
    assert sm.state == AppState.COMPLETE
