"""Write transcription segments to .txt, .srt, .vtt files, and parse SRT back.

A "segment" is any object exposing ``start: float`` (seconds), ``end: float``
(seconds), and ``text: str``. ``faster_whisper.transcribe.Segment`` matches
this shape; we accept any duck-typed equivalent so tests can pass simple
namedtuples.

Renderers stay strict — they always emit canonical form: LF endings, comma
millisecond separator in SRT, exactly one blank line between cues, single
trailing newline. The parser (:func:`parse_srt`) is the opposite — lenient
on everything real-world SRT files throw at it (BOM, CRLF, period decimals,
missing index numbers, extra blanks).

Phase 4e adds a fourth output format ``"json"`` — the Document JSON
artifact (see :class:`core.document.Document`). It's produced from a
``Document`` rather than raw segments because it needs metadata
(media_path, duration, language, model_name, created_at, cuts) the
segments alone can't carry. Callers that select ``"json"`` must pass a
``document=`` kwarg to :func:`write_outputs`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from core.document import Document, Segment


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

# Formats that render from raw segments (no Document metadata required).
SEGMENT_FORMATS: frozenset[str] = frozenset(_RENDERERS)
# Formats that need a full Document. These are dispatched separately in
# write_outputs because they carry metadata segments alone can't express.
DOCUMENT_FORMATS: frozenset[str] = frozenset({"json"})
ALL_FORMATS: frozenset[str] = SEGMENT_FORMATS | DOCUMENT_FORMATS

# The on-disk extension for each format. ``"json"`` writes
# ``<stem>.transcribe.json`` so users can spot it next to the .srt/.txt
# files without confusing it with arbitrary JSON.
_FORMAT_SUFFIXES: dict[str, str] = {
    "txt": "txt",
    "srt": "srt",
    "vtt": "vtt",
    "json": "transcribe.json",
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
    *,
    document: Document | None = None,
) -> dict[str, Path]:
    """Render and write the requested formats next to ``source_path``.

    Returns a mapping of format → path written. Unknown formats raise
    ``ValueError``. Existing target files are not overwritten — a numbered
    suffix is appended via :func:`resolve_output_path`.

    ``"json"`` writes the Document JSON artifact and requires
    ``document=`` to be passed; missing that raises ``ValueError`` with
    a clear message rather than silently dropping the format.
    """
    formats = list(formats)
    unknown = [f for f in formats if f not in ALL_FORMATS]
    if unknown:
        raise ValueError(f"unsupported output formats: {unknown}")
    if "json" in formats and document is None:
        raise ValueError("'json' output format requires document= to be passed")
    written: dict[str, Path] = {}
    materialized = list(segments)
    for fmt in formats:
        path = resolve_output_path(source_path, _FORMAT_SUFFIXES[fmt])
        if fmt in SEGMENT_FORMATS:
            path.write_text(_RENDERERS[fmt](materialized), encoding="utf-8")
        else:  # DOCUMENT_FORMATS — currently just "json"
            assert document is not None  # guarded above
            path.write_text(
                json.dumps(document.to_json(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        written[fmt] = path
    return written


# ---------------------------------------------------------------------------
# parse_srt — lenient on input, strict on output (renderer is the canonical form)
# ---------------------------------------------------------------------------


# Captures both SRT (comma) and VTT-style (period) decimals; tolerates extra
# whitespace anywhere on the line, including before/after the arrow.
_TIMESTAMP_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{3})"
)
_BLANK_LINE_RE = re.compile(r"\n[ \t]*\n")


def _ts_to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_srt(text: str) -> list[Segment]:
    """Parse an SRT (or VTT-flavored SRT) into :class:`Segment` objects.

    Tolerates: UTF-8 BOM, CRLF/CR/mixed line endings, missing index numbers,
    period decimals (``.500`` instead of ``,500``), whitespace around
    timestamps, extra blank lines between cues, and a missing trailing
    newline. Cues without a parseable timestamp line are dropped silently —
    we treat junk as junk rather than aborting on an ugly file.

    Returned segments have no word-level data (``words=()``); SRT carries
    only segment-level timing.
    """
    if text.startswith("﻿"):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    segments: list[Segment] = []
    for raw_block in _BLANK_LINE_RE.split(text):
        block = raw_block.strip("\n")
        if not block.strip():
            continue
        lines = block.split("\n")

        ts_match = None
        ts_line_idx = -1
        for idx, line in enumerate(lines):
            m = _TIMESTAMP_RE.search(line)
            if m is not None:
                ts_match = m
                ts_line_idx = idx
                break
        if ts_match is None:
            continue

        h1, m1, s1, ms1, h2, m2, s2, ms2 = ts_match.groups()
        start = _ts_to_seconds(h1, m1, s1, ms1)
        end = _ts_to_seconds(h2, m2, s2, ms2)

        cue_lines = lines[ts_line_idx + 1 :]
        cue_text = "\n".join(cue_lines).strip()
        segments.append(Segment(text=cue_text, start=start, end=end, words=()))

    return segments
