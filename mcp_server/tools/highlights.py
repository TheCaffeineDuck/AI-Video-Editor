"""Highlight lifecycle tools — propose / list / read / apply / list-renders / read-render.

Phase 6c-2 surface. Six tools that wrap the :mod:`core.highlight` and
:mod:`core.highlight_render` primitives in the MCP wire shape. Path:

    propose_highlights → list_highlights → read_highlight → apply_highlight
                                                          ↓
                            list_highlight_renders ← read_highlight_render

Highlights are authored by Claude (no human review on the highlight
path) and rendered via ``apply_highlight``. Each apply writes:

* A 9:16 .mp4 at ``<doc>.highlights/<id>.highlight.mp4`` (overwriting
  any prior .mp4 for that highlight).
* A render-result sidecar at
  ``<doc>.highlights/<render_result_id>.render-result.json`` recording
  what happened on this run (wall clock, face-detect outcome, crop
  window). Re-runs accumulate sidecars; the .mp4 is overwritten.

Distinct artifacts: ``Highlight.rendered_output_path`` is "where my
output mp4 currently is" (one per highlight); the render-result file
is "what happened on a specific render run" (one per ``apply_highlight``
call). No overlap — the highlight's path is the where; the render-result
is the what-happened.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from core.cache import cache_key
from core.edit_events import is_valid_reason
from core.highlight import (
    Highlight,
    HighlightRenderResult,
    StaleHighlightError,
    list_highlights_for_document,
    list_render_results_for_document,
    new_render_result_id,
    write_highlight,
    write_render_result,
)
from core.highlight import (
    read_highlight as core_read_highlight,
)
from core.highlight import (
    read_render_result as core_read_render_result,
)
from core.highlight_render import render_highlight as core_render_highlight
from mcp_server import errors
from mcp_server.schemas import (
    ApplyHighlightRequest,
    ApplyHighlightResult,
    CropBoxOut,
    HighlightSpec,
    HighlightSummary,
    ListHighlightRendersRequest,
    ListHighlightRendersResult,
    ListHighlightsRequest,
    ListHighlightsResult,
    ProposeHighlightsRequest,
    ProposeHighlightsResult,
    ProposeHighlightsResultEntry,
    ReadHighlightRenderRequest,
    ReadHighlightRenderResult,
    ReadHighlightRequest,
    RenderResultSummary,
)
from mcp_server.tools.document import _load_document

_LOG = logging.getLogger(__name__)

_VALID_REFRAME_MODES = ("speaker_locked", "center")


# ---------------------------------------------------------------------------
# Wire ↔ core translation
# ---------------------------------------------------------------------------


def _highlight_to_summary(h: Highlight) -> HighlightSummary:
    return HighlightSummary(
        highlight_id=h.highlight_id,
        source_start_s=float(h.span_source_start),
        source_end_s=float(h.span_source_end),
        reframe_mode=str(h.reframe_mode),
        captions_enabled=bool(h.captions_enabled),
        reason=str(h.reason),
        rendered_output_path=(
            None if h.rendered_output_path is None else str(h.rendered_output_path)
        ),
    )


def _render_result_to_summary(rr: HighlightRenderResult) -> RenderResultSummary:
    return RenderResultSummary(
        render_result_id=rr.render_result_id,
        highlight_id=rr.highlight_id,
        created_at=rr.created_at.isoformat(),
        output_path=str(rr.output_path),
        face_detection_used=rr.face_detection_used,
        wall_clock_s=float(rr.wall_clock_s),
    )


def _validate_spec(
    spec: HighlightSpec, source_duration_s: float, *, index: int
) -> None:
    """Validate one HighlightSpec against the parent doc's source duration.

    Raises an MCP ``INVALID_HIGHLIGHT`` error pointing at the offending
    spec index. Single-pass: the first failure short-circuits, so the
    caller's batch isn't half-persisted on a malformed entry.
    """
    if spec.reframe_mode not in _VALID_REFRAME_MODES:
        errors.raise_mcp(
            errors.INVALID_HIGHLIGHT,
            (
                f"highlights[{index}].reframe_mode={spec.reframe_mode!r} is "
                f"not one of {list(_VALID_REFRAME_MODES)!r}"
            ),
            data={"index": index, "reframe_mode": spec.reframe_mode},
        )
    if not is_valid_reason(spec.reason):
        errors.raise_mcp(
            errors.INVALID_HIGHLIGHT,
            (
                f"highlights[{index}].reason={spec.reason!r} is not a valid "
                "rationale; see core.edit_events.is_valid_reason"
            ),
            data={"index": index, "reason": spec.reason},
        )
    if spec.source_end_s <= spec.source_start_s:
        errors.raise_mcp(
            errors.INVALID_HIGHLIGHT,
            (
                f"highlights[{index}] has zero or negative duration: "
                f"start={spec.source_start_s} end={spec.source_end_s}"
            ),
            data={
                "index": index,
                "source_start_s": spec.source_start_s,
                "source_end_s": spec.source_end_s,
            },
        )
    if spec.source_start_s < 0.0:
        errors.raise_mcp(
            errors.INVALID_HIGHLIGHT,
            (
                f"highlights[{index}].source_start_s={spec.source_start_s} "
                "is below 0"
            ),
            data={"index": index, "source_start_s": spec.source_start_s},
        )
    # Source-duration upper bound: only enforced when the parent doc
    # registered a positive duration. Documents minted by tests
    # sometimes carry duration=0 placeholders.
    if source_duration_s > 0.0 and spec.source_end_s > source_duration_s + 1e-6:
        errors.raise_mcp(
            errors.INVALID_HIGHLIGHT,
            (
                f"highlights[{index}].source_end_s={spec.source_end_s} "
                f"exceeds source duration {source_duration_s}"
            ),
            data={
                "index": index,
                "source_end_s": spec.source_end_s,
                "source_duration_s": source_duration_s,
            },
        )


def _resolve_source_duration(
    doc_sources: dict, source_path: Path
) -> float:
    """Return the parent doc's recorded duration for ``source_path``, or 0.0.

    0.0 is "unknown / unregistered" — the validator treats that as "skip
    the upper-bound check" so we don't reject legitimate highlights
    against sources the test fixtures didn't fully populate.
    """
    for src in doc_sources.values():
        if Path(src.path) == source_path:
            return float(src.duration)
    return 0.0


def _resolve_source_hash(
    doc_sources: dict, source_path: Path
) -> str:
    """Return the cache_key for ``source_path``.

    Prefers ``MediaSource.hash`` from the parent doc when populated;
    falls back to live :func:`cache_key`. Both should produce the same
    value when they're both set — the parent_doc value is just a
    cached read of the same heuristic.
    """
    for src in doc_sources.values():
        if Path(src.path) == source_path and getattr(src, "hash", ""):
            return str(src.hash)
    try:
        return cache_key(source_path)
    except FileNotFoundError as exc:
        errors.raise_mcp(
            errors.FILE_NOT_FOUND,
            f"source media file does not exist: {source_path} ({exc})",
            data={"source_path": str(source_path)},
        )


# ---------------------------------------------------------------------------
# Tool: propose_highlights
# ---------------------------------------------------------------------------


async def propose_highlights(
    req: ProposeHighlightsRequest,
) -> ProposeHighlightsResult:
    """Validate ``req.highlights`` against the document, persist each spec.

    Validation is single-pass — the first failing spec short-circuits
    the entire batch, so a partial write is never observable on disk.
    """
    doc, doc_path, _ = _load_document(req.json_path)

    # Pre-validate every spec against the parent doc's source duration.
    # We resolve duration once per source path to avoid re-walking
    # ``doc.sources`` per spec.
    duration_cache: dict[Path, float] = {}
    for i, spec in enumerate(req.highlights):
        sp = Path(spec.source_path)
        if sp not in duration_cache:
            duration_cache[sp] = _resolve_source_duration(doc.sources, sp)
        _validate_spec(spec, duration_cache[sp], index=i)

    # Mint and persist after validation passes.
    entries: list[ProposeHighlightsResultEntry] = []
    for spec in req.highlights:
        sp = Path(spec.source_path)
        parent_source_hash = _resolve_source_hash(doc.sources, sp)
        candidate = Highlight(
            highlight_id="",  # auto-assigned by write_highlight
            created_at=datetime.now(UTC),
            parent_document_path=doc_path,
            parent_source_hash=parent_source_hash,
            span_source_path=sp,
            span_source_start=float(spec.source_start_s),
            span_source_end=float(spec.source_end_s),
            reason=str(spec.reason),
            reframe_mode=str(spec.reframe_mode),  # type: ignore[arg-type]
            captions_enabled=bool(spec.captions_enabled),
        )
        materialized, json_path = write_highlight(doc_path, candidate)
        entries.append(
            ProposeHighlightsResultEntry(
                highlight_id=materialized.highlight_id,
                json_path=str(json_path),
            )
        )
        _LOG.info(
            "propose_highlights: wrote highlight_id=%s to %s",
            materialized.highlight_id,
            json_path,
        )
    return ProposeHighlightsResult(highlights=entries)


# ---------------------------------------------------------------------------
# Tool: list_highlights
# ---------------------------------------------------------------------------


async def list_highlights(req: ListHighlightsRequest) -> ListHighlightsResult:
    """Directory scan of ``<doc>.highlights/*.highlight.json``."""
    _, doc_path, _ = _load_document(req.json_path)
    items = list_highlights_for_document(doc_path)
    return ListHighlightsResult(
        highlights=[_highlight_to_summary(h) for h in items]
    )


# ---------------------------------------------------------------------------
# Tool: read_highlight
# ---------------------------------------------------------------------------


async def read_highlight(req: ReadHighlightRequest) -> HighlightSummary:
    """By-id read; ``HIGHLIGHT_NOT_FOUND`` on miss.

    The wire shape returned mirrors :class:`HighlightSummary` — the
    same fields ``list_highlights`` returns per entry — so a client
    that already has the summary doesn't get a different shape on a
    follow-up read.
    """
    _, doc_path, _ = _load_document(req.json_path)
    try:
        h = core_read_highlight(doc_path, req.highlight_id)
    except FileNotFoundError as exc:
        errors.raise_mcp(
            errors.HIGHLIGHT_NOT_FOUND,
            str(exc),
            data={"highlight_id": req.highlight_id},
        )
    return _highlight_to_summary(h)


# ---------------------------------------------------------------------------
# Tool: apply_highlight
# ---------------------------------------------------------------------------


async def apply_highlight(req: ApplyHighlightRequest) -> ApplyHighlightResult:
    """Render a highlight, write its render-result sidecar, return the path.

    Re-running on the same id produces a fresh render-result file; the
    .mp4 output is overwritten. ``STALE_HIGHLIGHT`` fires when the live
    source file's ``cache_key`` doesn't match the stored
    ``parent_source_hash`` — re-author against the current source.
    Renderer failures (ffmpeg / smartcut) surface as ``RENDER_FAILED``.
    """
    doc, doc_path, _ = _load_document(req.json_path)
    try:
        h = core_read_highlight(doc_path, req.highlight_id)
    except FileNotFoundError as exc:
        errors.raise_mcp(
            errors.HIGHLIGHT_NOT_FOUND,
            str(exc),
            data={"highlight_id": req.highlight_id},
        )

    started = time.monotonic()
    try:
        metadata = core_render_highlight(h, doc)
    except StaleHighlightError as exc:
        errors.raise_mcp(
            errors.STALE_HIGHLIGHT,
            str(exc),
            data={
                "highlight_id": req.highlight_id,
                "parent_source_hash": h.parent_source_hash,
            },
        )
    except FileNotFoundError as exc:
        errors.raise_mcp(
            errors.FILE_NOT_FOUND,
            str(exc),
            data={"highlight_id": req.highlight_id},
        )
    except Exception as exc:
        # ffmpeg / smartcut / PyAV failures bubble through render_cut
        # as a heterogeneous mix (RuntimeError from ffmpeg, OSError
        # from disk, ``av.error.FFmpegError`` from PyAV decode, etc.).
        # Surface them all as RENDER_FAILED with the original message
        # so the client (or human reading the chat) can act, instead
        # of leaking a Python traceback through INTERNAL_ERROR.
        errors.raise_mcp(
            errors.RENDER_FAILED,
            f"render_highlight failed: {exc}",
            data={"highlight_id": req.highlight_id},
        )
    wall_clock = time.monotonic() - started

    render_result_id = new_render_result_id()
    rr = HighlightRenderResult(
        render_result_id=render_result_id,
        highlight_id=h.highlight_id,
        created_at=datetime.now(UTC),
        output_path=metadata.output_path,
        parent_source_hash=metadata.parent_source_hash,
        face_detection_used=metadata.face_detection_used,
        crop_box=metadata.crop_box,
        wall_clock_s=wall_clock,
    )
    write_render_result(doc_path, rr)

    _LOG.info(
        "apply_highlight: highlight_id=%s render_result_id=%s output=%s wall=%.2fs",
        h.highlight_id,
        render_result_id,
        metadata.output_path,
        wall_clock,
    )
    return ApplyHighlightResult(
        render_result_id=render_result_id,
        output_path=str(metadata.output_path),
    )


# ---------------------------------------------------------------------------
# Tool: list_highlight_renders
# ---------------------------------------------------------------------------


async def list_highlight_renders(
    req: ListHighlightRendersRequest,
) -> ListHighlightRendersResult:
    _, doc_path, _ = _load_document(req.json_path)
    items = list_render_results_for_document(doc_path, highlight_id=req.highlight_id)
    return ListHighlightRendersResult(
        render_results=[_render_result_to_summary(rr) for rr in items]
    )


# ---------------------------------------------------------------------------
# Tool: read_highlight_render
# ---------------------------------------------------------------------------


async def read_highlight_render(
    req: ReadHighlightRenderRequest,
) -> ReadHighlightRenderResult:
    _, doc_path, _ = _load_document(req.json_path)
    try:
        rr = core_read_render_result(doc_path, req.render_result_id)
    except FileNotFoundError as exc:
        errors.raise_mcp(
            errors.RENDER_RESULT_NOT_FOUND,
            str(exc),
            data={"render_result_id": req.render_result_id},
        )
    return ReadHighlightRenderResult(
        render_result_id=rr.render_result_id,
        highlight_id=rr.highlight_id,
        created_at=rr.created_at.isoformat(),
        output_path=str(rr.output_path),
        parent_source_hash=rr.parent_source_hash,
        face_detection_used=rr.face_detection_used,
        crop_box=CropBoxOut(
            x=rr.crop_box[0],
            y=rr.crop_box[1],
            w=rr.crop_box[2],
            h=rr.crop_box[3],
        ),
        wall_clock_s=float(rr.wall_clock_s),
    )
