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

from core.document import CutMark, Document, Segment, Word
from core.render import (
    _invert_cuts_to_keep_ranges,
    _join_times_in_output,
    _pad_and_merge_keep_ranges,
    _ProgressAdapter,
    _resolve_keep_ranges,
    _snap_cuts_to_word_boundaries,
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
    """After padding, two adjacent keep-ranges overlap → merge them."""
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
    """pad_lead=0.05 widens start by 0.05; pad_trail=0.20 widens end by 0.20."""
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
# _resolve_keep_ranges — end-to-end of the helper pipeline
# ---------------------------------------------------------------------------


def test_resolve_no_cuts_yields_full_range(tmp_path: Path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"fake")
    doc = _doc(media)
    assert _resolve_keep_ranges(
        doc, pad_lead=0.1, pad_trail=0.1, merge_gap=0.3
    ) == [(0.0, 30.0)]


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
    out = _resolve_keep_ranges(
        doc, pad_lead=0.10, pad_trail=0.10, merge_gap=0.30
    )
    assert out == [(0.0, 10.10), (13.90, 30.0)]


def test_resolve_full_duration_cut_yields_empty(tmp_path: Path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"fake")
    doc = _doc(media, cuts=[CutMark(0.0, 30.0)])
    assert _resolve_keep_ranges(
        doc, pad_lead=0.10, pad_trail=0.10, merge_gap=0.30
    ) == []


def test_resolve_handles_unsorted_cuts(tmp_path: Path):
    """Cuts in reverse order produce the same result as forward order."""
    media = tmp_path / "x.mp4"
    media.write_bytes(b"fake")
    cuts = [CutMark(20.0, 25.0), CutMark(5.0, 10.0)]
    doc = _doc(media, cuts=cuts)
    assert _resolve_keep_ranges(
        doc, pad_lead=0.0, pad_trail=0.0, merge_gap=0.0
    ) == [
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


# ---------------------------------------------------------------------------
# Phase 4f-1 — pad_lead / pad_trail asymmetric pad
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_render_cut_asymmetric_pad(
    synthetic_video: Path, probe_duration, is_playable, tmp_path: Path
):
    """pad_lead=0.05, pad_trail=0.20 on a 1s cut [12, 13]: keeps become
    [(0, 12.20), (12.95, 30)] → output ≈ 12.20 + 17.05 = 29.25s, i.e.
    source - cut + pad_lead + pad_trail = 30 - 1 + 0.05 + 0.20."""
    doc = _doc_for_video(synthetic_video, cuts=[CutMark(12.0, 13.0)])
    out = tmp_path / "out.mp4"
    render_cut(
        doc,
        out,
        pad_lead=0.05,
        pad_trail=0.20,
        audio_fade_ms=0,
    )
    assert is_playable(out)
    src_dur = probe_duration(synthetic_video)
    expected = src_dur - 1.0 + 0.05 + 0.20
    assert probe_duration(out) == pytest.approx(expected, abs=0.5)


@pytest.mark.slow
def test_render_cut_pad_kwarg_is_deprecated_but_works(
    synthetic_video: Path, probe_duration, is_playable, tmp_path: Path
):
    """The legacy ``pad=`` kwarg emits DeprecationWarning and matches the
    symmetric pad_lead=pad_trail=pad behavior."""
    doc = _doc_for_video(synthetic_video, cuts=[CutMark(12.0, 18.0)])
    out = tmp_path / "out.mp4"
    with pytest.warns(DeprecationWarning, match="pad_lead"):
        render_cut(doc, out, pad=0.10, audio_fade_ms=0)
    assert is_playable(out)
    src_dur = probe_duration(synthetic_video)
    # Identical to default-pad path: 30 - 6 + 0.20 = 24.20s.
    assert probe_duration(out) == pytest.approx(src_dur - 5.8, abs=0.5)


# ---------------------------------------------------------------------------
# Phase 4f-1 — _join_times_in_output
# ---------------------------------------------------------------------------


def test_join_times_single_keep_has_no_joins():
    assert _join_times_in_output([(0.0, 30.0)]) == []


def test_join_times_two_keeps_one_join():
    """Output time of the join = length of first keep-range."""
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
    amplitude envelope: max-abs samples in the 30ms before the join and the
    30ms after both attenuate toward t_join. Use pad=0 so the join sits at
    a known output time and a continuous-tone (440Hz, 0–10s segment of
    the synthetic video) lets us compare envelopes without conflating the
    fade with a frequency change.
    """
    import av
    import numpy as np

    # Cut [2.5, 3.5] inside the 0–10s 440Hz tone segment. With pad=0 the
    # join sits at output t=2.5 and source 440Hz tone continues either side.
    doc = _doc_for_video(synthetic_video, cuts=[CutMark(2.5, 3.5)])
    out = tmp_path / "out.mp4"
    render_cut(
        doc,
        out,
        pad_lead=0.0,
        pad_trail=0.0,
        audio_fade_ms=30,
    )

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

    # Reference window well outside the fade: full-amplitude 440Hz tone.
    ref = envelope(join_t - 0.500, join_t - 0.450)
    assert ref > 0.05, f"reference envelope unexpectedly low: {ref}"

    # The amplitude in a 4ms window centered on the join must be a small
    # fraction of the reference — linear afade brings gain to 0 at the join.
    near_join = envelope(join_t - 0.002, join_t + 0.002)
    assert near_join < ref * 0.20, (
        f"near-join envelope must be heavily attenuated: "
        f"near_join={near_join}, ref={ref}"
    )

    # Outside the 30ms fade window the signal must be at full amplitude.
    pre_outside = envelope(join_t - 0.060, join_t - 0.040)
    post_outside = envelope(join_t + 0.040, join_t + 0.060)
    assert pre_outside > ref * 0.5, (
        f"signal outside fade window (pre) should be near reference: "
        f"pre_outside={pre_outside}, ref={ref}"
    )
    assert post_outside > ref * 0.5, (
        f"signal outside fade window (post) should be near reference: "
        f"post_outside={post_outside}, ref={ref}"
    )

    # Monotonic-ish attenuation toward the join: a window 20ms before the
    # join is louder than a window 5ms before. Same on the post side.
    pre_far = envelope(join_t - 0.025, join_t - 0.020)
    pre_near = envelope(join_t - 0.005, join_t - 0.000)
    assert pre_far > pre_near, (
        f"pre-join envelope must attenuate toward the join: "
        f"pre_far={pre_far}, pre_near={pre_near}"
    )
    post_near = envelope(join_t + 0.000, join_t + 0.005)
    post_far = envelope(join_t + 0.020, join_t + 0.025)
    assert post_far > post_near, (
        f"post-join envelope must recover away from the join: "
        f"post_near={post_near}, post_far={post_far}"
    )


# ---------------------------------------------------------------------------
# Phase 4f-1 — _snap_cuts_to_word_boundaries
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


def test_snap_mid_word_cut_pulls_to_word_boundaries():
    seg = _seg_with_words([(1.0, 1.3), (1.5, 1.9), (2.1, 2.5)])
    cuts = [CutMark(1.15, 1.95)]
    out = _snap_cuts_to_word_boundaries(cuts, [seg])
    # start 1.15 → nearest word start (1.0); end 1.95 → nearest word end (1.9).
    assert out == [CutMark(1.0, 1.9)]


def test_snap_pure_silence_cut_passes_through():
    """A cut that doesn't overlap any word's [start, end] interval is left
    untouched — there's no word boundary to snap to and the cut is in
    silence between segments."""
    seg = _seg_with_words([(1.0, 1.3), (1.5, 1.9), (2.1, 2.5)])
    cuts = [CutMark(1.95, 2.05)]
    out = _snap_cuts_to_word_boundaries(cuts, [seg])
    assert out == [CutMark(1.95, 2.05)]


def test_snap_preserves_reason():
    seg = _seg_with_words([(1.0, 1.3), (1.5, 1.9)])
    cuts = [CutMark(1.15, 1.45, reason="filler")]
    out = _snap_cuts_to_word_boundaries(cuts, [seg])
    assert out[0].reason == "filler"


def test_snap_empty_words_leaves_cuts_unchanged():
    """No words in any segment → nothing to snap to."""
    seg = Segment(text="x", start=0.0, end=10.0, words=())
    cuts = [CutMark(2.0, 4.0)]
    out = _snap_cuts_to_word_boundaries(cuts, [seg])
    assert out == [CutMark(2.0, 4.0)]


def test_snap_tie_breaks_outward():
    """Cut start equidistant from two word starts → snap to the smaller.
    Cut end equidistant from two word ends → snap to the larger."""
    # Words: starts {0.0, 1.0, 2.0}, ends {0.3, 1.3, 2.3}.
    # Cut start 0.5 is tied between 0.0 and 1.0 (both at distance 0.5);
    # outward → 0.0.
    # Cut end 1.8 is tied between 1.3 and 2.3 (both at distance 0.5);
    # outward → 2.3.
    # The cut must overlap at least one word to be eligible for snapping;
    # [0.5, 1.8] ∩ [1.0, 1.3] is non-empty.
    seg = _seg_with_words([(0.0, 0.3), (1.0, 1.3), (2.0, 2.3)])
    cuts = [CutMark(0.5, 1.8)]
    out = _snap_cuts_to_word_boundaries(cuts, [seg])
    assert out == [CutMark(0.0, 2.3)]


def test_resolve_snaps_before_inverting(tmp_path: Path):
    """End-to-end: a sub-word cut handed to _resolve_keep_ranges is snapped,
    then inverted into keep-ranges that respect word boundaries."""
    media = tmp_path / "x.mp4"
    media.write_bytes(b"fake")
    seg = _seg_with_words([(1.0, 1.3), (1.5, 1.9), (2.1, 2.5)])
    doc = Document(
        media_path=media,
        duration=5.0,
        language="en",
        segments=[seg],
        cuts=[CutMark(1.15, 1.95)],
        created_at=datetime(2026, 4, 27),
        model_name="tiny",
    )
    keeps = _resolve_keep_ranges(
        doc, pad_lead=0.0, pad_trail=0.0, merge_gap=0.0
    )
    # Snap → cut becomes (1.0, 1.9). Invert → keeps [(0.0, 1.0), (1.9, 5.0)].
    assert keeps == [(0.0, 1.0), (1.9, 5.0)]
