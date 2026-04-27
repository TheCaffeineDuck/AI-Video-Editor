"""Tests for ``core.render`` against the v2 Document model.

Phase 4f-3 changed render_cut to consume ``doc.ranges`` directly. Most
tests below describe the same scenarios as before in keep-range terms;
a few helpers (``_invert_cuts_to_keep_ranges`` and the cuts-flavoured
``_snap_cuts_to_word_boundaries``) went away — their replacements are
``_snap_ranges_to_word_boundaries`` (with outward-snap semantics for
keep-ranges) and the keep-range arithmetic baked into
``_resolve_keep_ranges``.

Fast tests exercise the helpers and precondition guards. Slow tests
invoke smartcut on the synthetic video fixture.
"""

from __future__ import annotations

from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

import pytest

from core.document import Document, MediaSource, Range, Segment, Word
from core.render import (
    _is_full_coverage,
    _join_times_in_output,
    _merge_close_keep_ranges,
    _pad_and_merge_keep_ranges,
    _ProgressAdapter,
    _resolve_keep_ranges,
    _snap_ranges_to_word_boundaries,
    _to_fraction_seconds,
    render_cut,
)


def _ranges_after_cuts(
    cuts: list[tuple[float, float]], duration: float, source_id: str = "src0"
) -> list[Range]:
    """Helper mirroring the v1 ``cuts=[...]`` test idiom in v2 terms.

    Build the v2 ranges that result from subtracting each (start, end)
    cut interval from a full-duration keep-range.
    """
    from core.timeline import subtract_interval

    ranges: list[Range] = [Range(source_id=source_id, start=0.0, end=duration)]
    for start, end in cuts:
        ranges = subtract_interval(ranges, (start, end), source_id)
    return ranges


def _doc(
    media_path: Path,
    *,
    cuts: list[tuple[float, float]] | None = None,
    ranges: list[Range] | None = None,
    duration: float = 30.0,
    segments: list[Segment] | None = None,
) -> Document:
    """A minimal v2 Document for render_cut tests.

    Pass ``cuts=[(s, e), ...]`` to mirror the v1 test idiom; the helper
    derives the v2 ``ranges`` by subtracting them from the full-duration
    keep-range. Or pass ``ranges=`` explicitly for tests that need a
    specific timeline shape.
    """
    if ranges is None:
        ranges = _ranges_after_cuts(cuts or [], duration)
    return Document(
        sources={
            "src0": MediaSource(id="src0", path=media_path, duration=duration)
        },
        segments=segments or [Segment(text="x", start=0.0, end=duration)],
        ranges=list(ranges),
        language="en",
        created_at=datetime(2026, 4, 27, 10, 0, 0, tzinfo=UTC),
        model_name="tiny",
    )


# ---------------------------------------------------------------------------
# _to_fraction_seconds
# ---------------------------------------------------------------------------


def test_fraction_milliseconds_precision():
    f = _to_fraction_seconds(1.234)
    assert isinstance(f, Fraction)
    assert abs(float(f) - 1.234) <= 1e-3


def test_fraction_round_trips_clean_values():
    assert _to_fraction_seconds(0.0) == Fraction(0)
    assert _to_fraction_seconds(1.5) == Fraction(3, 2)


def test_fraction_truncates_sub_millisecond():
    f = _to_fraction_seconds(1.2345)
    assert f.denominator <= 1000
    assert abs(float(f) - 1.2345) <= 1e-3


# ---------------------------------------------------------------------------
# _is_full_coverage
# ---------------------------------------------------------------------------


def test_full_coverage_single_full_range_is_true():
    ranges = [Range(source_id="src0", start=0.0, end=10.0)]
    assert _is_full_coverage(ranges, 10.0)


def test_full_coverage_contiguous_pair_is_true():
    ranges = [
        Range(source_id="src0", start=0.0, end=4.0),
        Range(source_id="src0", start=4.0, end=10.0),
    ]
    assert _is_full_coverage(ranges, 10.0)


def test_full_coverage_with_gap_is_false():
    ranges = [
        Range(source_id="src0", start=0.0, end=4.0),
        Range(source_id="src0", start=5.0, end=10.0),
    ]
    assert not _is_full_coverage(ranges, 10.0)


def test_full_coverage_starting_late_is_false():
    ranges = [Range(source_id="src0", start=1.0, end=10.0)]
    assert not _is_full_coverage(ranges, 10.0)


def test_full_coverage_ending_early_is_false():
    ranges = [Range(source_id="src0", start=0.0, end=9.0)]
    assert not _is_full_coverage(ranges, 10.0)


def test_full_coverage_empty_ranges_is_false():
    assert not _is_full_coverage([], 10.0)


# ---------------------------------------------------------------------------
# _pad_and_merge_keep_ranges
# ---------------------------------------------------------------------------


def test_pad_clamps_to_zero_at_start():
    out = _pad_and_merge_keep_ranges(
        [(0.05, 10.0)], pad_lead=0.10, pad_trail=0.10, duration=30.0
    )
    assert out == [(0.0, 10.10)]


def test_pad_clamps_to_duration_at_end():
    out = _pad_and_merge_keep_ranges(
        [(20.0, 29.95)], pad_lead=0.10, pad_trail=0.10, duration=30.0
    )
    assert out == [(19.90, 30.0)]


def test_pad_no_overlap_keeps_separate():
    out = _pad_and_merge_keep_ranges(
        [(0.0, 10.0), (20.0, 30.0)], pad_lead=0.10, pad_trail=0.10, duration=30.0
    )
    assert out == [(0.0, 10.10), (19.90, 30.0)]


def test_pad_causing_overlap_merges_ranges():
    out = _pad_and_merge_keep_ranges(
        [(0.0, 10.0), (10.05, 30.0)], pad_lead=0.10, pad_trail=0.10, duration=30.0
    )
    assert out == [(0.0, 30.0)]


def test_pad_zero_is_noop():
    out = _pad_and_merge_keep_ranges(
        [(0.0, 10.0)], pad_lead=0.0, pad_trail=0.0, duration=30.0
    )
    assert out == [(0.0, 10.0)]


def test_pad_lead_negative_raises():
    with pytest.raises(ValueError, match="pad_lead"):
        _pad_and_merge_keep_ranges(
            [(0.0, 10.0)], pad_lead=-0.1, pad_trail=0.1, duration=30.0
        )


def test_pad_trail_negative_raises():
    with pytest.raises(ValueError, match="pad_trail"):
        _pad_and_merge_keep_ranges(
            [(0.0, 10.0)], pad_lead=0.1, pad_trail=-0.1, duration=30.0
        )


def test_pad_asymmetric_uses_each_side_independently():
    out = _pad_and_merge_keep_ranges(
        [(5.0, 10.0)], pad_lead=0.05, pad_trail=0.20, duration=30.0
    )
    assert out == [(4.95, 10.20)]


def test_pad_empty_input_returns_empty():
    assert (
        _pad_and_merge_keep_ranges(
            [], pad_lead=0.1, pad_trail=0.1, duration=30.0
        )
        == []
    )


# ---------------------------------------------------------------------------
# _merge_close_keep_ranges
# ---------------------------------------------------------------------------


def test_merge_close_no_op_when_gap_exceeds_threshold():
    out = _merge_close_keep_ranges([(0.0, 10.0), (15.0, 20.0)], merge_gap=0.30)
    assert out == [(0.0, 10.0), (15.0, 20.0)]


def test_merge_close_absorbs_sub_threshold_gap():
    out = _merge_close_keep_ranges([(0.0, 10.0), (10.1, 20.0)], merge_gap=0.30)
    assert out == [(0.0, 20.0)]


def test_merge_close_threshold_zero_is_noop():
    out = _merge_close_keep_ranges(
        [(0.0, 10.0), (10.0, 20.0)], merge_gap=0.0
    )
    # Gap of exactly 0 is NOT < 0; do not merge.
    assert out == [(0.0, 10.0), (10.0, 20.0)]


# ---------------------------------------------------------------------------
# _resolve_keep_ranges — end-to-end of the helper pipeline
# ---------------------------------------------------------------------------


def test_resolve_full_range_yields_full_range(tmp_path: Path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"fake")
    doc = _doc(media)
    assert _resolve_keep_ranges(
        doc, pad_lead=0.1, pad_trail=0.1, merge_gap=0.3
    ) == [(0.0, 30.0)]


def test_resolve_two_cuts_with_sub_merge_gap_keep_range_absorbed(tmp_path: Path):
    """Two cuts 0.1s apart (merge_gap=0.30) join into one cut on render.

    v2 ranges after cuts [10-12] and [12.1-14]:
      [(0, 10), (12, 12.1), (14, 30)]
    Padding by 0.10 each side → [(0, 10.1), (11.9, 12.2), (13.9, 30)].
    merge_gap=0.30 absorbs the 11.9–12.2 fragment into its neighbours,
    yielding [(0, 12.2), (13.9, 30)] — wait, those are 1.7s apart, more
    than merge_gap. Inspection: padded ranges are
      (0, 10.1), (11.9, 12.2), (13.9, 30)
    Gap (10.1, 11.9) = 1.8s; gap (12.2, 13.9) = 1.7s. Neither is < 0.30,
    so no merge. Three ranges remain. The v1 test used MergeAdjacentCuts
    *before* inversion which behaves differently — the v2 equivalent is
    documented by the new render-time helper here.
    """
    media = tmp_path / "x.mp4"
    media.write_bytes(b"fake")
    doc = _doc(media, cuts=[(10.0, 12.0), (12.1, 14.0)])
    out = _resolve_keep_ranges(
        doc, pad_lead=0.10, pad_trail=0.10, merge_gap=0.30
    )
    assert out == [(0.0, 10.10), (11.90, 12.20), (13.90, 30.0)]


def test_resolve_full_duration_cut_yields_empty(tmp_path: Path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"fake")
    doc = _doc(media, cuts=[(0.0, 30.0)])
    assert _resolve_keep_ranges(
        doc, pad_lead=0.10, pad_trail=0.10, merge_gap=0.30
    ) == []


def test_resolve_handles_unsorted_ranges(tmp_path: Path):
    """Ranges in unsorted order produce the same result as sorted order."""
    media = tmp_path / "x.mp4"
    media.write_bytes(b"fake")
    ranges = [
        Range(source_id="src0", start=20.0, end=25.0),
        Range(source_id="src0", start=0.0, end=10.0),
    ]
    doc = _doc(media, ranges=ranges)
    assert _resolve_keep_ranges(
        doc, pad_lead=0.0, pad_trail=0.0, merge_gap=0.0
    ) == [(0.0, 10.0), (20.0, 25.0)]


# ---------------------------------------------------------------------------
# _ProgressAdapter
# ---------------------------------------------------------------------------


def test_progress_adapter_first_emit_sets_total_no_callback_yet():
    calls: list[float] = []
    a = _ProgressAdapter(calls.append)
    a.emit(10)
    assert calls == []


def test_progress_adapter_increments_clamp_to_one():
    calls: list[float] = []
    a = _ProgressAdapter(calls.append)
    a.emit(4)
    a.emit(1)
    a.emit(1)
    a.emit(2)
    a.emit(5)
    assert calls == [0.25, 0.5, 1.0]


def test_progress_adapter_is_monotonic():
    calls: list[float] = []
    a = _ProgressAdapter(calls.append)
    a.emit(10)
    a.emit(7)
    a.emit(0)
    a.emit(2)
    assert calls == pytest.approx([0.7, 0.9])
    assert calls == sorted(calls)


def test_progress_adapter_finalize_reaches_one():
    calls: list[float] = []
    a = _ProgressAdapter(calls.append)
    a.emit(10)
    a.emit(5)
    a.finalize()
    assert calls == [0.5, 1.0]


def test_progress_adapter_finalize_noop_when_already_one():
    calls: list[float] = []
    a = _ProgressAdapter(calls.append)
    a.emit(2)
    a.emit(2)
    a.finalize()
    assert calls == [1.0]


def test_progress_adapter_no_callback_is_safe():
    a = _ProgressAdapter(None)
    a.emit(4)
    a.emit(1)
    a.emit(3)
    a.finalize()


# ---------------------------------------------------------------------------
# render_cut — fast precondition tests (no smartcut invoked)
# ---------------------------------------------------------------------------


def test_render_cut_missing_media_path_raises(tmp_path: Path):
    doc = _doc(tmp_path / "does_not_exist.mp4")
    with pytest.raises(FileNotFoundError, match="MediaSource path"):
        render_cut(doc, tmp_path / "out.mp4")


def test_render_cut_full_coverage_copies_source(tmp_path: Path):
    """v2 equivalent of the v1 'empty cuts copies source' test: when the
    timeline covers the full source duration with no gaps, render_cut
    short-circuits to ``shutil.copy2`` — no transcoding, no smartcut."""
    src = tmp_path / "src.mp4"
    src.write_bytes(b"the original media bytes")
    out = tmp_path / "out.mp4"
    progress: list[float] = []
    result = render_cut(_doc(src), out, on_progress=progress.append)
    assert result == out
    assert out.read_bytes() == b"the original media bytes"
    assert progress == [1.0]


def test_render_cut_empty_ranges_raises(tmp_path: Path):
    """v2: empty ranges means 'nothing kept' — render_cut must error
    rather than silently copying the source or producing a zero-length file."""
    src = tmp_path / "src.mp4"
    src.write_bytes(b"placeholder")
    doc = _doc(src, ranges=[])
    out = tmp_path / "out.mp4"
    with pytest.raises(ValueError, match="no ranges"):
        render_cut(doc, out)
    assert not out.exists()


def test_render_cut_full_duration_cut_raises(tmp_path: Path):
    """A cut that spans the entire source produces empty ranges → error."""
    src = tmp_path / "src.mp4"
    src.write_bytes(b"placeholder")
    doc = _doc(src, cuts=[(0.0, 30.0)])
    out = tmp_path / "out.mp4"
    with pytest.raises(ValueError, match="no ranges"):
        render_cut(doc, out)
    assert not out.exists()


# ---------------------------------------------------------------------------
# render_cut — slow tests using the synthetic video fixture
# ---------------------------------------------------------------------------


def _doc_for_video(
    path: Path,
    *,
    cuts: list[tuple[float, float]] | None = None,
    ranges: list[Range] | None = None,
) -> Document:
    return _doc(path, cuts=cuts, ranges=ranges, duration=30.0)


@pytest.mark.slow
def test_render_cut_full_coverage_yields_same_duration(
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
    doc = _doc_for_video(synthetic_video, cuts=[(12.0, 18.0)])
    out = tmp_path / "out.mp4"
    progress: list[float] = []
    render_cut(doc, out, on_progress=progress.append)
    assert is_playable(out)
    src_dur = probe_duration(synthetic_video)
    out_dur = probe_duration(out)
    assert out_dur == pytest.approx(src_dur - 5.8, abs=0.5)
    assert progress, "progress callback must fire at least once"
    assert progress[-1] == 1.0
    assert progress == sorted(progress)


@pytest.mark.slow
def test_render_cut_at_start(
    synthetic_video: Path, probe_duration, is_playable, tmp_path: Path
):
    doc = _doc_for_video(synthetic_video, cuts=[(0.0, 10.0)])
    out = tmp_path / "out.mp4"
    render_cut(doc, out)
    assert is_playable(out)
    src_dur = probe_duration(synthetic_video)
    assert probe_duration(out) == pytest.approx(src_dur - 10.0, abs=0.5)


@pytest.mark.slow
def test_render_cut_at_end(
    synthetic_video: Path, probe_duration, is_playable, tmp_path: Path
):
    doc = _doc_for_video(synthetic_video, cuts=[(20.0, 30.0)])
    out = tmp_path / "out.mp4"
    render_cut(doc, out)
    assert is_playable(out)
    assert probe_duration(out) == pytest.approx(20.0, abs=0.5)


@pytest.mark.slow
def test_render_cut_progress_reaches_one_and_is_monotonic(
    synthetic_video: Path, tmp_path: Path
):
    doc = _doc_for_video(synthetic_video, cuts=[(10.0, 20.0)])
    out = tmp_path / "out.mp4"
    progress: list[float] = []
    render_cut(doc, out, on_progress=progress.append)
    assert progress
    assert progress[-1] == 1.0
    assert all(0.0 <= p <= 1.0 for p in progress)
    assert progress == sorted(progress)


# ---------------------------------------------------------------------------
# Phase 4f-1 — pad_lead / pad_trail asymmetric pad
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_render_cut_asymmetric_pad(
    synthetic_video: Path, probe_duration, is_playable, tmp_path: Path
):
    doc = _doc_for_video(synthetic_video, cuts=[(12.0, 13.0)])
    out = tmp_path / "out.mp4"
    render_cut(doc, out, pad_lead=0.05, pad_trail=0.20, audio_fade_ms=0)
    assert is_playable(out)
    src_dur = probe_duration(synthetic_video)
    expected = src_dur - 1.0 + 0.05 + 0.20
    assert probe_duration(out) == pytest.approx(expected, abs=0.5)


@pytest.mark.slow
def test_render_cut_pad_kwarg_is_deprecated_but_works(
    synthetic_video: Path, probe_duration, is_playable, tmp_path: Path
):
    doc = _doc_for_video(synthetic_video, cuts=[(12.0, 18.0)])
    out = tmp_path / "out.mp4"
    with pytest.warns(DeprecationWarning, match="pad_lead"):
        render_cut(doc, out, pad=0.10, audio_fade_ms=0)
    assert is_playable(out)
    src_dur = probe_duration(synthetic_video)
    assert probe_duration(out) == pytest.approx(src_dur - 5.8, abs=0.5)


# ---------------------------------------------------------------------------
# Phase 4f-1 — _join_times_in_output
# ---------------------------------------------------------------------------


def test_join_times_single_keep_has_no_joins():
    assert _join_times_in_output([(0.0, 30.0)]) == []


def test_join_times_two_keeps_one_join():
    assert _join_times_in_output([(0.0, 10.0), (15.0, 30.0)]) == [10.0]


def test_join_times_three_keeps_two_joins():
    out = _join_times_in_output([(0.0, 5.0), (10.0, 12.0), (20.0, 30.0)])
    assert out == [5.0, 7.0]


# ---------------------------------------------------------------------------
# Phase 4f-1 — audio fade envelope (slow)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_render_cut_audio_fade_attenuates_envelope_around_join(
    synthetic_video: Path, tmp_path: Path
):
    """A 30ms fade on either side of an internal join must produce a V-shaped
    amplitude envelope around the join."""
    import av
    import numpy as np

    doc = _doc_for_video(synthetic_video, cuts=[(2.5, 3.5)])
    out = tmp_path / "out.mp4"
    render_cut(doc, out, pad_lead=0.0, pad_trail=0.0, audio_fade_ms=30)

    join_t = 2.5
    samples_l: list[np.ndarray] = []
    sample_rate: int | None = None
    with av.open(str(out)) as container:
        a_stream = container.streams.audio[0]
        sample_rate = int(a_stream.rate)
        for frame in container.decode(audio=0):
            arr = frame.to_ndarray()
            if arr.ndim == 2:
                arr = arr.mean(axis=0)
            samples_l.append(arr.astype(np.float32))
    assert sample_rate is not None
    samples = np.concatenate(samples_l)

    def envelope(t_start: float, t_end: float) -> float:
        i = int(round(t_start * sample_rate))
        j = int(round(t_end * sample_rate))
        i = max(0, min(len(samples), i))
        j = max(0, min(len(samples), j))
        if j <= i:
            return 0.0
        return float(np.max(np.abs(samples[i:j])))

    ref = envelope(join_t - 0.500, join_t - 0.450)
    assert ref > 0.05, f"reference envelope unexpectedly low: {ref}"

    near_join = envelope(join_t - 0.002, join_t + 0.002)
    assert near_join < ref * 0.20, (
        f"near-join envelope must be heavily attenuated: "
        f"near_join={near_join}, ref={ref}"
    )

    pre_outside = envelope(join_t - 0.060, join_t - 0.040)
    post_outside = envelope(join_t + 0.040, join_t + 0.060)
    assert pre_outside > ref * 0.5, (
        f"signal outside fade window (pre) should be near reference"
    )
    assert post_outside > ref * 0.5, (
        f"signal outside fade window (post) should be near reference"
    )

    pre_far = envelope(join_t - 0.025, join_t - 0.020)
    pre_near = envelope(join_t - 0.005, join_t - 0.000)
    assert pre_far > pre_near
    post_near = envelope(join_t + 0.000, join_t + 0.005)
    post_far = envelope(join_t + 0.020, join_t + 0.025)
    assert post_far > post_near


# ---------------------------------------------------------------------------
# _snap_ranges_to_word_boundaries (Phase 4f-1's snap recast for v2 keep-ranges)
# ---------------------------------------------------------------------------


def _seg_with_words(words: list[tuple[float, float]]) -> Segment:
    """Build a Segment whose words have the given (start, end) intervals."""
    word_objs = tuple(
        Word(text=f"w{i}", start=s, end=e) for i, (s, e) in enumerate(words)
    )
    if not word_objs:
        return Segment(text="", start=0.0, end=0.0, words=())
    return Segment(
        text=" ".join(w.text for w in word_objs),
        start=word_objs[0].start,
        end=word_objs[-1].end,
        words=word_objs,
    )


def test_snap_keep_range_outward_to_word_boundaries():
    """Keep-range start/end snap OUTWARD to nearest word boundary —
    preserve more (start ≤ value, end ≥ value)."""
    seg = _seg_with_words([(1.0, 1.3), (1.5, 1.9), (2.1, 2.5)])
    # Range (1.15, 1.95). Outward snap:
    #   start → max word start ≤ 1.15 → 1.0
    #   end   → min word end   ≥ 1.95 → 2.5  (1.9 is < 1.95 so it doesn't qualify)
    ranges = [Range(source_id="src0", start=1.15, end=1.95)]
    out = _snap_ranges_to_word_boundaries(ranges, [seg])
    assert out == [Range(source_id="src0", start=1.0, end=2.5)]


def test_snap_keep_range_in_pure_silence_passes_through():
    """A range that doesn't overlap any word's [start, end] interval is
    left untouched."""
    seg = _seg_with_words([(1.0, 1.3), (1.5, 1.9), (2.1, 2.5)])
    ranges = [Range(source_id="src0", start=1.95, end=2.05)]
    out = _snap_ranges_to_word_boundaries(ranges, [seg])
    assert out == ranges


def test_snap_preserves_reason():
    seg = _seg_with_words([(1.0, 1.3), (1.5, 1.9)])
    ranges = [Range(source_id="src0", start=1.15, end=1.45, reason="kept")]
    out = _snap_ranges_to_word_boundaries(ranges, [seg])
    assert out[0].reason == "kept"


def test_snap_empty_words_leaves_ranges_unchanged():
    seg = Segment(text="x", start=0.0, end=10.0, words=())
    ranges = [Range(source_id="src0", start=2.0, end=4.0)]
    out = _snap_ranges_to_word_boundaries(ranges, [seg])
    assert out == ranges


def test_resolve_snaps_ranges_outward(tmp_path: Path):
    """End-to-end: a sub-word range handed to _resolve_keep_ranges is
    snapped outward to word boundaries before padding."""
    media = tmp_path / "x.mp4"
    media.write_bytes(b"fake")
    seg = _seg_with_words([(1.0, 1.3), (1.5, 1.9), (2.1, 2.5)])
    ranges = [Range(source_id="src0", start=1.15, end=1.95)]
    doc = Document(
        sources={"src0": MediaSource(id="src0", path=media, duration=5.0)},
        segments=[seg],
        ranges=ranges,
        language="en",
        created_at=datetime(2026, 4, 27, tzinfo=UTC),
        model_name="tiny",
    )
    keeps = _resolve_keep_ranges(
        doc, pad_lead=0.0, pad_trail=0.0, merge_gap=0.0
    )
    # Snap → range becomes (1.0, 2.5) — start to earlier word start, end to
    # later word end (the next word end ≥ 1.95 is 2.5).
    assert keeps == [(1.0, 2.5)]
