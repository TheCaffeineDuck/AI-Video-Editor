"""Move proposals — a serializable batch of :class:`MoveClipSpan` ops.

A :class:`Proposal` lets an MCP client (or future LLM-driven tool)
author a batch of rearrangement operations against a known parent
:class:`~core.document.Document`, hand the JSON to a human reviewer,
and apply whichever subset the human accepts. The semantics:

- **Authoring against a parent.** ``parent_document_state_hash``
  records :func:`document_state_hash` of the doc at proposal time —
  a content hash over the full Document JSON (including ``edit_log``).
  ``apply_proposal`` refuses to apply if the live doc's
  ``document_state_hash`` differs — the proposal is stale, and the
  safe action is to re-author against the current state. This is a
  guard against the common "two clients edited the same project at
  once" failure as well as against intra-doc edits between authoring
  and apply.

- **Schema migration v1→v2.** v1 proposals used a field literally
  named ``parent_document_hash`` whose value was the doc's
  ``source_hash`` (a media-fingerprint, not a content hash). That
  meant intra-doc edits did not invalidate proposals, which is the
  bug the v2 rename fixes. v1 proposals on disk still load — the
  loader populates ``parent_document_state_hash`` with the value of
  the old field for compatibility, and the stale check is *suppressed*
  for v1 proposals (the old hash isn't comparable to a v2 content
  hash). v1 proposals are therefore "forever-stale-uncheckable":
  they apply unconditionally; humans are responsible for reviewing
  their relevance.

- **Per-move all-or-nothing, partial across moves.** Each move in
  the ``moves`` tuple is attempted on the post-prior state. If move
  N fails (span resolution / target resolution / contiguity), move
  N+1 still runs against the state produced by accepting move N-1.
  The :class:`MoveOutcome` list returned by ``apply_proposal``
  records, per move, whether it landed.

- **Selective apply via move_ids.** 6b-2 added a ``move_ids`` filter
  to ``apply_proposal`` so a human reviewer can accept a *subset* of
  the proposal's moves. The filter is order-preserving — moves apply
  in *proposal* order, not in the order the ids appear in the
  parameter. Unselected moves get a ``MoveOutcome`` with
  ``skipped=True``; the outcome list is always the same length as
  ``proposal.moves`` (full coverage).

- **Chain-of-custody hash.** Every successfully-applied move records
  ``post_state_hash`` — the sha256 of the document's full JSON
  (including ``edit_log``) at the moment the move landed. A reviewer
  reading the apply-result file can chain hash N to "the doc state
  produced after applying move N" deterministically.

- **Persistence: 6b-2 lands it.** The proposal and its apply-results
  are written to a sidecar directory next to the document
  (``<doc>.proposals/``) with timestamp-prefixed ids. The MCP layer
  in ``mcp_server/tools/proposals.py`` exposes
  ``propose_moves`` / ``list_proposals`` / ``read_proposal`` /
  ``apply_proposal`` / ``list_apply_results`` / ``read_apply_result``
  on top of this file layer.

The "human accepts a subset" workflow above is implemented via the
``move_ids`` filter on ``apply_proposal``. 6b-3 will pair this with
a GUI proposal-review surface and add ``human_rejection_reason``
fill-in for unselected moves.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.document import Document
from core.edit_events import ClipAnchor, is_valid_reason
from core.editing import MoveClipSpan, SpanResolutionError

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StaleProposalError(ValueError):
    """Raised when a proposal's ``parent_document_state_hash`` doesn't
    match the live document's :func:`document_state_hash`.

    The intended remediation is to discard the proposal and re-author
    against the current document state. This is a hard error — we do
    not silently apply a proposal whose authoring assumptions no
    longer hold, because the moves' anchors were chosen against a
    different timeline.
    """


class DuplicateMoveIdError(ValueError):
    """Raised when a Proposal would contain two moves with the same move_id.

    Surfaces as the ``DUPLICATE_MOVE_ID`` MCP error code at the tool
    boundary. Internal callers (file load, in-memory construction)
    use the exception directly.
    """


class InvalidMoveIdError(ValueError):
    """Raised when ``apply_proposal`` is given a ``move_ids`` filter
    that references an id not present in the proposal.

    Surfaces as ``INVALID_MOVE_ID`` at the MCP boundary.
    """


# ---------------------------------------------------------------------------
# Outcome dataclass
# ---------------------------------------------------------------------------


# Stable error codes for MoveOutcome.error_code. These mirror the MCP
# server's stable codes (mcp_server/errors.py) so an apply-result
# file can be inspected without re-deriving meaning. The set is small
# on purpose — clients branch on the *category* of failure, not the
# exact message.
MOVE_OUTCOME_ERROR_CODES: tuple[str, ...] = (
    "SPAN_NOT_FOUND",
    "SPAN_NOT_CONTIGUOUS",
    "TARGET_NOT_FOUND",
    "TARGET_INSIDE_SPAN",
    "INVALID_REASON",
    # 6b-3: human-rejection path. Set when ``apply_proposal_with_human_decisions``
    # records a ``Decision(accept=False)``; the human's free-form rationale
    # rides in ``MoveOutcome.human_rejection_reason``.
    "REJECTED_HUMAN",
    "OTHER",
)


@dataclass(frozen=True)
class MoveOutcome:
    """Per-move result returned by :func:`apply_proposal`.

    The outcome list is always the same length as the proposal's
    ``moves`` tuple — every move gets exactly one outcome, regardless
    of whether it was selected by the ``move_ids`` filter, attempted,
    or skipped.

    Field semantics:

    - ``move_id`` / ``index``: identity. ``index`` is the position in
      ``proposal.moves`` (zero-based); ``move_id`` is the proposal's
      stable id for this move.
    - ``applied``: True iff the move successfully changed the running
      document. False for both "skipped" and "errored" moves.
    - ``skipped``: True iff the move was *not selected* by the
      ``move_ids`` filter (the human said "skip this one"). False
      when ``move_ids is None`` (apply-all) or when the id was
      selected.
    - ``error`` / ``error_code``: failure reason. ``error`` is the
      human-readable message; ``error_code`` is one of
      :data:`MOVE_OUTCOME_ERROR_CODES`. Both ``None`` on success.
    - ``post_state_hash``: chain-of-custody hash of the document
      state *after* applying this move. ``None`` on skip / error
      (no state change). Computed via :func:`document_state_hash`,
      which hashes the full JSON including ``edit_log``.
    - ``human_rejection_reason``: free-form reason a human gave for
      *not* selecting this move. Filled in by 6b-3's GUI flow; in
      6b-2 the field exists but is always ``None`` because the
      MCP-driven flow has no place to author it.
    """

    move_id: str
    index: int
    applied: bool
    skipped: bool = False
    error: str | None = None
    error_code: str | None = None
    post_state_hash: str | None = None
    human_rejection_reason: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "move_id": self.move_id,
            "index": self.index,
            "applied": self.applied,
            "skipped": self.skipped,
            "error": self.error,
            "error_code": self.error_code,
            "post_state_hash": self.post_state_hash,
            "human_rejection_reason": self.human_rejection_reason,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> MoveOutcome:
        return cls(
            move_id=str(data["move_id"]),
            index=int(data["index"]),
            applied=bool(data["applied"]),
            skipped=bool(data.get("skipped", False)),
            error=(None if data.get("error") is None else str(data["error"])),
            error_code=(
                None if data.get("error_code") is None else str(data["error_code"])
            ),
            post_state_hash=(
                None if data.get("post_state_hash") is None
                else str(data["post_state_hash"])
            ),
            human_rejection_reason=(
                None if data.get("human_rejection_reason") is None
                else str(data["human_rejection_reason"])
            ),
        )


# ---------------------------------------------------------------------------
# Proposal dataclass
# ---------------------------------------------------------------------------


PROPOSAL_SCHEMA_VERSION = 2
"""On-disk proposal schema version.

v1 (pre-cleanup): ``parent_document_hash`` field carried the doc's
``source_hash`` (a media fingerprint). Intra-doc edits did not
invalidate proposals — a real bug that ``apply_proposal`` couldn't
catch.

v2 (current): ``parent_document_state_hash`` carries
:func:`document_state_hash` (sha256 over the full Document JSON
including ``edit_log``). Intra-doc edits flip the hash; the stale
check fires on drift.

v1 files still load. Their stale check is suppressed (see
:func:`_check_stale`); the field name on the in-memory dataclass is
``parent_document_state_hash`` for both versions but a v1 file's
value is the old source_hash, never comparable to a v2 content hash.
"""


@dataclass(frozen=True)
class Proposal:
    """A serializable batch of :class:`MoveClipSpan` ops.

    ``parent_document_state_hash`` is :func:`document_state_hash` of
    the document the proposal was authored against. ``None`` opts out
    of the stale check — useful for unit tests, but production callers
    should always set it.

    ``moves`` is the ordered tuple of moves to attempt, applied
    sequentially against the post-prior state.

    ``proposal_id`` and ``created_at`` are filled in at write time
    by :func:`write_proposal` (or its in-memory helper); a Proposal
    constructed directly may leave them ``None`` and they'll be
    assigned on persistence.

    ``schema_version`` records the on-disk version this proposal was
    loaded from (or :data:`PROPOSAL_SCHEMA_VERSION` for in-memory
    proposals). The stale check is suppressed for v1 proposals —
    their ``parent_document_state_hash`` value is a legacy
    ``source_hash`` that isn't comparable to a v2 content hash.
    """

    parent_document_state_hash: str | None
    moves: tuple[MoveClipSpan, ...]
    proposal_id: str | None = None
    created_at: datetime | None = None
    schema_version: int = PROPOSAL_SCHEMA_VERSION

    # ----- JSON IO -------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        """Return a plain-dict representation safe for ``json.dumps``.

        ``proposal_id`` and ``created_at`` are emitted only when set;
        consumer code should treat their absence as "this Proposal
        hasn't been persisted yet." Each move emits its ``move_id``
        verbatim — auto-assignment happens at write time, not here.

        Always emits the v2 shape (``schema_version: 2`` +
        ``parent_document_state_hash``). v1 files round-trip into v2
        on the next save (write-through migration).
        """
        out: dict[str, Any] = {
            "schema_version": PROPOSAL_SCHEMA_VERSION,
            "parent_document_state_hash": self.parent_document_state_hash,
            "moves": [
                {
                    "move_id": m.move_id,
                    "span": [a.to_json() for a in m.span],
                    "target": m.target.to_json() if m.target is not None else None,
                    "reason": m.reason,
                }
                for m in self.moves
            ],
        }
        if self.proposal_id is not None:
            out["proposal_id"] = self.proposal_id
        if self.created_at is not None:
            out["created_at"] = self.created_at.isoformat()
        return out

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Proposal:
        """Reconstruct a :class:`Proposal` from a dict.

        Each move's ``reason`` is re-validated by
        :class:`MoveClipSpan.__post_init__`; an unrecognized reason in
        the JSON raises :class:`ValueError` at load time, not later.
        Duplicate ``move_id`` values raise :class:`DuplicateMoveIdError`.
        """
        moves: list[MoveClipSpan] = []
        for m in data.get("moves", []):
            span = tuple(ClipAnchor.from_json(a) for a in m["span"])
            target_data = m.get("target")
            target = (
                None if target_data is None else ClipAnchor.from_json(target_data)
            )
            move_id_raw = m.get("move_id")
            move_id = None if move_id_raw is None else str(move_id_raw)
            moves.append(
                MoveClipSpan(
                    span=span,
                    target=target,
                    reason=str(m["reason"]),
                    move_id=move_id,
                )
            )
        _check_unique_move_ids(tuple(moves))
        # Schema migration: v1 used ``parent_document_hash`` carrying the
        # doc's source_hash; v2 uses ``parent_document_state_hash``
        # carrying document_state_hash. We accept either field on disk
        # — a v1 file is identified by the absence of ``schema_version``
        # (or ``schema_version == 1``) and the presence of the old
        # field name. The old value is preserved verbatim on the
        # in-memory dataclass; ``_check_stale`` suppresses the check
        # for v1 proposals because their hash isn't comparable.
        version_raw = data.get("schema_version")
        if version_raw is None:
            schema_version = (
                1 if "parent_document_hash" in data else PROPOSAL_SCHEMA_VERSION
            )
        else:
            schema_version = int(version_raw)
        if "parent_document_state_hash" in data:
            parent_hash_raw = data.get("parent_document_state_hash")
        else:
            parent_hash_raw = data.get("parent_document_hash")
        parent_hash = None if parent_hash_raw is None else str(parent_hash_raw)
        proposal_id_raw = data.get("proposal_id")
        created_at_raw = data.get("created_at")
        return cls(
            parent_document_state_hash=parent_hash,
            moves=tuple(moves),
            proposal_id=(None if proposal_id_raw is None else str(proposal_id_raw)),
            created_at=(
                None if created_at_raw is None
                else datetime.fromisoformat(str(created_at_raw))
            ),
            schema_version=schema_version,
        )

    def write(self, path: Path) -> None:
        """Write this Proposal to ``path`` as JSON.

        Note: this is the bare-bones write that 6b-1 used. 6b-2's
        higher-level :func:`write_proposal` assigns ids and timestamps
        before calling this; direct callers are responsible for
        whatever ids they want.
        """
        path.write_text(json.dumps(self.to_json(), indent=2), encoding="utf-8")

    @classmethod
    def read(cls, path: Path) -> Proposal:
        """Read a Proposal from ``path``."""
        return cls.from_json(json.loads(Path(path).read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# Apply-result wire format
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApplyResult:
    """The persisted outcome of one :func:`apply_proposal` run.

    Mirrors :class:`Proposal` on the storage side — every apply gets
    one of these and they live in the same sidecar directory.

    ``apply_result_id`` is timestamp-prefixed so apply-results sort
    chronologically. ``proposal_id`` links back to the proposal this
    apply was for; ``document_post_hash`` is the chain-tail (hash of
    the doc after the *last* applied move, or the input doc if no
    moves applied).
    """

    apply_result_id: str
    proposal_id: str
    created_at: datetime
    document_pre_hash: str
    document_post_hash: str
    move_ids_filter: tuple[str, ...] | None
    outcomes: tuple[MoveOutcome, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "apply_result_id": self.apply_result_id,
            "proposal_id": self.proposal_id,
            "created_at": self.created_at.isoformat(),
            "document_pre_hash": self.document_pre_hash,
            "document_post_hash": self.document_post_hash,
            "move_ids_filter": (
                None if self.move_ids_filter is None
                else list(self.move_ids_filter)
            ),
            "outcomes": [o.to_json() for o in self.outcomes],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ApplyResult:
        filter_raw = data.get("move_ids_filter")
        return cls(
            apply_result_id=str(data["apply_result_id"]),
            proposal_id=str(data["proposal_id"]),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            document_pre_hash=str(data["document_pre_hash"]),
            document_post_hash=str(data["document_post_hash"]),
            move_ids_filter=(
                None if filter_raw is None else tuple(str(x) for x in filter_raw)
            ),
            outcomes=tuple(
                MoveOutcome.from_json(o) for o in data.get("outcomes", [])
            ),
        )


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def document_state_hash(doc: Document) -> str:
    """Compute a deterministic chain-of-custody hash of ``doc``.

    The hash is sha256 over a sort-keys JSON dump of
    :meth:`Document.to_json`. That payload includes ``edit_log`` —
    a move's effect on the document state shows up both in the
    re-ordered ``main_timeline.clips`` and in the appended log entry,
    so both must hash for the chain to be sound.

    Caveat: ``EditEvent.timestamp`` is captured at apply time. Two
    apply runs of the same proposal at different wall-clock times
    will produce different hashes for the *same* logical state.
    That's correct for chain-of-custody (each apply is its own
    history), but means a hash isn't a content-only fingerprint.
    """
    payload = json.dumps(doc.to_json(), sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _check_unique_move_ids(moves: tuple[MoveClipSpan, ...]) -> None:
    """Raise :class:`DuplicateMoveIdError` if two moves share an id.

    ``None`` move_ids are not checked — auto-assignment happens at
    persistence time and assigns sequential ids that are unique by
    construction.
    """
    seen: set[str] = set()
    for m in moves:
        if m.move_id is None:
            continue
        if m.move_id in seen:
            raise DuplicateMoveIdError(
                f"duplicate move_id {m.move_id!r} in proposal"
            )
        seen.add(m.move_id)


def _autoassign_move_ids(moves: tuple[MoveClipSpan, ...]) -> tuple[MoveClipSpan, ...]:
    """Return a copy of ``moves`` where every ``move_id`` is set.

    Existing ids are preserved verbatim. Unset ids are filled with
    sequential ``m000`` / ``m001`` / … picked to avoid collisions
    with already-set ids (e.g., a hand-authored proposal that names
    its first move ``"swap-take-2-take-3"`` and leaves the next two
    ``None`` will get ``m000`` / ``m001`` for those, even though
    ``m000`` is index 0 in the loop).
    """
    used = {m.move_id for m in moves if m.move_id is not None}
    out: list[MoveClipSpan] = []
    counter = 0
    for m in moves:
        if m.move_id is not None:
            out.append(m)
            continue
        while True:
            candidate = f"m{counter:03d}"
            counter += 1
            if candidate not in used:
                used.add(candidate)
                break
        # MoveClipSpan is a (mutable) dataclass — but constructing a
        # fresh instance is cleaner than mutating the field in place.
        out.append(
            MoveClipSpan(
                span=m.span,
                target=m.target,
                reason=m.reason,
                move_id=candidate,
            )
        )
    return tuple(out)


def _new_id() -> str:
    """Return a sortable, mostly-unique id for a proposal or apply-result."""
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    suffix = secrets.token_hex(4)
    return f"{ts}-{suffix}"


# ---------------------------------------------------------------------------
# apply_proposal
# ---------------------------------------------------------------------------


def _classify_error(exc: BaseException) -> tuple[str, str]:
    """Map an apply-time exception to a (error_code, message) pair.

    Resolution errors from :class:`SpanResolutionError` carry an
    operator-grade message that already names the failure mode; we
    pattern-match on the message to assign the structured code.
    Anything else falls into ``"OTHER"`` with the stringified
    exception as the message.
    """
    msg = str(exc)
    if isinstance(exc, SpanResolutionError):
        if "not found" in msg and "target" in msg:
            return "TARGET_NOT_FOUND", msg
        if "not found" in msg:
            return "SPAN_NOT_FOUND", msg
        if "not contiguous" in msg:
            return "SPAN_NOT_CONTIGUOUS", msg
        if "inside the span" in msg:
            return "TARGET_INSIDE_SPAN", msg
        return "OTHER", msg
    if isinstance(exc, ValueError) and "rationale" in msg:
        return "INVALID_REASON", msg
    return "OTHER", msg


def _proposal_move_ids(proposal: Proposal) -> list[str]:
    """Return synthesized move_ids for a proposal — preserving caller-supplied
    ids and minting ``m{index:03d}`` for the unset ones.

    Shared by :func:`apply_proposal` and
    :func:`apply_proposal_with_human_decisions` so both paths name moves
    identically (an outcome's ``move_id`` is the same whether the apply
    came in via Path A or Path B).
    """
    return [
        m.move_id if m.move_id is not None else f"m{i:03d}"
        for i, m in enumerate(proposal.moves)
    ]


def _check_stale(doc: Document, proposal: Proposal) -> None:
    """Raise :class:`StaleProposalError` if the doc has drifted under the proposal.

    v1 proposals (``schema_version == 1``) skip the check entirely:
    their hash is the legacy ``source_hash``, not comparable to a v2
    content hash. v1 proposals are forever-stale-uncheckable; they
    apply unconditionally and humans review for relevance.
    """
    if proposal.schema_version < 2:
        return
    if proposal.parent_document_state_hash is None:
        return
    live_hash = document_state_hash(doc)
    if proposal.parent_document_state_hash != live_hash:
        raise StaleProposalError(
            f"proposal parent_document_state_hash="
            f"{proposal.parent_document_state_hash!r} does not match "
            f"document_state_hash={live_hash!r}; re-author the proposal "
            "against the current document state"
        )


def _apply_one_move(
    move: MoveClipSpan, cur: Document, move_id: str, index: int
) -> tuple[Document, MoveOutcome]:
    """Attempt ``move`` against ``cur``; return ``(post_state, outcome)``.

    On success the post-state is the new document; on failure the
    post-state is unchanged (``cur``) and the outcome carries the
    classified error. Either way the outcome has the right move_id /
    index already filled in; callers don't need to wrap.
    """
    try:
        new_doc = move.apply(cur)
    except (SpanResolutionError, NotImplementedError, ValueError) as exc:
        code, msg = _classify_error(exc)
        return cur, MoveOutcome(
            move_id=move_id,
            index=index,
            applied=False,
            skipped=False,
            error=msg,
            error_code=code,
        )
    return new_doc, MoveOutcome(
        move_id=move_id,
        index=index,
        applied=True,
        skipped=False,
        post_state_hash=document_state_hash(new_doc),
    )


def apply_proposal(
    doc: Document,
    proposal: Proposal,
    move_ids: list[str] | tuple[str, ...] | None = None,
) -> tuple[Document, list[MoveOutcome]]:
    """Apply a :class:`Proposal` to ``doc``.

    Stale check: if ``proposal.parent_document_state_hash`` is set
    and differs from :func:`document_state_hash` of ``doc``, raises
    :class:`StaleProposalError` before any move runs. v1 proposals
    skip the check (forever-stale-uncheckable; see module docstring).

    ``move_ids`` filter (6b-2):

    - ``None`` — apply every move (the 6b-1 default).
    - empty list/tuple — apply nothing; every outcome has
      ``skipped=True``. Useful for "show me what would happen if I
      rejected the whole proposal" UX.
    - non-empty — apply only the listed moves, *in proposal order*.
      The parameter is order-irrelevant: passing
      ``[m_002, m_000]`` is the same as ``[m_000, m_002]``.
      Unselected moves get ``skipped=True``.
      :class:`InvalidMoveIdError` is raised if a listed id isn't in
      the proposal.

    Per-move all-or-nothing, partial across moves: each *selected*
    move is attempted on the running Document. A
    :class:`SpanResolutionError` (or any other ``ValueError`` /
    ``NotImplementedError``) records a failed :class:`MoveOutcome`
    and the loop continues with the *pre-failure* state. A failure
    of move N does *not* prevent move N+1 from being attempted; it
    just isn't given an updated state.

    Returns ``(final_doc, outcomes)`` — ``outcomes`` is parallel to
    ``proposal.moves`` (full coverage). Apply paths set
    ``post_state_hash`` to :func:`document_state_hash` of the
    document *after* the move landed; skip / error paths leave it
    ``None``.

    The auto-id assignment from :func:`write_proposal` is *not* run
    here — the caller (or the MCP layer) is expected to have
    persisted ids. If a proposal carries ``None`` move_ids, the
    outcome's ``move_id`` will be a synthetic ``"m{index}"``.
    """
    _check_stale(doc, proposal)

    proposal_ids = _proposal_move_ids(proposal)
    if move_ids is None:
        selected_indices = set(range(len(proposal.moves)))
    else:
        wanted = set(str(x) for x in move_ids)
        unknown = wanted - set(proposal_ids)
        if unknown:
            raise InvalidMoveIdError(
                f"move_ids reference unknown ids: {sorted(unknown)!r}; "
                f"proposal contains: {proposal_ids!r}"
            )
        selected_indices = {
            i for i, mid in enumerate(proposal_ids) if mid in wanted
        }

    outcomes: list[MoveOutcome] = []
    cur = doc
    for i, move in enumerate(proposal.moves):
        move_id = proposal_ids[i]
        if i not in selected_indices:
            outcomes.append(
                MoveOutcome(
                    move_id=move_id,
                    index=i,
                    applied=False,
                    skipped=True,
                )
            )
            continue
        cur, outcome = _apply_one_move(move, cur, move_id, i)
        outcomes.append(outcome)
    return cur, outcomes


# ---------------------------------------------------------------------------
# Path B — apply_proposal_with_human_decisions (6b-3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """A human reviewer's verdict on one proposed move.

    Used by :func:`apply_proposal_with_human_decisions` to drive the
    Path-B (GUI) apply flow. The reviewer marks every move as either
    accepted (the move runs through the existing apply path) or
    rejected with a free-form rationale (the move is skipped, status
    becomes ``rejected_human``, the reason flows into
    :attr:`MoveOutcome.human_rejection_reason`).

    Validation:

    - ``accept=True`` → ``rejection_reason`` should be ``None``. Any
      string passed alongside ``accept=True`` is silently ignored at
      the apply-result wire layer (it's never read).
    - ``accept=False`` → ``rejection_reason`` is required and must
      pass :func:`~core.edit_events.is_valid_reason`. The validator
      enforces both "non-empty" and the substance check (8-char
      free-form floor or known category prefix).
    """

    accept: bool
    rejection_reason: str | None = None


def apply_proposal_with_human_decisions(
    proposal: Proposal,
    document: Document,
    decisions: dict[str, Decision],
) -> tuple[Document, list[MoveOutcome]]:
    """Apply a proposal under per-move human verdicts (Path B).

    Counterpart to :func:`apply_proposal` that lets a GUI reviewer
    *accept* or *reject-with-reason* each move individually. The two
    paths share machinery (stale check, move-id resolution, single-
    move apply, outcome shape, post_state_hash chain). The only
    structural difference is the per-move outcome:

    - ``decisions[move_id].accept = True`` → the move runs through
      the existing apply path. On success, ``applied=True`` and
      ``post_state_hash`` is filled. On apply-time failure (span
      not found, etc.) the outcome carries ``error_code`` per
      :func:`_classify_error`.
    - ``decisions[move_id].accept = False`` → the move is *not*
      attempted; the outcome has ``applied=False``, ``skipped=False``,
      ``error_code = "REJECTED_HUMAN"``, and
      ``human_rejection_reason`` populated. The running document is
      not advanced.
    - ``move_id`` not present in ``decisions`` → the outcome is
      ``skipped=True`` (matches Path A's "unselected" semantics).

    Per the spec, status names map to MoveOutcome shape as:

    - ``"applied"`` — ``applied=True``, ``skipped=False``.
    - ``"rejected_human"`` — ``applied=False``, ``skipped=False``,
      ``error_code="REJECTED_HUMAN"``, ``human_rejection_reason``
      set.
    - ``"rejected_system"`` — ``applied=False``, ``skipped=False``,
      ``error_code`` set to one of the resolution-error codes
      (``SPAN_NOT_FOUND`` etc.).
    - ``"skipped"`` — ``applied=False``, ``skipped=True``.

    Validation: every ``Decision(accept=False)`` MUST carry a
    ``rejection_reason`` that satisfies :func:`is_valid_reason`. If
    any reject-decision is missing or carries an invalid reason,
    raises :class:`ValueError` *before* any move runs (no partial
    write on validation failure).

    Stale check fires before the validation pass, matching Path A
    (a stale proposal is the most actionable failure to surface
    first).

    Returns ``(final_doc, outcomes)`` — ``outcomes`` is parallel to
    ``proposal.moves`` (full coverage), exactly like Path A.
    """
    _check_stale(document, proposal)

    proposal_ids = _proposal_move_ids(proposal)
    proposal_id_set = set(proposal_ids)

    # Validate decisions up-front. We accept decisions keyed by move_ids
    # not in the proposal (silently ignored) so a stale GUI cache
    # doesn't break the call; the spec is explicit that the apply only
    # consults what's *in* the proposal.
    for move_id, decision in decisions.items():
        if move_id not in proposal_id_set:
            continue
        if decision.accept:
            continue
        reason = decision.rejection_reason
        if reason is None or not str(reason).strip():
            raise ValueError(
                f"Decision for move_id={move_id!r} rejects the move but "
                "carries no rejection_reason"
            )
        if not is_valid_reason(reason):
            raise ValueError(
                f"Decision for move_id={move_id!r} has rejection_reason "
                f"{reason!r} which is not a valid rationale; see "
                "core.edit_events.is_valid_reason for accepted patterns"
            )

    outcomes: list[MoveOutcome] = []
    cur = document
    for i, move in enumerate(proposal.moves):
        move_id = proposal_ids[i]
        decision = decisions.get(move_id)
        if decision is None:
            outcomes.append(
                MoveOutcome(
                    move_id=move_id,
                    index=i,
                    applied=False,
                    skipped=True,
                )
            )
            continue
        if not decision.accept:
            outcomes.append(
                MoveOutcome(
                    move_id=move_id,
                    index=i,
                    applied=False,
                    skipped=False,
                    error_code="REJECTED_HUMAN",
                    human_rejection_reason=decision.rejection_reason,
                )
            )
            continue
        cur, outcome = _apply_one_move(move, cur, move_id, i)
        outcomes.append(outcome)
    return cur, outcomes


# ---------------------------------------------------------------------------
# Sidecar layout / persistence
# ---------------------------------------------------------------------------


_PROPOSAL_DIR_SUFFIX = ".proposals"


def proposals_dir_for_document(document_path: Path) -> Path:
    """Return the sidecar directory for ``<document_path>.proposals``.

    Mirrors the editor's ``<source_dir>/edit/`` direction (see
    docs/PRODUCTION_RULES.md "Output isolation"): keep derived
    artifacts in a predictable subdirectory rather than littering
    the source folder. 6b-2 places the directory next to the
    document file rather than nesting under ``<source_dir>/edit/``
    because the existing 4f code still writes the document JSON
    sidecar-style, and moving everything to ``edit/`` is its own
    project.
    """
    p = Path(document_path)
    return p.with_name(p.name + _PROPOSAL_DIR_SUFFIX)


def _proposal_path(dir_: Path, proposal_id: str) -> Path:
    return dir_ / f"{proposal_id}.proposal.json"


def _apply_result_path(dir_: Path, apply_result_id: str) -> Path:
    return dir_ / f"{apply_result_id}.apply-result.json"


def write_proposal(document_path: Path, proposal: Proposal) -> tuple[Proposal, Path]:
    """Persist ``proposal`` next to ``document_path`` and return
    ``(materialized_proposal, proposal_file_path)``.

    "Materialized" means: ``proposal_id`` is assigned (a fresh id
    via :func:`_new_id` if the input didn't carry one),
    ``created_at`` is set to ``datetime.now(UTC)``, and every move's
    ``move_id`` is non-``None`` (sequential ``m000``-style if the
    author left it blank). The on-disk JSON reflects the
    materialized form.
    """
    dir_ = proposals_dir_for_document(document_path)
    dir_.mkdir(parents=True, exist_ok=True)
    moves_with_ids = _autoassign_move_ids(proposal.moves)
    _check_unique_move_ids(moves_with_ids)
    materialized = Proposal(
        parent_document_state_hash=proposal.parent_document_state_hash,
        moves=moves_with_ids,
        proposal_id=proposal.proposal_id or _new_id(),
        created_at=proposal.created_at or datetime.now(UTC),
        schema_version=PROPOSAL_SCHEMA_VERSION,
    )
    out_path = _proposal_path(dir_, materialized.proposal_id or "unset")
    out_path.write_text(
        json.dumps(materialized.to_json(), indent=2),
        encoding="utf-8",
    )
    return materialized, out_path


def read_proposal(document_path: Path, proposal_id: str) -> Proposal:
    """Load the proposal with ``proposal_id`` from the document's sidecar dir.

    Raises :class:`FileNotFoundError` if the proposal directory or
    the named file is missing.
    """
    dir_ = proposals_dir_for_document(document_path)
    p = _proposal_path(dir_, proposal_id)
    if not p.is_file():
        raise FileNotFoundError(f"proposal {proposal_id!r} not found at {p}")
    return Proposal.from_json(json.loads(p.read_text(encoding="utf-8")))


def list_proposals(document_path: Path) -> list[Proposal]:
    """Return every proposal in the document's sidecar directory.

    Returns an empty list when the directory doesn't exist (no
    proposals authored yet) — the absence of a directory is not an
    error. Proposals sort by ``proposal_id`` ascending; since ids
    are timestamp-prefixed, that's chronological.
    """
    dir_ = proposals_dir_for_document(document_path)
    if not dir_.is_dir():
        return []
    out: list[Proposal] = []
    for entry in sorted(dir_.glob("*.proposal.json")):
        try:
            out.append(Proposal.from_json(json.loads(entry.read_text(encoding="utf-8"))))
        except (ValueError, json.JSONDecodeError):
            # A hand-edited or partial-write file should not break listing
            # for the rest of the directory. Logging is at the MCP layer.
            continue
    return out


def write_apply_result(
    document_path: Path,
    apply_result: ApplyResult,
) -> Path:
    """Persist ``apply_result`` next to ``document_path``."""
    dir_ = proposals_dir_for_document(document_path)
    dir_.mkdir(parents=True, exist_ok=True)
    out_path = _apply_result_path(dir_, apply_result.apply_result_id)
    out_path.write_text(
        json.dumps(apply_result.to_json(), indent=2),
        encoding="utf-8",
    )
    return out_path


def read_apply_result(
    document_path: Path,
    apply_result_id: str,
) -> ApplyResult:
    """Load the apply-result with ``apply_result_id`` from the sidecar dir."""
    dir_ = proposals_dir_for_document(document_path)
    p = _apply_result_path(dir_, apply_result_id)
    if not p.is_file():
        raise FileNotFoundError(
            f"apply-result {apply_result_id!r} not found at {p}"
        )
    return ApplyResult.from_json(json.loads(p.read_text(encoding="utf-8")))


def list_apply_results_for_document(
    document_path: Path,
    proposal_id: str | None = None,
) -> list[ApplyResult]:
    """Return apply-results in the document's sidecar dir.

    When ``proposal_id`` is provided, the list is filtered to results
    that target that proposal. Sort order is by
    ``apply_result_id`` ascending (chronological).

    Implementation note: this scans every ``*.apply-result.json``
    file in the directory and parses each. For documents with many
    edits the cost is O(n) per call; 6b-2 left it as the simplest
    thing that works because typical session counts are small (≤20
    proposals × ≤5 apply-results each). If a future profile shows
    list latency creeping past 100 ms, either an index file
    (``index.json`` cataloguing apply-results) or filename-encoded
    ``proposal_id`` (``<apply_id>.for-<proposal_id>.apply-result.json``)
    are both reasonable upgrades; do not adopt either pre-emptively.
    """
    dir_ = proposals_dir_for_document(document_path)
    if not dir_.is_dir():
        return []
    out: list[ApplyResult] = []
    for entry in sorted(dir_.glob("*.apply-result.json")):
        try:
            ar = ApplyResult.from_json(json.loads(entry.read_text(encoding="utf-8")))
        except (ValueError, json.JSONDecodeError):
            continue
        if proposal_id is not None and ar.proposal_id != proposal_id:
            continue
        out.append(ar)
    return out


def latest_apply_result_id_for(
    document_path: Path,
    proposal_id: str,
) -> str | None:
    """Return the most recent apply-result id for ``proposal_id``, or
    ``None`` if no apply-result exists for that proposal."""
    results = list_apply_results_for_document(document_path, proposal_id=proposal_id)
    if not results:
        return None
    return results[-1].apply_result_id
