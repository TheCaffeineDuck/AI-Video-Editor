"""Stable error codes for the MCP server's tool surface.

These string codes are part of the contract — clients (Claude, or any
other MCP consumer) branch on them. Don't rename or repurpose once 6b
starts depending on them; add new codes instead.

The MCP protocol carries an integer ``code`` field on errors (JSON-RPC
convention) plus a free-form ``message`` and optional ``data`` payload.
We map every server-side failure to one of the codes below; the integer
is always :data:`mcp.types.INVALID_PARAMS` for client-fixable errors and
:data:`mcp.types.INTERNAL_ERROR` for "the server tried, the worker
failed" errors. The string code lives in ``message`` (prefix) and
``data["code"]`` so clients have a stable key to switch on.
"""

from __future__ import annotations

from typing import Any, NoReturn

from mcp import types
from mcp.shared.exceptions import McpError

# Client-fixable: bad path, bad schema, malformed request.
FILE_NOT_FOUND = "FILE_NOT_FOUND"
INVALID_DOCUMENT = "INVALID_DOCUMENT"
UNSUPPORTED_SCHEMA = "UNSUPPORTED_SCHEMA"
WORD_BOUNDARY_VIOLATION = "WORD_BOUNDARY_VIOLATION"
CUT_INVALID = "CUT_INVALID"
# Phase 6a: editing on non-monotonic timelines is rejected by the core
# command stack with NotImplementedError; we surface that as a stable
# code rather than letting the trace bubble through.
EDIT_NOT_SUPPORTED = "EDIT_NOT_SUPPORTED"

# Phase 6b-2: proposal / apply-result lifecycle.
PROPOSAL_NOT_FOUND = "PROPOSAL_NOT_FOUND"
"""``read_proposal`` / ``apply_proposal`` referenced an id that has no
matching ``<id>.proposal.json`` file in the document's sidecar dir."""

APPLY_RESULT_NOT_FOUND = "APPLY_RESULT_NOT_FOUND"
"""``read_apply_result`` referenced an id with no matching file."""

INVALID_PROPOSAL = "INVALID_PROPOSAL"
"""``propose_moves`` / a stored proposal failed shape validation:
malformed move shape, anchor shape, or missing required field."""

INVALID_MOVE_ID = "INVALID_MOVE_ID"
"""``apply_proposal``'s ``move_ids`` parameter referenced an id not
present in the proposal."""

DUPLICATE_MOVE_ID = "DUPLICATE_MOVE_ID"
"""A proposal would contain two moves with the same explicit move_id.
Auto-assigned ids are unique by construction; this only fires for
caller-supplied ids."""

STALE_PROPOSAL = "STALE_PROPOSAL"
"""``apply_proposal``'s proposal carries a ``parent_document_state_hash``
that doesn't match the current document's content hash. The proposal was
authored against a different state and is no longer safe to apply.
Suppressed for v1 proposals (forever-stale-uncheckable; see
:mod:`core.proposal`)."""

INVALID_REASON = "INVALID_REASON"
"""A move's ``reason`` failed :func:`core.edit_events.is_valid_reason`.
Surfaces both at proposal-author time (``propose_moves``) and at apply
time (any move whose reason validator failed when reconstructed from
JSON)."""

# Phase 6c-2: highlight lifecycle.
HIGHLIGHT_NOT_FOUND = "HIGHLIGHT_NOT_FOUND"
"""``read_highlight`` / ``apply_highlight`` referenced an id that has no
matching ``<id>.highlight.json`` file in the document's sidecar dir."""

RENDER_RESULT_NOT_FOUND = "RENDER_RESULT_NOT_FOUND"
"""``read_highlight_render`` referenced an id with no matching
``<id>.render-result.json`` file."""

INVALID_HIGHLIGHT = "INVALID_HIGHLIGHT"
"""``propose_highlights`` rejected a spec: out-of-bounds span, zero
duration, unknown ``reframe_mode``, or a ``reason`` that fails
:func:`core.edit_events.is_valid_reason`. The error message names the
offending spec index so the client can fix one entry without
re-submitting the whole batch."""

STALE_HIGHLIGHT = "STALE_HIGHLIGHT"
"""``apply_highlight`` failed because the highlight's recorded
``parent_source_hash`` no longer matches the live source file's
:func:`core.cache.cache_key`. The source media has been replaced;
re-author the highlight against the current source. Distinct from
``STALE_PROPOSAL`` (which guards intra-doc edits) — highlights are
source-time spans, so only file replacement triggers staleness."""

# Server-side: a worker raised. Surface the underlying message so the
# client (or the human reading the chat) has something to act on.
TRANSCRIPTION_FAILED = "TRANSCRIPTION_FAILED"
RENDER_FAILED = "RENDER_FAILED"

_CLIENT_CODES = frozenset(
    {
        FILE_NOT_FOUND,
        INVALID_DOCUMENT,
        UNSUPPORTED_SCHEMA,
        WORD_BOUNDARY_VIOLATION,
        CUT_INVALID,
        EDIT_NOT_SUPPORTED,
        PROPOSAL_NOT_FOUND,
        APPLY_RESULT_NOT_FOUND,
        INVALID_PROPOSAL,
        INVALID_MOVE_ID,
        DUPLICATE_MOVE_ID,
        STALE_PROPOSAL,
        INVALID_REASON,
        HIGHLIGHT_NOT_FOUND,
        RENDER_RESULT_NOT_FOUND,
        INVALID_HIGHLIGHT,
        STALE_HIGHLIGHT,
    }
)


def raise_mcp(code: str, message: str, data: dict[str, Any] | None = None) -> NoReturn:
    """Raise :class:`McpError` with our stable code + a JSON-RPC integer.

    ``message`` is prefixed with the string code so a client that doesn't
    parse ``data`` still gets a recognizable label. ``data["code"]`` is
    the canonical machine-readable channel.
    """
    rpc_code = types.INVALID_PARAMS if code in _CLIENT_CODES else types.INTERNAL_ERROR
    payload: dict[str, Any] = {"code": code}
    if data:
        payload.update(data)
    raise McpError(
        types.ErrorData(
            code=rpc_code,
            message=f"{code}: {message}",
            data=payload,
        )
    )
