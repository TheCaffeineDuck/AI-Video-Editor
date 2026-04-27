"""Tests for ``core.timeline`` — subtract_interval and union_interval.

The two helpers are the load-bearing primitives every v2 EditCommand
reduces to and that the v1→v2 migration uses to derive ranges from
legacy cuts. Bugs here would propagate everywhere; the tests below
exhaustively cover the cases the spec calls out plus the input-validation
preconditions.
"""

from __future__ import annotations

import pytest

from core.document import Range
from core.timeline import subtract_interval, union_interval


def _r(start: float, end: float, *, reason: str = "", source_id: str = "src0") -> Range:
    return Range(source_id=source_id, start=start, end=end, reason=reason)


# ---------------------------------------------------------------------------
# subtract_interval
# ---------------------------------------------------------------------------


def test_subtract_from_empty_list_returns_empty():
    assert subtract_interval([], (1.0, 2.0), "src0") == []


def test_subtract_non_overlapping_interval_leaves_ranges_unchanged():
    ranges = [_r(0.0, 5.0), _r(10.0, 15.0)]
    out = subtract_interval(ranges, (6.0, 9.0), "src0")
    assert out == ranges


def test_subtract_interval_exactly_matching_range_drops_it():
    ranges = [_r(5.0, 10.0)]
    assert subtract_interval(ranges, (5.0, 10.0), "src0") == []


def test_subtract_interval_strictly_inside_range_splits_it():
    ranges = [_r(0.0, 10.0)]
    out = subtract_interval(ranges, (3.0, 5.0), "src0")
    assert out == [_r(0.0, 3.0), _r(5.0, 10.0)]


def test_subtract_interval_truncates_start():
    ranges = [_r(0.0, 10.0)]
    out = subtract_interval(ranges, (-1.0, 4.0), "src0")
    assert out == [_r(4.0, 10.0)]


def test_subtract_interval_truncates_end():
    ranges = [_r(0.0, 10.0)]
    out = subtract_interval(ranges, (7.0, 100.0), "src0")
    assert out == [_r(0.0, 7.0)]


def test_subtract_truncates_one_and_removes_another():
    """Cut spans the second half of range A and all of range B."""
    ranges = [_r(0.0, 10.0), _r(20.0, 30.0)]
    out = subtract_interval(ranges, (5.0, 30.0), "src0")
    assert out == [_r(0.0, 5.0)]


def test_subtract_spans_multiple_ranges_with_partial_overlaps():
    ranges = [_r(0.0, 5.0), _r(10.0, 15.0), _r(20.0, 25.0)]
    # Cut covers 3..22 → A truncated to (0,3), B fully removed, C truncated to (22,25).
    out = subtract_interval(ranges, (3.0, 22.0), "src0")
    assert out == [_r(0.0, 3.0), _r(22.0, 25.0)]


def test_subtract_preserves_reason_on_truncation():
    ranges = [_r(0.0, 10.0, reason="kept")]
    out = subtract_interval(ranges, (7.0, 10.0), "src0")
    assert out == [_r(0.0, 7.0, reason="kept")]


def test_subtract_preserves_reason_on_split():
    ranges = [_r(0.0, 10.0, reason="kept")]
    out = subtract_interval(ranges, (3.0, 5.0), "src0")
    assert all(r.reason == "kept" for r in out)


def test_subtract_does_not_mutate_input():
    ranges = [_r(0.0, 10.0), _r(20.0, 30.0)]
    snapshot = list(ranges)
    subtract_interval(ranges, (5.0, 25.0), "src0")
    assert ranges == snapshot
    assert ranges is not snapshot or ranges == snapshot  # explicit copy contract


def test_subtract_zero_width_interval_is_noop():
    ranges = [_r(0.0, 10.0)]
    out = subtract_interval(ranges, (5.0, 5.0), "src0")
    assert out == ranges
    assert out is not ranges  # always a new list


def test_subtract_inverted_interval_raises():
    with pytest.raises(ValueError, match="end .* < start"):
        subtract_interval([], (5.0, 3.0), "src0")


def test_subtract_mismatched_source_id_raises():
    ranges = [_r(0.0, 5.0, source_id="src1")]
    with pytest.raises(ValueError, match="source_id"):
        subtract_interval(ranges, (1.0, 2.0), "src0")


def test_subtract_returns_new_list_object_each_call():
    """Even no-op calls return a fresh list — callers can mutate the result."""
    ranges = [_r(0.0, 5.0)]
    out1 = subtract_interval(ranges, (10.0, 20.0), "src0")
    out2 = subtract_interval(ranges, (10.0, 20.0), "src0")
    assert out1 is not out2
    assert out1 is not ranges


# ---------------------------------------------------------------------------
# union_interval
# ---------------------------------------------------------------------------


def test_union_into_empty_list_returns_single_range():
    out = union_interval([], (3.0, 7.0), "src0")
    assert out == [_r(3.0, 7.0)]


def test_union_non_overlapping_interval_inserts_in_sorted_order():
    ranges = [_r(0.0, 5.0), _r(20.0, 25.0)]
    out = union_interval(ranges, (10.0, 15.0), "src0")
    assert out == [_r(0.0, 5.0), _r(10.0, 15.0), _r(20.0, 25.0)]


def test_union_touching_existing_range_merges_with_no_gap():
    """Adjacent ranges (one ends exactly where the other starts) merge."""
    ranges = [_r(0.0, 5.0, reason="prev")]
    out = union_interval(ranges, (5.0, 10.0), "src0")
    assert out == [_r(0.0, 10.0, reason="prev")]


def test_union_touching_on_other_side_merges():
    ranges = [_r(5.0, 10.0, reason="next")]
    out = union_interval(ranges, (0.0, 5.0), "src0")
    assert out == [_r(0.0, 10.0, reason="next")]


def test_union_overlapping_a_single_range_merges():
    ranges = [_r(0.0, 5.0, reason="kept")]
    out = union_interval(ranges, (3.0, 8.0), "src0")
    assert out == [_r(0.0, 8.0, reason="kept")]


def test_union_spanning_multiple_ranges_collapses_them():
    ranges = [_r(0.0, 2.0, reason="a"), _r(4.0, 6.0, reason="b"), _r(8.0, 10.0, reason="c")]
    out = union_interval(ranges, (1.0, 9.0), "src0")
    assert out == [_r(0.0, 10.0, reason="a")]  # leftmost reason wins


def test_union_interval_already_covered_returns_equivalent_list():
    ranges = [_r(0.0, 10.0, reason="big")]
    out = union_interval(ranges, (3.0, 7.0), "src0")
    assert out == [_r(0.0, 10.0, reason="big")]


def test_union_interval_extending_past_an_outer_range_keeps_outer_reason():
    """Even if the new interval extends the merged range, the leftmost
    existing range's reason wins."""
    ranges = [_r(0.0, 5.0, reason="outer")]
    out = union_interval(ranges, (3.0, 10.0), "src0")
    assert out == [_r(0.0, 10.0, reason="outer")]


def test_union_zero_width_interval_is_noop():
    ranges = [_r(0.0, 5.0)]
    out = union_interval(ranges, (3.0, 3.0), "src0")
    assert out == ranges
    assert out is not ranges


def test_union_into_empty_with_no_overlap_yields_empty_reason():
    """When there's no existing range to inherit from, reason is empty."""
    out = union_interval([], (1.0, 2.0), "src0")
    assert out[0].reason == ""


def test_union_inverted_interval_raises():
    with pytest.raises(ValueError, match="end .* < start"):
        union_interval([], (5.0, 3.0), "src0")


def test_union_mismatched_source_id_raises():
    ranges = [_r(0.0, 5.0, source_id="src1")]
    with pytest.raises(ValueError, match="source_id"):
        union_interval(ranges, (1.0, 2.0), "src0")


def test_union_does_not_mutate_input():
    ranges = [_r(0.0, 5.0), _r(10.0, 15.0)]
    snapshot = [_r(r.start, r.end, reason=r.reason) for r in ranges]
    union_interval(ranges, (4.0, 11.0), "src0")
    assert ranges == snapshot


def test_union_returns_sorted_output():
    ranges = [_r(0.0, 2.0), _r(20.0, 30.0)]
    out = union_interval(ranges, (5.0, 10.0), "src0")
    assert out == sorted(out, key=lambda r: r.start)
