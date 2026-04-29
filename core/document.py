"""Canonical transcription document types.

Phase 4a introduced :class:`Word` and :class:`Segment` so the rest of the
system stops leaking faster-whisper's objects across module boundaries.
Phase 4b adds :class:`Document` and :class:`CutMark` on top of these,
plus JSON serialization with explicit schema versioning. Phase 4f-3
ships schema v2: the live Document model now stores ``sources`` (a
``dict[str, MediaSource]``) and ``ranges`` (a list of keep-ranges on
the editable timeline) instead of v1's flat ``media_path`` /
``duration`` / ``cuts`` triple. v1 sidecars on disk continue to load
via the migration in :meth:`Document.from_json`.

Immutability contract: :class:`Document`, :class:`MediaSource`,
:class:`Range`, :class:`Segment`, :class:`Word` (and the legacy
:class:`CutMark`) are all ``frozen=True``. Edit commands in
:mod:`core.editing` produce *new* Documents via
:func:`dataclasses.replace`. The inner ``ranges`` and ``segments``
lists remain mutable Python lists, so callers must always pass a fresh
list (``replace(doc, ranges=[*doc.ranges, x])``) — never
``doc.ranges.append(...)``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from core.edit_events import EditEvent

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Word:
    """A single word emitted by the transcriber.

    ``probability`` is whisper's per-word confidence (0..1) and may be ``None``
    when imported from a source that doesn't carry per-word confidence (e.g.
    parsed SRT cues that only have segment-level timing).
    """

    text: str
    start: float
    end: float
    probability: float | None = None


@dataclass(frozen=True)
class Segment:
    """A transcribed segment (one SRT cue).

    ``words`` is empty when the segment was produced without word-level
    timestamps (e.g. parsed from an SRT, or transcribed with
    ``word_timestamps=False``).

    Phase 4f-3 left segments source-id-less. The single-source assumption
    holds for transcripts within Phase 4f; multi-source compositions
    (Phase 5+) will need a per-segment ``source_id`` field.
    """

    text: str
    start: float
    end: float
    words: tuple[Word, ...] = ()


@dataclass(frozen=True)
class CutMark:
    """A range of media marked for removal.

    Retired from the live Document model in Phase 4f-3 — v2 Documents
    store keep-ranges (:class:`Range`) instead. The class stays defined
    as the v1→v2 migration intermediate in :meth:`Document.from_json`.
    """

    start: float
    end: float
    reason: str = ""


@dataclass(frozen=True)
class MediaSource:
    """A single piece of source media referenced by a v2 Document.

    Phase 4f-3 introduced the multi-source model: a Document may compose
    spans from any number of media files. Each file gets a ``MediaSource``
    entry with a stable ``id`` (``"src0"``, ``"src1"``, …) used by
    :class:`Range` to point back to it.

    ``hash`` is the cache key from :func:`core.cache.cache_key` — kept on
    the source for self-describing JSON sidecars. It's redundant with the
    Document-level ``source_hash`` for single-source projects, but makes
    multi-source caches well-defined when one of several sources changes.
    """

    id: str
    path: Path
    duration: float
    hash: str = ""


@dataclass(frozen=True)
class Range:
    """A KEEP-range on the editable timeline.

    A v2 Document's ``ranges`` list is the timeline: an ordered sequence
    of these. Each range names which source it samples from and which
    ``[start, end]`` interval (in source time, not output time). Reasons
    are inherited from the v1 cut they came from when migrating, or set
    by edit commands going forward.
    """

    source_id: str
    start: float
    end: float
    reason: str = ""


class UnsupportedSchemaError(ValueError):
    """Raised when ``Document.from_json`` is asked to load an unknown schema."""


_SCHEMA_VERSION = 3.1


@dataclass(frozen=True)
class Document:
    """The canonical, serializable transcription project.

    SRT/TXT/VTT files written next to a media file are *derivatives* of this
    model — the editor round-trips through Document JSON, not through the
    subtitle files.

    Phase 6a (schema v3): the on-disk shape replaces v2's
    ``ranges: [{source_id, start, end, reason}, ...]`` with
    ``main_timeline: {clips: [{source_path, source_start, source_end, reason}, ...]}``.
    The in-memory Document keeps ``ranges: list[Range]`` as the storage
    field for backward compat; :attr:`main_timeline` is a derived
    :class:`~core.timeline.Timeline` view that renderers and editors use
    to ask `is_source_monotonic()`. The list order on ``ranges`` is
    preserved verbatim from v3 JSON load — for hand-built non-monotonic
    fixtures this list is *not* sorted by start, and ``main_timeline``
    will report ``is_source_monotonic() == False`` accordingly.

    Frozen: callers can't reassign ``doc.sources`` / ``doc.ranges`` /
    ``doc.segments``. The inner lists are still mutable Python lists, so
    edit commands are responsible for never appending to them — they
    always pass a fresh list to :func:`dataclasses.replace`.
    """

    sources: dict[str, MediaSource]
    segments: list[Segment]
    ranges: list[Range]
    language: str | None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    model_name: str = ""
    source_hash: str | None = None
    edit_log: tuple[EditEvent, ...] = ()

    SCHEMA_VERSION: ClassVar[float] = _SCHEMA_VERSION

    @property
    def main_timeline(self) -> Timeline:  # noqa: F821 — forward ref to core.timeline
        """The v3 :class:`~core.timeline.Timeline` view derived from ``ranges``.

        Each :class:`~core.document.Range` becomes one
        :class:`~core.timeline.Clip` in playlist order. The clip's
        ``source_path`` is looked up in ``sources`` by ``source_id``;
        a missing source is a hard error (the document's invariant is
        that every range references a registered source).

        The view is a fresh tuple per call — cheap, but callers that
        access it in a hot loop should cache locally.
        """
        from core.timeline import Clip, Timeline  # local import to avoid cycle
        clips: list[Clip] = []
        for r in self.ranges:
            src = self.sources.get(r.source_id)
            if src is None:
                # An unregistered source is a malformed Document; raise loudly
                # rather than synthesize a path the renderer can't validate.
                raise ValueError(
                    f"Range references source_id={r.source_id!r} not in "
                    f"sources ({list(self.sources)!r})"
                )
            clips.append(
                Clip(
                    source_path=src.path,
                    source_start=float(r.start),
                    source_end=float(r.end),
                    reason=r.reason,
                )
            )
        return Timeline(clips=tuple(clips))

    def to_json(self) -> dict[str, Any]:
        """Return a plain-dict representation safe for ``json.dumps``.

        Emits ``schema_version=3.1`` with the v3 ``main_timeline`` shape
        and the v3.1 ``edit_log`` array. Each clip carries its source
        path explicitly (a redundant copy of the data in
        ``sources[source_id].path``, but cheap and lets a future
        multi-source loader skip the indirection). ``source_hash`` is
        omitted when ``None``. ``edit_log`` is always present, even
        when empty, so consumers can rely on the key existing.
        """
        out: dict[str, Any] = {
            "schema_version": self.SCHEMA_VERSION,
            "sources": {
                sid: {
                    "id": s.id,
                    "path": str(s.path),
                    "duration": s.duration,
                    "hash": s.hash,
                }
                for sid, s in self.sources.items()
            },
            "language": self.language,
            "model_name": self.model_name,
            "created_at": self.created_at.isoformat(),
            "segments": [
                {
                    "text": s.text,
                    "start": s.start,
                    "end": s.end,
                    "words": [
                        {
                            "text": w.text,
                            "start": w.start,
                            "end": w.end,
                            "probability": w.probability,
                        }
                        for w in s.words
                    ],
                }
                for s in self.segments
            ],
            "main_timeline": {
                "clips": [
                    {
                        "source_id": r.source_id,
                        "source_path": str(self.sources[r.source_id].path)
                        if r.source_id in self.sources
                        else "",
                        "source_start": r.start,
                        "source_end": r.end,
                        "reason": r.reason,
                    }
                    for r in self.ranges
                ],
            },
            "edit_log": [e.to_json() for e in self.edit_log],
        }
        if self.source_hash is not None:
            out["source_hash"] = self.source_hash
        return out

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Document:
        """Reconstruct a :class:`Document` from a dict.

        Schema-version handling:

        - ``schema_version == 3.1`` — load directly.
        - ``schema_version == 3`` — migrate via :func:`_migrate_v3_to_v31_data`.
        - ``schema_version == 2`` — migrate v2 → v3 → v3.1.
        - ``schema_version == 1`` — migrate v1 → v2 → v3 → v3.1.
        - missing / null / any other version — raise
          :class:`UnsupportedSchemaError`.

        Migrations run on read; the on-disk file is not rewritten as a
        side effect (write-through on next save). Never silently coerce
        across schema versions: an unknown version is a hard error.

        v3 → v3.1 adds an append-only ``edit_log`` to the Document.
        v3 inputs migrate with an empty ``edit_log`` — the structural
        history of a pre-v3.1 file is unrecoverable at load time, but
        every subsequent edit appends to the log going forward.
        """
        if "schema_version" not in data:
            raise UnsupportedSchemaError(
                "Document JSON has no 'schema_version' field; cannot load. "
                f"This build expects schema_version={cls.SCHEMA_VERSION}."
            )
        version = data["schema_version"]
        if version is None:
            raise UnsupportedSchemaError(
                "Document JSON 'schema_version' is null; cannot load. "
                f"This build expects schema_version={cls.SCHEMA_VERSION}."
            )
        if version == 1:
            v2 = _migrate_v1_to_v2_data(data)
            v3 = _migrate_v2_to_v3_data(v2)
            return _load_v31(cls, _migrate_v3_to_v31_data(v3))
        if version == 2:
            v3 = _migrate_v2_to_v3_data(data)
            return _load_v31(cls, _migrate_v3_to_v31_data(v3))
        if version == 3:
            return _load_v31(cls, _migrate_v3_to_v31_data(data))
        if version == cls.SCHEMA_VERSION:
            return _load_v31(cls, data)
        raise UnsupportedSchemaError(
            f"Document JSON schema_version={version!r} is not supported "
            f"by this build (expects schema_version={cls.SCHEMA_VERSION})."
        )


def _parse_segments(seg_data: list[dict[str, Any]]) -> list[Segment]:
    return [
        Segment(
            text=s["text"],
            start=float(s["start"]),
            end=float(s["end"]),
            words=tuple(
                Word(
                    text=w["text"],
                    start=float(w["start"]),
                    end=float(w["end"]),
                    probability=(
                        float(w["probability"])
                        if w.get("probability") is not None
                        else None
                    ),
                )
                for w in s.get("words", [])
            ),
        )
        for s in seg_data
    ]


def _load_v31(cls: type[Document], data: dict[str, Any]) -> Document:
    """Load a v3.1 (or up-migrated) document.

    The clip list order from JSON is preserved verbatim onto
    ``ranges`` — for non-monotonic v3 fixtures this list will *not*
    be sorted by start, and ``main_timeline.is_source_monotonic()``
    will report ``False``. Edit commands branch on that.

    ``edit_log`` is parsed via :meth:`EditEvent.from_json`. A missing
    or empty array yields an empty tuple — that's the v3 default after
    migration.
    """
    sources_raw = data.get("sources", {})
    sources = {
        sid: MediaSource(
            id=str(s.get("id", sid)),
            path=Path(s["path"]),
            duration=float(s["duration"]),
            hash=str(s.get("hash", "")),
        )
        for sid, s in sources_raw.items()
    }
    main_timeline = data.get("main_timeline", {})
    clip_dicts = main_timeline.get("clips", []) if isinstance(main_timeline, dict) else []
    ranges = [
        Range(
            source_id=_resolve_source_id(c, sources),
            start=float(c["source_start"]),
            end=float(c["source_end"]),
            reason=str(c.get("reason", "")),
        )
        for c in clip_dicts
    ]
    edit_log = tuple(EditEvent.from_json(e) for e in data.get("edit_log", []))
    return cls(
        sources=sources,
        segments=_parse_segments(data.get("segments", [])),
        ranges=ranges,
        language=data.get("language"),
        created_at=datetime.fromisoformat(data["created_at"]),
        model_name=data.get("model_name", ""),
        source_hash=data.get("source_hash"),
        edit_log=edit_log,
    )


def _resolve_source_id(clip_dict: dict[str, Any], sources: dict[str, MediaSource]) -> str:
    """Find the matching ``source_id`` for a v3 clip by id or path.

    v3 JSON carries both ``source_id`` (preferred) and ``source_path``
    (redundant copy). When ``source_id`` is present and registered we
    use it; otherwise we look up by path. Hand-built fixtures may pass
    only ``source_path``.
    """
    sid = clip_dict.get("source_id")
    if sid is not None and str(sid) in sources:
        return str(sid)
    target_path = clip_dict.get("source_path")
    if target_path is not None:
        for entry_id, src in sources.items():
            if str(src.path) == str(target_path):
                return entry_id
    if sid is not None:
        # Fallback: trust the id even if not registered. The Document
        # invariant check (in main_timeline) will catch it on first use.
        return str(sid)
    raise ValueError(
        f"clip {clip_dict!r} could not be resolved against sources {list(sources)!r}"
    )


def _migrate_v2_to_v3_data(data: dict[str, Any]) -> dict[str, Any]:
    """Migrate a v2 JSON dict to a v3 JSON dict.

    Pure data-shape rewrite: ``ranges: [{source_id, start, end, reason}]``
    becomes ``main_timeline: {clips: [{source_id, source_path,
    source_start, source_end, reason}]}``. ``schema_version`` flips to
    3. Source paths are looked up from the v2 ``sources`` dict and
    copied onto each clip for redundancy. The migration is lossless
    for source-monotonic v2 documents (every v2 doc is monotonic by
    construction).
    """
    sources = data.get("sources", {})
    clips: list[dict[str, Any]] = []
    for r in data.get("ranges", []):
        sid = str(r.get("source_id", ""))
        src = sources.get(sid, {}) if isinstance(sources, dict) else {}
        clips.append(
            {
                "source_id": sid,
                "source_path": src.get("path", "") if isinstance(src, dict) else "",
                "source_start": float(r["start"]),
                "source_end": float(r["end"]),
                "reason": str(r.get("reason", "")),
            }
        )
    out = dict(data)
    out["schema_version"] = 3
    out["main_timeline"] = {"clips": clips}
    out.pop("ranges", None)
    return out


def _migrate_v3_to_v31_data(data: dict[str, Any]) -> dict[str, Any]:
    """Migrate a v3 JSON dict to a v3.1 JSON dict.

    Pure data-shape rewrite: ``schema_version`` flips to 3.1 and an
    empty ``edit_log`` array is added if the input doesn't already
    have one. The structural history of pre-v3.1 documents is
    unrecoverable at load time — every edit going forward populates
    the log, but the past stays opaque.
    """
    out = dict(data)
    out["schema_version"] = 3.1
    out.setdefault("edit_log", [])
    return out


def _migrate_v1_to_v2_data(data: dict[str, Any]) -> dict[str, Any]:
    """Migrate a v1 JSON dict to a v2 JSON dict (cuts → ranges).

    This used to return a Document directly; refactored in 6a so the
    v1→v2→v3 chain composes cleanly. The cut-to-range derivation logic
    is unchanged.
    """
    from core.timeline import subtract_interval

    duration = float(data["duration"])
    source_hash_raw = data.get("source_hash")
    media_source_hash = source_hash_raw if source_hash_raw is not None else ""
    sources_dict = {
        "src0": {
            "id": "src0",
            "path": str(data["media_path"]),
            "duration": duration,
            "hash": media_source_hash,
        }
    }

    ranges: list[Range] = [Range(source_id="src0", start=0.0, end=duration)]
    cuts_sorted = sorted(
        (
            CutMark(
                start=float(c["start"]),
                end=float(c["end"]),
                reason=c.get("reason", ""),
            )
            for c in data.get("cuts", [])
        ),
        key=lambda c: c.start,
    )
    for cut in cuts_sorted:
        ranges = subtract_interval(ranges, (cut.start, cut.end), "src0")
        ranges = _attach_cut_reason(ranges, cut)

    out: dict[str, Any] = {
        "schema_version": 2,
        "sources": sources_dict,
        "segments": data.get("segments", []),
        "ranges": [
            {
                "source_id": r.source_id,
                "start": r.start,
                "end": r.end,
                "reason": r.reason,
            }
            for r in ranges
        ],
        "language": data.get("language"),
        "created_at": data["created_at"],
        "model_name": data.get("model_name", ""),
    }
    if source_hash_raw is not None:
        out["source_hash"] = source_hash_raw
    return out


def _attach_cut_reason(ranges: list[Range], cut: CutMark) -> list[Range]:
    """Attach ``cut.reason`` to the range immediately preceding the cut.

    Falls through to the immediately following range when the cut sits at
    the file's start. If neither exists (the cut covers everything), the
    reason is dropped with a warning. ``ranges`` is treated as immutable
    input; a new list is returned (or the same list if no attach happened).
    """
    if not cut.reason:
        return ranges
    preceding_idx: int | None = None
    following_idx: int | None = None
    for i, r in enumerate(ranges):
        if r.end == cut.start:
            preceding_idx = i
            break
    if preceding_idx is None:
        for i, r in enumerate(ranges):
            if r.start == cut.end:
                following_idx = i
                break
    target_idx = preceding_idx if preceding_idx is not None else following_idx
    if target_idx is None:
        _LOG.warning(
            "v1->v2 migration dropping reason %r — cut [%s, %s] left no surviving range",
            cut.reason,
            cut.start,
            cut.end,
        )
        return ranges
    return [
        *ranges[:target_idx],
        replace(ranges[target_idx], reason=cut.reason),
        *ranges[target_idx + 1 :],
    ]


def build_document(
    *,
    media_path: Path,
    duration: float,
    language: str | None,
    segments: Iterable[Segment],
    model_name: str,
    source_hash: str | None = None,
) -> Document:
    """Assemble a freshly-transcribed v2 :class:`Document` from core types.

    Builds a single-source Document keyed by ``"src0"`` whose initial
    timeline is one full-duration keep-range. ``created_at`` is captured
    at call time as a UTC ``datetime``.

    ``source_hash`` is propagated to both the Document field (kept for
    backward-compat sniffing) and the embedded :class:`MediaSource.hash`.
    """
    src = MediaSource(
        id="src0",
        path=Path(media_path),
        duration=float(duration),
        hash=source_hash or "",
    )
    return Document(
        sources={"src0": src},
        segments=list(segments),
        ranges=[Range(source_id="src0", start=0.0, end=float(duration))],
        language=language,
        created_at=datetime.now(UTC),
        model_name=model_name,
        source_hash=source_hash,
    )
