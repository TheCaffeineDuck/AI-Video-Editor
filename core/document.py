"""Canonical transcription document types.

Phase 4a introduced :class:`Word` and :class:`Segment` so the rest of the
system stops leaking faster-whisper's objects across module boundaries.
Phase 4b adds :class:`Document` (the persisted project model) and
:class:`CutMark` (an edit-state primitive) on top of these, plus JSON
serialization with explicit schema versioning.

Immutability contract (Phase 4c): :class:`Document`, :class:`CutMark`,
:class:`Segment`, and :class:`Word` are all ``frozen=True``. Edit commands
in :mod:`core.editing` produce *new* Documents via
``dataclasses.replace`` and always pass a fresh list for ``cuts`` /
``segments`` — never mutate the inner list of an existing Document, since
``frozen`` prevents reassigning the attribute but cannot prevent
``doc.cuts.append(...)``. Stick to ``replace(doc, cuts=[*doc.cuts, x])``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar


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
    """

    text: str
    start: float
    end: float
    words: tuple[Word, ...] = ()


@dataclass(frozen=True)
class CutMark:
    """A range of media marked for removal.

    ``reason`` is a free-form tag we expect callers to populate with values
    like ``"manual"``, ``"filler"``, ``"silence"`` — not enforced as an enum
    so future kinds of cuts don't require schema bumps.
    """

    start: float
    end: float
    reason: str = ""


class UnsupportedSchemaError(ValueError):
    """Raised when ``Document.from_json`` is asked to load an unknown schema."""


@dataclass(frozen=True)
class Document:
    """The canonical, serializable transcription project.

    SRT/TXT/VTT files written next to a media file are *derivatives* of this
    model — the editor (Phase 5+) will round-trip through Document JSON, not
    through the subtitle files.

    Frozen: callers can't reassign ``doc.cuts`` / ``doc.segments``. The
    inner lists are still mutable Python lists, so edit commands are
    responsible for never appending to them — they always pass a fresh
    list to :func:`dataclasses.replace`.
    """

    media_path: Path
    duration: float
    language: str | None
    segments: list[Segment]
    cuts: list[CutMark] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    model_name: str = ""

    SCHEMA_VERSION: ClassVar[int] = 1

    def to_json(self) -> dict[str, Any]:
        """Return a plain-dict representation safe for ``json.dumps``.

        Always emits ``schema_version`` matching :attr:`SCHEMA_VERSION`.
        """
        return {
            "schema_version": self.SCHEMA_VERSION,
            "media_path": str(self.media_path),
            "duration": self.duration,
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
            "cuts": [
                {"start": c.start, "end": c.end, "reason": c.reason}
                for c in self.cuts
            ],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Document:
        """Reconstruct a :class:`Document` from a dict.

        Raises :class:`UnsupportedSchemaError` if ``schema_version`` is
        missing, ``None``, or an unknown integer. We never silently coerce
        across schema versions — the caller must opt in to migration.
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
        if version != cls.SCHEMA_VERSION:
            raise UnsupportedSchemaError(
                f"Document JSON schema_version={version!r} is not supported "
                f"by this build (expects schema_version={cls.SCHEMA_VERSION})."
            )

        segments = [
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
            for s in data.get("segments", [])
        ]
        cuts = [
            CutMark(
                start=float(c["start"]),
                end=float(c["end"]),
                reason=c.get("reason", ""),
            )
            for c in data.get("cuts", [])
        ]
        return cls(
            media_path=Path(data["media_path"]),
            duration=float(data["duration"]),
            language=data.get("language"),
            segments=segments,
            cuts=cuts,
            created_at=datetime.fromisoformat(data["created_at"]),
            model_name=data.get("model_name", ""),
        )
