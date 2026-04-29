"""Proposal lifecycle tools — propose / list / read / apply / list-results / read-result.

Phase 6b-2 surface. Every tool here translates between the MCP wire
shape (Pydantic models in :mod:`mcp_server.schemas`) and the internal
core types (:class:`core.editing.MoveClipSpan`,
:class:`core.proposal.Proposal` / :class:`MoveOutcome` /
:class:`ApplyResult`). The translation layer is intentionally
explicit — the dataclass surface stays Pydantic-free, and any future
changes to the wire shape land here, not in ``core/``.

Path A end-to-end: load doc → propose_moves → read_proposal →
apply_proposal → read_apply_result. The proposal/apply-result files
live in a sidecar dir at ``<doc>.proposals/`` (see
:func:`core.proposal.proposals_dir_for_document`).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from core.edit_events import ClipAnchor, is_valid_reason
from core.editing import MoveClipSpan
from core.proposal import (
    ApplyResult,
    DuplicateMoveIdError,
    InvalidMoveIdError,
    MoveOutcome,
    Proposal,
    StaleProposalError,
    _new_id,
    document_state_hash,
    latest_apply_result_id_for,
    list_apply_results_for_document,
    write_apply_result,
    write_proposal,
)
from core.proposal import (
    apply_proposal as core_apply_proposal,
)
from core.proposal import (
    list_proposals as core_list_proposals,
)
from core.proposal import (
    read_apply_result as core_read_apply_result,
)
from core.proposal import (
    read_proposal as core_read_proposal,
)
from mcp_server import errors
from mcp_server.schemas import (
    ApplyProposalRequest,
    ApplyProposalResult,
    ApplyResultSummary,
    ClipAnchorOut,
    ClipAnchorRequest,
    ListApplyResultsRequest,
    ListApplyResultsResult,
    ListProposalsRequest,
    ListProposalsResult,
    MoveOut,
    MoveOutcomeOut,
    MoveRequest,
    ProposalSummary,
    ProposeMovesRequest,
    ProposeMovesResult,
    ReadApplyResultRequest,
    ReadApplyResultResult,
    ReadProposalRequest,
    ReadProposalResult,
)
from mcp_server.tools.document import _load_document, _save_document

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Wire ↔ core translation
# ---------------------------------------------------------------------------


def _anchor_from_request(req: ClipAnchorRequest) -> ClipAnchor:
    return ClipAnchor(
        source_path=Path(req.source_path),
        source_start=float(req.source_start_s),
        source_end=float(req.source_end_s),
    )


def _anchor_to_out(a: ClipAnchor) -> ClipAnchorOut:
    return ClipAnchorOut(
        source_path=str(a.source_path),
        source_start_s=float(a.source_start),
        source_end_s=float(a.source_end),
    )


def _move_from_request(req: MoveRequest) -> MoveClipSpan:
    """Translate a wire-shape MoveRequest into a core MoveClipSpan.

    Reason validation is owed to the core layer
    (:meth:`MoveClipSpan.__post_init__` calls :func:`is_valid_reason`).
    We pre-check here so the failure surfaces as the stable
    ``INVALID_REASON`` MCP code rather than as a generic
    :class:`ValueError`.
    """
    if not is_valid_reason(req.reason):
        errors.raise_mcp(
            errors.INVALID_REASON,
            f"reason {req.reason!r} is not a valid rationale; "
            "see core.edit_events.is_valid_reason for accepted patterns",
            data={"move_id": req.move_id, "reason": req.reason},
        )
    span = tuple(_anchor_from_request(a) for a in req.span)
    target = None if req.target is None else _anchor_from_request(req.target)
    return MoveClipSpan(
        span=span,
        target=target,
        reason=req.reason,
        move_id=req.move_id,
    )


def _move_to_out(m: MoveClipSpan) -> MoveOut:
    return MoveOut(
        move_id=m.move_id or "",
        span=[_anchor_to_out(a) for a in m.span],
        target=None if m.target is None else _anchor_to_out(m.target),
        reason=m.reason,
    )


def _outcome_to_out(o: MoveOutcome) -> MoveOutcomeOut:
    return MoveOutcomeOut(
        move_id=o.move_id,
        index=o.index,
        applied=o.applied,
        skipped=o.skipped,
        error=o.error,
        error_code=o.error_code,
        post_state_hash=o.post_state_hash,
        human_rejection_reason=o.human_rejection_reason,
    )


def _count_outcomes(outcomes: tuple[MoveOutcome, ...]) -> tuple[int, int, int]:
    """Return ``(applied, skipped, failed)`` counts."""
    applied = sum(1 for o in outcomes if o.applied)
    skipped = sum(1 for o in outcomes if o.skipped)
    failed = sum(
        1 for o in outcomes if not o.applied and not o.skipped and o.error is not None
    )
    return applied, skipped, failed


# ---------------------------------------------------------------------------
# Tool: propose_moves
# ---------------------------------------------------------------------------


async def propose_moves(req: ProposeMovesRequest) -> ProposeMovesResult:
    """Validate ``req.moves`` against the document, persist a proposal file."""
    doc, doc_path, _ = _load_document(req.json_path)

    # Translate moves; INVALID_REASON / INVALID_PROPOSAL surface here.
    moves: list[MoveClipSpan] = []
    for i, m in enumerate(req.moves):
        try:
            moves.append(_move_from_request(m))
        except ValidationError as exc:
            errors.raise_mcp(
                errors.INVALID_PROPOSAL,
                f"moves[{i}] failed validation: {exc}",
                data={"index": i, "errors": exc.errors()},
            )
        except ValueError as exc:
            # MoveClipSpan __post_init__ raises ValueError for empty span,
            # invalid reason, or self-cycle. INVALID_REASON has been
            # pre-checked above; the remaining messages map to
            # INVALID_PROPOSAL.
            msg = str(exc)
            if "rationale" in msg:
                errors.raise_mcp(
                    errors.INVALID_REASON,
                    msg,
                    data={"index": i},
                )
            errors.raise_mcp(
                errors.INVALID_PROPOSAL,
                f"moves[{i}] is invalid: {msg}",
                data={"index": i},
            )

    proposal = Proposal(
        parent_document_state_hash=document_state_hash(doc),
        moves=tuple(moves),
    )
    try:
        materialized, out_path = write_proposal(doc_path, proposal)
    except DuplicateMoveIdError as exc:
        errors.raise_mcp(
            errors.DUPLICATE_MOVE_ID,
            str(exc),
        )

    _LOG.info(
        "propose_moves: wrote proposal_id=%s moves=%d to %s",
        materialized.proposal_id,
        len(materialized.moves),
        out_path,
    )
    return ProposeMovesResult(
        proposal_id=materialized.proposal_id or "",
        proposal_path=str(out_path),
        parent_document_state_hash=materialized.parent_document_state_hash,
        move_ids=[m.move_id or "" for m in materialized.moves],
    )


# ---------------------------------------------------------------------------
# Tool: list_proposals
# ---------------------------------------------------------------------------


async def list_proposals(req: ListProposalsRequest) -> ListProposalsResult:
    """Return every proposal in the document's sidecar dir + their latest
    apply-result id (or ``None`` if none has been written)."""
    _, doc_path, _ = _load_document(req.json_path)
    proposals = core_list_proposals(doc_path)
    summaries: list[ProposalSummary] = []
    for p in proposals:
        latest = (
            latest_apply_result_id_for(doc_path, p.proposal_id)
            if p.proposal_id is not None
            else None
        )
        summaries.append(
            ProposalSummary(
                proposal_id=p.proposal_id or "",
                parent_document_state_hash=p.parent_document_state_hash,
                move_count=len(p.moves),
                created_at=(
                    p.created_at.isoformat() if p.created_at is not None
                    else datetime.fromtimestamp(0, UTC).isoformat()
                ),
                latest_apply_result_id=latest,
            )
        )
    return ListProposalsResult(proposals=summaries)


# ---------------------------------------------------------------------------
# Tool: read_proposal
# ---------------------------------------------------------------------------


async def read_proposal(req: ReadProposalRequest) -> ReadProposalResult:
    """Round-trip a stored proposal back over the wire."""
    _, doc_path, _ = _load_document(req.json_path)
    try:
        p = core_read_proposal(doc_path, req.proposal_id)
    except FileNotFoundError as exc:
        errors.raise_mcp(
            errors.PROPOSAL_NOT_FOUND,
            str(exc),
            data={"proposal_id": req.proposal_id},
        )
    return ReadProposalResult(
        proposal_id=p.proposal_id or req.proposal_id,
        parent_document_state_hash=p.parent_document_state_hash,
        created_at=(
            p.created_at.isoformat() if p.created_at is not None
            else datetime.fromtimestamp(0, UTC).isoformat()
        ),
        moves=[_move_to_out(m) for m in p.moves],
    )


# ---------------------------------------------------------------------------
# Tool: apply_proposal
# ---------------------------------------------------------------------------


async def apply_proposal(req: ApplyProposalRequest) -> ApplyProposalResult:
    """Apply (a subset of) a proposal's moves and persist both the document
    and the apply-result file.

    Every successful move appends to ``Document.edit_log`` (recorded
    via :class:`MoveClipSpan.apply` inside core). The document JSON
    is rewritten when at least one move applied; for an all-skipped
    or all-failed run the document file is left untouched, but the
    apply-result file is always written so the run is auditable.
    """
    doc, doc_path, _ = _load_document(req.json_path)
    pre_hash = document_state_hash(doc)

    try:
        proposal = core_read_proposal(doc_path, req.proposal_id)
    except FileNotFoundError as exc:
        errors.raise_mcp(
            errors.PROPOSAL_NOT_FOUND,
            str(exc),
            data={"proposal_id": req.proposal_id},
        )

    try:
        new_doc, outcomes = core_apply_proposal(doc, proposal, move_ids=req.move_ids)
    except StaleProposalError as exc:
        errors.raise_mcp(
            errors.STALE_PROPOSAL,
            str(exc),
            data={
                "proposal_id": req.proposal_id,
                "parent_document_state_hash": (
                    proposal.parent_document_state_hash
                ),
                "live_document_state_hash": document_state_hash(doc),
            },
        )
    except InvalidMoveIdError as exc:
        errors.raise_mcp(
            errors.INVALID_MOVE_ID,
            str(exc),
            data={"proposal_id": req.proposal_id, "move_ids": req.move_ids},
        )

    applied_count, skipped_count, failed_count = _count_outcomes(tuple(outcomes))
    if applied_count > 0:
        _save_document(new_doc, doc_path)
        _LOG.info(
            "apply_proposal: wrote %d applied move(s) for proposal_id=%s to %s",
            applied_count,
            req.proposal_id,
            doc_path,
        )

    apply_result_id = _new_id()
    apply_result = ApplyResult(
        apply_result_id=apply_result_id,
        proposal_id=req.proposal_id,
        created_at=datetime.now(UTC),
        document_pre_hash=pre_hash,
        document_post_hash=document_state_hash(new_doc),
        move_ids_filter=(
            None if req.move_ids is None else tuple(str(x) for x in req.move_ids)
        ),
        outcomes=tuple(outcomes),
    )
    out_path = write_apply_result(doc_path, apply_result)

    return ApplyProposalResult(
        apply_result_id=apply_result_id,
        apply_result_path=str(out_path),
        proposal_id=req.proposal_id,
        document_pre_hash=apply_result.document_pre_hash,
        document_post_hash=apply_result.document_post_hash,
        applied_count=applied_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
        outcomes=[_outcome_to_out(o) for o in apply_result.outcomes],
    )


# ---------------------------------------------------------------------------
# Tool: list_apply_results
# ---------------------------------------------------------------------------


async def list_apply_results(
    req: ListApplyResultsRequest,
) -> ListApplyResultsResult:
    _, doc_path, _ = _load_document(req.json_path)
    items = list_apply_results_for_document(doc_path, proposal_id=req.proposal_id)
    summaries: list[ApplyResultSummary] = []
    for ar in items:
        applied, skipped, failed = _count_outcomes(ar.outcomes)
        summaries.append(
            ApplyResultSummary(
                apply_result_id=ar.apply_result_id,
                proposal_id=ar.proposal_id,
                created_at=ar.created_at.isoformat(),
                applied_count=applied,
                skipped_count=skipped,
                failed_count=failed,
            )
        )
    return ListApplyResultsResult(apply_results=summaries)


# ---------------------------------------------------------------------------
# Tool: read_apply_result
# ---------------------------------------------------------------------------


async def read_apply_result(req: ReadApplyResultRequest) -> ReadApplyResultResult:
    _, doc_path, _ = _load_document(req.json_path)
    try:
        ar = core_read_apply_result(doc_path, req.apply_result_id)
    except FileNotFoundError as exc:
        errors.raise_mcp(
            errors.APPLY_RESULT_NOT_FOUND,
            str(exc),
            data={"apply_result_id": req.apply_result_id},
        )
    return ReadApplyResultResult(
        apply_result_id=ar.apply_result_id,
        proposal_id=ar.proposal_id,
        created_at=ar.created_at.isoformat(),
        document_pre_hash=ar.document_pre_hash,
        document_post_hash=ar.document_post_hash,
        move_ids_filter=(
            None if ar.move_ids_filter is None else list(ar.move_ids_filter)
        ),
        outcomes=[_outcome_to_out(o) for o in ar.outcomes],
    )


