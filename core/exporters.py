"""Write transcription segments to .txt, .srt, .vtt files.

A "segment" is any object exposing ``start: float`` (seconds), ``end: float``
(seconds), and ``text: str``. ``faster_whisper.transcribe.Segment`` matches
this shape; we accept any duck-typed equivalent so tests can pass simple
namedtuples.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class SegmentLike(Protocol):
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class SimpleSegment:
    """Minimal segment for tests / non-faster-whisper callers."""

    start: float
    end: float
    text: str


def _format_timestamp(seconds: float, *, separator: str) -> str:
    """Format a non-negative timestamp as HH:MM:SS<sep>mmm."""
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    hours, remainder_ms = divmod(total_ms, 3_600_000)
    minutes, remainder_ms = divmod(remainder_ms, 60_000)
    secs, ms = divmod(remainder_ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{ms:03d}"


def format_srt_timestamp(seconds: float) -> str:
    return _format_timestamp(seconds, separator=",")


def format_vtt_timestamp(seconds: float) -> str:
    return _format_timestamp(seconds, separator=".")


def _materialize(segments: Iterable[SegmentLike]) -> list[SegmentLike]:
    if isinstance(segments, list):
        return segments
    return list(segments)


def render_txt(segments: Iterable[SegmentLike]) -> str:
    """Plain-text rendering: trimmed segment texts joined with single spaces.

    Internal newlines inside a segment are preserved as-is so users can still
    spot paragraph breaks the model emitted; trailing newlines on individual
    segments are stripped to avoid awkward double-blanks. Output ends with a
    single trailing newline.
    """
    parts = [seg.text.strip() for seg in segments]
    parts = [p for p in parts if p]
    if not parts:
        return ""
    return " ".join(parts) + "\n"


def render_srt(segments: Iterable[SegmentLike]) -> str:
    """SubRip subtitles. 1-based index, blank line between entries."""
    materialized = _materialize(segments)
    if not materialized:
        return ""
    blocks: list[str] = []
    for idx, seg in enumerate(materialized, start=1):
        ts = f"{format_srt_timestamp(seg.start)} --> {format_srt_timestamp(seg.end)}"
        text = seg.text.strip() or " "
        blocks.append(f"{idx}\n{ts}\n{text}\n")
    return "\n".join(blocks)


def render_vtt(segments: Iterable[SegmentLike]) -> str:
    """WebVTT subtitles. ``WEBVTT`` header, blank line between cues."""
    materialized = _materialize(segments)
    body_blocks: list[str] = []
    for seg in materialized:
        ts = f"{format_vtt_timestamp(seg.start)} --> {format_vtt_timestamp(seg.end)}"
        text = seg.text.strip() or " "
        body_blocks.append(f"{ts}\n{text}\n")
    if not body_blocks:
        return "WEBVTT\n"
    return "WEBVTT\n\n" + "\n".join(body_blocks)


_RENDERERS = {
    "txt": render_txt,
    "srt": render_srt,
    "vtt": render_vtt,
}


def resolve_output_path(base: Path, suffix: str) -> Path:
    """Return ``base.with_suffix('.' + suffix)``, appending ``_1``, ``_2``…
    until the path is free.

    ``base`` is the *source* path (e.g. ``/tmp/lecture.mp4``); the suffix is
    swapped to produce ``/tmp/lecture.txt``, and ``_N`` is inserted on the
    stem if a file already exists there.
    """
    if not suffix or suffix.startswith("."):
        raise ValueError(f"suffix must be a bare extension like 'txt', got {suffix!r}")
    target = base.with_suffix(f".{suffix}")
    if not target.exists():
        return target
    stem = base.stem
    parent = base.parent
    n = 1
    while True:
        candidate = parent / f"{stem}_{n}.{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def write_outputs(
    source_path: Path,
    segments: Sequence[SegmentLike],
    formats: Iterable[str],
) -> dict[str, Path]:
    """Render and write the requested formats next to ``source_path``.

    Returns a mapping of format → path written. Unknown formats raise
    ``ValueError``. Existing target files are not overwritten — a numbered
    suffix is appended instead.
    """
    formats = list(formats)
    unknown = [f for f in formats if f not in _RENDERERS]
    if unknown:
        raise ValueError(f"unsupported output formats: {unknown}")
    written: dict[str, Path] = {}
    materialized = list(segments)
    for fmt in formats:
        path = resolve_output_path(source_path, fmt)
        path.write_text(_RENDERERS[fmt](materialized), encoding="utf-8")
        written[fmt] = path
    return written
