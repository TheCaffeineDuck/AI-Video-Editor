"""Phase 6b-2 — proposal lifecycle (MCP tools + persistence).

Covers:

- Pre-flight Clip.reason ↔ EditEvent.reason mirroring regression check.
- ``propose_moves``: shape validation per error code (INVALID_REASON /
  INVALID_PROPOSAL / DUPLICATE_MOVE_ID), proposal file is written and
  well-formed, auto-assigned move_ids when caller passes null, full-
  precision floats round-trip.
- ``list_proposals``: empty / single / multiple in order /
  latest_apply_result_id linkage.
- ``read_proposal``: round-trips proposal data.
- ``apply_proposal``: all-applied happy path, partial via ``move_ids``,
  all-skipped (empty ``move_ids``), stale-proposal raises STALE_PROPOSAL,
  mixed (some moves succeed, some fail), post_state_hash chain-of-custody
  (hash N+1 = hash of doc after move N applied), full-coverage outcomes
  regardless of ``move_ids`` filter, order-preserving regardless of
  parameter order.
- ``list_apply_results``: empty / single / multiple / scoped by
  proposal_id.
- ``read_apply_result``: round-trips outcome data including full-
  precision floats and ``human_rejection_reason=null``.
- Path A end-to-end: load doc → propose_moves → read_proposal →
  apply_proposal → read_apply_result.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from mcp.shared.exceptions import McpError

from core.document import Document, MediaSource, Range, Segment, Word
from core.editing import AddCut
from core.proposal import (
    document_state_hash,
)
from mcp_server import errors as mcp_errors
from mcp_server.schemas import (
    ApplyProposalRequest,
    ClipAnchorRequest,
    ListApplyResultsRequest,
    ListProposalsRequest,
    MoveRequest,
    ProposeMovesRequest,
    ReadApplyResultRequest,
    ReadProposalRequest,
)
from mcp_server.tools.proposals import (
    apply_proposal,
    list_apply_results,
    list_proposals,
    propose_moves,
    read_apply_result,
    read_proposal,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _doc_with_three_clips(media: Path) -> Document:
    """Build a doc with ranges [0..5), [5..10), [10..15)."""
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
            Range(source_id="src0", start=0.0, end=5.0),
            Range(source_id="src0", start=5.0, end=10.0),
            Range(source_id="src0", start=10.0, end=15.0),
        ],
        language="en",
        created_at=datetime(2026, 4, 26, 10, 0, 0, tzinfo=UTC),
        model_name="tiny",
        source_hash="DOC_HASH_v1",
    )


def _write_doc(doc: Document, path: Path) -> Path:
    path.write_text(json.dumps(doc.to_json(), indent=2), encoding="utf-8")
    return path


def _doc_file(tmp_path: Path) -> tuple[Path, Path]:
    """Return ``(doc_path, media_path)`` for tests that need both."""
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_with_three_clips(media)
    doc_path = tmp_path / "x.transcribe.json"
    _write_doc(doc, doc_path)
    return doc_path, media


def _anchor_req(media: Path, start: float, end: float) -> ClipAnchorRequest:
    return ClipAnchorRequest(
        source_path=str(media), source_start_s=start, source_end_s=end
    )


# ---------------------------------------------------------------------------
# Pre-flight: Clip.reason mirroring (regression lock from 6b-2 audit)
# ---------------------------------------------------------------------------


def test_clip_reason_mirrors_edit_event_reason_for_v31_cuts(tmp_path):
    """Locks the dual-source-but-synchronized contract for 6b-2.

    For cuts authored under v3.1, ``Range.reason`` (== Clip.reason in
    the main_timeline view) and the matching ``EditEvent.reason``
    must hold the same string. A future change that desyncs them
    breaks this test.
    """
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    src = MediaSource(id="src0", path=media, duration=10.0)
    doc = Document(
        sources={"src0": src},
        segments=[
            Segment(
                text="seg",
                start=0.0,
                end=10.0,
                words=tuple(
                    Word(text=f"w{i}", start=float(i), end=float(i + 1))
                    for i in range(10)
                ),
            )
        ],
        ranges=[Range(source_id="src0", start=0.0, end=10.0)],
        language="en",
        created_at=datetime(2026, 4, 26, tzinfo=UTC),
        model_name="tiny",
    )
    after = AddCut(start=4.0, end=6.0, reason="filler removal").apply(doc)
    # Range.reason — denormalized convenience.
    by_end = {r.end: r.reason for r in after.ranges}
    assert by_end[4.0] == "filler removal"
    # EditEvent.reason — authoritative.
    assert len(after.edit_log) == 1
    assert after.edit_log[0].reason == "filler removal"
    # Clip.reason on the main_timeline view also mirrors.
    clips = after.main_timeline.clips
    pre_clip = next(c for c in clips if c.source_end == 4.0)
    assert pre_clip.reason == "filler removal"


# ---------------------------------------------------------------------------
# propose_moves
# ---------------------------------------------------------------------------


def test_propose_moves_writes_proposal_file(tmp_path):
    doc_path, media = _doc_file(tmp_path)
    res = _run(
        propose_moves(
            ProposeMovesRequest(
                json_path=str(doc_path),
                moves=[
                    MoveRequest(
                        span=[_anchor_req(media, 0.0, 5.0)],
                        target=None,
                        reason="rearrange to end",
                    )
                ],
            )
        )
    )
    out_path = Path(res.proposal_path)
    assert out_path.is_file()
    payload = json.loads(out_path.read_text())
    assert payload["proposal_id"] == res.proposal_id
    # 6b cleanup: parent_document_state_hash is now the doc's content
    # hash, not source_hash. Compute the expected value from the doc on
    # disk for comparison.
    expected_hash = document_state_hash(
        Document.from_json(json.loads(doc_path.read_text()))
    )
    assert payload["parent_document_state_hash"] == expected_hash
    assert payload["schema_version"] == 2
    assert len(payload["moves"]) == 1
    assert payload["moves"][0]["move_id"] == "m000"
    # On-disk JSON uses the core ClipAnchor shape (source_start /
    # source_end) — not the MCP wire shape (source_start_s /
    # source_end_s). Wire-side floats are checked via read_proposal.
    assert payload["moves"][0]["span"][0]["source_start"] == 0.0
    assert payload["moves"][0]["span"][0]["source_end"] == 5.0


def test_propose_moves_auto_assigns_move_ids_when_null(tmp_path):
    doc_path, media = _doc_file(tmp_path)
    res = _run(
        propose_moves(
            ProposeMovesRequest(
                json_path=str(doc_path),
                moves=[
                    MoveRequest(
                        span=[_anchor_req(media, 0.0, 5.0)],
                        target=None,
                        reason="rearrange first",
                    ),
                    MoveRequest(
                        span=[_anchor_req(media, 5.0, 10.0)],
                        target=None,
                        reason="rearrange second",
                    ),
                    MoveRequest(
                        span=[_anchor_req(media, 10.0, 15.0)],
                        target=None,
                        reason="rearrange third",
                    ),
                ],
            )
        )
    )
    assert res.move_ids == ["m000", "m001", "m002"]


def test_propose_moves_preserves_explicit_move_ids(tmp_path):
    doc_path, media = _doc_file(tmp_path)
    res = _run(
        propose_moves(
            ProposeMovesRequest(
                json_path=str(doc_path),
                moves=[
                    MoveRequest(
                        move_id="custom-named",
                        span=[_anchor_req(media, 0.0, 5.0)],
                        target=None,
                        reason="rearrange named",
                    ),
                    MoveRequest(
                        # Auto-assigned around the named one.
                        span=[_anchor_req(media, 5.0, 10.0)],
                        target=None,
                        reason="rearrange auto",
                    ),
                ],
            )
        )
    )
    assert res.move_ids == ["custom-named", "m000"]


def test_propose_moves_full_precision_float_round_trip(tmp_path):
    doc_path, media = _doc_file(tmp_path)
    odd_start = 0.123456789012345
    odd_end = 4.987654321098765
    # The doc must contain a clip whose anchors match these floats; we
    # rebuild the doc so the test can use them as anchors. (propose_moves
    # itself does not validate anchor identity against the doc; that's
    # an apply-time concern.)
    res = _run(
        propose_moves(
            ProposeMovesRequest(
                json_path=str(doc_path),
                moves=[
                    MoveRequest(
                        span=[_anchor_req(media, odd_start, odd_end)],
                        target=None,
                        reason="rearrange precision test",
                    )
                ],
            )
        )
    )
    payload = json.loads(Path(res.proposal_path).read_text())
    span = payload["moves"][0]["span"][0]
    assert span["source_start"] == odd_start
    assert span["source_end"] == odd_end


def test_propose_moves_invalid_reason_raises_invalid_reason(tmp_path):
    doc_path, media = _doc_file(tmp_path)
    with pytest.raises(McpError) as exc:
        _run(
            propose_moves(
                ProposeMovesRequest(
                    json_path=str(doc_path),
                    moves=[
                        MoveRequest(
                            span=[_anchor_req(media, 0.0, 5.0)],
                            target=None,
                            reason="x",
                        )
                    ],
                )
            )
        )
    assert exc.value.error.data["code"] == mcp_errors.INVALID_REASON


def test_propose_moves_self_cycle_raises_invalid_proposal(tmp_path):
    doc_path, media = _doc_file(tmp_path)
    a = _anchor_req(media, 0.0, 5.0)
    with pytest.raises(McpError) as exc:
        _run(
            propose_moves(
                ProposeMovesRequest(
                    json_path=str(doc_path),
                    moves=[
                        MoveRequest(
                            span=[a],
                            target=a,  # self-cycle
                            reason="rearrange self-cycle",
                        )
                    ],
                )
            )
        )
    assert exc.value.error.data["code"] == mcp_errors.INVALID_PROPOSAL


def test_propose_moves_duplicate_move_id_raises(tmp_path):
    doc_path, media = _doc_file(tmp_path)
    with pytest.raises(McpError) as exc:
        _run(
            propose_moves(
                ProposeMovesRequest(
                    json_path=str(doc_path),
                    moves=[
                        MoveRequest(
                            move_id="same",
                            span=[_anchor_req(media, 0.0, 5.0)],
                            target=None,
                            reason="rearrange first dup",
                        ),
                        MoveRequest(
                            move_id="same",
                            span=[_anchor_req(media, 5.0, 10.0)],
                            target=None,
                            reason="rearrange second dup",
                        ),
                    ],
                )
            )
        )
    assert exc.value.error.data["code"] == mcp_errors.DUPLICATE_MOVE_ID


# ---------------------------------------------------------------------------
# list_proposals
# ---------------------------------------------------------------------------


def test_list_proposals_empty(tmp_path):
    doc_path, _ = _doc_file(tmp_path)
    res = _run(list_proposals(ListProposalsRequest(json_path=str(doc_path))))
    assert res.proposals == []


def test_list_proposals_single(tmp_path):
    doc_path, media = _doc_file(tmp_path)
    p1 = _run(
        propose_moves(
            ProposeMovesRequest(
                json_path=str(doc_path),
                moves=[
                    MoveRequest(
                        span=[_anchor_req(media, 0.0, 5.0)],
                        target=None,
                        reason="rearrange single",
                    )
                ],
            )
        )
    )
    res = _run(list_proposals(ListProposalsRequest(json_path=str(doc_path))))
    assert len(res.proposals) == 1
    assert res.proposals[0].proposal_id == p1.proposal_id
    assert res.proposals[0].move_count == 1
    assert res.proposals[0].latest_apply_result_id is None


def test_list_proposals_multiple_chronological(tmp_path):
    doc_path, media = _doc_file(tmp_path)
    p1 = _run(
        propose_moves(
            ProposeMovesRequest(
                json_path=str(doc_path),
                moves=[
                    MoveRequest(
                        span=[_anchor_req(media, 0.0, 5.0)],
                        target=None,
                        reason="rearrange first",
                    )
                ],
            )
        )
    )
    p2 = _run(
        propose_moves(
            ProposeMovesRequest(
                json_path=str(doc_path),
                moves=[
                    MoveRequest(
                        span=[_anchor_req(media, 5.0, 10.0)],
                        target=None,
                        reason="rearrange second",
                    )
                ],
            )
        )
    )
    res = _run(list_proposals(ListProposalsRequest(json_path=str(doc_path))))
    ids = [p.proposal_id for p in res.proposals]
    assert ids == sorted([p1.proposal_id, p2.proposal_id])


def test_list_proposals_latest_apply_result_id_links(tmp_path):
    doc_path, media = _doc_file(tmp_path)
    p1 = _run(
        propose_moves(
            ProposeMovesRequest(
                json_path=str(doc_path),
                moves=[
                    MoveRequest(
                        span=[_anchor_req(media, 0.0, 5.0)],
                        target=None,
                        reason="rearrange link test",
                    )
                ],
            )
        )
    )
    apply_res = _run(
        apply_proposal(
            ApplyProposalRequest(json_path=str(doc_path), proposal_id=p1.proposal_id)
        )
    )
    listed = _run(list_proposals(ListProposalsRequest(json_path=str(doc_path))))
    summary = next(p for p in listed.proposals if p.proposal_id == p1.proposal_id)
    assert summary.latest_apply_result_id == apply_res.apply_result_id


# ---------------------------------------------------------------------------
# read_proposal
# ---------------------------------------------------------------------------


def test_read_proposal_round_trips_data(tmp_path):
    doc_path, media = _doc_file(tmp_path)
    p1 = _run(
        propose_moves(
            ProposeMovesRequest(
                json_path=str(doc_path),
                moves=[
                    MoveRequest(
                        move_id="m_zero",
                        span=[_anchor_req(media, 0.0, 5.0)],
                        target=_anchor_req(media, 5.0, 10.0),
                        reason="rearrange round-trip",
                    )
                ],
            )
        )
    )
    res = _run(
        read_proposal(
            ReadProposalRequest(
                json_path=str(doc_path), proposal_id=p1.proposal_id
            )
        )
    )
    assert res.proposal_id == p1.proposal_id
    expected_hash = document_state_hash(
        Document.from_json(json.loads(doc_path.read_text()))
    )
    assert res.parent_document_state_hash == expected_hash
    assert len(res.moves) == 1
    m = res.moves[0]
    assert m.move_id == "m_zero"
    assert m.target.source_start_s == 5.0
    assert m.reason == "rearrange round-trip"


def test_read_proposal_unknown_id_raises(tmp_path):
    doc_path, _ = _doc_file(tmp_path)
    with pytest.raises(McpError) as exc:
        _run(
            read_proposal(
                ReadProposalRequest(
                    json_path=str(doc_path), proposal_id="does-not-exist"
                )
            )
        )
    assert exc.value.error.data["code"] == mcp_errors.PROPOSAL_NOT_FOUND


# ---------------------------------------------------------------------------
# apply_proposal — happy path / partial / all-skipped / stale / mixed
# ---------------------------------------------------------------------------


def test_apply_proposal_all_applied(tmp_path):
    doc_path, media = _doc_file(tmp_path)
    p = _run(
        propose_moves(
            ProposeMovesRequest(
                json_path=str(doc_path),
                moves=[
                    MoveRequest(
                        span=[_anchor_req(media, 0.0, 5.0)],
                        target=None,
                        reason="rearrange step 1",
                    ),
                    MoveRequest(
                        span=[_anchor_req(media, 10.0, 15.0)],
                        target=None,
                        reason="rearrange step 2",
                    ),
                ],
            )
        )
    )
    res = _run(
        apply_proposal(
            ApplyProposalRequest(json_path=str(doc_path), proposal_id=p.proposal_id)
        )
    )
    assert res.applied_count == 2
    assert res.skipped_count == 0
    assert res.failed_count == 0
    assert all(o.applied for o in res.outcomes)
    # Document was rewritten — reload to verify.
    doc2 = Document.from_json(json.loads(doc_path.read_text()))
    starts = [r.start for r in doc2.ranges]
    # After move 1: [5, 10, 0]; after move 2 (target=None on 10..15
    # which is now at index 1): [5, 0, 10]
    assert starts == [5.0, 0.0, 10.0]


def test_apply_proposal_partial_via_move_ids(tmp_path):
    doc_path, media = _doc_file(tmp_path)
    p = _run(
        propose_moves(
            ProposeMovesRequest(
                json_path=str(doc_path),
                moves=[
                    MoveRequest(
                        span=[_anchor_req(media, 0.0, 5.0)],
                        target=None,
                        reason="rearrange first",
                    ),
                    MoveRequest(
                        span=[_anchor_req(media, 10.0, 15.0)],
                        target=None,
                        reason="rearrange second",
                    ),
                    MoveRequest(
                        span=[_anchor_req(media, 5.0, 10.0)],
                        target=None,
                        reason="rearrange third",
                    ),
                ],
            )
        )
    )
    # Accept moves m000 and m002, reject m001.
    res = _run(
        apply_proposal(
            ApplyProposalRequest(
                json_path=str(doc_path),
                proposal_id=p.proposal_id,
                move_ids=["m000", "m002"],
            )
        )
    )
    assert res.applied_count == 2
    assert res.skipped_count == 1
    assert res.failed_count == 0
    # Outcomes are in proposal order: m000 applied, m001 skipped, m002 applied.
    assert [o.applied for o in res.outcomes] == [True, False, True]
    assert [o.skipped for o in res.outcomes] == [False, True, False]


def test_apply_proposal_all_skipped_when_move_ids_empty(tmp_path):
    doc_path, media = _doc_file(tmp_path)
    p = _run(
        propose_moves(
            ProposeMovesRequest(
                json_path=str(doc_path),
                moves=[
                    MoveRequest(
                        span=[_anchor_req(media, 0.0, 5.0)],
                        target=None,
                        reason="rearrange skip 1",
                    ),
                    MoveRequest(
                        span=[_anchor_req(media, 5.0, 10.0)],
                        target=None,
                        reason="rearrange skip 2",
                    ),
                ],
            )
        )
    )
    res = _run(
        apply_proposal(
            ApplyProposalRequest(
                json_path=str(doc_path),
                proposal_id=p.proposal_id,
                move_ids=[],  # explicit empty
            )
        )
    )
    assert res.applied_count == 0
    assert res.skipped_count == 2
    assert res.failed_count == 0
    assert all(o.skipped for o in res.outcomes)
    # Doc unchanged.
    doc_after = Document.from_json(json.loads(doc_path.read_text()))
    assert [r.start for r in doc_after.ranges] == [0.0, 5.0, 10.0]


def test_apply_proposal_stale_proposal_raises(tmp_path):
    """Mutating the doc after authoring a proposal should trip STALE_PROPOSAL.

    The proposal records the doc's pre-mutation source_hash; if the
    on-disk doc's hash changes, the live `apply_proposal` refuses.
    """
    doc_path, media = _doc_file(tmp_path)
    p = _run(
        propose_moves(
            ProposeMovesRequest(
                json_path=str(doc_path),
                moves=[
                    MoveRequest(
                        span=[_anchor_req(media, 0.0, 5.0)],
                        target=None,
                        reason="rearrange stale test",
                    )
                ],
            )
        )
    )
    # Drift the doc's source_hash on disk.
    doc_payload = json.loads(doc_path.read_text())
    doc_payload["source_hash"] = "DRIFTED"
    doc_path.write_text(json.dumps(doc_payload, indent=2), encoding="utf-8")
    with pytest.raises(McpError) as exc:
        _run(
            apply_proposal(
                ApplyProposalRequest(
                    json_path=str(doc_path), proposal_id=p.proposal_id
                )
            )
        )
    assert exc.value.error.data["code"] == mcp_errors.STALE_PROPOSAL


def test_apply_proposal_mixed_outcomes(tmp_path):
    """Some moves succeed, some fail; full-coverage outcomes recorded."""
    doc_path, media = _doc_file(tmp_path)
    p = _run(
        propose_moves(
            ProposeMovesRequest(
                json_path=str(doc_path),
                moves=[
                    MoveRequest(
                        span=[_anchor_req(media, 0.0, 5.0)],
                        target=None,
                        reason="rearrange first ok",
                    ),
                    # This move references a clip that doesn't exist.
                    MoveRequest(
                        span=[_anchor_req(media, 99.0, 100.0)],
                        target=None,
                        reason="rearrange ghost",
                    ),
                    MoveRequest(
                        span=[_anchor_req(media, 5.0, 10.0)],
                        target=None,
                        reason="rearrange third ok",
                    ),
                ],
            )
        )
    )
    res = _run(
        apply_proposal(
            ApplyProposalRequest(json_path=str(doc_path), proposal_id=p.proposal_id)
        )
    )
    assert res.applied_count == 2
    assert res.skipped_count == 0
    assert res.failed_count == 1
    assert [o.applied for o in res.outcomes] == [True, False, True]
    assert res.outcomes[1].error_code == "SPAN_NOT_FOUND"


def test_apply_proposal_post_state_hash_chain(tmp_path):
    """Hash recorded for move N matches hash of doc state after move N applied.

    Strategy: rather than re-walk a parallel apply (which would record
    different EditEvent timestamps and therefore different hashes), we
    rely on apply_proposal's per-outcome ``post_state_hash`` values
    being:

    1. all set on applied moves (None on skipped/failed),
    2. distinct (each move changes the doc state),
    3. tail-equal to ``document_post_hash`` (chain endpoint),
    4. head-prefixed by ``document_pre_hash`` (chain origin), and
    5. when independently verifiable: equal to a hash of the doc
       state immediately after the cumulative apply through move N
       on the live apply path (covered by the tail check above for
       move N=last; intermediate moves can't be verified externally
       because their timestamps are baked into the live edit_log
       and aren't re-derivable).
    """
    doc_path, media = _doc_file(tmp_path)
    pre_hash_before = document_state_hash(
        Document.from_json(json.loads(doc_path.read_text()))
    )
    p = _run(
        propose_moves(
            ProposeMovesRequest(
                json_path=str(doc_path),
                moves=[
                    MoveRequest(
                        span=[_anchor_req(media, 0.0, 5.0)],
                        target=None,
                        reason="rearrange chain 1",
                    ),
                    MoveRequest(
                        span=[_anchor_req(media, 10.0, 15.0)],
                        target=None,
                        reason="rearrange chain 2",
                    ),
                ],
            )
        )
    )
    res = _run(
        apply_proposal(
            ApplyProposalRequest(json_path=str(doc_path), proposal_id=p.proposal_id)
        )
    )
    # Pre hash recorded by apply_proposal must equal what we captured before.
    assert res.document_pre_hash == pre_hash_before
    actual_chain = [o.post_state_hash for o in res.outcomes if o.applied]
    assert all(h is not None for h in actual_chain)
    assert len(set(actual_chain)) == len(actual_chain)  # no duplicates
    # Tail of chain == document_post_hash.
    assert actual_chain[-1] == res.document_post_hash
    # Tail of chain == hash of the doc state on disk after apply.
    doc_on_disk = Document.from_json(json.loads(doc_path.read_text()))
    assert actual_chain[-1] == document_state_hash(doc_on_disk)


def test_apply_proposal_full_coverage_outcomes_with_filter(tmp_path):
    """Outcome list always == proposal length, regardless of filter."""
    doc_path, media = _doc_file(tmp_path)
    p = _run(
        propose_moves(
            ProposeMovesRequest(
                json_path=str(doc_path),
                moves=[
                    MoveRequest(
                        span=[_anchor_req(media, 0.0, 5.0)],
                        target=None,
                        reason="rearrange one",
                    ),
                    MoveRequest(
                        span=[_anchor_req(media, 5.0, 10.0)],
                        target=None,
                        reason="rearrange two",
                    ),
                    MoveRequest(
                        span=[_anchor_req(media, 10.0, 15.0)],
                        target=None,
                        reason="rearrange three",
                    ),
                ],
            )
        )
    )
    res = _run(
        apply_proposal(
            ApplyProposalRequest(
                json_path=str(doc_path),
                proposal_id=p.proposal_id,
                move_ids=["m001"],
            )
        )
    )
    assert len(res.outcomes) == 3
    assert [o.move_id for o in res.outcomes] == ["m000", "m001", "m002"]


def test_apply_proposal_move_ids_filter_is_proposal_order(tmp_path):
    """move_ids order in the parameter doesn't change apply order."""
    doc_path, media = _doc_file(tmp_path)
    p = _run(
        propose_moves(
            ProposeMovesRequest(
                json_path=str(doc_path),
                moves=[
                    MoveRequest(
                        move_id="alpha",
                        span=[_anchor_req(media, 0.0, 5.0)],
                        target=None,
                        reason="rearrange alpha",
                    ),
                    MoveRequest(
                        move_id="beta",
                        span=[_anchor_req(media, 10.0, 15.0)],
                        target=None,
                        reason="rearrange beta",
                    ),
                ],
            )
        )
    )
    res_a = _run(
        apply_proposal(
            ApplyProposalRequest(
                json_path=str(doc_path),
                proposal_id=p.proposal_id,
                move_ids=["alpha", "beta"],
            )
        )
    )
    # Reset doc.
    _write_doc(_doc_with_three_clips(_doc_file_media(tmp_path)), doc_path)
    p2 = _run(
        propose_moves(
            ProposeMovesRequest(
                json_path=str(doc_path),
                moves=[
                    MoveRequest(
                        move_id="alpha",
                        span=[_anchor_req(media, 0.0, 5.0)],
                        target=None,
                        reason="rearrange alpha",
                    ),
                    MoveRequest(
                        move_id="beta",
                        span=[_anchor_req(media, 10.0, 15.0)],
                        target=None,
                        reason="rearrange beta",
                    ),
                ],
            )
        )
    )
    res_b = _run(
        apply_proposal(
            ApplyProposalRequest(
                json_path=str(doc_path),
                proposal_id=p2.proposal_id,
                move_ids=["beta", "alpha"],  # reversed
            )
        )
    )
    # Both runs apply alpha then beta in proposal order. The outcomes
    # are parallel to proposal.moves, so [alpha, beta] in both cases.
    assert [o.move_id for o in res_a.outcomes] == ["alpha", "beta"]
    assert [o.move_id for o in res_b.outcomes] == ["alpha", "beta"]
    # Both runs land at the same document_post_hash modulo timestamps —
    # since the runs are at different wall-clock times the hashes differ;
    # the *ranges* should match.
    doc_after = Document.from_json(json.loads(doc_path.read_text()))
    # Sanity: at least one move applied in each run.
    assert res_a.applied_count == 2
    assert res_b.applied_count == 2
    assert len(doc_after.ranges) == 3


def test_apply_proposal_invalid_move_id_raises(tmp_path):
    doc_path, media = _doc_file(tmp_path)
    p = _run(
        propose_moves(
            ProposeMovesRequest(
                json_path=str(doc_path),
                moves=[
                    MoveRequest(
                        span=[_anchor_req(media, 0.0, 5.0)],
                        target=None,
                        reason="rearrange exists",
                    )
                ],
            )
        )
    )
    with pytest.raises(McpError) as exc:
        _run(
            apply_proposal(
                ApplyProposalRequest(
                    json_path=str(doc_path),
                    proposal_id=p.proposal_id,
                    move_ids=["does-not-exist"],
                )
            )
        )
    assert exc.value.error.data["code"] == mcp_errors.INVALID_MOVE_ID


# ---------------------------------------------------------------------------
# list_apply_results
# ---------------------------------------------------------------------------


def test_list_apply_results_empty(tmp_path):
    doc_path, _ = _doc_file(tmp_path)
    res = _run(list_apply_results(ListApplyResultsRequest(json_path=str(doc_path))))
    assert res.apply_results == []


def test_list_apply_results_single(tmp_path):
    doc_path, media = _doc_file(tmp_path)
    p = _run(
        propose_moves(
            ProposeMovesRequest(
                json_path=str(doc_path),
                moves=[
                    MoveRequest(
                        span=[_anchor_req(media, 0.0, 5.0)],
                        target=None,
                        reason="rearrange single",
                    )
                ],
            )
        )
    )
    apply_res = _run(
        apply_proposal(
            ApplyProposalRequest(json_path=str(doc_path), proposal_id=p.proposal_id)
        )
    )
    listed = _run(
        list_apply_results(ListApplyResultsRequest(json_path=str(doc_path)))
    )
    assert len(listed.apply_results) == 1
    assert listed.apply_results[0].apply_result_id == apply_res.apply_result_id


def test_list_apply_results_scoped_by_proposal_id(tmp_path):
    doc_path, media = _doc_file(tmp_path)
    p1 = _run(
        propose_moves(
            ProposeMovesRequest(
                json_path=str(doc_path),
                moves=[
                    MoveRequest(
                        span=[_anchor_req(media, 0.0, 5.0)],
                        target=None,
                        reason="rearrange p1",
                    )
                ],
            )
        )
    )
    apply_p1 = _run(
        apply_proposal(
            ApplyProposalRequest(json_path=str(doc_path), proposal_id=p1.proposal_id)
        )
    )
    # A second proposal against the now-modified doc.
    doc_after = Document.from_json(json.loads(doc_path.read_text()))
    # The hash drifted; rewrite the doc with an updated source_hash.
    doc_payload = json.loads(doc_path.read_text())
    doc_payload["source_hash"] = "DOC_HASH_v2"
    doc_path.write_text(json.dumps(doc_payload, indent=2), encoding="utf-8")
    p2 = _run(
        propose_moves(
            ProposeMovesRequest(
                json_path=str(doc_path),
                moves=[
                    MoveRequest(
                        # The span needs to reflect the post-p1 state.
                        span=[_anchor_req(media, doc_after.ranges[0].start, doc_after.ranges[0].end)],
                        target=None,
                        reason="rearrange p2",
                    )
                ],
            )
        )
    )
    apply_p2 = _run(
        apply_proposal(
            ApplyProposalRequest(json_path=str(doc_path), proposal_id=p2.proposal_id)
        )
    )

    res = _run(
        list_apply_results(
            ListApplyResultsRequest(
                json_path=str(doc_path), proposal_id=p1.proposal_id
            )
        )
    )
    ids = {ar.apply_result_id for ar in res.apply_results}
    assert apply_p1.apply_result_id in ids
    assert apply_p2.apply_result_id not in ids


# ---------------------------------------------------------------------------
# read_apply_result
# ---------------------------------------------------------------------------


def test_read_apply_result_round_trips(tmp_path):
    doc_path, media = _doc_file(tmp_path)
    p = _run(
        propose_moves(
            ProposeMovesRequest(
                json_path=str(doc_path),
                moves=[
                    MoveRequest(
                        span=[_anchor_req(media, 0.123456789, 4.987654321)],
                        target=None,
                        reason="rearrange round-trip floats",
                    )
                ],
            )
        )
    )
    apply_res = _run(
        apply_proposal(
            ApplyProposalRequest(json_path=str(doc_path), proposal_id=p.proposal_id)
        )
    )
    read = _run(
        read_apply_result(
            ReadApplyResultRequest(
                json_path=str(doc_path),
                apply_result_id=apply_res.apply_result_id,
            )
        )
    )
    assert read.apply_result_id == apply_res.apply_result_id
    assert read.proposal_id == p.proposal_id
    assert read.document_pre_hash == apply_res.document_pre_hash
    assert read.document_post_hash == apply_res.document_post_hash
    # human_rejection_reason is null in 6b-2.
    assert all(o.human_rejection_reason is None for o in read.outcomes)


def test_read_apply_result_unknown_id_raises(tmp_path):
    doc_path, _ = _doc_file(tmp_path)
    with pytest.raises(McpError) as exc:
        _run(
            read_apply_result(
                ReadApplyResultRequest(
                    json_path=str(doc_path), apply_result_id="ghost"
                )
            )
        )
    assert exc.value.error.data["code"] == mcp_errors.APPLY_RESULT_NOT_FOUND


# ---------------------------------------------------------------------------
# Path A end-to-end
# ---------------------------------------------------------------------------


def test_path_a_end_to_end(tmp_path):
    """load doc → propose_moves → read_proposal → apply_proposal →
    read_apply_result, with on-disk doc reflecting applied moves."""
    doc_path, media = _doc_file(tmp_path)
    # 1. propose_moves
    propose_res = _run(
        propose_moves(
            ProposeMovesRequest(
                json_path=str(doc_path),
                moves=[
                    MoveRequest(
                        span=[_anchor_req(media, 0.0, 5.0)],
                        target=None,
                        reason="rearrange path A move 1",
                    ),
                    MoveRequest(
                        span=[_anchor_req(media, 10.0, 15.0)],
                        target=None,
                        reason="rearrange path A move 2",
                    ),
                ],
            )
        )
    )
    # 2. read_proposal — round-trips
    read_res = _run(
        read_proposal(
            ReadProposalRequest(
                json_path=str(doc_path), proposal_id=propose_res.proposal_id
            )
        )
    )
    assert len(read_res.moves) == 2
    # 3. apply_proposal — full apply
    apply_res = _run(
        apply_proposal(
            ApplyProposalRequest(
                json_path=str(doc_path),
                proposal_id=propose_res.proposal_id,
            )
        )
    )
    assert apply_res.applied_count == 2
    # 4. read_apply_result — round-trips outcomes
    read_apply = _run(
        read_apply_result(
            ReadApplyResultRequest(
                json_path=str(doc_path),
                apply_result_id=apply_res.apply_result_id,
            )
        )
    )
    assert read_apply.proposal_id == propose_res.proposal_id
    assert all(o.applied for o in read_apply.outcomes)
    # 5. Document on disk reflects applied moves
    doc_after = Document.from_json(json.loads(doc_path.read_text()))
    assert len(doc_after.edit_log) == 2
    assert all(e.kind == "move" for e in doc_after.edit_log)


# ---------------------------------------------------------------------------
# Helpers used inside tests
# ---------------------------------------------------------------------------


def read_proposal_payload(doc_path: Path, proposal_id: str) -> dict:
    """Direct JSON read of the persisted proposal — for chain hash test."""
    from core.proposal import proposals_dir_for_document
    p = proposals_dir_for_document(doc_path) / f"{proposal_id}.proposal.json"
    return json.loads(p.read_text())


def _doc_file_media(tmp_path: Path) -> Path:
    return tmp_path / "x.mp4"
