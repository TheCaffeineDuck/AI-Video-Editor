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


_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class Document:
    """The canonical, serializable transcription project.

    SRT/TXT/VTT files written next to a media file are *derivatives* of this
    model — the editor (Phase 5+) will round-trip through Document JSON, not
    through the subtitle files.

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

    SCHEMA_VERSION: ClassVar[int] = _SCHEMA_VERSION

    def to_json(self) -> dict[str, Any]:
        """Return a plain-dict representation safe for ``json.dumps``.

        Always emits ``schema_version=2``. ``source_hash`` is omitted when
        ``None`` (kept additive so v2 Documents lacking the field load and
        round-trip cleanly).
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
            "ranges": [
                {
                    "source_id": r.source_id,
                    "start": r.start,
                    "end": r.end,
                    "reason": r.reason,
                }
                for r in self.ranges
            ],
        }
        if self.source_hash is not None:
            out["source_hash"] = self.source_hash
        return out

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Document:
        """Reconstruct a :class:`Document` from a dict.

        Schema-version handling:

        - ``schema_version == 2`` — load directly.
        - ``schema_version == 1`` — migrate via :func:`_migrate_v1_to_v2`.
          The migrated Document is returned in memory; **the on-disk file
          is not rewritten**. Migration on read, write-through on next save
          (see ``PRODUCTION_RULES.md``).
        - missing / null / any other integer — raise
          :class:`UnsupportedSchemaError`.

        Never silently coerce across schema versions: an unknown integer
        is a hard error, not a "best-effort" load.
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
            return _migrate_v1_to_v2(cls, data)
        if version == cls.SCHEMA_VERSION:
            return _load_v2(cls, data)
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


def _load_v2(cls: type[Document], data: dict[str, Any]) -> Document:
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
    ranges = [
        Range(
            source_id=str(r["source_id"]),
            start=float(r["start"]),
            end=float(r["end"]),
            reason=str(r.get("reason", "")),
        )
        for r in data.get("ranges", [])
    ]
    return cls(
        sources=sources,
        segments=_parse_segments(data.get("segments", [])),
        ranges=ranges,
        language=data.get("language"),
        created_at=datetime.fromisoformat(data["created_at"]),
        model_name=data.get("model_name", ""),
        source_hash=data.get("source_hash"),
    )


def _migrate_v1_to_v2(cls: type[Document], data: dict[str, Any]) -> Document:
    """Migrate a v1 Document JSON to a v2 :class:`Document` in memory.

    Single-source: the v1 ``media_path`` / ``duration`` / ``source_hash``
    triple becomes one :class:`MediaSource` with ``id="src0"``.

    Cuts → ranges: start with the full source as one keep-range, subtract
    each v1 cut, attach each cut's ``reason`` to the range immediately
    preceding the cut (or the range immediately following if the cut sits
    at the file's start). If neither exists (the cut spans the entire
    source), drop the reason and warn — there's no surviving range to
    attach it to.

    The on-disk JSON is **not** rewritten. The Document object that the
    caller now holds is v2 in memory; the next ``to_json``/save will emit
    v2 to disk. Aggressive auto-migrate-on-load would surprise users who
    keep project files under version control.
    """
    from core.timeline import subtract_interval

    duration = float(data["duration"])
    source_hash_raw = data.get("source_hash")
    media_source_hash = source_hash_raw if source_hash_raw is not None else ""
    src = MediaSource(
        id="src0",
        path=Path(data["media_path"]),
        duration=duration,
        hash=media_source_hash,
    )
    sources = {"src0": src}

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

    return cls(
        sources=sources,
        segments=_parse_segments(data.get("segments", [])),
        ranges=ranges,
        language=data.get("language"),
        created_at=datetime.fromisoformat(data["created_at"]),
        model_name=data.get("model_name", ""),
        source_hash=source_hash_raw,
    )


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
