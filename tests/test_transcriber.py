"""Tests for core.transcriber.

The real-model integration test is marked slow because it downloads the
``tiny`` weights on first run (~75 MB) and runs CPU inference end-to-end.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from core import transcriber

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE = REPO_ROOT / "tests" / "fixtures" / "sample.wav"


# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------


def test_default_compute_type_is_int8():
    """Spec says default compute_type='int8' on this platform."""
    assert transcriber.DEFAULT_COMPUTE_TYPE == "int8"


def test_default_device_is_auto():
    assert transcriber.DEFAULT_DEVICE == "auto"


# ---------------------------------------------------------------------------
# Cancellation logic — tested without a real model via _consume_segments.
# ---------------------------------------------------------------------------


@dataclass
class FakeSegment:
    text: str
    start: float
    end: float


class _StubTranscriber:
    """Re-uses the production cancellation logic without loading a model."""

    def __init__(self):
        import threading
        self._cancelled = threading.Event()

    cancel = transcriber.Transcriber.cancel
    reset_cancel = transcriber.Transcriber.reset_cancel
    is_cancelled = transcriber.Transcriber.is_cancelled
    _consume_segments = transcriber.Transcriber._consume_segments


def test_consume_segments_fires_callbacks_and_collects():
    t = _StubTranscriber()
    segs = [
        FakeSegment("hello", 0.0, 1.0),
        FakeSegment("world", 1.0, 2.0),
    ]
    texts: list[str] = []
    progresses: list[float] = []
    collected = t._consume_segments(
        iter(segs),
        total_duration=2.0,
        on_segment=texts.append,
        on_progress=progresses.append,
    )
    assert [s.text for s in collected] == ["hello", "world"]
    assert texts == ["hello", "world"]
    assert progresses == [pytest.approx(0.5), pytest.approx(1.0)]


def test_consume_segments_clamps_progress_to_unit_interval():
    t = _StubTranscriber()
    segs = [FakeSegment("late", 0.0, 100.0)]  # end well past total_duration
    progresses: list[float] = []
    t._consume_segments(
        iter(segs), total_duration=2.0, on_segment=lambda _t: None, on_progress=progresses.append
    )
    assert progresses == [1.0]


def test_consume_segments_no_progress_when_duration_unknown():
    t = _StubTranscriber()
    segs = [FakeSegment("x", 0.0, 1.0)]
    progresses: list[float] = []
    t._consume_segments(
        iter(segs), total_duration=0.0, on_segment=lambda _t: None, on_progress=progresses.append
    )
    assert progresses == []


def test_consume_segments_stops_when_cancelled_mid_iteration():
    t = _StubTranscriber()

    def gen():
        yield FakeSegment("first", 0.0, 1.0)
        # Cancel before yielding the second.
        t.cancel()
        yield FakeSegment("second", 1.0, 2.0)
        yield FakeSegment("third", 2.0, 3.0)

    texts: list[str] = []
    collected = t._consume_segments(
        gen(), total_duration=3.0, on_segment=texts.append, on_progress=lambda _p: None
    )
    # First segment was yielded before cancel was set, so it was collected.
    # Loop checks _cancelled at the top of each iteration, so "second"
    # is observed-but-not-collected.
    assert [s.text for s in collected] == ["first"]
    assert texts == ["first"]


def test_reset_cancel_clears_flag():
    t = _StubTranscriber()
    t.cancel()
    assert t.is_cancelled is True
    t.reset_cancel()
    assert t.is_cancelled is False


# ---------------------------------------------------------------------------
# Integration test — downloads tiny model on first run.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_real_transcription_on_sample_fixture():
    tx = transcriber.Transcriber("tiny", device="auto", compute_type="int8")
    assert tx.compute_type == "int8"

    texts: list[str] = []
    progresses: list[float] = []
    segments, info = tx.transcribe(
        SAMPLE,
        language=None,
        on_segment=texts.append,
        on_progress=progresses.append,
    )

    assert len(segments) >= 1, "expected at least one segment from a 6s sample"
    assert len(texts) == len(segments)
    assert len(progresses) >= 1
    assert info.language, "expected language detection to set info.language"
    # Final progress callback should reach the end of the file.
    assert progresses[-1] == pytest.approx(1.0, abs=0.05)
