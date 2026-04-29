"""Tests for ``core.editing`` — v2 edit commands, immutability, command stack.

Phase 4f-3 rewrote every edit command against the v2 keep-range model:

- ``AddCut`` now subtracts an interval from ``doc.ranges`` instead of
  appending a CutMark.
- ``RestoreRange`` replaces v1's ``RemoveCut(index=…)`` (which indexed
  into the cut list — a list that no longer exists in v2).
- ``MergeAdjacentCuts`` is gone; v2 ranges are canonicalized at every
  edit so the explicit "merge close cuts" command had no remaining job.
- ``CutWordRange`` resolves a word range to ``(start, end)`` and calls
  the same subtract path as ``AddCut``.

All command tests below assert against ``doc.ranges`` rather than
``doc.cuts``.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.document import Document, MediaSource, Range, Segment, Word
from core.editing import (
    AddCut,
    CommandStack,
    CutWordRange,
    EditCommand,
    RestoreRange,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _doc(
    *,
    segments: list[Segment] | None = None,
    ranges: list[Range] | None = None,
    duration: float = 5.0,
) -> Document:
    """A minimal v2 Document for command tests."""
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
    if ranges is None:
        ranges = [Range(source_id="src0", start=0.0, end=duration)]
    return Document(
        sources={"src0": MediaSource(id="src0", path=Path("/tmp/test.wav"), duration=duration)},
        segments=segments,
        ranges=list(ranges),
        language="en",
        created_at=datetime(2026, 4, 26, 10, 0, 0, tzinfo=UTC),
        model_name="tiny",
    )


# ---------------------------------------------------------------------------
# Document immutability
# ---------------------------------------------------------------------------


def test_document_is_frozen():
    d = _doc()
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.ranges = []  # type: ignore[misc]


def test_document_segments_is_frozen():
    d = _doc()
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.segments = []  # type: ignore[misc]


def test_apply_does_not_mutate_original_ranges_list():
    d1 = _doc()
    cmd = AddCut(start=2.0, end=2.5)
    d2 = cmd.apply(d1)
    assert d1.ranges is not d2.ranges
    d2.ranges.append(Range(source_id="src0", start=99.0, end=100.0))
    assert all(r.start < 99.0 for r in d1.ranges)


# ---------------------------------------------------------------------------
# AddCut
# ---------------------------------------------------------------------------


def test_add_cut_subtracts_from_full_range():
    d2 = AddCut(start=1.0, end=2.0).apply(_doc())
    assert d2.ranges == [
        Range(source_id="src0", start=0.0, end=1.0),
        Range(source_id="src0", start=2.0, end=5.0),
    ]


def test_add_cut_round_trip_restores_original_ranges():
    d1 = _doc()
    cmd = AddCut(start=2.0, end=3.0)
    d2 = cmd.apply(d1)
    d3 = cmd.revert(d2)
    assert d3 == d1


def test_add_cut_revert_before_apply_raises():
    with pytest.raises(RuntimeError, match="before apply"):
        AddCut(start=10.0, end=11.0).revert(_doc())


def test_add_cut_default_reason_is_none():
    """Phase 6a: AddCut.reason defaults to None (was 'manual' in v2)."""
    cmd = AddCut(start=0.0, end=1.0)
    assert cmd.reason is None


def test_add_cut_description():
    cmd = AddCut(start=1.234, end=2.345)
    assert "1.23" in cmd.description and "2.35" in cmd.description


def test_add_cut_on_already_partial_timeline():
    """Subtract from a non-trivial starting timeline."""
    d1 = _doc(
        ranges=[
            Range(source_id="src0", start=0.0, end=2.0),
            Range(source_id="src0", start=3.0, end=5.0),
        ]
    )
    d2 = AddCut(start=0.5, end=1.0).apply(d1)
    assert d2.ranges == [
        Range(source_id="src0", start=0.0, end=0.5),
        Range(source_id="src0", start=1.0, end=2.0),
        Range(source_id="src0", start=3.0, end=5.0),
    ]


# ---------------------------------------------------------------------------
# RestoreRange
# ---------------------------------------------------------------------------


def test_restore_range_into_gap_unions_back():
    d1 = _doc(
        ranges=[
            Range(source_id="src0", start=0.0, end=2.0),
            Range(source_id="src0", start=3.0, end=5.0),
        ]
    )
    d2 = RestoreRange(start=2.0, end=3.0).apply(d1)
    # Touching adjacents merge into a single full-duration range.
    assert d2.ranges == [Range(source_id="src0", start=0.0, end=5.0)]


def test_restore_range_round_trip():
    d1 = _doc(
        ranges=[
            Range(source_id="src0", start=0.0, end=2.0),
            Range(source_id="src0", start=3.0, end=5.0),
        ]
    )
    cmd = RestoreRange(start=2.0, end=3.0)
    d2 = cmd.apply(d1)
    d3 = cmd.revert(d2)
    assert d3 == d1


def test_restore_range_revert_before_apply_raises():
    with pytest.raises(RuntimeError, match="before apply"):
        RestoreRange(start=0.0, end=1.0).revert(_doc())


def test_restore_range_description():
    cmd = RestoreRange(start=1.0, end=2.0)
    assert "1.00" in cmd.description and "2.00" in cmd.description


# ---------------------------------------------------------------------------
# CutWordRange
# ---------------------------------------------------------------------------


def test_cut_word_range_single_word():
    d2 = CutWordRange(seg_idx=0, word_start_idx=2, word_end_idx=2).apply(_doc())
    # Word "foo" at 2.0–3.0; subtracted from full range.
    assert d2.ranges == [
        Range(source_id="src0", start=0.0, end=2.0),
        Range(source_id="src0", start=3.0, end=5.0),
    ]


def test_cut_word_range_first_word():
    d2 = CutWordRange(seg_idx=0, word_start_idx=0, word_end_idx=0).apply(_doc())
    assert d2.ranges == [Range(source_id="src0", start=1.0, end=5.0)]


def test_cut_word_range_last_word():
    d2 = CutWordRange(seg_idx=0, word_start_idx=4, word_end_idx=4).apply(_doc())
    assert d2.ranges == [Range(source_id="src0", start=0.0, end=4.0)]


def test_cut_word_range_all_words():
    d2 = CutWordRange(seg_idx=0, word_start_idx=0, word_end_idx=4).apply(_doc())
    # Subtracting (0, 5) from (0, 5) leaves an empty timeline.
    assert d2.ranges == []


def test_cut_word_range_inclusive_endpoints():
    d2 = CutWordRange(seg_idx=0, word_start_idx=1, word_end_idx=3).apply(_doc())
    # Spans words "world" (1–2), "foo" (2–3), "bar" (3–4) → cut (1, 4).
    assert d2.ranges == [
        Range(source_id="src0", start=0.0, end=1.0),
        Range(source_id="src0", start=4.0, end=5.0),
    ]


def test_cut_word_range_invalid_seg_idx_raises():
    with pytest.raises(ValueError, match="seg_idx"):
        CutWordRange(seg_idx=5, word_start_idx=0, word_end_idx=0).apply(_doc())


def test_cut_word_range_word_idx_out_of_range_raises():
    with pytest.raises(ValueError, match="word range"):
        CutWordRange(seg_idx=0, word_start_idx=0, word_end_idx=99).apply(_doc())


def test_cut_word_range_inverted_range_raises():
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


@pytest.mark.parametrize(
    "cmd",
    [
        AddCut(start=0.0, end=1.0),
        RestoreRange(start=0.0, end=1.0),
        CutWordRange(seg_idx=0, word_start_idx=0, word_end_idx=0),
    ],
)
def test_command_satisfies_protocol(cmd):
    assert isinstance(cmd, EditCommand)
    assert isinstance(cmd.description, str)
    assert cmd.description


# ---------------------------------------------------------------------------
# CommandStack
# ---------------------------------------------------------------------------


def _push(stack: CommandStack, cmd: EditCommand, before: Document) -> Document:
    """Apply, push, return the new doc."""
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
    d1 = _push(s, AddCut(0.0, 0.5), d0)
    d2 = _push(s, AddCut(2.0, 2.5), d1)
    assert s.undo() == d1
    assert s.undo() == d0
    assert s.redo() == d1
    assert s.redo() == d2
    assert not s.can_redo


def test_stack_fork_discards_redo_branch():
    """User does A→B→C, undoes back to A, then does D — B and C are gone forever."""
    s = CommandStack()
    d0 = _doc()
    d1 = _push(s, AddCut(0.0, 0.5), d0)
    d2 = _push(s, AddCut(1.0, 1.5), d1)
    _push(s, AddCut(2.0, 2.5), d2)
    assert s.undo() == d2
    assert s.undo() == d1
    assert s.can_redo
    _push(s, AddCut(3.0, 3.5), d1)
    assert not s.can_redo, "fork must discard redo stack"
    assert s.undo() == d1
    assert s.undo() == d0
    assert s.undo() is None


def test_stack_max_depth_enforced():
    s = CommandStack(max_depth=3)
    d0 = _doc(duration=10.0)
    docs = [d0]
    for i in range(5):
        new = _push(
            s,
            AddCut(start=float(i) * 1.5, end=float(i) * 1.5 + 0.5),
            docs[-1],
        )
        docs.append(new)
    assert s.undo_depth == 3
    assert s.undo() == docs[4]
    assert s.undo() == docs[3]
    assert s.undo() == docs[2]
    assert s.undo() is None


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
    """A realistic sequence: AddCut, AddCut, RestoreRange (undoes the second cut)."""
    s = CommandStack()
    d0 = _doc()
    add1 = AddCut(0.0, 1.0)
    add2 = AddCut(2.0, 3.0)
    restore = RestoreRange(2.0, 3.0)

    d1 = _push(s, add1, d0)
    d2 = _push(s, add2, d1)
    d3 = _push(s, restore, d2)
    assert d3.ranges == d1.ranges  # restore undid the second cut

    assert s.undo() == d2  # un-restore (cut is back)
    assert s.undo() == d1
    assert s.undo() == d0
    assert s.redo() == d1
    assert s.redo() == d2
    assert s.redo() == d3
    assert not s.can_redo
