"""Tests for core.render.

Fast tests exercise the helpers (cut inversion, padding, fraction
conversion, progress adapter) and the precondition guards on
``render_cut`` (empty cuts, full-duration cuts, missing media). The
slow tests actually invoke smartcut on the synthetic video fixture and
verify cut correctness end-to-end via duration probing and decode.
"""

from __future__ import annotations

from datetime import datetime
from fractions import Fraction
from pathlib import Path

import pytest

from core.document import CutMark, Document, Segment
from core.render import (
    _invert_cuts_to_keep_ranges,
    _pad_and_merge_keep_ranges,
    _ProgressAdapter,
    _resolve_keep_ranges,
    _to_fraction_seconds,
    render_cut,
)


def _doc(media_path: Path, *, cuts: list[CutMark] | None = None, duration: float = 30.0) -> Document:
    return Document(
        media_path=media_path,
        duration=duration,
        language="en",
        segments=[Segment(text="x", start=0.0, end=duration)],
        cuts=list(cuts) if cuts is not None else [],
        created_at=datetime(2026, 4, 27, 10, 0, 0),
        model_name="tiny",
    )


# ---------------------------------------------------------------------------
# _to_fraction_seconds
# ---------------------------------------------------------------------------


def test_fraction_milliseconds_precision():
    f = _to_fraction_seconds(1.234)
    assert isinstance(f, Fraction)
    # Within 1ms of input
    assert abs(float(f) - 1.234) <= 1e-3


def test_fraction_round_trips_clean_values():
    assert _to_fraction_seconds(0.0) == Fraction(0)
    assert _to_fraction_seconds(1.5) == Fraction(3, 2)


def test_fraction_truncates_sub_millisecond():
    """1.2345 → 1.234 or 1.235 — within 1ms either way."""
    f = _to_fraction_seconds(1.2345)
    assert f.denominator <= 1000
    assert abs(float(f) - 1.2345) <= 1e-3


# ---------------------------------------------------------------------------
# _invert_cuts_to_keep_ranges
# ---------------------------------------------------------------------------


def test_invert_no_cuts_returns_full_range():
    assert _invert_cuts_to_keep_ranges([], 30.0) == [(0.0, 30.0)]


def test_invert_single_middle_cut():
    cuts = [CutMark(10.0, 15.0)]
    assert _invert_cuts_to_keep_ranges(cuts, 30.0) == [(0.0, 10.0), (15.0, 30.0)]


def test_invert_cut_at_start():
    cuts = [CutMark(0.0, 5.0)]
    assert _invert_cuts_to_keep_ranges(cuts, 30.0) == [(5.0, 30.0)]


def test_invert_cut_at_end():
    cuts = [CutMark(25.0, 30.0)]
    assert _invert_cuts_to_keep_ranges(cuts, 30.0) == [(0.0, 25.0)]


def test_invert_cut_covering_entire_duration():
    cuts = [CutMark(0.0, 30.0)]
    assert _invert_cuts_to_keep_ranges(cuts, 30.0) == []


def test_invert_clamps_cut_beyond_duration():
    cuts = [CutMark(25.0, 100.0)]
    assert _invert_cuts_to_keep_ranges(cuts, 30.0) == [(0.0, 25.0)]


def test_invert_two_separated_cuts():
    cuts = [CutMark(5.0, 10.0), CutMark(20.0, 25.0)]
    assert _invert_cuts_to_keep_ranges(cuts, 30.0) == [
        (0.0, 5.0), (10.0, 20.0), (25.0, 30.0),
    ]


# ---------------------------------------------------------------------------
# _pad_and_merge_keep_ranges
# ---------------------------------------------------------------------------


def test_pad_clamps_to_zero_at_start():
    """Pad must not push start below 0."""
    out = _pad_and_merge_keep_ranges([(0.05, 10.0)], pad=0.10, duration=30.0)
    assert out == [(0.0, 10.10)]


def test_pad_clamps_to_duration_at_end():
    out = _pad_and_merge_keep_ranges([(20.0, 29.95)], pad=0.10, duration=30.0)
    assert out == [(19.90, 30.0)]


def test_pad_no_overlap_keeps_separate():
    out = _pad_and_merge_keep_ranges(
        [(0.0, 10.0), (20.0, 30.0)], pad=0.10, duration=30.0
    )
    assert out == [(0.0, 10.10), (19.90, 30.0)]


def test_pad_causing_overlap_merges_ranges():
    """After padding, two adjacent keep-ranges overlap → merge them."""
    out = _pad_and_merge_keep_ranges(
        [(0.0, 10.0), (10.05, 30.0)], pad=0.10, duration=30.0
    )
    assert out == [(0.0, 30.0)]


def test_pad_zero_is_noop():
    out = _pad_and_merge_keep_ranges([(0.0, 10.0)], pad=0.0, duration=30.0)
    assert out == [(0.0, 10.0)]


def test_pad_negative_raises():
    with pytest.raises(ValueError, match="pad"):
        _pad_and_merge_keep_ranges([(0.0, 10.0)], pad=-0.1, duration=30.0)


def test_pad_empty_input_returns_empty():
    assert _pad_and_merge_keep_ranges([], pad=0.1, duration=30.0) == []


# ---------------------------------------------------------------------------
# _resolve_keep_ranges — end-to-end of the helper pipeline
# ---------------------------------------------------------------------------


def test_resolve_no_cuts_yields_full_range(tmp_path: Path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"fake")
    doc = _doc(media)
    assert _resolve_keep_ranges(doc, pad=0.1, merge_gap=0.3) == [(0.0, 30.0)]


def test_resolve_two_cuts_with_sub_merge_gap_keep_range_absorbed(tmp_path: Path):
    """Two cuts 0.1s apart (merge_gap=0.30) join into one cut.

    Cuts [10-12] and [12.1-14] have a 0.1s keep-range between them.
    With merge_gap=0.30, the cuts pre-merge into [10-14]. Inversion gives
    keep-ranges [(0, 10), (14, 30)]; pad=0.10 expands each outward,
    eating 0.1s into the cut on each side, yielding [(0, 10.1), (13.9, 30)].
    Crucially: only TWO keep-ranges, not three — the tiny middle keep
    was absorbed by the cut-merge.
    """
    media = tmp_path / "x.mp4"
    media.write_bytes(b"fake")
    doc = _doc(media, cuts=[CutMark(10.0, 12.0), CutMark(12.1, 14.0)])
    out = _resolve_keep_ranges(doc, pad=0.10, merge_gap=0.30)
    assert out == [(0.0, 10.10), (13.90, 30.0)]


def test_resolve_full_duration_cut_yields_empty(tmp_path: Path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"fake")
    doc = _doc(media, cuts=[CutMark(0.0, 30.0)])
    assert _resolve_keep_ranges(doc, pad=0.10, merge_gap=0.30) == []


def test_resolve_handles_unsorted_cuts(tmp_path: Path):
    """Cuts in reverse order produce the same result as forward order."""
    media = tmp_path / "x.mp4"
    media.write_bytes(b"fake")
    cuts = [CutMark(20.0, 25.0), CutMark(5.0, 10.0)]
    doc = _doc(media, cuts=cuts)
    assert _resolve_keep_ranges(doc, pad=0.0, merge_gap=0.0) == [
        (0.0, 5.0), (10.0, 20.0), (25.0, 30.0),
    ]


# ---------------------------------------------------------------------------
# _ProgressAdapter
# ---------------------------------------------------------------------------


def test_progress_adapter_first_emit_sets_total_no_callback_yet():
    calls: list[float] = []
    a = _ProgressAdapter(calls.append)
    a.emit(10)  # total = 10
    assert calls == []  # no callback on the total announcement


def test_progress_adapter_increments_clamp_to_one():
    calls: list[float] = []
    a = _ProgressAdapter(calls.append)
    a.emit(4)        # total = 4
    a.emit(1)        # 1/4
    a.emit(1)        # 2/4
    a.emit(2)        # 4/4 — exactly 1.0
    a.emit(5)        # over budget → clamped, no callback (not > last)
    assert calls == [0.25, 0.5, 1.0]


def test_progress_adapter_is_monotonic():
    """Even with non-uniform increments, fraction never decreases."""
    calls: list[float] = []
    a = _ProgressAdapter(calls.append)
    a.emit(10)
    a.emit(7)   # 7/10 = 0.7
    a.emit(0)   # 0 increment — must NOT call back (not > last)
    a.emit(2)   # 9/10 = 0.9
    assert calls == pytest.approx([0.7, 0.9])
    assert calls == sorted(calls)


def test_progress_adapter_finalize_reaches_one():
    calls: list[float] = []
    a = _ProgressAdapter(calls.append)
    a.emit(10)
    a.emit(5)        # 0.5
    a.finalize()     # promote to 1.0
    assert calls == [0.5, 1.0]


def test_progress_adapter_finalize_noop_when_already_one():
    calls: list[float] = []
    a = _ProgressAdapter(calls.append)
    a.emit(2)
    a.emit(2)        # 2/2 = 1.0
    a.finalize()
    assert calls == [1.0]


def test_progress_adapter_no_callback_is_safe():
    """A None callback must not raise on emit or finalize."""
    a = _ProgressAdapter(None)
    a.emit(4)
    a.emit(1)
    a.emit(3)
    a.finalize()  # must not raise


# ---------------------------------------------------------------------------
# render_cut — fast precondition tests (no smartcut invoked)
# ---------------------------------------------------------------------------


def test_render_cut_missing_media_path_raises(tmp_path: Path):
    doc = _doc(tmp_path / "does_not_exist.mp4")
    with pytest.raises(FileNotFoundError, match="media_path"):
        render_cut(doc, tmp_path / "out.mp4")


def test_render_cut_empty_cuts_copies_source(tmp_path: Path):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"the original media bytes")
    out = tmp_path / "out.mp4"
    progress: list[float] = []
    result = render_cut(_doc(src), out, on_progress=progress.append)
    assert result == out
    assert out.read_bytes() == b"the original media bytes"
    assert progress == [1.0]


def test_render_cut_full_duration_cut_raises_before_smartcut(tmp_path: Path):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"placeholder")
    doc = _doc(src, cuts=[CutMark(0.0, 30.0)])
    out = tmp_path / "out.mp4"
    with pytest.raises(ValueError, match="entire media"):
        render_cut(doc, out)
    assert not out.exists()  # no zero-length file written


# ---------------------------------------------------------------------------
# render_cut — slow tests using the synthetic video fixture
# ---------------------------------------------------------------------------


def _doc_for_video(path: Path, *, cuts: list[CutMark] | None = None) -> Document:
    return _doc(path, cuts=cuts, duration=30.0)


@pytest.mark.slow
def test_render_cut_empty_cuts_yields_same_duration(
    synthetic_video: Path, probe_duration, is_playable, tmp_path: Path
):
    out = tmp_path / "out.mp4"
    render_cut(_doc_for_video(synthetic_video), out)
    assert is_playable(out)
    assert probe_duration(out) == pytest.approx(probe_duration(synthetic_video), abs=0.05)


@pytest.mark.slow
def test_render_cut_single_middle_cut_shortens_by_cut_length(
    synthetic_video: Path, probe_duration, is_playable, tmp_path: Path
):
    """Cut [12, 18] (6s wide) on a 30s file → output ≈ 24s (with default pad)."""
    doc = _doc_for_video(synthetic_video, cuts=[CutMark(12.0, 18.0)])
    out = tmp_path / "out.mp4"
    progress: list[float] = []
    render_cut(doc, out, on_progress=progress.append)
    assert is_playable(out)
    src_dur = probe_duration(synthetic_video)
    out_dur = probe_duration(out)
    # Expected keep: [0, 12.10) + (17.90, 30] → 12.10 + 12.10 = 24.20s.
    # Allow ±0.5s for keyframe rounding.
    assert out_dur == pytest.approx(src_dur - 5.8, abs=0.5)
    assert progress, "progress callback must fire at least once"
    assert progress[-1] == 1.0
    assert progress == sorted(progress)


@pytest.mark.slow
def test_render_cut_at_start(
    synthetic_video: Path, probe_duration, is_playable, tmp_path: Path
):
    """Cut [0, 10] → output ≈ duration - 10."""
    doc = _doc_for_video(synthetic_video, cuts=[CutMark(0.0, 10.0)])
    out = tmp_path / "out.mp4"
    render_cut(doc, out)
    assert is_playable(out)
    src_dur = probe_duration(synthetic_video)
    assert probe_duration(out) == pytest.approx(src_dur - 10.0, abs=0.5)


@pytest.mark.slow
def test_render_cut_at_end(
    synthetic_video: Path, probe_duration, is_playable, tmp_path: Path
):
    """Cut [20, 30] on 30s file → output ≈ 20s."""
    doc = _doc_for_video(synthetic_video, cuts=[CutMark(20.0, 30.0)])
    out = tmp_path / "out.mp4"
    render_cut(doc, out)
    assert is_playable(out)
    assert probe_duration(out) == pytest.approx(20.0, abs=0.5)


@pytest.mark.slow
def test_render_cut_two_close_cuts_join_via_merge_gap(
    synthetic_video: Path, probe_duration, is_playable, tmp_path: Path
):
    """Cuts [10, 12] and [12.1, 14] merge (gap 0.1 < merge_gap 0.30)
    into [10, 14] before rendering. Output ≈ 30 - 4 = 26s."""
    doc = _doc_for_video(
        synthetic_video,
        cuts=[CutMark(10.0, 12.0), CutMark(12.1, 14.0)],
    )
    out = tmp_path / "out.mp4"
    render_cut(doc, out)
    assert is_playable(out)
    assert probe_duration(out) == pytest.approx(26.0, abs=0.5)


@pytest.mark.slow
def test_render_cut_progress_reaches_one_and_is_monotonic(
    synthetic_video: Path, tmp_path: Path
):
    """End-to-end progress contract: at least one call, ends at 1.0, monotonic."""
    doc = _doc_for_video(synthetic_video, cuts=[CutMark(10.0, 20.0)])
    out = tmp_path / "out.mp4"
    progress: list[float] = []
    render_cut(doc, out, on_progress=progress.append)
    assert progress
    assert progress[-1] == 1.0
    assert all(0.0 <= p <= 1.0 for p in progress)
    assert progress == sorted(progress), "progress went backwards"
