"""Canonical transcription document types.

Phase 4a introduces ``Word`` and ``Segment`` so the rest of the system stops
leaking faster-whisper's objects across module boundaries. Phase 4b adds
``Document`` and ``CutMark`` (the persisted project model) on top of these.
"""

from __future__ import annotations

from dataclasses import dataclass


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
