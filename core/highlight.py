"""Highlight artifact — a span on a parent Document marked for vertical clip render.

Phase 6c-1 introduces highlights as a distinct artifact rather than a
:class:`~core.document.Document` subtype. A highlight is "this span on
this parent doc, with this rationale, rendered 9:16 with these reframe
+ caption settings." The renderer (in :mod:`core.highlight_render`)
wraps the existing run-batched :func:`~core.render.render_cut` with the
parent Document loaded as input — highlights do not own their own
cutting pipeline, only their reframe + caption pipeline.

Storage mirrors the proposals pattern (:mod:`core.proposal`). Each
parent Document gets a sidecar directory at
``<doc>.highlights/`` containing one ``<id>.highlight.json`` per
highlight; rendered output files (``<id>.highlight.mp4``) live in the
same directory and are reachable via :attr:`Highlight.rendered_output_path`
once the renderer fills it in.

Stale-hash guard (schema v2): every highlight records the parent
*source media file*'s :func:`~core.cache.cache_key` at authoring time
— path + mtime + size, the same heuristic used for transcript caching.
The renderer refuses to run when the live source file's cache_key
diverges. **The hash deliberately does NOT track Document state.**
Highlight spans are coordinates in source time; intra-doc edits (cuts,
restores, moves) don't shift them. Only file-replacement at the OS
layer invalidates a highlight, and that's exactly what cache_key
detects. Storing the document state hash (the v1 schema) was wrong:
every cut to the parent doc would have spuriously invalidated unrelated
highlights against the same source file. The reason validator
(:func:`~core.edit_events.is_valid_reason`) already accepts a
``highlight`` prefix, so 6c-1 needs no new categories.

v1 files (which used ``parent_document_state_hash``) cannot be migrated
silently — the v1 hash is incomparable to a v2 source hash. Loading
a v1 highlight raises :class:`~core.document.UnsupportedSchemaError`
with a remediation message; the user must re-propose against the
current source.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from core.document import UnsupportedSchemaError
from core.edit_events import is_valid_reason

HIGHLIGHT_SCHEMA_VERSION = 2
"""On-disk schema version for highlight JSON files.

v1 stored ``parent_document_state_hash`` (sha256 of full Document JSON);
intra-doc edits incorrectly invalidated source-time spans. v2 stores
``parent_source_hash`` (the media file's :func:`~core.cache.cache_key`)
so only file-replacement triggers staleness. v1 is unsupported — see
the module docstring.
"""

ReframeMode = Literal["speaker_locked", "center"]
"""How a highlight crops the source frame onto the 9:16 output canvas.

- ``"speaker_locked"`` (default): detect the dominant face once at the
  span's midpoint and hold a static crop centered on it for the
  duration. Falls back to ``"center"`` at render time if no face is
  detected (the fallback is logged, not raised).
- ``"center"``: static center crop of the source's 16:9 frame.

Dynamic speaker tracking (continuous follow) is Phase 7+.
"""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StaleHighlightError(ValueError):
    """Raised when a highlight's recorded parent source hash doesn't match.

    The remediation is identical to :class:`~core.proposal.StaleProposalError`:
    re-author the highlight against the current source file. Render
    refuses to run on a stale highlight because the source media has
    been replaced and the span's coordinates may no longer point at the
    same content.
    """


# ---------------------------------------------------------------------------
# Highlight dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Highlight:
    """A serializable instruction to render a 9:16 vertical clip.

    Field semantics:

    - ``schema_version`` — on-disk version. v2 is the only shape today;
      v1 raises :class:`~core.document.UnsupportedSchemaError` on load.
    - ``highlight_id`` — sortable, mostly-unique id; also the filename
      stem (``<id>.highlight.json`` / ``<id>.highlight.mp4``).
    - ``created_at`` — UTC authoring time. Persisted via ``isoformat``.
    - ``parent_document_path`` — absolute path to the parent
      :class:`~core.document.Document` JSON.
    - ``parent_source_hash`` — :func:`~core.cache.cache_key` of the
      ``span_source_path`` media file at authoring time. Compared
      against the live cache_key at render time; mismatch raises
      :class:`StaleHighlightError`. Stores the media file's identity,
      *not* the Document state — intra-doc edits don't invalidate
      source-time spans.
    - ``span_source_path`` — the media file the highlight samples
      from. Almost always equals
      ``parent_doc.sources[…].path`` for a single-source highlight,
      but stored explicitly so a future multi-source parent can carry
      a highlight that samples one specific source.
    - ``span_source_start`` / ``span_source_end`` — the highlight's
      window in source time (seconds). Variable-length, content-driven
      (no target duration).
    - ``reason`` — required, non-empty rationale. Re-validated on load
      via :func:`~core.edit_events.is_valid_reason`.
    - ``reframe_mode`` — see :data:`ReframeMode`.
    - ``captions_enabled`` — when True the renderer burns SRT captions
      derived from the parent doc's word timestamps. Default styling.
    - ``rendered_output_path`` — absolute path to the rendered
      ``<id>.highlight.mp4``. ``None`` until the renderer succeeds and
      :func:`mark_rendered` writes the back-reference. The renderer
      keeps Highlights frozen by re-writing the JSON on disk rather
      than mutating the in-memory dataclass.
    """

    highlight_id: str
    created_at: datetime
    parent_document_path: Path
    parent_source_hash: str
    span_source_path: Path
    span_source_start: float
    span_source_end: float
    reason: str
    reframe_mode: ReframeMode = "speaker_locked"
    captions_enabled: bool = False
    rendered_output_path: Path | None = None
    schema_version: int = HIGHLIGHT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.span_source_end <= self.span_source_start:
            raise ValueError(
                f"Highlight span must have positive duration; got "
                f"start={self.span_source_start!r} end={self.span_source_end!r}"
            )
        if not is_valid_reason(self.reason):
            raise ValueError(
                f"Highlight.reason {self.reason!r} is not a valid rationale; "
                "see core.edit_events.is_valid_reason"
            )
        if self.reframe_mode not in ("speaker_locked", "center"):
            raise ValueError(
                f"Highlight.reframe_mode must be 'speaker_locked' or 'center', "
                f"got {self.reframe_mode!r}"
            )

    @property
    def duration(self) -> float:
        return float(self.span_source_end) - float(self.span_source_start)

    # ----- JSON IO -------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema_version": int(self.schema_version),
            "highlight_id": self.highlight_id,
            "created_at": self.created_at.isoformat(),
            "parent_document_path": str(self.parent_document_path),
            "parent_source_hash": self.parent_source_hash,
            "span_source_path": str(self.span_source_path),
            "span_source_start": float(self.span_source_start),
            "span_source_end": float(self.span_source_end),
            "reason": self.reason,
            "reframe_mode": self.reframe_mode,
            "captions_enabled": bool(self.captions_enabled),
            "rendered_output_path": (
                None
                if self.rendered_output_path is None
                else str(self.rendered_output_path)
            ),
        }
        return out

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Highlight:
        version_raw = data.get("schema_version")
        if version_raw is None:
            raise UnsupportedSchemaError(
                "Highlight JSON has no 'schema_version' field; cannot load."
            )
        version = int(version_raw)
        if version == 1:
            raise UnsupportedSchemaError(
                "Highlight schema v1 is unsupported (it stored "
                "parent_document_state_hash, which incorrectly invalidated "
                "source-time spans on intra-doc edits). Re-propose this "
                "highlight against the current source to get a v2 record."
            )
        if version != HIGHLIGHT_SCHEMA_VERSION:
            raise UnsupportedSchemaError(
                f"Highlight JSON schema_version={version!r} unsupported "
                f"(this build expects {HIGHLIGHT_SCHEMA_VERSION})."
            )
        rendered_raw = data.get("rendered_output_path")
        return cls(
            highlight_id=str(data["highlight_id"]),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            parent_document_path=Path(str(data["parent_document_path"])),
            parent_source_hash=str(data["parent_source_hash"]),
            span_source_path=Path(str(data["span_source_path"])),
            span_source_start=float(data["span_source_start"]),
            span_source_end=float(data["span_source_end"]),
            reason=str(data["reason"]),
            reframe_mode=str(data.get("reframe_mode", "speaker_locked")),  # type: ignore[arg-type]
            captions_enabled=bool(data.get("captions_enabled", False)),
            rendered_output_path=(
                None if rendered_raw is None else Path(str(rendered_raw))
            ),
            schema_version=version,
        )


# ---------------------------------------------------------------------------
# Sidecar layout / persistence
# ---------------------------------------------------------------------------


_HIGHLIGHT_DIR_SUFFIX = ".highlights"


def highlights_dir_for_document(document_path: Path) -> Path:
    """Return the sidecar directory ``<document_path>.highlights``.

    Mirrors :func:`core.proposal.proposals_dir_for_document` — keep
    derived artifacts in a predictable subdirectory next to the source
    document. Rendered ``<id>.highlight.mp4`` files land in the same
    directory as the JSON sidecars so a "delete all my highlights"
    becomes one ``rm -rf``.
    """
    p = Path(document_path)
    return p.with_name(p.name + _HIGHLIGHT_DIR_SUFFIX)


def _highlight_json_path(dir_: Path, highlight_id: str) -> Path:
    return dir_ / f"{highlight_id}.highlight.json"


def rendered_output_path_for(dir_: Path, highlight_id: str) -> Path:
    """Conventional output path for ``<id>.highlight.mp4`` next to its sidecar.

    Returned even if the file doesn't exist yet — the renderer uses
    this to *decide* where to write.
    """
    return dir_ / f"{highlight_id}.highlight.mp4"


def _new_highlight_id() -> str:
    """Sortable, mostly-unique id (timestamp + 4-byte random suffix)."""
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{ts}-{secrets.token_hex(4)}"


def write_highlight(
    document_path: Path,
    highlight: Highlight,
) -> tuple[Highlight, Path]:
    """Persist ``highlight`` next to ``document_path``; return ``(materialized, path)``.

    "Materialized" means the returned highlight has a non-empty
    ``highlight_id`` and ``created_at`` set — fields that callers may
    leave defaults on construction. The on-disk JSON reflects the
    materialized form.
    """
    dir_ = highlights_dir_for_document(document_path)
    dir_.mkdir(parents=True, exist_ok=True)
    materialized = highlight
    if not materialized.highlight_id:
        materialized = replace(materialized, highlight_id=_new_highlight_id())
    out_path = _highlight_json_path(dir_, materialized.highlight_id)
    out_path.write_text(
        json.dumps(materialized.to_json(), indent=2),
        encoding="utf-8",
    )
    return materialized, out_path


def read_highlight(document_path: Path, highlight_id: str) -> Highlight:
    """Load ``<id>.highlight.json`` from the document's sidecar directory."""
    dir_ = highlights_dir_for_document(document_path)
    p = _highlight_json_path(dir_, highlight_id)
    if not p.is_file():
        raise FileNotFoundError(f"highlight {highlight_id!r} not found at {p}")
    return Highlight.from_json(json.loads(p.read_text(encoding="utf-8")))


def list_highlights_for_document(document_path: Path) -> list[Highlight]:
    """Return every highlight in the sidecar directory, chronologically.

    An absent directory returns an empty list — not an error. Files
    that fail to parse are silently skipped (matches
    :func:`core.proposal.list_proposals`).
    """
    dir_ = highlights_dir_for_document(document_path)
    if not dir_.is_dir():
        return []
    out: list[Highlight] = []
    for entry in sorted(dir_.glob("*.highlight.json")):
        try:
            out.append(Highlight.from_json(json.loads(entry.read_text(encoding="utf-8"))))
        except (ValueError, json.JSONDecodeError):
            continue
    return out


def mark_rendered(
    document_path: Path,
    highlight: Highlight,
    rendered_output_path: Path,
) -> Highlight:
    """Persist ``rendered_output_path`` onto ``highlight``'s sidecar JSON.

    Returns the updated dataclass. The renderer is the canonical caller —
    apply_highlight (Phase 6c-2) will also reach for this when
    re-rendering a stale output.
    """
    updated = replace(highlight, rendered_output_path=rendered_output_path)
    dir_ = highlights_dir_for_document(document_path)
    dir_.mkdir(parents=True, exist_ok=True)
    out_path = _highlight_json_path(dir_, updated.highlight_id)
    out_path.write_text(json.dumps(updated.to_json(), indent=2), encoding="utf-8")
    return updated


# ---------------------------------------------------------------------------
# Render-result sidecar (Phase 6c-2)
# ---------------------------------------------------------------------------
#
# Distinct artifact from ``Highlight`` itself. Responsibilities:
#
# * The Highlight owns "where my output mp4 currently is" — one path
#   per highlight, overwritten on re-render. ``rendered_output_path``.
# * The HighlightRenderResult owns "what happened on a specific render
#   run" — a fresh file per ``apply_highlight`` invocation, recording
#   wall-clock, fallback state, crop window, and the source hash
#   matched at render time. Re-renders accumulate sidecars; the .mp4
#   is overwritten. No deduplication.
#
# Same shape philosophy as ``ApplyResult`` for proposals: the apply-
# result file is the auditable record of one apply run, distinct from
# the proposal that authored it.


@dataclass(frozen=True)
class HighlightRenderResult:
    """Per-run record of a single :func:`render_highlight` invocation."""

    render_result_id: str
    highlight_id: str
    created_at: datetime
    output_path: Path
    parent_source_hash: str
    face_detection_used: str
    """One of ``"speaker_locked"`` / ``"speaker_locked_fallback_to_center"``
    / ``"center"`` — see :class:`core.highlight_render.HighlightRenderMetadata`."""
    crop_box: tuple[int, int, int, int]
    """The ``(x, y, w, h)`` crop window applied before scale=1080:1920."""
    wall_clock_s: float

    SCHEMA_VERSION: int = 1

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": int(self.SCHEMA_VERSION),
            "render_result_id": self.render_result_id,
            "highlight_id": self.highlight_id,
            "created_at": self.created_at.isoformat(),
            "output_path": str(self.output_path),
            "parent_source_hash": self.parent_source_hash,
            "face_detection_used": self.face_detection_used,
            "crop_box": {
                "x": int(self.crop_box[0]),
                "y": int(self.crop_box[1]),
                "w": int(self.crop_box[2]),
                "h": int(self.crop_box[3]),
            },
            "wall_clock_s": float(self.wall_clock_s),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> HighlightRenderResult:
        version_raw = data.get("schema_version")
        if version_raw is None:
            raise UnsupportedSchemaError(
                "HighlightRenderResult JSON has no 'schema_version' field."
            )
        version = int(version_raw)
        if version != 1:
            raise UnsupportedSchemaError(
                f"HighlightRenderResult schema_version={version!r} unsupported "
                "(this build expects 1)."
            )
        crop = data["crop_box"]
        return cls(
            render_result_id=str(data["render_result_id"]),
            highlight_id=str(data["highlight_id"]),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            output_path=Path(str(data["output_path"])),
            parent_source_hash=str(data["parent_source_hash"]),
            face_detection_used=str(data["face_detection_used"]),
            crop_box=(int(crop["x"]), int(crop["y"]), int(crop["w"]), int(crop["h"])),
            wall_clock_s=float(data["wall_clock_s"]),
        )


def _render_result_path(dir_: Path, render_result_id: str) -> Path:
    return dir_ / f"{render_result_id}.render-result.json"


def write_render_result(
    document_path: Path,
    result: HighlightRenderResult,
) -> Path:
    """Persist ``result`` next to ``document_path``; return the file path."""
    dir_ = highlights_dir_for_document(document_path)
    dir_.mkdir(parents=True, exist_ok=True)
    out = _render_result_path(dir_, result.render_result_id)
    out.write_text(json.dumps(result.to_json(), indent=2), encoding="utf-8")
    return out


def read_render_result(
    document_path: Path, render_result_id: str
) -> HighlightRenderResult:
    """Load a render-result by id from the document's sidecar dir."""
    dir_ = highlights_dir_for_document(document_path)
    p = _render_result_path(dir_, render_result_id)
    if not p.is_file():
        raise FileNotFoundError(
            f"render-result {render_result_id!r} not found at {p}"
        )
    return HighlightRenderResult.from_json(json.loads(p.read_text(encoding="utf-8")))


def list_render_results_for_document(
    document_path: Path,
    *,
    highlight_id: str | None = None,
) -> list[HighlightRenderResult]:
    """Return every render-result in the sidecar dir, chronologically.

    ``highlight_id`` filters to results for one highlight; ``None``
    returns every result for the document. Files that fail to parse
    are silently skipped (matches :func:`list_highlights_for_document`).
    """
    dir_ = highlights_dir_for_document(document_path)
    if not dir_.is_dir():
        return []
    out: list[HighlightRenderResult] = []
    for entry in sorted(dir_.glob("*.render-result.json")):
        try:
            rr = HighlightRenderResult.from_json(
                json.loads(entry.read_text(encoding="utf-8"))
            )
        except (ValueError, json.JSONDecodeError, UnsupportedSchemaError):
            continue
        if highlight_id is not None and rr.highlight_id != highlight_id:
            continue
        out.append(rr)
    return out


def new_render_result_id() -> str:
    """Public id minter (timestamp + 4-byte random suffix)."""
    return _new_highlight_id()
