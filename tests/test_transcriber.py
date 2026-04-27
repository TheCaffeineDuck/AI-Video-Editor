"""Tests for core.transcriber.

The real-model integration test is marked slow because it downloads the
``tiny`` weights on first run (~75 MB) and runs CPU inference end-to-end.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from core import transcriber
from core.document import Segment, Word

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
# Boundary conversion: faster-whisper → core.document.Segment
# ---------------------------------------------------------------------------


@dataclass
class _FWWord:
    """Shape-compatible with faster_whisper.transcribe.Word (start/end/word/probability)."""

    start: float
    end: float
    word: str
    probability: float


@dataclass
class _FWSegment:
    """Shape-compatible with faster_whisper.transcribe.Segment for our purposes."""

    start: float
    end: float
    text: str
    words: list[_FWWord] | None = field(default=None)


def test_to_core_segment_with_words():
    raw = _FWSegment(
        start=0.0,
        end=1.5,
        text=" Hello world",
        words=[
            _FWWord(start=0.0, end=0.5, word=" Hello", probability=0.95),
            _FWWord(start=0.6, end=1.4, word=" world", probability=0.88),
        ],
    )
    converted = transcriber._to_core_segment(raw)
    assert isinstance(converted, Segment)
    assert converted.text == " Hello world"
    assert converted.start == 0.0
    assert converted.end == 1.5
    assert len(converted.words) == 2
    assert isinstance(converted.words, tuple)
    first = converted.words[0]
    assert isinstance(first, Word)
    assert first.text == " Hello"
    assert first.start == 0.0
    assert first.end == 0.5
    assert first.probability == 0.95


def test_to_core_segment_without_words():
    """word_timestamps=False yields seg.words == None — must convert to ()."""
    raw = _FWSegment(start=0.0, end=2.0, text="anything", words=None)
    converted = transcriber._to_core_segment(raw)
    assert converted.words == ()


def test_to_core_segment_handles_none_probability():
    raw = _FWSegment(
        start=0.0,
        end=1.0,
        text="hi",
        words=[_FWWord(start=0.0, end=0.5, word="hi", probability=None)],  # type: ignore[arg-type]
    )
    converted = transcriber._to_core_segment(raw)
    assert converted.words[0].probability is None


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
    # Phase 4a: returned segments are core.document.Segment, with words by default.
    assert all(isinstance(s, Segment) for s in segments)
    flat_words = [w for s in segments for w in s.words]
    assert flat_words, "expected non-empty word-level timestamps with default word_timestamps=True"
    assert all(isinstance(w, Word) for w in flat_words)
    # Word boundaries should be monotonic and lie within their parent segment.
    for s in segments:
        for w in s.words:
            assert s.start - 0.05 <= w.start <= w.end <= s.end + 0.05


@pytest.mark.slow
def test_real_transcription_word_timestamps_can_be_disabled():
    tx = transcriber.Transcriber("tiny", device="auto", compute_type="int8")
    segments, _ = tx.transcribe(
        SAMPLE,
        language=None,
        on_segment=lambda _t: None,
        on_progress=lambda _p: None,
        word_timestamps=False,
    )
    assert all(s.words == () for s in segments)


@pytest.mark.slow
def test_end_to_end_writes_loadable_transcribe_json(tmp_path: Path):
    """Phase 4e: transcribe sample.wav, write json, re-load Document, verify
    word-level data and metadata survived."""
    import json
    import shutil

    from core import exporters
    from core.document import Document, Range, build_document

    # Copy the fixture into tmp so we write its outputs alongside without
    # polluting the repo.
    src = tmp_path / "sample.wav"
    shutil.copy2(SAMPLE, src)

    tx = transcriber.Transcriber("tiny", device="auto", compute_type="int8")
    segments, info = tx.transcribe(
        src,
        language=None,
        on_segment=lambda _t: None,
        on_progress=lambda _p: None,
    )

    doc = build_document(
        media_path=src,
        duration=float(info.duration or 0.0),
        language=info.language,
        segments=segments,
        model_name="tiny",
    )
    written = exporters.write_outputs(
        src, segments, ["txt", "srt", "json"], document=doc
    )

    json_path = written["json"]
    assert json_path == tmp_path / "sample.transcribe.json"
    assert json_path.is_file()

    restored = Document.from_json(json.loads(json_path.read_text()))
    src_entry = restored.sources["src0"]
    assert src_entry.path == src
    assert src_entry.duration == pytest.approx(info.duration, abs=0.05)
    assert restored.language == info.language
    assert restored.model_name == "tiny"
    # Initial timeline is one full-duration keep-range — no edits applied.
    assert restored.ranges == [
        Range(source_id="src0", start=0.0, end=src_entry.duration),
    ]
    # Word-level data flows through end-to-end:
    flat_words = [w for s in restored.segments for w in s.words]
    assert flat_words, "expected word-level timestamps to round-trip through JSON"
