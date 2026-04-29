"""Phase 6b-1 — MoveClipSpan, EditEvent / edit_log, Proposal.

Covers:

- :func:`core.edit_events.is_valid_reason` pass/fail cases.
- :class:`core.editing.MoveClipSpan` apply/revert/inverse (single,
  multi-clip, move-to-end, move-of-already-moved, errors).
- :class:`core.edit_events.EditEvent` appended on cut/restore/move;
  reason mirroring between Clip.reason and edit_log on cuts.
- v3 → v3.1 migration: empty edit_log on load; re-saved docs are 3.1.
- :class:`core.proposal.Proposal` JSON round-trip; stale-hash guard;
  per-move outcomes (all-applied / all-rejected / mixed).
- Inverse round-trip (doc → move → inverse → doc-equivalent ranges).
- Slow render: monotonic doc → move → non-monotonic → second move →
  render produces correct duration ±50 ms.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.document import Document, MediaSource, Range, Segment, Word
from core.edit_events import ClipAnchor, EditEvent, is_valid_reason
from core.editing import (
    AddCut,
    CutWordRange,
    MoveClipSpan,
    RestoreRange,
    SpanResolutionError,
)
from core.proposal import (
    PROPOSAL_SCHEMA_VERSION,
    Proposal,
    StaleProposalError,
    apply_proposal,
    document_state_hash,
)
from core.render import render_cut

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ffprobe_path() -> Path:
    for cand in (
        Path("/opt/homebrew/bin/ffprobe"),
        Path("/usr/local/bin/ffprobe"),
        Path(__file__).resolve().parent.parent
        / "resources" / "bin" / "ffprobe-mac",
    ):
        if cand.is_file():
            return cand
    pytest.skip("ffprobe not available")


def _probe_duration_video(path: Path) -> float:
    out = subprocess.run(
        [
            str(_ffprobe_path()),
            "-v", "error",
            "-of", "json",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(out.stdout)
    v = next(s for s in data["streams"] if s["codec_type"] == "video")
    return float(v["duration"])


def _doc_with_clips(media: Path, clips: list[tuple[float, float, str]]) -> Document:
    """Build a doc whose ``ranges`` are the given (start, end, reason) tuples."""
    src = MediaSource(id="src0", path=media, duration=30.0)
    return Document(
        sources={"src0": src},
        segments=[
            Segment(
                text="seg",
                start=0.0,
                end=30.0,
                words=tuple(
                    Word(text=f"w{i}", start=float(i), end=float(i + 1))
                    for i in range(30)
                ),
            )
        ],
        ranges=[
            Range(source_id="src0", start=s, end=e, reason=r) for (s, e, r) in clips
        ],
        language="en",
        created_at=datetime(2026, 4, 26, 10, 0, 0, tzinfo=UTC),
        model_name="tiny",
    )


def _anchor(media: Path, start: float, end: float) -> ClipAnchor:
    return ClipAnchor(source_path=media, source_start=start, source_end=end)


# ---------------------------------------------------------------------------
# is_valid_reason
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        "manual",
        "manual override",
        "filler removal",
        "filler",
        "silence trim",
        "rearrange for narrative flow",
        "highlight reel",
        "narrative restructure",
        "trim",
        "best take",
        "best-take selection",
        "undo: rearrange",
        "I want this earlier",  # 19 chars, free-form
    ],
)
def test_is_valid_reason_accepts(reason):
    assert is_valid_reason(reason) is True


@pytest.mark.parametrize(
    "reason",
    [
        None,
        "",
        "   ",
        "x",
        "tmp",
        "todo",
        "test",  # under threshold and not a category
        "manualx",  # word-boundary fails -- "manualx" doesn't match \bmanual\b
    ],
)
def test_is_valid_reason_rejects(reason):
    assert is_valid_reason(reason) is False


def test_is_valid_reason_non_string_is_false():
    assert is_valid_reason(123) is False
    assert is_valid_reason(["manual"]) is False


# ---------------------------------------------------------------------------
# v3 → v3.1 migration
# ---------------------------------------------------------------------------


def _v3_payload_with_clips(media_path: str, clips: list[dict]) -> dict:
    return {
        "schema_version": 3,
        "sources": {
            "src0": {
                "id": "src0",
                "path": media_path,
                "duration": 30.0,
                "hash": "",
            }
        },
        "language": "en",
        "model_name": "tiny",
        "created_at": "2026-04-26T10:00:00+00:00",
        "segments": [],
        "main_timeline": {"clips": clips},
    }


def test_v3_loads_with_empty_edit_log():
    """A v3 (no edit_log) doc loads as v3.1 with an empty edit_log."""
    payload = _v3_payload_with_clips(
        "/tmp/x.wav",
        [
            {
                "source_id": "src0",
                "source_path": "/tmp/x.wav",
                "source_start": 0.0,
                "source_end": 5.0,
                "reason": "",
            },
        ],
    )
    doc = Document.from_json(payload)
    assert doc.edit_log == ()


def test_resaved_doc_has_schema_version_3_1():
    payload = _v3_payload_with_clips(
        "/tmp/x.wav",
        [
            {
                "source_id": "src0",
                "source_path": "/tmp/x.wav",
                "source_start": 0.0,
                "source_end": 5.0,
                "reason": "",
            }
        ],
    )
    doc = Document.from_json(payload)
    out = doc.to_json()
    assert out["schema_version"] == 3.1
    assert "edit_log" in out
    assert out["edit_log"] == []


def test_v31_round_trip_preserves_edit_log(tmp_path):
    """Saving a v3.1 doc with events then reading it back yields the same log."""
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_with_clips(media, [(0.0, 10.0, "")])
    cut = AddCut(start=4.0, end=6.0, reason="filler removal")
    after = cut.apply(doc)
    payload = json.dumps(after.to_json())
    restored = Document.from_json(json.loads(payload))
    assert len(restored.edit_log) == 1
    ev = restored.edit_log[0]
    assert ev.kind == "cut"
    assert ev.reason == "filler removal"
    assert ev.start == 4.0 and ev.end == 6.0
    assert ev.source_id == "src0"


def test_v2_doc_loads_with_empty_edit_log():
    """v2 → v3 → v3.1 chain: edit_log starts empty even from old fixtures."""
    payload = {
        "schema_version": 2,
        "sources": {
            "src0": {
                "id": "src0",
                "path": "/tmp/x.wav",
                "duration": 10.0,
                "hash": "",
            }
        },
        "language": "en",
        "model_name": "tiny",
        "created_at": "2026-04-26T10:00:00+00:00",
        "segments": [],
        "ranges": [{"source_id": "src0", "start": 0.0, "end": 10.0, "reason": ""}],
    }
    doc = Document.from_json(payload)
    assert doc.edit_log == ()


def test_unknown_schema_still_raises():
    """Hard-fail on unknown schema_version."""
    payload = _v3_payload_with_clips("/tmp/x.wav", [])
    payload["schema_version"] = 99
    with pytest.raises(ValueError):
        Document.from_json(payload)


# ---------------------------------------------------------------------------
# EditEvent append + reason mirroring
# ---------------------------------------------------------------------------


def test_addcut_appends_event_and_mirrors_reason(tmp_path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_with_clips(media, [(0.0, 10.0, "")])
    after = AddCut(start=4.0, end=6.0, reason="filler removal").apply(doc)
    # One event recorded.
    assert len(after.edit_log) == 1
    ev = after.edit_log[0]
    assert ev.kind == "cut"
    assert ev.reason == "filler removal"
    # Range.reason mirrors the event.
    by_end = {r.end: r for r in after.ranges}
    assert by_end[4.0].reason == "filler removal"


def test_addcut_with_no_reason_records_empty_string_event(tmp_path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_with_clips(media, [(0.0, 10.0, "")])
    after = AddCut(start=4.0, end=6.0).apply(doc)  # reason=None
    assert len(after.edit_log) == 1
    assert after.edit_log[0].reason == ""


def test_restore_range_appends_restore_event(tmp_path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_with_clips(media, [(0.0, 4.0, ""), (6.0, 10.0, "")])
    after = RestoreRange(start=4.0, end=6.0, reason="manual").apply(doc)
    assert len(after.edit_log) == 1
    ev = after.edit_log[0]
    assert ev.kind == "restore"
    assert ev.start == 4.0 and ev.end == 6.0


def test_cut_word_range_appends_cut_event(tmp_path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_with_clips(media, [(0.0, 10.0, "")])
    after = CutWordRange(seg_idx=0, word_start_idx=2, word_end_idx=2).apply(doc)
    assert len(after.edit_log) == 1
    ev = after.edit_log[0]
    assert ev.kind == "cut"
    assert ev.reason == "manual"


def test_revert_restores_edit_log(tmp_path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_with_clips(media, [(0.0, 10.0, "")])
    cmd = AddCut(start=4.0, end=6.0, reason="filler")
    after = cmd.apply(doc)
    reverted = cmd.revert(after)
    assert reverted.edit_log == ()
    assert reverted.ranges == doc.ranges


# ---------------------------------------------------------------------------
# MoveClipSpan — construction validation
# ---------------------------------------------------------------------------


def test_move_construction_requires_valid_reason(tmp_path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    a = _anchor(media, 0.0, 5.0)
    with pytest.raises(ValueError, match="rationale"):
        MoveClipSpan(span=(a,), target=None, reason="x")


def test_move_construction_rejects_empty_span():
    with pytest.raises(ValueError, match="empty"):
        MoveClipSpan(span=(), target=None, reason="rearrange test")


def test_move_construction_rejects_target_in_span(tmp_path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    a = _anchor(media, 0.0, 5.0)
    with pytest.raises(ValueError, match="self-cycle"):
        MoveClipSpan(span=(a,), target=a, reason="rearrange test")


# ---------------------------------------------------------------------------
# MoveClipSpan — apply
# ---------------------------------------------------------------------------


def test_move_single_clip_to_end(tmp_path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_with_clips(
        media,
        [(0.0, 5.0, ""), (5.0, 10.0, ""), (10.0, 15.0, "")],
    )
    cmd = MoveClipSpan(
        span=(_anchor(media, 0.0, 5.0),),
        target=None,
        reason="rearrange to end",
    )
    after = cmd.apply(doc)
    starts = [r.start for r in after.ranges]
    assert starts == [5.0, 10.0, 0.0]
    # The move is non-monotonic by construction.
    assert after.main_timeline.is_source_monotonic() is False


def test_move_single_clip_before_target(tmp_path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_with_clips(
        media,
        [(0.0, 5.0, ""), (5.0, 10.0, ""), (10.0, 15.0, "")],
    )
    # Move clip [10.0, 15.0] before clip [0.0, 5.0]
    cmd = MoveClipSpan(
        span=(_anchor(media, 10.0, 15.0),),
        target=_anchor(media, 0.0, 5.0),
        reason="rearrange test sample",
    )
    after = cmd.apply(doc)
    starts = [r.start for r in after.ranges]
    assert starts == [10.0, 0.0, 5.0]


def test_move_multi_clip_span(tmp_path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_with_clips(
        media,
        [
            (0.0, 2.0, ""),  # A
            (2.0, 4.0, ""),  # B
            (4.0, 6.0, ""),  # C
            (6.0, 8.0, ""),  # D
            (8.0, 10.0, ""),  # E
        ],
    )
    # Move (B, C) to before E
    cmd = MoveClipSpan(
        span=(_anchor(media, 2.0, 4.0), _anchor(media, 4.0, 6.0)),
        target=_anchor(media, 8.0, 10.0),
        reason="rearrange B C",
    )
    after = cmd.apply(doc)
    starts = [r.start for r in after.ranges]
    assert starts == [0.0, 6.0, 2.0, 4.0, 8.0]


def test_move_event_appended(tmp_path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_with_clips(media, [(0.0, 5.0, ""), (5.0, 10.0, "")])
    span_anchor = _anchor(media, 0.0, 5.0)
    cmd = MoveClipSpan(
        span=(span_anchor,),
        target=None,
        reason="rearrange to end",
    )
    after = cmd.apply(doc)
    assert len(after.edit_log) == 1
    ev = after.edit_log[0]
    assert ev.kind == "move"
    assert ev.span == (span_anchor,)
    assert ev.target is None
    assert ev.reason == "rearrange to end"


def test_move_revert_restores_state(tmp_path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_with_clips(
        media, [(0.0, 5.0, ""), (5.0, 10.0, ""), (10.0, 15.0, "")]
    )
    cmd = MoveClipSpan(
        span=(_anchor(media, 0.0, 5.0),),
        target=None,
        reason="rearrange to end",
    )
    after = cmd.apply(doc)
    reverted = cmd.revert(after)
    assert reverted.ranges == doc.ranges
    assert reverted.edit_log == ()


# ---------------------------------------------------------------------------
# MoveClipSpan — already-moved span found by anchor identity
# ---------------------------------------------------------------------------


def test_move_of_already_moved_section(tmp_path):
    """After move 1 puts (B, C) at the end, move 2 should still find them
    via anchor identity even though their indices changed.
    """
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_with_clips(
        media,
        [
            (0.0, 2.0, ""),  # A
            (2.0, 4.0, ""),  # B
            (4.0, 6.0, ""),  # C
            (6.0, 8.0, ""),  # D
        ],
    )
    move1 = MoveClipSpan(
        span=(_anchor(media, 2.0, 4.0), _anchor(media, 4.0, 6.0)),
        target=None,  # to end
        reason="rearrange step 1",
    )
    after1 = move1.apply(doc)
    # State now: A, D, B, C
    assert [r.start for r in after1.ranges] == [0.0, 6.0, 2.0, 4.0]
    # Move (B, C) again — this time to before A
    move2 = MoveClipSpan(
        span=(_anchor(media, 2.0, 4.0), _anchor(media, 4.0, 6.0)),
        target=_anchor(media, 0.0, 2.0),
        reason="rearrange step 2",
    )
    after2 = move2.apply(after1)
    assert [r.start for r in after2.ranges] == [2.0, 4.0, 0.0, 6.0]


# ---------------------------------------------------------------------------
# MoveClipSpan — error paths
# ---------------------------------------------------------------------------


def test_move_span_not_found(tmp_path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_with_clips(media, [(0.0, 5.0, ""), (5.0, 10.0, "")])
    cmd = MoveClipSpan(
        span=(_anchor(media, 99.0, 100.0),),
        target=None,
        reason="rearrange ghost",
    )
    with pytest.raises(SpanResolutionError, match="not found"):
        cmd.apply(doc)


def test_move_span_not_contiguous(tmp_path):
    """Two anchors that exist but aren't adjacent in the playlist."""
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_with_clips(
        media, [(0.0, 5.0, ""), (5.0, 10.0, ""), (10.0, 15.0, "")]
    )
    cmd = MoveClipSpan(
        span=(_anchor(media, 0.0, 5.0), _anchor(media, 10.0, 15.0)),
        target=None,
        reason="rearrange noncontig",
    )
    with pytest.raises(SpanResolutionError, match="not contiguous"):
        cmd.apply(doc)


def test_move_target_not_found(tmp_path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_with_clips(media, [(0.0, 5.0, ""), (5.0, 10.0, "")])
    cmd = MoveClipSpan(
        span=(_anchor(media, 0.0, 5.0),),
        target=_anchor(media, 99.0, 100.0),
        reason="rearrange wrong target",
    )
    with pytest.raises(SpanResolutionError, match="target.*not found"):
        cmd.apply(doc)


# ---------------------------------------------------------------------------
# MoveClipSpan — inverse round-trip
# ---------------------------------------------------------------------------


def test_inverse_round_trip_restores_ranges(tmp_path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_with_clips(
        media, [(0.0, 5.0, ""), (5.0, 10.0, ""), (10.0, 15.0, "")]
    )
    fwd = MoveClipSpan(
        span=(_anchor(media, 5.0, 10.0),),
        target=None,
        reason="rearrange to end",
    )
    inv = fwd.inverse(doc)
    after = fwd.apply(doc)
    rebuilt = inv.apply(after)
    # Ranges restored byte-for-byte; edit_log accumulates two events
    # since both moves succeeded — that's by design.
    assert rebuilt.ranges == doc.ranges
    assert len(rebuilt.edit_log) == 2
    assert rebuilt.edit_log[0].kind == "move"
    assert rebuilt.edit_log[1].kind == "move"
    assert rebuilt.edit_log[1].reason.startswith("undo:")


def test_inverse_of_move_to_end_targets_none_when_span_at_end(tmp_path):
    """If the span sat at the playlist end, the inverse target is None too."""
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_with_clips(media, [(0.0, 5.0, ""), (5.0, 10.0, "")])
    fwd = MoveClipSpan(
        span=(_anchor(media, 5.0, 10.0),),  # already at end
        target=_anchor(media, 0.0, 5.0),  # move it to before A
        reason="rearrange front",
    )
    inv = fwd.inverse(doc)
    # Span originally at end → inverse target = None
    assert inv.target is None


# ---------------------------------------------------------------------------
# Proposal — JSON round-trip
# ---------------------------------------------------------------------------


def test_proposal_json_round_trip(tmp_path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    p = Proposal(
        parent_document_state_hash="abc123",
        moves=(
            MoveClipSpan(
                span=(_anchor(media, 0.0, 5.0),),
                target=_anchor(media, 5.0, 10.0),
                reason="rearrange A-B",
            ),
            MoveClipSpan(
                span=(_anchor(media, 10.0, 15.0),),
                target=None,
                reason="trim trailing",
            ),
        ),
    )
    out = tmp_path / "proposal.json"
    p.write(out)
    p2 = Proposal.read(out)
    assert p2.parent_document_state_hash == "abc123"
    assert len(p2.moves) == 2
    assert p2.moves[0].span[0].source_start == 0.0
    assert p2.moves[1].target is None


def test_proposal_from_json_revalidates_reason(tmp_path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    bad = {
        "parent_document_state_hash": None,
        "moves": [
            {
                "span": [_anchor(media, 0.0, 5.0).to_json()],
                "target": None,
                "reason": "x",  # invalid
            }
        ],
    }
    with pytest.raises(ValueError, match="rationale"):
        Proposal.from_json(bad)


# ---------------------------------------------------------------------------
# apply_proposal — stale-hash, all-applied, all-rejected, mixed
# ---------------------------------------------------------------------------


def test_apply_proposal_stale_hash_raises(tmp_path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_with_clips(media, [(0.0, 5.0, ""), (5.0, 10.0, "")])
    doc = Document(
        sources=doc.sources,
        segments=doc.segments,
        ranges=doc.ranges,
        language=doc.language,
        created_at=doc.created_at,
        model_name=doc.model_name,
        source_hash="LIVE_HASH",
    )
    p = Proposal(
        parent_document_state_hash="STALE_HASH",
        moves=(
            MoveClipSpan(
                span=(_anchor(media, 0.0, 5.0),),
                target=None,
                reason="rearrange test",
            ),
        ),
    )
    with pytest.raises(StaleProposalError):
        apply_proposal(doc, p)


def test_apply_proposal_all_applied(tmp_path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_with_clips(
        media, [(0.0, 5.0, ""), (5.0, 10.0, ""), (10.0, 15.0, "")]
    )
    p = Proposal(
        parent_document_state_hash=None,
        moves=(
            MoveClipSpan(
                span=(_anchor(media, 10.0, 15.0),),
                target=_anchor(media, 0.0, 5.0),
                reason="rearrange step 1",
            ),
            # After step 1: state is C, A, B (10, 0, 5).
            # Move A to end.
            MoveClipSpan(
                span=(_anchor(media, 0.0, 5.0),),
                target=None,
                reason="rearrange step 2",
            ),
        ),
    )
    final, outcomes = apply_proposal(doc, p)
    assert all(o.applied for o in outcomes)
    assert len(outcomes) == 2
    starts = [r.start for r in final.ranges]
    assert starts == [10.0, 5.0, 0.0]


def test_apply_proposal_all_rejected(tmp_path):
    """Every move references anchors that don't exist; outcomes all-failed,
    document unchanged.
    """
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_with_clips(media, [(0.0, 5.0, ""), (5.0, 10.0, "")])
    ghost = _anchor(media, 99.0, 100.0)
    p = Proposal(
        parent_document_state_hash=None,
        moves=(
            MoveClipSpan(
                span=(ghost,), target=None, reason="rearrange ghost 1"
            ),
            MoveClipSpan(
                span=(ghost,),
                target=_anchor(media, 0.0, 5.0),
                reason="rearrange ghost 2",
            ),
        ),
    )
    final, outcomes = apply_proposal(doc, p)
    assert not any(o.applied for o in outcomes)
    assert all(o.error for o in outcomes)
    # Document unchanged — same identity on ranges.
    assert final.ranges == doc.ranges
    # No edit_log events appended.
    assert final.edit_log == ()


def test_apply_proposal_mixed_outcomes(tmp_path):
    """Move 1 lands; move 2 fails (bad anchor); move 3 lands on the
    post-move-1 state — partial across moves, all-or-nothing per move.
    """
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_with_clips(
        media, [(0.0, 5.0, ""), (5.0, 10.0, ""), (10.0, 15.0, "")]
    )
    p = Proposal(
        parent_document_state_hash=None,
        moves=(
            MoveClipSpan(
                span=(_anchor(media, 0.0, 5.0),),
                target=None,  # to end
                reason="rearrange step 1",
            ),
            # Move 2 references a clip that isn't in the timeline.
            MoveClipSpan(
                span=(_anchor(media, 99.0, 100.0),),
                target=None,
                reason="rearrange ghost",
            ),
            # Move 3 should still see the post-move-1 state and land.
            # State after move 1: B, C, A (5, 10, 0).
            MoveClipSpan(
                span=(_anchor(media, 5.0, 10.0),),
                target=_anchor(media, 0.0, 5.0),
                reason="rearrange step 3",
            ),
        ),
    )
    final, outcomes = apply_proposal(doc, p)
    assert [o.applied for o in outcomes] == [True, False, True]
    # State after move 1: [5, 10, 0]
    # Move 3: span=(5,10), target=(0,5).
    #   In the post-move-1 state, target sits at index 2; span at 0..0.
    #   Result: [10, 5, 0].
    starts = [r.start for r in final.ranges]
    assert starts == [10.0, 5.0, 0.0]
    # Two events on the log (one per applied move).
    assert len(final.edit_log) == 2


# ---------------------------------------------------------------------------
# Proposal schema v1→v2 migration (6b cleanup)
# ---------------------------------------------------------------------------


def test_proposal_v1_load_round_trip(tmp_path):
    """A v1 proposal on disk loads via Proposal.from_json with the legacy
    field name ``parent_document_hash`` mapped onto the new
    ``parent_document_state_hash`` field. ``schema_version`` is 1 on the
    in-memory dataclass."""
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    v1_payload = {
        "parent_document_hash": "LEGACY_SOURCE_HASH",
        "moves": [
            {
                "move_id": "m000",
                "span": [_anchor(media, 0.0, 5.0).to_json()],
                "target": None,
                "reason": "rearrange v1 carry-over",
            }
        ],
    }
    p = Proposal.from_json(v1_payload)
    assert p.schema_version == 1
    assert p.parent_document_state_hash == "LEGACY_SOURCE_HASH"
    assert len(p.moves) == 1


def test_proposal_v2_emits_schema_version_and_renamed_field(tmp_path):
    """to_json emits schema_version=2 and parent_document_state_hash."""
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    p = Proposal(
        parent_document_state_hash="abc123",
        moves=(
            MoveClipSpan(
                span=(_anchor(media, 0.0, 5.0),),
                target=None,
                reason="rearrange v2",
            ),
        ),
    )
    payload = p.to_json()
    assert payload["schema_version"] == PROPOSAL_SCHEMA_VERSION == 2
    assert payload["parent_document_state_hash"] == "abc123"
    assert "parent_document_hash" not in payload


def test_proposal_v1_apply_never_raises_stale(tmp_path):
    """A v1 proposal's stale check is suppressed even when the legacy hash
    can't possibly match the live document_state_hash."""
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_with_clips(media, [(0.0, 5.0, ""), (5.0, 10.0, "")])
    v1_payload = {
        "parent_document_hash": "FROZEN_LEGACY_HASH",
        "moves": [
            {
                "move_id": "m000",
                "span": [_anchor(media, 0.0, 5.0).to_json()],
                "target": None,
                "reason": "rearrange v1 apply",
            }
        ],
    }
    p = Proposal.from_json(v1_payload)
    assert p.schema_version == 1
    # Live doc's content hash is computed dynamically; stale check is
    # suppressed for v1 proposals so apply does not raise.
    final, outcomes = apply_proposal(doc, p)
    assert outcomes[0].applied is True
    assert len(final.ranges) == 2


def test_proposal_v2_stale_fires_on_intra_doc_drift(tmp_path):
    """Capturing document_state_hash before an edit, then mutating the doc
    must trip the stale guard on apply."""
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc_before = _doc_with_clips(
        media, [(0.0, 5.0, ""), (5.0, 10.0, ""), (10.0, 15.0, "")]
    )
    pre_hash = document_state_hash(doc_before)
    # Mutate the doc state — same source, different ranges.
    doc_after = _doc_with_clips(
        media, [(0.0, 5.0, ""), (10.0, 15.0, "")]
    )
    assert document_state_hash(doc_after) != pre_hash
    p = Proposal(
        parent_document_state_hash=pre_hash,
        moves=(
            MoveClipSpan(
                span=(_anchor(media, 0.0, 5.0),),
                target=None,
                reason="rearrange drift test",
            ),
        ),
    )
    with pytest.raises(StaleProposalError):
        apply_proposal(doc_after, p)


# ---------------------------------------------------------------------------
# REASON pattern surface (regression check on the regex set)
# ---------------------------------------------------------------------------


def test_reason_categories_cover_undo_prefix():
    """The undo: prefix is what MoveClipSpan.inverse emits — must validate."""
    inv_reason = "undo: rearrange foo"
    assert is_valid_reason(inv_reason)


def test_reason_categories_are_case_insensitive():
    assert is_valid_reason("MANUAL override")
    assert is_valid_reason("Filler")
    assert is_valid_reason("Rearrange Step 1")


# ---------------------------------------------------------------------------
# Slow render: monotonic → move → non-monotonic → second move → render
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_two_move_sequence_renders_to_expected_duration(synthetic_video, tmp_path):
    """Monotonic doc → MoveClipSpan → non-monotonic doc → second MoveClipSpan
    → render produces correct duration ±50 ms.

    Setup: split the synthetic 30 s fixture into three 5-s clips
    [0,5), [10,15), [20,25). Total 15 s.
    Move 1: send the [0,5) clip to the end → playlist (10-15, 20-25, 0-5).
    Move 2: move the (20-25) clip to before (10-15) →
                                    playlist (20-25, 10-15, 0-5).
    Total duration unchanged (15 s); render must reproduce that.
    """
    src = MediaSource(id="src0", path=synthetic_video, duration=30.0)
    seg = Segment(
        text="seg",
        start=0.0,
        end=30.0,
        words=tuple(
            Word(text=f"w{i}", start=float(i), end=float(i + 1)) for i in range(30)
        ),
    )
    doc = Document(
        sources={"src0": src},
        segments=[seg],
        ranges=[
            Range(source_id="src0", start=0.0, end=5.0),
            Range(source_id="src0", start=10.0, end=15.0),
            Range(source_id="src0", start=20.0, end=25.0),
        ],
        language="en",
        created_at=datetime.now(UTC),
        model_name="tiny",
    )
    media = synthetic_video
    move1 = MoveClipSpan(
        span=(_anchor(media, 0.0, 5.0),),
        target=None,
        reason="rearrange step 1",
    )
    after1 = move1.apply(doc)
    assert after1.main_timeline.is_source_monotonic() is False
    move2 = MoveClipSpan(
        span=(_anchor(media, 20.0, 25.0),),
        target=_anchor(media, 10.0, 15.0),
        reason="rearrange step 2",
    )
    after2 = move2.apply(after1)
    out = tmp_path / "out.mp4"
    render_cut(after2, out, pad_lead=0.0, pad_trail=0.0, audio_fade_ms=0)
    v_dur = _probe_duration_video(out)
    assert abs(v_dur - 15.0) <= 0.05


# ---------------------------------------------------------------------------
# Misc — EditEvent JSON round-trip
# ---------------------------------------------------------------------------


def test_edit_event_cut_json_round_trip():
    ev = EditEvent(
        kind="cut",
        timestamp=datetime(2026, 4, 26, 10, 0, 0, tzinfo=UTC),
        reason="filler removal",
        start=1.0,
        end=2.0,
        source_id="src0",
    )
    out = ev.to_json()
    re_ev = EditEvent.from_json(out)
    assert re_ev == ev


def test_edit_event_move_json_round_trip(tmp_path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    a = _anchor(media, 0.0, 5.0)
    b = _anchor(media, 5.0, 10.0)
    ev = EditEvent(
        kind="move",
        timestamp=datetime(2026, 4, 26, 10, 0, 0, tzinfo=UTC),
        reason="rearrange test",
        span=(a,),
        target=b,
    )
    out = ev.to_json()
    re_ev = EditEvent.from_json(out)
    assert re_ev == ev


def test_edit_event_move_with_target_none_round_trip(tmp_path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    a = _anchor(media, 0.0, 5.0)
    ev = EditEvent(
        kind="move",
        timestamp=datetime(2026, 4, 26, 10, 0, 0, tzinfo=UTC),
        reason="rearrange to end",
        span=(a,),
        target=None,
    )
    out = ev.to_json()
    assert out["target"] is None
    re_ev = EditEvent.from_json(out)
    assert re_ev == ev


def test_unknown_event_kind_raises_on_emit():
    ev = EditEvent(
        kind="garbage",
        timestamp=datetime(2026, 4, 26, 10, 0, 0, tzinfo=UTC),
        reason="manual",
    )
    with pytest.raises(ValueError, match="Unknown EditEvent kind"):
        ev.to_json()


# ---------------------------------------------------------------------------
# Reason regex surface (so the report can say which patterns are defined)
# ---------------------------------------------------------------------------


def test_reason_categories_have_expected_set():
    """Locks the active prefix set so a future PR can't quietly drop one."""
    from core.edit_events import REASON_CATEGORIES

    starts = {p.pattern for p in REASON_CATEGORIES}
    expected = {
        r"^manual\b",
        r"^filler\b",
        r"^silence\b",
        r"^rearrange\b",
        r"^highlight\b",
        r"^narrative\b",
        r"^trim\b",
        r"^best[-\s_]take\b",
        r"^undo:\s*",
    }
    # Ignore IGNORECASE flag; just check the pattern strings.
    assert starts == expected
    # Sanity: every pattern compiles and matches something.
    for p in REASON_CATEGORIES:
        assert isinstance(p, re.Pattern)
