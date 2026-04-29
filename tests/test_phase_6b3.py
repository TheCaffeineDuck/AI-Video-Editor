"""Phase 6b-3 — Path B (human decisions) + GUI proposal review.

Three test families:

1. **Unit (core.proposal)** — `apply_proposal_with_human_decisions`
   parity with `apply_proposal` for all-accepted; mixed
   accept/reject; missing-decision → skipped; reject-no-reason and
   reject-with-bad-reason raise; outcome shape round-trips
   `human_rejection_reason`; stale-proposal raises; chain-of-custody
   hash on the apply-result schema.
2. **Qt headless (ProposalReviewPane)** — pane loads with synthetic
   proposal fixture; latest-by-default selection; Apply disabled
   until all moves decided; Reject reason field enforces the 8-char
   floor; staleness banner appears on parent-hash mismatch.
3. **Integration (round-trip)** — write doc + proposal on disk, drive
   the pane, click Apply, read the apply-result back via the MCP
   `read_apply_result` tool, verify `human_rejection_reason` flows
   through verbatim.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.document import Document, MediaSource, Range, Segment, Word
from core.edit_events import ClipAnchor
from core.editing import MoveClipSpan
from core.proposal import (
    ApplyResult,
    Decision,
    Proposal,
    StaleProposalError,
    _new_id,
    apply_proposal,
    apply_proposal_with_human_decisions,
    document_state_hash,
    write_apply_result,
    write_proposal,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _doc_three_clips(media: Path, source_hash: str = "DOC_HASH_v1") -> Document:
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
        source_hash=source_hash,
    )


def _anchor(media: Path, start: float, end: float) -> ClipAnchor:
    return ClipAnchor(source_path=media, source_start=start, source_end=end)


def _move(media: Path, start: float, end: float, reason: str, move_id: str) -> MoveClipSpan:
    return MoveClipSpan(
        span=(_anchor(media, start, end),),
        target=None,
        reason=reason,
        move_id=move_id,
    )


def _three_move_proposal(media: Path, parent_hash: str | None = None) -> Proposal:
    return Proposal(
        parent_document_state_hash=parent_hash,
        moves=(
            _move(media, 0.0, 5.0, "rearrange first", "alpha"),
            _move(media, 10.0, 15.0, "rearrange second", "beta"),
            _move(media, 5.0, 10.0, "rearrange third", "gamma"),
        ),
    )


# ===========================================================================
# 1. Unit tests — apply_proposal_with_human_decisions
# ===========================================================================


def test_all_accepted_equals_apply_proposal_modulo_timestamps(tmp_path):
    """Accept every move → same ranges as apply_proposal full apply.

    Hashes will differ because ``EditEvent.timestamp`` is wall-clock,
    so we compare the structurally-meaningful field (ranges).
    """
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_three_clips(media)
    proposal = _three_move_proposal(media)
    decisions = {
        "alpha": Decision(accept=True),
        "beta": Decision(accept=True),
        "gamma": Decision(accept=True),
    }
    new_b, outcomes_b = apply_proposal_with_human_decisions(proposal, doc, decisions)

    # Path A apply for comparison.
    new_a, outcomes_a = apply_proposal(doc, proposal)

    assert [r.start for r in new_b.ranges] == [r.start for r in new_a.ranges]
    assert all(o.applied for o in outcomes_b)
    assert all(o.applied for o in outcomes_a)
    # Per-move move_ids identical too.
    assert [o.move_id for o in outcomes_b] == [o.move_id for o in outcomes_a]


def test_mixed_accept_and_reject_produces_correct_outcomes(tmp_path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_three_clips(media)
    proposal = _three_move_proposal(media)
    decisions = {
        "alpha": Decision(accept=True),
        "beta": Decision(accept=False, rejection_reason="the segue doesn't work"),
        "gamma": Decision(accept=True),
    }
    new_doc, outcomes = apply_proposal_with_human_decisions(proposal, doc, decisions)
    assert [o.applied for o in outcomes] == [True, False, True]
    assert [o.skipped for o in outcomes] == [False, False, False]
    assert outcomes[1].error_code == "REJECTED_HUMAN"
    assert outcomes[1].human_rejection_reason == "the segue doesn't work"
    # Applied moves carry post_state_hash; rejected does not.
    assert outcomes[0].post_state_hash is not None
    assert outcomes[1].post_state_hash is None
    assert outcomes[2].post_state_hash is not None


def test_decision_missing_for_some_move_ids_is_skipped(tmp_path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_three_clips(media)
    proposal = _three_move_proposal(media)
    # Only decide on alpha and gamma; beta missing.
    decisions = {
        "alpha": Decision(accept=True),
        "gamma": Decision(accept=True),
    }
    _, outcomes = apply_proposal_with_human_decisions(proposal, doc, decisions)
    assert [o.applied for o in outcomes] == [True, False, True]
    assert [o.skipped for o in outcomes] == [False, True, False]
    # Skipped outcomes have neither error_code nor reason.
    assert outcomes[1].error_code is None
    assert outcomes[1].human_rejection_reason is None


def test_reject_without_rejection_reason_raises_value_error(tmp_path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_three_clips(media)
    proposal = _three_move_proposal(media)
    decisions = {
        "alpha": Decision(accept=False, rejection_reason=None),
    }
    with pytest.raises(ValueError, match="rejection_reason"):
        apply_proposal_with_human_decisions(proposal, doc, decisions)


def test_reject_with_empty_rejection_reason_raises(tmp_path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_three_clips(media)
    proposal = _three_move_proposal(media)
    decisions = {
        "alpha": Decision(accept=False, rejection_reason="   "),
    }
    with pytest.raises(ValueError, match="rejection_reason"):
        apply_proposal_with_human_decisions(proposal, doc, decisions)


def test_reject_with_short_rejection_reason_raises(tmp_path):
    """Substance check (≥8 char free-form, or category prefix) applies."""
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_three_clips(media)
    proposal = _three_move_proposal(media)
    # 4 chars, no category prefix.
    decisions = {
        "alpha": Decision(accept=False, rejection_reason="ugh!"),
    }
    with pytest.raises(ValueError, match="rationale"):
        apply_proposal_with_human_decisions(proposal, doc, decisions)


def test_validation_failure_writes_nothing(tmp_path):
    """Bad reason on any reject → no partial apply (strict pre-check)."""
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_three_clips(media)
    proposal = _three_move_proposal(media)
    decisions = {
        "alpha": Decision(accept=True),
        "beta": Decision(accept=False, rejection_reason="x"),  # invalid
        "gamma": Decision(accept=True),
    }
    with pytest.raises(ValueError):
        apply_proposal_with_human_decisions(proposal, doc, decisions)
    # Doc unchanged — no partial apply happened.
    assert [r.start for r in doc.ranges] == [0.0, 5.0, 10.0]


def test_apply_result_schema_round_trips_human_rejection_reason(tmp_path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_three_clips(media)
    proposal = _three_move_proposal(media)
    decisions = {
        "alpha": Decision(accept=True),
        "beta": Decision(accept=False, rejection_reason="the segue doesn't work"),
        "gamma": Decision(accept=True),
    }
    new_doc, outcomes = apply_proposal_with_human_decisions(proposal, doc, decisions)

    apply_result = ApplyResult(
        apply_result_id="20260101T000000-deadbeef",
        proposal_id="prop-id",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        document_pre_hash=document_state_hash(doc),
        document_post_hash=document_state_hash(new_doc),
        move_ids_filter=None,
        outcomes=tuple(outcomes),
    )
    payload = apply_result.to_json()
    rehydrated = ApplyResult.from_json(payload)
    out_b = rehydrated.outcomes[1]
    assert out_b.move_id == "beta"
    assert out_b.applied is False
    assert out_b.skipped is False
    assert out_b.error_code == "REJECTED_HUMAN"
    assert out_b.human_rejection_reason == "the segue doesn't work"


def test_stale_proposal_raises_before_validation(tmp_path):
    """Stale check fires before decision validation — most actionable failure first."""
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_three_clips(media, source_hash="LIVE_HASH")
    proposal = _three_move_proposal(media, parent_hash="OLD_HASH")
    decisions = {"alpha": Decision(accept=False, rejection_reason="x")}
    with pytest.raises(StaleProposalError):
        apply_proposal_with_human_decisions(proposal, doc, decisions)


def test_decisions_for_unknown_move_ids_are_silently_ignored(tmp_path):
    """Stale GUI cache shouldn't break apply when it carries a stray decision."""
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_three_clips(media)
    proposal = _three_move_proposal(media)
    decisions = {
        "alpha": Decision(accept=True),
        "beta": Decision(accept=True),
        "gamma": Decision(accept=True),
        "phantom": Decision(accept=False, rejection_reason="rearrange ghost"),  # unknown id
    }
    _, outcomes = apply_proposal_with_human_decisions(proposal, doc, decisions)
    # Only the three real moves get outcomes; the phantom is dropped.
    assert [o.move_id for o in outcomes] == ["alpha", "beta", "gamma"]
    assert all(o.applied for o in outcomes)


def test_post_state_hash_chain_matches_document_post_hash(tmp_path):
    """Tail of the post_state_hash chain == hash(final_doc) — same contract as Path A."""
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_three_clips(media)
    proposal = _three_move_proposal(media)
    decisions = {
        "alpha": Decision(accept=True),
        "beta": Decision(accept=False, rejection_reason="rearrange skip the second"),
        "gamma": Decision(accept=True),
    }
    new_doc, outcomes = apply_proposal_with_human_decisions(proposal, doc, decisions)
    applied_chain = [o.post_state_hash for o in outcomes if o.applied]
    assert all(h is not None for h in applied_chain)
    assert applied_chain[-1] == document_state_hash(new_doc)


# ===========================================================================
# 2. Qt headless tests — ProposalReviewPane
# ===========================================================================


def _have_qt() -> bool:
    if os.environ.get("WHISPER_NO_QT"):
        return False
    try:
        import PySide6  # noqa: F401
    except ImportError:
        return False
    return True


qt_skip = pytest.mark.skipif(not _have_qt(), reason="PySide6 unavailable / WHISPER_NO_QT set")


@qt_skip
def test_pane_loads_with_synthetic_proposal(qtbot, tmp_path):
    """ProposalReviewPane shows the outline when a proposal exists on disk."""
    from ui_qt.components.proposal_review_pane import ProposalReviewPane

    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_three_clips(media)
    doc_path = tmp_path / "x.transcribe.json"
    doc_path.write_text(json.dumps(doc.to_json(), indent=2), encoding="utf-8")
    proposal = _three_move_proposal(media)
    write_proposal(doc_path, proposal)

    pane = ProposalReviewPane()
    qtbot.addWidget(pane)
    pane.set_document(doc, doc_path)

    # Three move cards present, all pending.
    assert pane.current_proposal is not None
    assert len(pane._move_cards) == 3
    assert all(c.decision is None for c in pane._move_cards)
    # Apply disabled until all decisions made.
    assert pane._apply_btn.isEnabled() is False


@qt_skip
def test_latest_by_default_when_multiple_proposals(qtbot, tmp_path):
    """When the sidecar has multiple proposals, the most recent is selected."""
    from ui_qt.components.proposal_review_pane import ProposalReviewPane

    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_three_clips(media)
    doc_path = tmp_path / "x.transcribe.json"
    doc_path.write_text(json.dumps(doc.to_json(), indent=2), encoding="utf-8")

    p_old = Proposal(
        parent_document_state_hash="DOC_HASH_v1",
        moves=(_move(media, 0.0, 5.0, "rearrange old", "old"),),
        proposal_id="20260101T000000-old00000",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    p_new = Proposal(
        parent_document_state_hash="DOC_HASH_v1",
        moves=(_move(media, 5.0, 10.0, "rearrange new", "new"),),
        proposal_id="20260201T000000-new00000",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    write_proposal(doc_path, p_old)
    write_proposal(doc_path, p_new)

    pane = ProposalReviewPane()
    qtbot.addWidget(pane)
    pane.set_document(doc, doc_path)

    # Most recent (chronological tail) is selected.
    assert pane.current_proposal is not None
    assert pane.current_proposal.proposal_id == "20260201T000000-new00000"
    # Picker would be shown when the parent is rendered (both items
    # present, not explicitly hidden — the visibility-vs-hidden
    # distinction documented in the reject-input test applies here too).
    assert pane._picker.isHidden() is False
    assert pane._picker.count() == 2


@qt_skip
def test_apply_button_disabled_until_all_decisions_made(qtbot, tmp_path):
    from ui_qt.components.proposal_review_pane import ProposalReviewPane

    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_three_clips(media)
    doc_path = tmp_path / "x.transcribe.json"
    doc_path.write_text(json.dumps(doc.to_json(), indent=2), encoding="utf-8")
    write_proposal(doc_path, _three_move_proposal(media))

    pane = ProposalReviewPane()
    qtbot.addWidget(pane)
    pane.set_document(doc, doc_path)

    # No decisions yet → disabled.
    assert pane._apply_btn.isEnabled() is False

    # Accept move 0 only → still disabled.
    pane._move_cards[0]._on_accept_clicked()
    assert pane._apply_btn.isEnabled() is False

    # Accept move 1 → still disabled.
    pane._move_cards[1]._on_accept_clicked()
    assert pane._apply_btn.isEnabled() is False

    # Accept move 2 → now enabled.
    pane._move_cards[2]._on_accept_clicked()
    assert pane._apply_btn.isEnabled() is True


@qt_skip
def test_reject_input_enables_apply_only_when_reason_long_enough(qtbot, tmp_path):
    from ui_qt.components.proposal_review_pane import ProposalReviewPane

    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_three_clips(media)
    doc_path = tmp_path / "x.transcribe.json"
    doc_path.write_text(json.dumps(doc.to_json(), indent=2), encoding="utf-8")
    # Single-move proposal so we can isolate the reject behavior.
    proposal = Proposal(
        parent_document_state_hash="DOC_HASH_v1",
        moves=(_move(media, 0.0, 5.0, "rearrange single", "only"),),
    )
    write_proposal(doc_path, proposal)

    pane = ProposalReviewPane()
    qtbot.addWidget(pane)
    pane.set_document(doc, doc_path)
    card = pane._move_cards[0]

    # Click Reject — input appears (not explicitly hidden), Submit
    # disabled (empty text). ``isHidden()`` is what we check because
    # ``isVisible`` requires the parent chain to be shown — fragile in
    # qtbot's headless mode where pane.show() isn't called.
    card._on_reject_clicked()
    assert card._reason_input.isHidden() is False
    assert card._reason_submit.isEnabled() is False
    assert pane._apply_btn.isEnabled() is False  # decision not committed yet

    # Type 4 chars — still disabled (under 8).
    card._reason_input.setText("ugh!")
    assert card._reason_submit.isEnabled() is False

    # Type 12-char free-form rationale — Submit enables.
    card._reason_input.setText("too rambling")
    assert card._reason_submit.isEnabled() is True
    assert pane._apply_btn.isEnabled() is False  # still uncommitted

    # Submit → decision committed → Apply enables.
    card._on_submit_reject()
    assert card.decision is not None
    assert card.decision.accept is False
    assert card.decision.rejection_reason == "too rambling"
    assert pane._apply_btn.isEnabled() is True


@qt_skip
def test_staleness_banner_appears_on_parent_hash_mismatch(qtbot, tmp_path):
    from ui_qt.components.proposal_review_pane import ProposalReviewPane

    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    # Live doc has hash "DRIFTED"; proposal's parent_hash is "DOC_HASH_v1".
    live_doc = _doc_three_clips(media, source_hash="DRIFTED")
    doc_path = tmp_path / "x.transcribe.json"
    doc_path.write_text(json.dumps(live_doc.to_json(), indent=2), encoding="utf-8")
    write_proposal(doc_path, _three_move_proposal(media, parent_hash="DOC_HASH_v1"))

    pane = ProposalReviewPane()
    qtbot.addWidget(pane)
    pane.set_document(live_doc, doc_path)

    # Banner is shown (not explicitly hidden) because parent_hash !=
    # live source_hash. ``isHidden()`` rather than ``isVisible()`` for
    # the same reason as the reject-input test.
    assert pane._staleness_banner.isHidden() is False
    assert "stale" in pane._staleness_banner.text().lower()


# ===========================================================================
# 3. Integration — round-trip through MCP read_apply_result
# ===========================================================================


@qt_skip
def test_roundtrip_human_rejection_reason_via_mcp_read_apply_result(qtbot, tmp_path):
    """The big one: GUI rejection reason flows to MCP read_apply_result.

    Drives the pane (synthetic doc + proposal on disk) the same way the
    real Edit menu does: pane → apply_requested → core path B → write
    apply-result. Then reads the result back via the MCP tool to
    confirm the human reason survived the wire trip.
    """
    from mcp_server.schemas import ReadApplyResultRequest
    from mcp_server.tools.proposals import read_apply_result
    from ui_qt.components.proposal_review_pane import ProposalReviewPane

    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_three_clips(media)
    doc_path = tmp_path / "x.transcribe.json"
    doc_path.write_text(json.dumps(doc.to_json(), indent=2), encoding="utf-8")

    materialized, _ = write_proposal(doc_path, _three_move_proposal(media))

    pane = ProposalReviewPane()
    qtbot.addWidget(pane)
    pane.set_document(doc, doc_path)

    # Mark decisions: accept/reject-with-reason/accept.
    pane._move_cards[0]._on_accept_clicked()
    pane._move_cards[1]._on_reject_clicked()
    pane._move_cards[1]._reason_input.setText("the segue doesn't work")
    pane._move_cards[1]._on_submit_reject()
    pane._move_cards[2]._on_accept_clicked()

    # Drive the apply directly (bypassing the Qt signal would let us
    # mock-host; the production wiring lives in ui_qt.app, which is
    # exercised by the manual smoke test). For the round-trip we just
    # need the apply_result file to land on disk, so we replicate
    # MainWindow's handler shape:
    decisions = pane.collected_decisions()
    assert len(decisions) == 3
    new_doc, outcomes = apply_proposal_with_human_decisions(
        pane.current_proposal, doc, decisions
    )
    apply_result = ApplyResult(
        apply_result_id=_new_id(),
        proposal_id=materialized.proposal_id or "",
        created_at=datetime.now(UTC),
        document_pre_hash=document_state_hash(doc),
        document_post_hash=document_state_hash(new_doc),
        move_ids_filter=None,
        outcomes=tuple(outcomes),
    )
    write_apply_result(doc_path, apply_result)
    # Persist post-state doc so MCP layer can re-load.
    doc_path.write_text(json.dumps(new_doc.to_json(), indent=2), encoding="utf-8")

    # Read it back via the MCP tool — this is the wire that the LLM
    # sees on its next turn.
    res = asyncio.run(
        read_apply_result(
            ReadApplyResultRequest(
                json_path=str(doc_path),
                apply_result_id=apply_result.apply_result_id,
            )
        )
    )
    assert res.proposal_id == materialized.proposal_id
    out_b = next(o for o in res.outcomes if o.move_id == "beta")
    assert out_b.applied is False
    assert out_b.skipped is False
    assert out_b.error_code == "REJECTED_HUMAN"
    assert out_b.human_rejection_reason == "the segue doesn't work"
    # Other moves are clean applied/no-reason.
    out_a = next(o for o in res.outcomes if o.move_id == "alpha")
    assert out_a.applied is True
    assert out_a.human_rejection_reason is None


# ===========================================================================
# 4. Phase 6b post-smoke cleanup — freshness signal + dock state hygiene
# ===========================================================================


@qt_skip
def test_proposals_changed_signal_carries_fresh_proposal_id(qtbot, tmp_path):
    """The pane emits ``(count, latest_fresh_proposal_id)`` on reload.

    A proposal is "fresh" if no apply-result exists for the doc, or
    its created_at is newer than the most recent apply-result. With
    one un-applied proposal on disk, the second arg is the proposal's
    id; once an apply-result lands that's newer than the proposal,
    the second arg becomes empty string.
    """
    from ui_qt.components.proposal_review_pane import ProposalReviewPane

    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_three_clips(media)
    doc_path = tmp_path / "x.transcribe.json"
    doc_path.write_text(json.dumps(doc.to_json(), indent=2), encoding="utf-8")
    materialized, _ = write_proposal(doc_path, _three_move_proposal(media))

    pane = ProposalReviewPane()
    qtbot.addWidget(pane)
    received: list[tuple[int, str]] = []
    pane.proposals_changed.connect(lambda c, fid: received.append((c, fid)))
    pane.set_document(doc, doc_path)

    assert received, "proposals_changed should fire on set_document"
    count, fresh_id = received[-1]
    assert count == 1
    assert fresh_id == materialized.proposal_id

    # Land an apply-result that's newer than the proposal — proposal
    # is no longer fresh.
    apply_result = ApplyResult(
        apply_result_id=_new_id(),
        proposal_id=materialized.proposal_id or "",
        created_at=datetime.now(UTC),
        document_pre_hash="pre",
        document_post_hash="post",
        move_ids_filter=None,
        outcomes=(),
    )
    write_apply_result(doc_path, apply_result)
    pane.reload_proposals()
    count, fresh_id = received[-1]
    assert count == 1
    assert fresh_id == "", "proposal preceding the apply-result is no longer fresh"


@qt_skip
def test_set_document_resets_decision_state_visually(qtbot, tmp_path):
    """6b3-A investigation: rebinding the pane recreates move cards.

    When ``set_document`` is called on a pane that previously had
    decisions made against a different proposal, the dock should
    reset to a clean "review me" frame — no stale decision indicators
    persisting across binds. The implementation guarantees this by
    recreating ``_move_cards`` from scratch in ``_render_outline``.
    """
    from ui_qt.components.proposal_review_pane import ProposalReviewPane

    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc_three_clips(media)
    doc_path = tmp_path / "x.transcribe.json"
    doc_path.write_text(json.dumps(doc.to_json(), indent=2), encoding="utf-8")
    write_proposal(doc_path, _three_move_proposal(media))

    pane = ProposalReviewPane()
    qtbot.addWidget(pane)
    pane.show()  # spec calls for real pane.show() — not offscreen-only.
    pane.set_document(doc, doc_path)
    # Decide on the first card.
    pane._move_cards[0]._on_accept_clicked()
    assert pane._move_cards[0].decision is not None
    first_card_id = id(pane._move_cards[0])

    # Re-bind to the same doc — cards should be freshly built; no
    # decision should carry over.
    pane.set_document(doc, doc_path)
    assert all(c.decision is None for c in pane._move_cards)
    # Card identity changed too (recreated, not mutated in place).
    assert id(pane._move_cards[0]) != first_card_id
    assert pane._apply_btn.isEnabled() is False
