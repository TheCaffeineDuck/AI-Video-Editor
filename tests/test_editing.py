"""Tests for core.editing — edit commands, immutability, command stack."""

from __future__ import annotations

import dataclasses
from datetime import datetime
from pathlib import Path

import pytest

from core.document import CutMark, Document, Segment, Word
from core.editing import (
    AddCut,
    CommandStack,
    CutWordRange,
    EditCommand,
    MergeAdjacentCuts,
    RemoveCut,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _doc(
    *,
    segments: list[Segment] | None = None,
    cuts: list[CutMark] | None = None,
) -> Document:
    """A minimal Document for command tests."""
    if segments is None:
        segments = [
            Segment(
                text="hello world foo bar baz",
                start=0.0,
                end=5.0,
                words=(
                    Word("hello", 0.0, 1.0, 0.95),
                    Word("world", 1.0, 2.0, 0.92),
                    Word("foo", 2.0, 3.0, 0.85),
                    Word("bar", 3.0, 4.0, 0.80),
                    Word("baz", 4.0, 5.0, 0.78),
                ),
            ),
        ]
    return Document(
        media_path=Path("/tmp/test.wav"),
        duration=5.0,
        language="en",
        segments=segments,
        cuts=list(cuts) if cuts is not None else [],
        created_at=datetime(2026, 4, 26, 10, 0, 0),
        model_name="tiny",
    )


# ---------------------------------------------------------------------------
# Document immutability — Phase 4c contract
# ---------------------------------------------------------------------------


def test_document_is_frozen():
    """Reassigning a frozen Document field must raise."""
    d = _doc()
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.cuts = []  # type: ignore[misc]


def test_document_segments_is_frozen():
    d = _doc()
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.segments = []  # type: ignore[misc]


def test_apply_does_not_mutate_original_cuts_list():
    """The original Document's cuts list is never the same object as the new one's."""
    d1 = _doc(cuts=[CutMark(0.0, 0.5, "x")])
    cmd = AddCut(start=2.0, end=2.5, reason="y")
    d2 = cmd.apply(d1)
    assert d1.cuts is not d2.cuts
    # And mutating d2.cuts must not affect d1.cuts.
    d2.cuts.append(CutMark(99.0, 100.0))
    assert len(d1.cuts) == 1
    assert d1.cuts[0] == CutMark(0.0, 0.5, "x")


def test_remove_cut_does_not_mutate_original():
    d1 = _doc(cuts=[CutMark(0.0, 0.5), CutMark(1.0, 1.5)])
    d2 = RemoveCut(index=0).apply(d1)
    assert d1.cuts is not d2.cuts
    assert len(d1.cuts) == 2  # unchanged
    assert len(d2.cuts) == 1


# ---------------------------------------------------------------------------
# AddCut
# ---------------------------------------------------------------------------


def test_add_cut_appends():
    d1 = _doc()
    d2 = AddCut(start=1.0, end=2.0, reason="filler").apply(d1)
    assert len(d2.cuts) == 1
    assert d2.cuts[0] == CutMark(start=1.0, end=2.0, reason="filler")


def test_add_cut_default_reason_is_manual():
    d2 = AddCut(start=0.0, end=1.0).apply(_doc())
    assert d2.cuts[0].reason == "manual"


def test_add_cut_round_trip():
    d1 = _doc(cuts=[CutMark(0.0, 0.5, "existing")])
    cmd = AddCut(start=2.0, end=3.0, reason="filler")
    d2 = cmd.apply(d1)
    d3 = cmd.revert(d2)
    assert d3 == d1


def test_add_cut_revert_when_target_missing_raises():
    cmd = AddCut(start=10.0, end=11.0, reason="manual")
    with pytest.raises(ValueError, match="no matching cut"):
        cmd.revert(_doc())  # never applied — there's nothing to remove


def test_add_cut_description():
    cmd = AddCut(start=1.234, end=2.345, reason="manual")
    assert "1.23" in cmd.description and "2.35" in cmd.description


# ---------------------------------------------------------------------------
# RemoveCut
# ---------------------------------------------------------------------------


def test_remove_cut_removes_at_index():
    d1 = _doc(cuts=[
        CutMark(0.0, 0.5, "a"),
        CutMark(1.0, 1.5, "b"),
        CutMark(2.0, 2.5, "c"),
    ])
    d2 = RemoveCut(index=1).apply(d1)
    assert [c.reason for c in d2.cuts] == ["a", "c"]


def test_remove_cut_invalid_index_raises():
    d1 = _doc(cuts=[CutMark(0.0, 0.5)])
    with pytest.raises(IndexError):
        RemoveCut(index=5).apply(d1)


def test_remove_cut_revert_before_apply_raises():
    cmd = RemoveCut(index=0)
    with pytest.raises(RuntimeError, match="before apply"):
        cmd.revert(_doc())


def test_remove_cut_round_trip_preserves_position():
    d1 = _doc(cuts=[
        CutMark(0.0, 0.5, "a"),
        CutMark(1.0, 1.5, "b"),
        CutMark(2.0, 2.5, "c"),
    ])
    cmd = RemoveCut(index=1)
    d2 = cmd.apply(d1)
    d3 = cmd.revert(d2)
    assert d3 == d1
    # And specifically, "b" went back into the middle, not the end.
    assert [c.reason for c in d3.cuts] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# MergeAdjacentCuts
# ---------------------------------------------------------------------------


def test_merge_adjacent_cuts_three_cuts_partial_merge():
    """A and B merge (gap 0.1s); C is far enough away to stay separate."""
    cuts = [
        CutMark(0.0, 1.0, "a"),
        CutMark(1.1, 2.0, "b"),    # gap 0.1s — merges with A
        CutMark(5.0, 6.0, "c"),    # gap 3.0s — stays separate
    ]
    d2 = MergeAdjacentCuts(threshold_seconds=0.30).apply(_doc(cuts=cuts))
    assert len(d2.cuts) == 2
    assert d2.cuts[0] == CutMark(0.0, 2.0, "a")  # A's reason wins
    assert d2.cuts[1] == CutMark(5.0, 6.0, "c")


def test_merge_is_order_independent():
    forward = [
        CutMark(0.0, 1.0, "a"),
        CutMark(1.1, 2.0, "b"),
        CutMark(5.0, 6.0, "c"),
    ]
    reversed_input = list(reversed(forward))
    out_a = MergeAdjacentCuts(0.30).apply(_doc(cuts=forward)).cuts
    out_b = MergeAdjacentCuts(0.30).apply(_doc(cuts=reversed_input)).cuts
    assert out_a == out_b


def test_merge_overlapping_cuts_always_merge_regardless_of_threshold():
    """Overlap (B.start < A.end) must merge even when threshold is 0."""
    cuts = [
        CutMark(0.0, 2.0, "a"),
        CutMark(1.0, 3.0, "b"),  # overlaps A
    ]
    d2 = MergeAdjacentCuts(threshold_seconds=0.0).apply(_doc(cuts=cuts))
    assert len(d2.cuts) == 1
    assert d2.cuts[0] == CutMark(0.0, 3.0, "a")


def test_merge_overlap_keeps_max_end():
    """If B is wholly inside A, merged.end stays at A.end (not B.end)."""
    cuts = [
        CutMark(0.0, 5.0, "outer"),
        CutMark(1.0, 2.0, "inner"),  # wholly inside outer
    ]
    d2 = MergeAdjacentCuts(threshold_seconds=0.0).apply(_doc(cuts=cuts))
    assert len(d2.cuts) == 1
    assert d2.cuts[0] == CutMark(0.0, 5.0, "outer")


def test_merge_threshold_zero_only_merges_exact_touch():
    """gap == 0 (touching) merges; gap == 0.001 doesn't."""
    touching = MergeAdjacentCuts(0.0).apply(_doc(cuts=[
        CutMark(0.0, 1.0, "a"),
        CutMark(1.0, 2.0, "b"),  # touches A exactly
    ])).cuts
    assert len(touching) == 1

    not_touching = MergeAdjacentCuts(0.0).apply(_doc(cuts=[
        CutMark(0.0, 1.0, "a"),
        CutMark(1.001, 2.0, "b"),
    ])).cuts
    assert len(not_touching) == 2


def test_merge_round_trip_preserves_original_cuts():
    cuts = [
        CutMark(0.0, 1.0, "a"),
        CutMark(1.1, 2.0, "b"),
        CutMark(5.0, 6.0, "c"),
    ]
    d1 = _doc(cuts=cuts)
    cmd = MergeAdjacentCuts(threshold_seconds=0.30)
    d2 = cmd.apply(d1)
    d3 = cmd.revert(d2)
    assert d3 == d1
    # Order preserved (we captured the original list, not a sorted view).
    assert d3.cuts == cuts


def test_merge_revert_before_apply_raises():
    with pytest.raises(RuntimeError, match="before apply"):
        MergeAdjacentCuts(0.5).revert(_doc())


def test_merge_empty_cuts_is_noop():
    d1 = _doc(cuts=[])
    d2 = MergeAdjacentCuts(1.0).apply(d1)
    assert d2.cuts == []


# ---------------------------------------------------------------------------
# CutWordRange
# ---------------------------------------------------------------------------


def test_cut_word_range_single_word():
    """start == end picks exactly one word."""
    d2 = CutWordRange(seg_idx=0, word_start_idx=2, word_end_idx=2).apply(_doc())
    assert len(d2.cuts) == 1
    # Word "foo" at 2.0-3.0
    assert d2.cuts[0].start == pytest.approx(2.0)
    assert d2.cuts[0].end == pytest.approx(3.0)


def test_cut_word_range_first_word():
    d2 = CutWordRange(seg_idx=0, word_start_idx=0, word_end_idx=0).apply(_doc())
    assert d2.cuts[0].start == pytest.approx(0.0)
    assert d2.cuts[0].end == pytest.approx(1.0)


def test_cut_word_range_last_word():
    d2 = CutWordRange(seg_idx=0, word_start_idx=4, word_end_idx=4).apply(_doc())
    assert d2.cuts[0].start == pytest.approx(4.0)
    assert d2.cuts[0].end == pytest.approx(5.0)


def test_cut_word_range_all_words():
    d2 = CutWordRange(seg_idx=0, word_start_idx=0, word_end_idx=4).apply(_doc())
    assert d2.cuts[0].start == pytest.approx(0.0)
    assert d2.cuts[0].end == pytest.approx(5.0)


def test_cut_word_range_inclusive_endpoints():
    """words 1..3 must span words 1, 2, AND 3 (inclusive)."""
    d2 = CutWordRange(seg_idx=0, word_start_idx=1, word_end_idx=3).apply(_doc())
    # Spans "world" (1-2), "foo" (2-3), "bar" (3-4) → 1.0..4.0.
    assert d2.cuts[0].start == pytest.approx(1.0)
    assert d2.cuts[0].end == pytest.approx(4.0)


def test_cut_word_range_invalid_seg_idx_raises():
    with pytest.raises(ValueError, match="seg_idx"):
        CutWordRange(seg_idx=5, word_start_idx=0, word_end_idx=0).apply(_doc())


def test_cut_word_range_word_idx_out_of_range_raises():
    with pytest.raises(ValueError, match="word range"):
        CutWordRange(seg_idx=0, word_start_idx=0, word_end_idx=99).apply(_doc())


def test_cut_word_range_inverted_range_raises():
    """end < start is invalid."""
    with pytest.raises(ValueError, match="word range"):
        CutWordRange(seg_idx=0, word_start_idx=3, word_end_idx=1).apply(_doc())


def test_cut_word_range_segment_without_words_raises():
    seg = Segment(text="hi", start=0.0, end=1.0, words=())
    d = _doc(segments=[seg])
    with pytest.raises(ValueError, match="no word-level"):
        CutWordRange(seg_idx=0, word_start_idx=0, word_end_idx=0).apply(d)


def test_cut_word_range_round_trip():
    d1 = _doc()
    cmd = CutWordRange(seg_idx=0, word_start_idx=1, word_end_idx=3, reason="filler")
    d2 = cmd.apply(d1)
    d3 = cmd.revert(d2)
    assert d3 == d1


# ---------------------------------------------------------------------------
# EditCommand Protocol — runtime conformance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cmd", [
    AddCut(start=0.0, end=1.0),
    RemoveCut(index=0),
    MergeAdjacentCuts(threshold_seconds=0.5),
    CutWordRange(seg_idx=0, word_start_idx=0, word_end_idx=0),
])
def test_command_satisfies_protocol(cmd):
    assert isinstance(cmd, EditCommand)
    assert isinstance(cmd.description, str)
    assert cmd.description  # non-empty


# ---------------------------------------------------------------------------
# CommandStack
# ---------------------------------------------------------------------------


def _push(stack: CommandStack, cmd: EditCommand, before: Document) -> Document:
    """Helper: apply, push, return the new doc."""
    after = cmd.apply(before)
    stack.push(cmd, before, after)
    return after


def test_stack_starts_empty():
    s = CommandStack()
    assert not s.can_undo
    assert not s.can_redo
    assert s.undo() is None
    assert s.redo() is None


def test_stack_push_then_undo():
    s = CommandStack()
    d0 = _doc()
    _push(s, AddCut(0.0, 1.0), d0)
    assert s.can_undo and not s.can_redo
    assert s.undo() == d0
    assert not s.can_undo and s.can_redo


def test_stack_undo_then_redo():
    s = CommandStack()
    d0 = _doc()
    d1 = _push(s, AddCut(0.0, 1.0), d0)
    assert s.undo() == d0
    assert s.redo() == d1
    assert s.can_undo and not s.can_redo


def test_stack_two_pushes_undo_undo_redo():
    s = CommandStack()
    d0 = _doc()
    d1 = _push(s, AddCut(0.0, 1.0, "a"), d0)
    d2 = _push(s, AddCut(2.0, 3.0, "b"), d1)
    assert s.undo() == d1
    assert s.undo() == d0
    assert s.redo() == d1
    assert s.redo() == d2
    assert not s.can_redo


def test_stack_fork_discards_redo_branch():
    """User does A→B→C, undoes back to A, then does D — B and C are gone forever."""
    s = CommandStack()
    d0 = _doc()
    d1 = _push(s, AddCut(0.0, 1.0, "A"), d0)
    d2 = _push(s, AddCut(1.0, 2.0, "B"), d1)
    _push(s, AddCut(2.0, 3.0, "C"), d2)
    # Undo back to d1 (after A, before B).
    assert s.undo() == d2
    assert s.undo() == d1
    assert s.can_redo
    # New command D pushed — redo branch must be wiped.
    _push(s, AddCut(10.0, 11.0, "D"), d1)
    assert not s.can_redo, "fork must discard redo stack"
    # And we cannot redo our way to B or C — only undo back to d1, then d0.
    assert s.undo() == d1
    assert s.undo() == d0
    assert s.undo() is None


def test_stack_max_depth_enforced():
    s = CommandStack(max_depth=3)
    d0 = _doc()
    docs = [d0]
    for i in range(5):
        new = _push(s, AddCut(float(i), float(i) + 0.5, f"c{i}"), docs[-1])
        docs.append(new)
    # Only the last 3 entries are kept; we can undo 3 times max.
    assert s.undo_depth == 3
    assert s.undo() == docs[4]  # transitions back from d5 to d4
    assert s.undo() == docs[3]
    assert s.undo() == docs[2]
    assert s.undo() is None  # the older two transitions were dropped


def test_stack_max_depth_must_be_positive():
    with pytest.raises(ValueError, match="max_depth"):
        CommandStack(max_depth=0)


def test_stack_default_max_depth_is_100():
    assert CommandStack().max_depth == 100


def test_stack_clear_drops_both_branches():
    s = CommandStack()
    d0 = _doc()
    _push(s, AddCut(0.0, 1.0), d0)
    s.undo()
    assert s.can_redo
    s.clear()
    assert not s.can_undo and not s.can_redo


def test_stack_undo_redo_works_with_mixed_commands():
    """A realistic sequence: AddCut, RemoveCut, MergeAdjacentCuts."""
    s = CommandStack()
    d0 = _doc()
    add1 = AddCut(0.0, 1.0, "a")
    add2 = AddCut(1.1, 2.0, "b")
    merge = MergeAdjacentCuts(0.2)

    d1 = _push(s, add1, d0)
    d2 = _push(s, add2, d1)
    d3 = _push(s, merge, d2)
    assert len(d3.cuts) == 1  # merged

    assert s.undo() == d2  # un-merged
    assert s.undo() == d1  # un-added second
    assert s.undo() == d0  # back to start
    assert s.redo() == d1
    assert s.redo() == d2
    assert s.redo() == d3
    assert not s.can_redo
