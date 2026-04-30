"""Highlight artifact — a span (or ordered fragments) marked for vertical clip render.

Phase 6c-1 introduced highlights as single-source single-span "this
piece of video, reframed 9:16, optionally captioned" artifacts. Phase 7
generalizes them: a highlight is now an ordered tuple of fragments
(:class:`SubSpan`), each of which names a source media path + an
``[start, end]`` interval. A single-camera podcast highlight is still
the common case (one fragment, one source); a multi-camera highlight
strings together fragments from several cameras with audio always
coming from a designated audio master.

When a highlight carries a ``sync_group_id`` the renderer pulls
*video* from each fragment's source and *audio* from the sync group's
audio master, translated through the per-camera offset (see
:mod:`core.sync`). When ``sync_group_id`` is ``None`` the renderer
treats every fragment's audio as authoritative — that's the
single-camera path that 6c-1 already handled, plus the new
multi-fragment-same-source case where you stitch several segments
from one camera.

Stale-hash guard (schema v3): every highlight records the
:func:`~core.cache.cache_key` of every source it references at
authoring time, keyed by source path string. The renderer compares
each entry against the live cache_key at apply time and refuses with
:class:`StaleHighlightError` on the first mismatch. The hash
deliberately tracks *files* (path + mtime + size), not Document
state — intra-doc edits to the parent timeline don't invalidate
source-time spans. The sync group's own audio master is hashed inside
the sync-group sidecar (see :class:`core.sync.SyncGroup`); the
highlight's ``parent_source_hashes`` covers only the per-fragment
source paths that appear in ``sub_spans``.

Storage continues to mirror the proposals pattern. Each parent
Document gets ``<doc>.highlights/`` containing one
``<id>.highlight.json`` per highlight; rendered output files
(``<id>.highlight.mp4``) live in the same directory.

Schema migration:

- v1 (legacy ``parent_document_state_hash``) raises
  :class:`~core.document.UnsupportedSchemaError` — its hash is
  incomparable to the v3 source-key dict and the bug it carried (see
  6c-A) is the reason the field was retired.
- v2 (single-span ``parent_source_hash`` + ``span_source_path`` /
  ``span_source_start`` / ``span_source_end``) migrates on read into a
  v3 highlight with one :class:`SubSpan` and a one-entry
  ``parent_source_hashes`` dict. No ``sync_group_id``. The on-disk
  file is *not* rewritten as a side effect of loading; the next save
  emits v3 (write-through migration, matching
  :class:`core.document.Document`'s policy).
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from core.document import UnsupportedSchemaError
from core.edit_events import is_valid_reason

HIGHLIGHT_SCHEMA_VERSION = 3
"""On-disk schema version for highlight JSON files.

* v1 stored ``parent_document_state_hash`` (sha256 of full Document
  JSON); intra-doc edits incorrectly invalidated source-time spans.
  v1 is unsupported — :meth:`Highlight.from_json` raises with a
  re-propose remediation message.
* v2 (Phase 6c) stored a single-source span via
  ``parent_source_hash`` + ``span_source_path`` /
  ``span_source_start`` / ``span_source_end``. v2 files migrate on
  read into v3 single-fragment highlights with no sync group.
* v3 (Phase 7) stores ``sub_spans`` (an ordered list of
  ``{source_path, source_start, source_end, reason}`` fragments) and
  ``parent_source_hashes`` (dict path-string → ``cache_key``) so a
  highlight can reference multiple cameras. Optional
  ``sync_group_id`` ties the highlight to a :class:`core.sync.SyncGroup`
  whose audio master overrides per-camera audio.
"""

ReframeMode = Literal["speaker_locked", "center"]
"""How a highlight crops the source frame onto the 9:16 output canvas.

- ``"speaker_locked"`` (default): detect the dominant face once *per
  unique source* at one of its fragment midpoints and hold a static
  crop centered on it for every fragment from that source. Falls
  back to ``"center"`` per-source if no face is detected; the
  fallback is logged, not raised, and recorded in the render-result
  sidecar.
- ``"center"``: static center crop of the source's frame.

Dynamic per-frame speaker tracking is still future work.
"""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StaleHighlightError(ValueError):
    """Raised when a highlight's recorded source hash doesn't match.

    The error names the offending source path so the operator (or
    Claude) knows which file changed. The remediation is to re-author
    the highlight against the current source. Render refuses to run
    on a stale highlight because the bytes the span referenced may no
    longer be there.
    """


# ---------------------------------------------------------------------------
# SubSpan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubSpan:
    """One ordered fragment in a highlight's playlist.

    A SubSpan names a source media path + a half-open interval in
    *source time*. ``reason`` is optional rationale at fragment
    granularity (e.g., "switch to wide for the laughter beat") — the
    overall highlight has its own ``reason`` field; per-fragment
    reasons stay free-form to support the camera-angle decision
    rationale that the propose tool now asks the model to surface.
    """

    source_path: Path
    source_start: float
    source_end: float
    reason: str = ""

    def __post_init__(self) -> None:
        if self.source_end <= self.source_start:
            raise ValueError(
                "SubSpan must have positive duration; got "
                f"start={self.source_start!r} end={self.source_end!r}"
            )

    @property
    def duration(self) -> float:
        return float(self.source_end) - float(self.source_start)

    def to_json(self) -> dict[str, Any]:
        return {
            "source_path": str(self.source_path),
            "source_start": float(self.source_start),
            "source_end": float(self.source_end),
            "reason": self.reason,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SubSpan:
        return cls(
            source_path=Path(str(data["source_path"])),
            source_start=float(data["source_start"]),
            source_end=float(data["source_end"]),
            reason=str(data.get("reason", "")),
        )


# ---------------------------------------------------------------------------
# Highlight dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Highlight:
    """A serializable instruction to render a 9:16 vertical clip.

    Phase 7 fields:

    - ``sub_spans`` — ordered tuple of :class:`SubSpan`. A
      single-camera highlight has length 1; a multi-camera highlight
      has the playlist of camera + interval picks. The renderer
      concatenates fragments in order. Empty tuples are rejected.
    - ``parent_source_hashes`` — :class:`dict` mapping
      ``str(source_path)`` → :func:`~core.cache.cache_key` value at
      authoring time. The renderer iterates the dict at apply time
      and refuses if any entry's live cache_key has drifted. A
      single-source highlight has one entry; a multi-source one has
      one entry per unique camera.
    - ``sync_group_id`` — id of the :class:`core.sync.SyncGroup` whose
      audio master overrides the cameras' audio. ``None`` means
      "use camera audio" (the single-source single-fragment historical
      behavior or a multi-fragment same-source stitch).

    Bookkeeping fields are unchanged from v2:

    - ``schema_version`` — on-disk version (3 today).
    - ``highlight_id`` — sortable, mostly-unique id; also the filename
      stem.
    - ``created_at`` — UTC authoring time.
    - ``parent_document_path`` — absolute path to the parent
      :class:`~core.document.Document` JSON.
    - ``reason`` — required rationale, validated by
      :func:`~core.edit_events.is_valid_reason`.
    - ``reframe_mode`` — see :data:`ReframeMode`.
    - ``captions_enabled`` — burn captions when True. For sync-group
      highlights, captions come from the audio master's transcript
      (the parent doc, since the parent doc is the audio master's
      transcript by convention). For non-sync-group highlights captions
      come from the parent doc as before.
    - ``rendered_output_path`` — populated by :func:`mark_rendered`.
    """

    highlight_id: str
    created_at: datetime
    parent_document_path: Path
    parent_source_hashes: dict[str, str]
    sub_spans: tuple[SubSpan, ...]
    reason: str
    reframe_mode: ReframeMode = "speaker_locked"
    captions_enabled: bool = False
    sync_group_id: str | None = None
    rendered_output_path: Path | None = None
    schema_version: int = HIGHLIGHT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.sub_spans:
            raise ValueError("Highlight must have at least one sub_span")
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
        # Every unique source path in sub_spans must have a recorded hash.
        unique_paths = {str(s.source_path) for s in self.sub_spans}
        missing = unique_paths - set(self.parent_source_hashes)
        if missing:
            raise ValueError(
                f"Highlight.parent_source_hashes is missing entries for "
                f"sources {sorted(missing)!r}"
            )

    @property
    def duration(self) -> float:
        """Total playlist duration in source time (sum of fragment durations)."""
        return sum(s.duration for s in self.sub_spans)

    @property
    def unique_source_paths(self) -> tuple[Path, ...]:
        """Deduplicated source paths in playlist-first-seen order."""
        seen: dict[str, Path] = {}
        for s in self.sub_spans:
            key = str(s.source_path)
            if key not in seen:
                seen[key] = s.source_path
        return tuple(seen.values())

    # ----- v2 single-span compatibility shims ----------------------------
    #
    # Phase 7 widened the schema, but a fair amount of internal call-site
    # code (and the v2 tests that have been the canonical fixtures since
    # 6c-1) reaches for ``span_source_path`` / ``span_source_start`` /
    # ``span_source_end`` on a Highlight. These properties keep that read
    # path working for single-fragment highlights, and raise a clear
    # ``ValueError`` for multi-fragment ones — surfacing "this caller is
    # making a single-fragment assumption" early instead of silently
    # operating on the first fragment.

    @property
    def span_source_path(self) -> Path:
        if len(self.sub_spans) != 1:
            raise ValueError(
                f"Highlight {self.highlight_id!r} has "
                f"{len(self.sub_spans)} fragments; span_source_path is only "
                "defined for single-fragment highlights"
            )
        return self.sub_spans[0].source_path

    @property
    def span_source_start(self) -> float:
        if len(self.sub_spans) != 1:
            raise ValueError(
                f"Highlight {self.highlight_id!r} has "
                f"{len(self.sub_spans)} fragments; span_source_start is "
                "only defined for single-fragment highlights"
            )
        return float(self.sub_spans[0].source_start)

    @property
    def span_source_end(self) -> float:
        if len(self.sub_spans) != 1:
            raise ValueError(
                f"Highlight {self.highlight_id!r} has "
                f"{len(self.sub_spans)} fragments; span_source_end is "
                "only defined for single-fragment highlights"
            )
        return float(self.sub_spans[0].source_end)

    @property
    def parent_source_hash(self) -> str:
        """Single-source convenience accessor.

        Mirrors the v2 ``parent_source_hash`` field for callers that
        haven't been updated. Raises when the highlight references
        multiple sources.
        """
        if len(self.parent_source_hashes) != 1:
            raise ValueError(
                f"Highlight {self.highlight_id!r} references "
                f"{len(self.parent_source_hashes)} sources; "
                "parent_source_hash is only defined for single-source "
                "highlights"
            )
        return next(iter(self.parent_source_hashes.values()))

    # ----- JSON IO -------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema_version": int(self.schema_version),
            "highlight_id": self.highlight_id,
            "created_at": self.created_at.isoformat(),
            "parent_document_path": str(self.parent_document_path),
            "parent_source_hashes": {
                str(k): str(v) for k, v in self.parent_source_hashes.items()
            },
            "sub_spans": [s.to_json() for s in self.sub_spans],
            "reason": self.reason,
            "reframe_mode": self.reframe_mode,
            "captions_enabled": bool(self.captions_enabled),
            "sync_group_id": self.sync_group_id,
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
                "highlight against the current source to get a v3 record."
            )
        if version == 2:
            return _load_v2_as_v3(cls, data)
        if version != HIGHLIGHT_SCHEMA_VERSION:
            raise UnsupportedSchemaError(
                f"Highlight JSON schema_version={version!r} unsupported "
                f"(this build expects {HIGHLIGHT_SCHEMA_VERSION})."
            )
        rendered_raw = data.get("rendered_output_path")
        sub_spans = tuple(
            SubSpan.from_json(s) for s in data.get("sub_spans", [])
        )
        hashes_raw = data.get("parent_source_hashes", {})
        return cls(
            highlight_id=str(data["highlight_id"]),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            parent_document_path=Path(str(data["parent_document_path"])),
            parent_source_hashes={str(k): str(v) for k, v in hashes_raw.items()},
            sub_spans=sub_spans,
            reason=str(data["reason"]),
            reframe_mode=str(data.get("reframe_mode", "speaker_locked")),  # type: ignore[arg-type]
            captions_enabled=bool(data.get("captions_enabled", False)),
            sync_group_id=(
                None
                if data.get("sync_group_id") is None
                else str(data["sync_group_id"])
            ),
            rendered_output_path=(
                None if rendered_raw is None else Path(str(rendered_raw))
            ),
            schema_version=version,
        )


def _load_v2_as_v3(cls: type[Highlight], data: dict[str, Any]) -> Highlight:
    """Migrate a v2 Highlight JSON dict into a v3 Highlight in memory.

    v2 had:
      ``parent_source_hash: str``,
      ``span_source_path``, ``span_source_start``, ``span_source_end``.
    v3 has:
      ``parent_source_hashes: dict[str, str]``,
      ``sub_spans: tuple[SubSpan, ...]``,
      optional ``sync_group_id``.

    The migration packs the single span into one :class:`SubSpan`,
    wraps the single hash into a one-entry dict keyed by the span's
    source path, and leaves ``sync_group_id`` ``None``. Read-only
    migration: the on-disk file is not rewritten until the next save.
    """
    span_path = str(data["span_source_path"])
    sub_spans = (
        SubSpan(
            source_path=Path(span_path),
            source_start=float(data["span_source_start"]),
            source_end=float(data["span_source_end"]),
        ),
    )
    rendered_raw = data.get("rendered_output_path")
    return cls(
        highlight_id=str(data["highlight_id"]),
        created_at=datetime.fromisoformat(str(data["created_at"])),
        parent_document_path=Path(str(data["parent_document_path"])),
        parent_source_hashes={span_path: str(data["parent_source_hash"])},
        sub_spans=sub_spans,
        reason=str(data["reason"]),
        reframe_mode=str(data.get("reframe_mode", "speaker_locked")),  # type: ignore[arg-type]
        captions_enabled=bool(data.get("captions_enabled", False)),
        sync_group_id=None,
        rendered_output_path=(
            None if rendered_raw is None else Path(str(rendered_raw))
        ),
        # Note: schema_version reflects the in-memory v3 shape, not the
        # on-disk v2. The next save (via to_json) emits v3 and the file
        # gets the new shape lazily.
        schema_version=HIGHLIGHT_SCHEMA_VERSION,
    )


# ---------------------------------------------------------------------------
# Sidecar layout / persistence
# ---------------------------------------------------------------------------


_HIGHLIGHT_DIR_SUFFIX = ".highlights"


def highlights_dir_for_document(document_path: Path) -> Path:
    """Return the sidecar directory ``<document_path>.highlights``."""
    p = Path(document_path)
    return p.with_name(p.name + _HIGHLIGHT_DIR_SUFFIX)


def _highlight_json_path(dir_: Path, highlight_id: str) -> Path:
    return dir_ / f"{highlight_id}.highlight.json"


def rendered_output_path_for(dir_: Path, highlight_id: str) -> Path:
    """Conventional output path for ``<id>.highlight.mp4`` next to its sidecar."""
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

    "Materialized" means: ``highlight_id`` is assigned (a fresh
    timestamp-prefixed id when the input left the field blank); the
    on-disk JSON reflects the materialized form.
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
    that fail to parse are silently skipped (matches the proposals
    listing pattern).
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

    Returns the updated dataclass.
    """
    updated = replace(highlight, rendered_output_path=rendered_output_path)
    dir_ = highlights_dir_for_document(document_path)
    dir_.mkdir(parents=True, exist_ok=True)
    out_path = _highlight_json_path(dir_, updated.highlight_id)
    out_path.write_text(json.dumps(updated.to_json(), indent=2), encoding="utf-8")
    return updated


def reassign_fragment_source(
    document_path: Path,
    highlight: Highlight,
    fragment_index: int,
    new_source_path: Path,
    new_source_hash: str,
) -> Highlight:
    """Swap one fragment's ``source_path``; rewrite the sidecar.

    Used by the GUI when the operator manually picks a different
    camera angle for a fragment within a sync-group highlight. The
    fragment's ``source_start``/``source_end`` are kept verbatim — the
    sync group's offset translation handles the rest at render time.

    The new source's hash is recorded onto ``parent_source_hashes``;
    obsolete entries (no fragment references that path anymore) are
    pruned to keep the stale-guard surface tight. Returns the updated
    Highlight (also persisted to disk).
    """
    if not (0 <= fragment_index < len(highlight.sub_spans)):
        raise IndexError(
            f"fragment_index {fragment_index} out of range for "
            f"{len(highlight.sub_spans)}-fragment highlight"
        )
    new_sub_spans = list(highlight.sub_spans)
    old = new_sub_spans[fragment_index]
    new_sub_spans[fragment_index] = SubSpan(
        source_path=new_source_path,
        source_start=old.source_start,
        source_end=old.source_end,
        reason=old.reason,
    )
    new_hashes = {
        str(s.source_path): highlight.parent_source_hashes.get(
            str(s.source_path), ""
        )
        for s in new_sub_spans
    }
    new_hashes[str(new_source_path)] = new_source_hash
    updated = replace(
        highlight,
        sub_spans=tuple(new_sub_spans),
        parent_source_hashes=new_hashes,
        # Re-render is required after a reassignment.
        rendered_output_path=None,
    )
    dir_ = highlights_dir_for_document(document_path)
    dir_.mkdir(parents=True, exist_ok=True)
    out_path = _highlight_json_path(dir_, updated.highlight_id)
    out_path.write_text(json.dumps(updated.to_json(), indent=2), encoding="utf-8")
    return updated


# ---------------------------------------------------------------------------
# Render-result sidecar (Phase 6c-2, extended in Phase 7)
# ---------------------------------------------------------------------------
#
# The Highlight owns "where my output mp4 currently is" — one path per
# highlight, overwritten on re-render. The HighlightRenderResult owns
# "what happened on a specific render run" — a fresh file per
# ``apply_highlight`` invocation, recording wall-clock, fallback state,
# crop window, source hash. Phase 7 extends the wire shape with
# per-source crop info (``crop_boxes_by_source``) so a multi-camera
# render-result records the crop applied to each unique camera.
# ``crop_box`` (singular) stays for single-source highlights and is
# the first crop in ``crop_boxes_by_source`` when the highlight has
# more than one source — the field stays present for backward compat
# with v1 render-result readers.


@dataclass(frozen=True)
class HighlightRenderResult:
    """Per-run record of a single :func:`render_highlight` invocation."""

    render_result_id: str
    highlight_id: str
    created_at: datetime
    output_path: Path
    parent_source_hashes: dict[str, str]
    """v2 expanded the field from a single ``parent_source_hash`` to a
    dict keyed by source path. v1 render-result files still load via
    :meth:`from_json` (the migration pulls the single value into a
    one-entry dict using the highlight's first sub_span source as the
    key)."""
    face_detection_used: str
    """One of ``"speaker_locked"`` /
    ``"speaker_locked_fallback_to_center"`` / ``"center"`` — see
    :class:`core.highlight_render.HighlightRenderMetadata`."""
    crop_box: tuple[int, int, int, int]
    """The ``(x, y, w, h)`` crop window applied to the *first* unique
    source. Kept for single-source compat; multi-camera renders also
    populate ``crop_boxes_by_source`` (below) with one entry per
    unique source."""
    wall_clock_s: float
    crop_boxes_by_source: dict[str, tuple[int, int, int, int]] = field(
        default_factory=dict
    )
    """Per-unique-source crop window. Populated for every render —
    single-source renders have one entry. Older v1 render-results that
    only wrote the singular ``crop_box`` migrate to a one-entry dict
    on read."""
    sync_group_id: str | None = None
    """The sync group used at render time, when applicable."""

    SCHEMA_VERSION: int = 2

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": int(self.SCHEMA_VERSION),
            "render_result_id": self.render_result_id,
            "highlight_id": self.highlight_id,
            "created_at": self.created_at.isoformat(),
            "output_path": str(self.output_path),
            "parent_source_hashes": {
                str(k): str(v) for k, v in self.parent_source_hashes.items()
            },
            "face_detection_used": self.face_detection_used,
            "crop_box": {
                "x": int(self.crop_box[0]),
                "y": int(self.crop_box[1]),
                "w": int(self.crop_box[2]),
                "h": int(self.crop_box[3]),
            },
            "crop_boxes_by_source": {
                str(k): {
                    "x": int(v[0]),
                    "y": int(v[1]),
                    "w": int(v[2]),
                    "h": int(v[3]),
                }
                for k, v in self.crop_boxes_by_source.items()
            },
            "sync_group_id": self.sync_group_id,
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
        if version not in (1, 2):
            raise UnsupportedSchemaError(
                f"HighlightRenderResult schema_version={version!r} unsupported "
                "(this build expects 2)."
            )
        crop = data["crop_box"]
        crop_box = (
            int(crop["x"]),
            int(crop["y"]),
            int(crop["w"]),
            int(crop["h"]),
        )
        # v1 → v2 lift: single ``parent_source_hash`` becomes a one-entry
        # dict keyed by the literal string "primary" (we lost the
        # source path identity at v1 — clients should not rely on the
        # key for v1-migrated records).
        if version == 1:
            hashes = {"primary": str(data["parent_source_hash"])}
        else:
            hashes_raw = data.get("parent_source_hashes", {})
            hashes = {str(k): str(v) for k, v in hashes_raw.items()}
        boxes_raw = data.get("crop_boxes_by_source", {})
        crop_boxes_by_source: dict[str, tuple[int, int, int, int]] = {}
        if boxes_raw:
            for k, v in boxes_raw.items():
                crop_boxes_by_source[str(k)] = (
                    int(v["x"]),
                    int(v["y"]),
                    int(v["w"]),
                    int(v["h"]),
                )
        else:
            # v1 records or v2 records that only carry singular crop_box.
            crop_boxes_by_source = {
                next(iter(hashes), "primary"): crop_box
            }
        return cls(
            render_result_id=str(data["render_result_id"]),
            highlight_id=str(data["highlight_id"]),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            output_path=Path(str(data["output_path"])),
            parent_source_hashes=hashes,
            face_detection_used=str(data["face_detection_used"]),
            crop_box=crop_box,
            crop_boxes_by_source=crop_boxes_by_source,
            sync_group_id=(
                None
                if data.get("sync_group_id") is None
                else str(data["sync_group_id"])
            ),
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
    """Return every render-result in the sidecar dir, chronologically."""
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
