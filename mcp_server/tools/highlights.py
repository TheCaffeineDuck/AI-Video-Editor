"""Highlight lifecycle tools — propose / list / read / apply / list-renders / read-render.

Phase 7 update: highlights now carry an ordered tuple of fragments
(``sub_spans``), and may reference a sync group whose audio master
overrides the cameras' audio at render time. The wire shape accepts
both the new ``sub_spans`` form and the legacy single-span shortcut
(``source_path`` / ``source_start_s`` / ``source_end_s``); the
propose tool translates internally.

Single-pass spec validation: the first failing entry short-circuits
the entire batch with ``INVALID_HIGHLIGHT`` naming the offending
index, so a partial write never lands on disk. Sync-group references
are validated up-front: every fragment whose source is a camera in a
named group must be one of the cameras *in that group*; foreign paths
raise ``INVALID_HIGHLIGHT``.
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
    SubSpan,
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
from core.sync import (
    StaleSyncGroupError,
    read_sync_group,
)
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
    SubSpanOut,
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
        sub_spans=[
            SubSpanOut(
                source_path=str(s.source_path),
                source_start_s=float(s.source_start),
                source_end_s=float(s.source_end),
                reason=s.reason,
            )
            for s in h.sub_spans
        ],
        sync_group_id=h.sync_group_id,
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
        sync_group_id=rr.sync_group_id,
        wall_clock_s=float(rr.wall_clock_s),
    )


def _normalize_spec_to_subspans(
    spec: HighlightSpec, *, index: int
) -> list[tuple[str, float, float, str]]:
    """Reduce ``spec`` to a list of ``(source_path, start, end, reason)`` fragments.

    Accepts either the new ``sub_spans`` form or the legacy single-span
    shortcut. Mixing both raises ``INVALID_HIGHLIGHT`` so the contract
    stays crisp; clients should pick one form per spec.
    """
    has_sub_spans = bool(spec.sub_spans)
    has_legacy = (
        spec.source_path is not None
        or spec.source_start_s is not None
        or spec.source_end_s is not None
    )
    if has_sub_spans and has_legacy:
        errors.raise_mcp(
            errors.INVALID_HIGHLIGHT,
            (
                f"highlights[{index}] mixes sub_spans with legacy single-span "
                "fields (source_path / source_start_s / source_end_s); pick one"
            ),
            data={"index": index},
        )
    if has_sub_spans:
        return [
            (
                str(s.source_path),
                float(s.source_start_s),
                float(s.source_end_s),
                str(s.reason or ""),
            )
            for s in spec.sub_spans
        ]
    if not has_legacy:
        errors.raise_mcp(
            errors.INVALID_HIGHLIGHT,
            (
                f"highlights[{index}] has neither sub_spans nor legacy "
                "single-span fields; nothing to render"
            ),
            data={"index": index},
        )
    if (
        spec.source_path is None
        or spec.source_start_s is None
        or spec.source_end_s is None
    ):
        errors.raise_mcp(
            errors.INVALID_HIGHLIGHT,
            (
                f"highlights[{index}] legacy single-span fields incomplete: "
                f"source_path={spec.source_path!r} "
                f"source_start_s={spec.source_start_s} "
                f"source_end_s={spec.source_end_s}"
            ),
            data={"index": index},
        )
    return [
        (
            str(spec.source_path),
            float(spec.source_start_s),
            float(spec.source_end_s),
            "",
        )
    ]


def _validate_spec(
    spec: HighlightSpec,
    fragments: list[tuple[str, float, float, str]],
    source_durations: dict[str, float],
    *,
    index: int,
    sync_group_camera_paths: set[str] | None,
) -> None:
    """Validate one HighlightSpec end-to-end at the wire boundary."""
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
    for fi, (path, start, end, _r) in enumerate(fragments):
        if end <= start:
            errors.raise_mcp(
                errors.INVALID_HIGHLIGHT,
                (
                    f"highlights[{index}].sub_spans[{fi}] has zero or "
                    f"negative duration: start={start} end={end}"
                ),
                data={"index": index, "fragment_index": fi},
            )
        if start < 0.0:
            errors.raise_mcp(
                errors.INVALID_HIGHLIGHT,
                (
                    f"highlights[{index}].sub_spans[{fi}].source_start_s="
                    f"{start} is below 0"
                ),
                data={"index": index, "fragment_index": fi},
            )
        duration = source_durations.get(path, 0.0)
        if duration > 0.0 and end > duration + 1e-6:
            errors.raise_mcp(
                errors.INVALID_HIGHLIGHT,
                (
                    f"highlights[{index}].sub_spans[{fi}].source_end_s={end} "
                    f"exceeds source duration {duration}"
                ),
                data={"index": index, "fragment_index": fi, "duration": duration},
            )
        if sync_group_camera_paths is not None and path not in sync_group_camera_paths:
            errors.raise_mcp(
                errors.INVALID_HIGHLIGHT,
                (
                    f"highlights[{index}].sub_spans[{fi}].source_path={path!r} "
                    f"is not a camera registered in sync_group "
                    f"{spec.sync_group_id!r}; known cameras: "
                    f"{sorted(sync_group_camera_paths)!r}"
                ),
                data={
                    "index": index,
                    "fragment_index": fi,
                    "source_path": path,
                    "sync_group_id": spec.sync_group_id,
                },
            )


def _resolve_source_duration(doc_sources: dict, source_path: Path) -> float:
    """Return the parent doc's recorded duration for ``source_path``, or 0.0."""
    for src in doc_sources.values():
        if Path(src.path) == source_path:
            return float(src.duration)
    return 0.0


def _resolve_source_hash(doc_sources: dict, source_path: Path) -> str:
    """Return the cache_key for ``source_path``."""
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

    # Pre-resolve sync group cameras (one read per group id).
    sync_group_cameras: dict[str, set[str]] = {}
    for spec in req.highlights:
        if spec.sync_group_id and spec.sync_group_id not in sync_group_cameras:
            try:
                group = read_sync_group(doc_path, spec.sync_group_id)
            except FileNotFoundError as exc:
                errors.raise_mcp(
                    errors.SYNC_GROUP_NOT_FOUND,
                    str(exc),
                    data={"sync_group_id": spec.sync_group_id},
                )
            sync_group_cameras[spec.sync_group_id] = set(group.cameras.keys())

    # Translate every spec's payload to a fragment list once for both the
    # validation pass and the write pass.
    per_spec_fragments: list[list[tuple[str, float, float, str]]] = [
        _normalize_spec_to_subspans(spec, index=i)
        for i, spec in enumerate(req.highlights)
    ]
    duration_cache: dict[str, float] = {}
    for i, (spec, fragments) in enumerate(
        zip(req.highlights, per_spec_fragments, strict=True)
    ):
        for path_str, _, _, _ in fragments:
            if path_str not in duration_cache:
                duration_cache[path_str] = _resolve_source_duration(
                    doc.sources, Path(path_str)
                )
        cameras = (
            sync_group_cameras.get(spec.sync_group_id)
            if spec.sync_group_id
            else None
        )
        _validate_spec(
            spec,
            fragments,
            duration_cache,
            index=i,
            sync_group_camera_paths=cameras,
        )

    # Mint and persist after validation passes.
    entries: list[ProposeHighlightsResultEntry] = []
    for spec, fragments in zip(req.highlights, per_spec_fragments, strict=True):
        sub_spans = tuple(
            SubSpan(
                source_path=Path(p),
                source_start=s,
                source_end=e,
                reason=r,
            )
            for (p, s, e, r) in fragments
        )
        # Per-source hashes: gather one cache_key per unique source path.
        unique_paths = {str(s.source_path) for s in sub_spans}
        parent_source_hashes: dict[str, str] = {}
        for p in unique_paths:
            parent_source_hashes[p] = _resolve_source_hash(doc.sources, Path(p))
        candidate = Highlight(
            highlight_id="",  # auto-assigned by write_highlight
            created_at=datetime.now(UTC),
            parent_document_path=doc_path,
            parent_source_hashes=parent_source_hashes,
            sub_spans=sub_spans,
            reason=str(spec.reason),
            reframe_mode=str(spec.reframe_mode),  # type: ignore[arg-type]
            captions_enabled=bool(spec.captions_enabled),
            sync_group_id=spec.sync_group_id,
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
    """By-id read; ``HIGHLIGHT_NOT_FOUND`` on miss."""
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
    """Render a highlight, write its render-result sidecar, return the path."""
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
                "parent_source_hashes": h.parent_source_hashes,
            },
        )
    except StaleSyncGroupError as exc:
        errors.raise_mcp(
            errors.STALE_SYNC_GROUP,
            str(exc),
            data={
                "highlight_id": req.highlight_id,
                "sync_group_id": h.sync_group_id,
            },
        )
    except FileNotFoundError as exc:
        errors.raise_mcp(
            errors.FILE_NOT_FOUND,
            str(exc),
            data={"highlight_id": req.highlight_id},
        )
    except Exception as exc:
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
        parent_source_hashes=metadata.parent_source_hashes,
        face_detection_used=metadata.face_detection_used,
        crop_box=metadata.crop_box,
        crop_boxes_by_source=metadata.crop_boxes_by_source,
        sync_group_id=metadata.sync_group_id,
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
        parent_source_hashes=rr.parent_source_hashes,
        face_detection_used=rr.face_detection_used,
        crop_box=CropBoxOut(
            x=rr.crop_box[0],
            y=rr.crop_box[1],
            w=rr.crop_box[2],
            h=rr.crop_box[3],
        ),
        crop_boxes_by_source={
            k: CropBoxOut(x=v[0], y=v[1], w=v[2], h=v[3])
            for k, v in rr.crop_boxes_by_source.items()
        },
        sync_group_id=rr.sync_group_id,
        wall_clock_s=float(rr.wall_clock_s),
    )
