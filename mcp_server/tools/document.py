"""Document-side tools: load, read, mutate.

All five tools (``load_document``, ``get_transcript``, ``get_ranges``,
``apply_cuts``, ``restore_ranges``) share the same load-from-disk
helper and the same word-boundary validation. Mutating tools commit
the new Document back to disk through :func:`_save_document`, which
goes through :meth:`Document.to_json` so the v2 schema is emitted
consistently.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from core.document import Document, UnsupportedSchemaError
from core.editing import AddCut, CommandStack, RestoreRange
from mcp_server import errors
from mcp_server.schemas import (
    ApplyCutsRequest,
    ApplyCutsResult,
    CutRequest,
    DocumentSummary,
    GetTranscriptRequest,
    JsonPathRequest,
    RangeOut,
    RangesResult,
    RestoreRangesRequest,
    RestoreRequestItem,
    RestoreResult,
    TranscriptResult,
    TranscriptWord,
)

_LOG = logging.getLogger(__name__)

# A 1-millisecond tolerance bands together cut endpoints that "should"
# match a word boundary but are off by a sub-ms float drift. Tighter than
# this and saved/reloaded times start failing the equality check; looser
# and we'd accept genuinely-mid-word cuts.
_BOUNDARY_TOLERANCE_S = 0.001


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------


def _load_document(json_path: str) -> tuple[Document, Path, dict]:
    """Load a Document from disk and surface every failure as an MCP error.

    Returns the parsed Document, the resolved path, and the original
    raw dict (so callers that need to re-emit ``schema_version`` from
    the on-disk file have it without a re-read).
    """
    path = Path(json_path)
    if not path.is_file():
        errors.raise_mcp(
            errors.FILE_NOT_FOUND,
            f"document path does not exist or is not a file: {path}",
            data={"path": str(path)},
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.raise_mcp(
            errors.INVALID_DOCUMENT,
            f"document at {path} is not valid JSON: {exc}",
            data={"path": str(path)},
        )
    except OSError as exc:
        errors.raise_mcp(
            errors.FILE_NOT_FOUND,
            f"could not read document at {path}: {exc}",
            data={"path": str(path)},
        )
    if not isinstance(raw, dict):
        errors.raise_mcp(
            errors.INVALID_DOCUMENT,
            f"document at {path} is not a JSON object",
            data={"path": str(path)},
        )
    try:
        doc = Document.from_json(raw)
    except UnsupportedSchemaError as exc:
        errors.raise_mcp(
            errors.UNSUPPORTED_SCHEMA,
            str(exc),
            data={"path": str(path)},
        )
    except (KeyError, ValueError) as exc:
        errors.raise_mcp(
            errors.INVALID_DOCUMENT,
            f"document at {path} failed schema parse: {exc}",
            data={"path": str(path)},
        )
    return doc, path, raw


def _save_document(doc: Document, path: Path) -> None:
    """Write ``doc`` back to ``path`` as v2 JSON.

    Mirrors the editor pane's save (`json.dumps(..., indent=2)`) so
    files round-trip diff-cleanly between GUI and MCP saves.
    """
    payload = json.dumps(doc.to_json(), indent=2)
    path.write_text(payload, encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _primary_source_path(doc: Document) -> str:
    """The first source's path as a string, or '' for source-less Documents."""
    if not doc.sources:
        return ""
    return str(next(iter(doc.sources.values())).path)


def _source_duration(doc: Document) -> float:
    if not doc.sources:
        return 0.0
    return float(next(iter(doc.sources.values())).duration)


def _word_count(doc: Document) -> int:
    return sum(len(seg.words) for seg in doc.segments)


def _word_boundary_set(doc: Document) -> tuple[set[float], list[tuple[float, float]]]:
    """Return (boundary times, word intervals) for word-boundary validation.

    Boundary times are ``word.start`` and ``word.end`` for every word in
    every segment, plus 0.0 and the source duration (the file's outer
    boundaries are valid cut endpoints by construction).
    """
    boundaries: set[float] = {0.0, _source_duration(doc)}
    intervals: list[tuple[float, float]] = []
    for seg in doc.segments:
        for w in seg.words:
            boundaries.add(float(w.start))
            boundaries.add(float(w.end))
            intervals.append((float(w.start), float(w.end)))
    return boundaries, intervals


def _is_word_boundary(t: float, boundaries: set[float], intervals: list[tuple[float, float]]) -> bool:
    """A timestamp is at a "word boundary" if it matches a word edge or
    falls outside every word.

    Falling outside every word is the silence-between-segments case that
    :func:`core.render._snap_cuts_to_word_boundaries` deliberately lets
    pass through unchanged; treating those as boundary-aligned here
    keeps the MCP layer consistent with the renderer.
    """
    for b in boundaries:
        if abs(t - b) <= _BOUNDARY_TOLERANCE_S:
            return True
    for ws, we in intervals:
        if ws < t < we:
            return False
    return True


def _ranges_to_payload(doc: Document) -> tuple[list[RangeOut], float, float]:
    """Marshal ``doc.ranges`` into the wire format + kept/cut totals."""
    out_ranges = [
        RangeOut(start_s=float(r.start), end_s=float(r.end), reason=r.reason)
        for r in doc.ranges
    ]
    total_kept = sum(r.end - r.start for r in doc.ranges)
    total_cut = max(0.0, _source_duration(doc) - total_kept)
    return out_ranges, total_kept, total_cut


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def load_document(req: JsonPathRequest) -> DocumentSummary:
    doc, path, raw = _load_document(req.json_path)
    schema_version = int(raw.get("schema_version", Document.SCHEMA_VERSION))
    return DocumentSummary(
        path=str(path),
        source_path=_primary_source_path(doc),
        duration_s=_source_duration(doc),
        word_count=_word_count(doc),
        range_count=len(doc.ranges),
        schema_version=schema_version,
        created_at=doc.created_at.isoformat(),
    )


async def get_transcript(req: GetTranscriptRequest) -> TranscriptResult:
    doc, _, _ = _load_document(req.json_path)
    kept_intervals = [(float(r.start), float(r.end)) for r in doc.ranges]

    def is_kept(start: float, end: float) -> bool:
        # A word is kept if any of doc.ranges fully contains its midpoint.
        # Using midpoint sidesteps boundary fuzz when a kept range's end
        # equals a word's end exactly.
        midpoint = 0.5 * (start + end)
        return any(s <= midpoint <= e for s, e in kept_intervals)

    words: list[TranscriptWord] = []
    for seg_idx, seg in enumerate(doc.segments):
        for w in seg.words:
            kept = is_kept(float(w.start), float(w.end))
            struck = not kept
            if not req.include_struck and struck:
                continue
            words.append(
                TranscriptWord(
                    word=w.text,
                    start_s=float(w.start),
                    end_s=float(w.end),
                    segment_idx=seg_idx,
                    struck=struck if req.include_struck else False,
                )
            )
    return TranscriptResult(words=words)


async def get_ranges(req: JsonPathRequest) -> RangesResult:
    """Return the document's keep-ranges in source-time order.

    Phase 6a: this tool predates the v3 ``Timeline`` shape. For
    monotonic v3 documents (every doc 6a editing produces) the output
    is identical to the v2 contract. For non-monotonic v3 documents
    (hand-built fixtures or 6b's rearrangement output) the playlist
    order is *flattened* into source-time order here — the per-clip
    ordering is lost, which is a lossy view of the timeline. Callers
    that need playlist order should use :func:`get_timeline` instead.
    The ``is_source_monotonic`` flag in the response signals whether
    this flattening dropped information.
    """
    doc, _, _ = _load_document(req.json_path)
    ranges, kept, cut = _ranges_to_payload(doc)
    return RangesResult(
        ranges=ranges,
        total_kept_s=kept,
        total_cut_s=cut,
        is_source_monotonic=doc.main_timeline.is_source_monotonic(),
    )


async def get_timeline(req: JsonPathRequest) -> TimelineResult:  # noqa: F821
    """Return the v3 ``main_timeline`` clips in playlist order.

    Each clip carries ``source_path``, ``source_start_s``,
    ``source_end_s``, and any ``reason`` set by an edit command. The
    list preserves playlist (not source-time) order, so non-monotonic
    re-arrangements round-trip cleanly.
    """
    from mcp_server.schemas import ClipOut, TimelineResult

    doc, _, _ = _load_document(req.json_path)
    timeline = doc.main_timeline
    return TimelineResult(
        clips=[
            ClipOut(
                source_path=str(c.source_path),
                source_start_s=float(c.source_start),
                source_end_s=float(c.source_end),
                reason=c.reason,
            )
            for c in timeline.clips
        ],
        total_duration_s=float(timeline.total_duration_s),
        is_source_monotonic=timeline.is_source_monotonic(),
    )


# ---------------------------------------------------------------------------
# apply_cuts / restore_ranges
# ---------------------------------------------------------------------------


def _validate_word_boundaries(
    doc: Document, intervals: list[tuple[float, float, str]]
) -> None:
    """Raise WORD_BOUNDARY_VIOLATION if any endpoint sits inside a word.

    ``intervals`` is a list of ``(start, end, label)`` for error messages.
    Validation is run for every interval before any mutation — the
    all-or-nothing semantic the spec requires.
    """
    boundaries, word_intervals = _word_boundary_set(doc)
    for start, end, label in intervals:
        if end < start:
            errors.raise_mcp(
                errors.CUT_INVALID,
                f"{label} has end ({end}) < start ({start})",
                data={"start_s": start, "end_s": end},
            )
        if not _is_word_boundary(start, boundaries, word_intervals):
            errors.raise_mcp(
                errors.WORD_BOUNDARY_VIOLATION,
                f"{label} start ({start}) does not align to a word boundary",
                data={"start_s": start, "end_s": end},
            )
        if not _is_word_boundary(end, boundaries, word_intervals):
            errors.raise_mcp(
                errors.WORD_BOUNDARY_VIOLATION,
                f"{label} end ({end}) does not align to a word boundary",
                data={"start_s": start, "end_s": end},
            )


def _primary_source_id(doc: Document) -> str:
    """Pick the source_id to attach new ranges/cuts to.

    The MCP layer is single-source by design (multi-source compositing
    is post-6c work); raising on multi-source documents matches the
    constraint :func:`core.render._select_single_source` enforces.
    """
    if not doc.sources:
        errors.raise_mcp(
            errors.INVALID_DOCUMENT,
            "document has no sources; cannot apply cuts or restore ranges",
        )
    if len(doc.sources) > 1:
        errors.raise_mcp(
            errors.INVALID_DOCUMENT,
            f"document has multiple sources {list(doc.sources)!r}; "
            "MCP cuts/restores are single-source only in 6a",
        )
    return next(iter(doc.sources))


def _interval_inside_existing_cut(
    doc: Document, start: float, end: float
) -> bool:
    """True iff ``[start, end]`` is fully outside every kept range —
    i.e., already entirely cut. The spec calls this case a skip, not an
    error.
    """
    for r in doc.ranges:
        if r.start < end and r.end > start:
            return False
    return True


def _interval_already_kept(
    doc: Document, start: float, end: float
) -> bool:
    """True iff ``[start, end]`` is fully covered by an existing kept
    range — i.e., a restore that wouldn't change anything. Skip case
    for ``restore_ranges``.
    """
    for r in doc.ranges:
        if r.start <= start and r.end >= end:
            return True
    return False


def _require_monotonic_timeline(doc: Document) -> None:
    """Refuse non-monotonic edits with EDIT_NOT_SUPPORTED.

    The :class:`AddCut` / :class:`RestoreRange` commands raise
    :class:`NotImplementedError` at apply time on non-monotonic
    timelines. The MCP contract is for clients to branch on stable
    string codes, not Python exception types — so we check at the tool
    boundary and raise the documented error code instead of letting
    the core trace surface as ``INTERNAL_ERROR``.
    """
    if not doc.main_timeline.is_source_monotonic():
        errors.raise_mcp(
            errors.EDIT_NOT_SUPPORTED,
            "this document's timeline is non-monotonic — editing "
            "(apply_cuts, restore_ranges) requires a source-monotonic "
            "playlist. Rearrangement-aware editing is scheduled for Phase 6b.",
        )


async def apply_cuts(req: ApplyCutsRequest) -> ApplyCutsResult:
    doc, path, _ = _load_document(req.json_path)
    _require_monotonic_timeline(doc)
    source_id = _primary_source_id(doc)

    cuts_in: list[CutRequest] = list(req.cuts)
    intervals = [
        (float(c.start_s), float(c.end_s), f"cuts[{i}]")
        for i, c in enumerate(cuts_in)
    ]
    _validate_word_boundaries(doc, intervals)

    stack = CommandStack()
    current = doc
    applied = 0
    skipped = 0
    for c in cuts_in:
        start, end = float(c.start_s), float(c.end_s)
        if start == end:
            skipped += 1
            continue
        if _interval_inside_existing_cut(current, start, end):
            skipped += 1
            continue
        cmd = AddCut(
            start=start,
            end=end,
            reason=c.reason if c.reason is not None else "manual",
            source_id=source_id,
        )
        before = current
        current = cmd.apply(current)
        if current.ranges == before.ranges:
            # subtract_interval was a no-op (e.g. the cut sat exactly on
            # a range edge with zero overlap). Treat as skip rather than
            # claiming an applied edit.
            skipped += 1
            continue
        stack.push(cmd, before, current)
        applied += 1

    if applied > 0:
        _save_document(current, path)
        _LOG.info("apply_cuts: wrote %d cut(s) to %s", applied, path)

    out_ranges, kept, cut_total = _ranges_to_payload(current)
    return ApplyCutsResult(
        applied_count=applied,
        skipped_count=skipped,
        ranges_after=out_ranges,
        total_kept_s=kept,
        total_cut_s=cut_total,
    )


async def restore_ranges(req: RestoreRangesRequest) -> RestoreResult:
    doc, path, _ = _load_document(req.json_path)
    _require_monotonic_timeline(doc)
    source_id = _primary_source_id(doc)

    items: list[RestoreRequestItem] = list(req.ranges)
    intervals = [
        (float(r.start_s), float(r.end_s), f"ranges[{i}]")
        for i, r in enumerate(items)
    ]
    _validate_word_boundaries(doc, intervals)

    current = doc
    applied = 0
    skipped = 0
    for r in items:
        start, end = float(r.start_s), float(r.end_s)
        if start == end:
            skipped += 1
            continue
        if _interval_already_kept(current, start, end):
            skipped += 1
            continue
        cmd = RestoreRange(start=start, end=end, source_id=source_id)
        before = current
        current = cmd.apply(current)
        if current.ranges == before.ranges:
            skipped += 1
            continue
        applied += 1

    if applied > 0:
        _save_document(current, path)
        _LOG.info("restore_ranges: wrote %d restore(s) to %s", applied, path)

    out_ranges, kept, cut_total = _ranges_to_payload(current)
    return RestoreResult(
        applied_count=applied,
        skipped_count=skipped,
        ranges_after=out_ranges,
        total_kept_s=kept,
        total_cut_s=cut_total,
    )


