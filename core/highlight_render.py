"""Render a :class:`~core.highlight.Highlight` to a 9:16 vertical clip.

Two-stage pipeline:

1. **Cut.** Build an ephemeral single-clip :class:`~core.document.Document`
   whose only keep-range is the highlight's span, hand it to
   :func:`~core.render.render_cut`. Output is a temp 16:9 mp4 at the
   source's native resolution; smartcut handles the cut losslessly.
2. **Reframe (+ optional captions).** A single ffmpeg invocation crops
   the temp file to a 9:16 window, scales to 1080×1920, and (if
   captions are enabled) burns in an SRT generated from the parent
   document's word timestamps. This is the only re-encode in the
   pipeline — captions ride along on the same encode step.

Reframe modes:

- ``"speaker_locked"`` — sample one frame at the span's midpoint, run
  MediaPipe face detection, pick the largest face by bbox area, place
  the face's center at upper-third of the output canvas. If no face is
  detected (or MediaPipe import / inference fails), fall back to
  ``"center"`` and log it. The fallback is *not* surfaced as an error;
  the render still produces a playable output.
- ``"center"`` — static center crop of the source frame at full
  source height, scaled to 1080×1920.

Caption styling: white text, black outline, bottom-third positioning,
48pt. ``force_style`` parameters chosen to read at 1080×1920 without
overpowering the frame. Captions always have one cue per "line";
words are grouped into lines of 3-5 words, breaking on gaps > 300 ms.

Future work flagged in spec: dynamic speaker tracking (continuous
follow), smart caption layouts, per-highlight style overrides — all
Phase 7+. The single-pass crop+scale+subtitles ffmpeg invocation here
is also the place where future filters (LUT correction, noise
reduction) will land if they ever need to.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import replace as _replace
from pathlib import Path
from typing import Literal

from core.audio import get_ffmpeg_path
from core.cache import cache_key
from core.document import Document, MediaSource, Range
from core.exporters import format_srt_timestamp
from core.highlight import (
    Highlight,
    StaleHighlightError,
    highlights_dir_for_document,
    mark_rendered,
    rendered_output_path_for,
)
from core.render import render_cut

_LOG = logging.getLogger(__name__)

OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920
OUTPUT_ASPECT = OUTPUT_WIDTH / OUTPUT_HEIGHT  # 9/16 = 0.5625

CAPTION_FORCE_STYLE = (
    "Fontname=Arial,Fontsize=56,"
    "PrimaryColour=&Hffffff&,OutlineColour=&H000000&,"
    "BorderStyle=1,Outline=3,Shadow=0,"
    "Alignment=2,MarginV=80"
)
"""Default ASS style block burned into highlight captions.

Tuned for 1080×1920 output:

* ``Fontname=Arial`` / ``Fontsize=56`` — readable at typical viewing
  distance; bumped from 48 in Phase 6c-3 because the previous size
  felt small on the 9:16 canvas.
* ``PrimaryColour=&Hffffff&`` — white fill (ASS uses ``&Haabbggrr&``
  byte order with the alpha first; ``ff`` alpha is opaque white).
* ``OutlineColour=&H000000&`` + ``BorderStyle=1`` + ``Outline=3`` —
  black 3 px stroke, no box, gives legibility against any background.
* ``Shadow=0`` — drop shadows compete with the stroke and read as
  noisy at this canvas size.
* ``Alignment=2`` — bottom-center.
* ``MarginV=80`` — 80 px vertical margin from the bottom edge,
  keeping the caption out of the bottom safe-area on a 1920 px tall
  canvas without crowding the visual.

Per-highlight caption-style overrides are Phase 7+ work; for now
this constant is the only knob.
"""

# Internal alias preserved for the existing ffmpeg invocation below;
# treats the public name as the source of truth.
_CAPTION_FORCE_STYLE = CAPTION_FORCE_STYLE

_CAPTION_LINE_GAP_S = 0.30
_CAPTION_MAX_WORDS = 5
_CAPTION_MIN_WORDS = 3


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FaceDetectionError(RuntimeError):
    """Internal signal that face detection produced no usable bbox.

    Raised inside :func:`detect_speaker_bbox` and caught at the
    :func:`render_highlight` boundary to trigger the ``center``
    fallback. Not part of the public API — the render path logs the
    fallback rather than re-raising.
    """


# ---------------------------------------------------------------------------
# Frame sampling
# ---------------------------------------------------------------------------


def _sample_frame(source_path: Path, t_seconds: float, out_path: Path) -> None:
    """Extract a single frame from ``source_path`` at ``t_seconds`` to ``out_path``.

    Uses ``-ss`` before ``-i`` for fast keyframe seek. For midpoint
    sampling, sub-frame accuracy doesn't matter; the tradeoff favors
    speed.
    """
    ffmpeg = get_ffmpeg_path()
    cmd = [
        str(ffmpeg),
        "-y",
        "-loglevel",
        "error",
        "-ss",
        f"{t_seconds:.3f}",
        "-i",
        str(source_path),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0 or not out_path.is_file():
        raise FaceDetectionError(
            f"frame sample failed (rc={result.returncode}): {result.stderr[-200:]}"
        )


# ---------------------------------------------------------------------------
# Face detection
# ---------------------------------------------------------------------------


def detect_speaker_bbox(image_path: Path) -> tuple[float, float, float, float]:
    """Return ``(x, y, w, h)`` of the largest detected face in absolute pixels.

    Uses OpenCV's Haar cascade frontal-face classifier (bundled with
    ``opencv-python`` under ``cv2.data.haarcascades``). MediaPipe was
    the spec's preferred detector but the 0.10.x release line on
    Apple Silicon ships only the new Tasks API which requires
    downloading model files separately — we took the spec's documented
    fallback instead. Haar is CPU-cheap, model-file-free, and good
    enough for "find the dominant face in a frame" at 1080p.

    Raises :class:`FaceDetectionError` when no faces are detected.
    Multi-face frames pick the largest bbox by area; Phase 7+ may add
    speaker selection logic (audio diarization, persistent identity).
    """
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - hard dep at install
        raise FaceDetectionError(f"cv2 not available: {exc}") from exc

    img = cv2.imread(str(image_path))
    if img is None:
        raise FaceDetectionError(f"could not read frame at {image_path}")
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    detector = cv2.CascadeClassifier(str(cascade_path))
    if detector.empty():  # pragma: no cover - install integrity
        raise FaceDetectionError(f"could not load haar cascade at {cascade_path}")

    # ``minSize`` rejects tiny false positives on busy frames; relative to
    # 1080p the floor of 80 px corresponds to ~7 % of frame width — small
    # enough to catch a head in a wide podcast shot, big enough to ignore
    # background noise.
    detections = detector.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=5, minSize=(80, 80)
    )
    if len(detections) == 0:
        raise FaceDetectionError("no faces detected")
    x, y, w, h = max(detections, key=lambda r: r[2] * r[3])
    return float(x), float(y), float(w), float(h)


# ---------------------------------------------------------------------------
# Crop math
# ---------------------------------------------------------------------------


def compute_speaker_locked_crop(
    source_w: int,
    source_h: int,
    face_box: tuple[float, float, float, float],
) -> tuple[int, int, int, int]:
    """Return ``(crop_w, crop_h, crop_x, crop_y)`` for a 9:16 face-locked window.

    Strategy: maximize crop. Use the full source height as crop height,
    derive crop width from the 9:16 aspect, clamp horizontal position
    so the face center sits at the crop's horizontal midline (and
    therefore at the output's horizontal midline after scale). Vertical
    positioning aims for face center at upper-third of the *crop*; for
    typical 16:9 sources the crop already fills source height so
    crop_y is forced to 0 — the upper-third heuristic still applies via
    the scale transform.

    Boundary-safe: crops that would slip off the frame are clamped to
    the source rect.

    **Vertical composition is source-framed (Phase 7+ debt).** When
    the source's aspect ratio is ≥ 9:16 (which covers every 16:9
    podcast / interview shot we ship today), the 9:16 crop fills the
    full source height. Vertical placement of the speaker therefore
    follows wherever the source framing put them — we cannot shift
    them up or down without throwing away pixel rows. Sub-full-height
    crop (so we can re-center vertically) and dynamic per-frame
    tracking are Phase 7+ work; this routine deliberately stays
    one-shot and source-framed for 6c-3.
    """
    fx, fy, fw, fh = face_box
    face_cx = fx + fw / 2.0
    face_cy = fy + fh / 2.0

    crop_h = source_h
    crop_w = int(round(crop_h * OUTPUT_ASPECT))
    if crop_w > source_w:
        crop_w = source_w
        crop_h = int(round(crop_w / OUTPUT_ASPECT))

    crop_x = int(round(face_cx - crop_w / 2.0))
    crop_y = int(round(face_cy - crop_h / 3.0))

    crop_x = max(0, min(crop_x, source_w - crop_w))
    crop_y = max(0, min(crop_y, source_h - crop_h))
    return crop_w, crop_h, crop_x, crop_y


def compute_center_crop(source_w: int, source_h: int) -> tuple[int, int, int, int]:
    """Return ``(crop_w, crop_h, crop_x, crop_y)`` for a centered 9:16 window."""
    crop_h = source_h
    crop_w = int(round(crop_h * OUTPUT_ASPECT))
    if crop_w > source_w:
        crop_w = source_w
        crop_h = int(round(crop_w / OUTPUT_ASPECT))
    crop_x = (source_w - crop_w) // 2
    crop_y = (source_h - crop_h) // 2
    return crop_w, crop_h, crop_x, crop_y


def _probe_dimensions(path: Path) -> tuple[int, int]:
    """Return the source's ``(width, height)`` via ffprobe."""
    ffprobe = _ffprobe_path()
    cmd = [
        str(ffprobe),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "csv=s=x:p=0",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr[-200:]}")
    w_s, h_s = result.stdout.strip().split("x")
    return int(w_s), int(h_s)


def _ffprobe_path() -> Path:
    """Look up ffprobe — vendored next to ffmpeg, or system PATH."""
    ffmpeg = get_ffmpeg_path()
    candidate = ffmpeg.parent / "ffprobe-mac"
    if candidate.is_file():
        return candidate
    for cand in (Path("/opt/homebrew/bin/ffprobe"), Path("/usr/local/bin/ffprobe")):
        if cand.is_file():
            return cand
    raise FileNotFoundError(
        "ffprobe not found (looked next to ffmpeg and on common /opt paths)"
    )


# ---------------------------------------------------------------------------
# Caption SRT generation
# ---------------------------------------------------------------------------


def words_in_span(
    document: Document, span_start: float, span_end: float
) -> list[tuple[str, float, float]]:
    """Return ``(text, start, end)`` for words overlapping the span.

    Overlap convention matches the rest of the editor: ``word.end >
    span_start and word.start < span_end``. Timestamps are returned in
    *source* time; relative-to-span conversion happens in
    :func:`build_caption_srt`.
    """
    out: list[tuple[str, float, float]] = []
    for seg in document.segments:
        for w in seg.words:
            if w.end > span_start and w.start < span_end:
                out.append((w.text, float(w.start), float(w.end)))
    return out


def _group_words_into_lines(
    words: list[tuple[str, float, float]],
) -> list[list[tuple[str, float, float]]]:
    """Group a flat word list into caption lines.

    A new line starts when the gap between consecutive words exceeds
    :data:`_CAPTION_LINE_GAP_S`, OR when the current line has hit
    :data:`_CAPTION_MAX_WORDS`. The min-words floor isn't strict — a
    natural pause can still split a 1- or 2-word line, because forcing
    short lines to grow until they hit the floor would dilute the
    pause-driven phrasing.
    """
    if not words:
        return []
    lines: list[list[tuple[str, float, float]]] = []
    current: list[tuple[str, float, float]] = [words[0]]
    for w in words[1:]:
        prev_end = current[-1][2]
        gap = w[1] - prev_end
        if gap > _CAPTION_LINE_GAP_S or len(current) >= _CAPTION_MAX_WORDS:
            lines.append(current)
            current = [w]
        else:
            current.append(w)
    if current:
        lines.append(current)
    return lines


def build_caption_srt(
    document: Document, span_start: float, span_end: float
) -> str:
    """Build an SRT body for the highlight span, timestamps relative to span start.

    Returns the empty string when no words overlap the span — caller
    can then skip the subtitles ffmpeg filter rather than burn an
    empty SRT (which libass tolerates but is wasteful).
    """
    words = words_in_span(document, span_start, span_end)
    if not words:
        return ""
    lines = _group_words_into_lines(words)
    blocks: list[str] = []
    for idx, line in enumerate(lines, start=1):
        rel_start = max(0.0, line[0][1] - span_start)
        rel_end = max(rel_start + 0.05, line[-1][2] - span_start)
        ts = f"{format_srt_timestamp(rel_start)} --> {format_srt_timestamp(rel_end)}"
        text = " ".join(w[0].strip() for w in line).strip()
        if not text:
            continue
        _ = _CAPTION_MIN_WORDS  # documented floor; not enforced (see docstring)
        blocks.append(f"{idx}\n{ts}\n{text}\n")
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# Ephemeral document for the cut step
# ---------------------------------------------------------------------------


def _ephemeral_doc_for_span(
    parent: Document, highlight: Highlight
) -> Document:
    """Build a single-clip Document covering the highlight's span.

    All Document fields except ``ranges`` and ``sources`` pass through
    from the parent so word-boundary snapping in render_cut still sees
    every word. We re-register the matching source with the same id
    the parent used so the ephemeral Range can reference it; the
    source's ``duration`` is inherited verbatim. The ephemeral doc is
    not persisted — it lives only for the duration of the render call.
    """
    sid: str | None = None
    for k, src in parent.sources.items():
        if Path(src.path) == Path(highlight.span_source_path):
            sid = k
            break
    if sid is None:
        # Mint a fresh source entry so the renderer has something to point
        # to. This happens when a highlight references a media file that
        # was registered post-author or under a different normalized path.
        sid = "highlight_src"
        new_src = MediaSource(
            id=sid,
            path=Path(highlight.span_source_path),
            duration=max(
                highlight.span_source_end + 1.0,
                _probe_duration_seconds(highlight.span_source_path),
            ),
        )
        sources = {**parent.sources, sid: new_src}
    else:
        sources = parent.sources

    span_range = Range(
        source_id=sid,
        start=float(highlight.span_source_start),
        end=float(highlight.span_source_end),
        reason=highlight.reason,
    )
    return _replace(parent, sources=dict(sources), ranges=[span_range])


def _probe_duration_seconds(path: Path) -> float:
    """Return media duration in seconds (best-effort ffprobe)."""
    try:
        ffprobe = _ffprobe_path()
    except FileNotFoundError:
        return 0.0
    cmd = [
        str(ffprobe),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return 0.0
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Top-level render entrypoint
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HighlightRenderMetadata:
    """What happened on a single :func:`render_highlight` invocation.

    Carries the bookkeeping the Phase 6c-2 ``apply_highlight`` MCP tool
    persists into a render-result sidecar. The render path produces this
    alongside the output path so the caller doesn't have to re-derive
    crop / fallback state by ffprobing the file after the fact.

    - ``output_path`` — the rendered ``<id>.highlight.mp4``.
    - ``face_detection_used`` — one of ``"speaker_locked"`` (face
      detected and used), ``"speaker_locked_fallback_to_center"`` (the
      caller asked for speaker_locked but face detection failed and
      the renderer fell back), or ``"center"`` (caller asked for
      center mode).
    - ``crop_box`` — the ``(x, y, w, h)`` window taken from the cut
      stage's frame *before* ``scale=1080:1920``.
    - ``parent_source_hash`` — the live ``cache_key`` value the stale
      check matched against. Recorded so the render-result sidecar
      can chain back to the highlight without a second cache_key
      probe.
    """

    output_path: Path
    face_detection_used: Literal[
        "speaker_locked",
        "speaker_locked_fallback_to_center",
        "center",
    ]
    crop_box: tuple[int, int, int, int]
    parent_source_hash: str


def render_highlight(
    highlight: Highlight,
    parent_document: Document,
    progress_callback: Callable[[float], None] | None = None,
) -> HighlightRenderMetadata:
    """Render a Highlight to ``<id>.highlight.mp4``; return render metadata.

    Stale-hash check fires first: if the highlight's recorded
    ``parent_source_hash`` doesn't match the live source file's
    :func:`~core.cache.cache_key`, raises :class:`StaleHighlightError`.
    The hash tracks the source media file (path + mtime + size), not
    the Document state — intra-doc edits don't shift source-time spans,
    so they don't invalidate highlights. Only file-replacement at the
    OS layer triggers staleness.

    Renderer composition (worst case is two passes — one ffmpeg
    invocation for cut via smartcut, one for reframe + captions; the
    spec allowance to combine reframe + captions into a single pass is
    honored here):

    1. Build an ephemeral single-clip Document (the span) and call
       :func:`~core.render.render_cut` to produce a temp 16:9 mp4 at
       source-native resolution. Lossless under the existing pipeline.
    2. (Speaker-locked only) Sample a frame at the span midpoint; run
       face detection to find the dominant face. On detection failure,
       fall back to ``center`` mode silently (logged) and record the
       fallback in :attr:`HighlightRenderMetadata.face_detection_used`.
    3. Build the SRT (when ``captions_enabled``) into a temp file in
       the highlights output directory.
    4. One ffmpeg invocation: ``crop`` → ``scale=1080:1920``
       → (optional) ``subtitles=…`` → encoded mp4 at the final path.
    5. Update the highlight's sidecar JSON with
       :func:`~core.highlight.mark_rendered` and return the metadata.

    ``progress_callback`` is forwarded to ``render_cut`` for the cut
    stage; reframe/caption stages don't emit fine-grained progress
    (they're a single re-encode), so the caller sees the cut's
    [0..1] stream and then the file appears.
    """
    try:
        live_hash = cache_key(highlight.span_source_path)
    except FileNotFoundError as exc:
        raise StaleHighlightError(
            f"highlight span_source_path={highlight.span_source_path!r} "
            f"is missing on disk: {exc}"
        ) from exc
    if highlight.parent_source_hash != live_hash:
        raise StaleHighlightError(
            f"highlight parent_source_hash={highlight.parent_source_hash!r} "
            f"does not match live cache_key={live_hash!r}; the source media "
            "file has been replaced. Re-author the highlight against the "
            "current source."
        )

    out_dir = highlights_dir_for_document(highlight.parent_document_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    final_path = rendered_output_path_for(out_dir, highlight.highlight_id)

    cut_path = out_dir / f"{highlight.highlight_id}.cut.mp4"
    ephemeral_doc = _ephemeral_doc_for_span(parent_document, highlight)

    face_detection_used: Literal[
        "speaker_locked",
        "speaker_locked_fallback_to_center",
        "center",
    ]
    try:
        render_cut(ephemeral_doc, cut_path, on_progress=progress_callback)

        # Reframe decisions.
        source_w, source_h = _probe_dimensions(cut_path)
        if highlight.reframe_mode == "speaker_locked":
            try:
                crop = _resolve_speaker_locked_crop(cut_path, source_w, source_h)
                face_detection_used = "speaker_locked"
            except FaceDetectionError as exc:
                _LOG.warning(
                    "speaker-lock fallback to center for highlight %s: %s",
                    highlight.highlight_id,
                    exc,
                )
                crop = compute_center_crop(source_w, source_h)
                face_detection_used = "speaker_locked_fallback_to_center"
        else:
            crop = compute_center_crop(source_w, source_h)
            face_detection_used = "center"

        crop_w, crop_h, crop_x, crop_y = crop

        # Optional captions.
        srt_path: Path | None = None
        if highlight.captions_enabled:
            srt_body = build_caption_srt(
                parent_document,
                highlight.span_source_start,
                highlight.span_source_end,
            )
            if srt_body:
                srt_path = out_dir / f"{highlight.highlight_id}.captions.srt"
                srt_path.write_text(srt_body, encoding="utf-8")

        _ffmpeg_reframe(
            cut_path,
            final_path,
            crop=(crop_w, crop_h, crop_x, crop_y),
            srt_path=srt_path,
        )
    finally:
        try:
            cut_path.unlink()
        except FileNotFoundError:
            pass
        srt = out_dir / f"{highlight.highlight_id}.captions.srt"
        if srt.is_file():
            try:
                srt.unlink()
            except FileNotFoundError:
                pass

    mark_rendered(highlight.parent_document_path, highlight, final_path)
    return HighlightRenderMetadata(
        output_path=final_path,
        face_detection_used=face_detection_used,
        crop_box=(crop_x, crop_y, crop_w, crop_h),
        parent_source_hash=live_hash,
    )


def _resolve_speaker_locked_crop(
    source_path: Path, source_w: int, source_h: int
) -> tuple[int, int, int, int]:
    """Detect the dominant face on a midpoint frame; return the crop window."""
    duration = _probe_duration_seconds(source_path)
    if duration <= 0:
        raise FaceDetectionError("could not probe duration for midpoint sample")
    midpoint = duration / 2.0
    sample_dir = source_path.parent / f".{source_path.stem}.sample"
    sample_dir.mkdir(exist_ok=True)
    sample = sample_dir / "frame.png"
    try:
        _sample_frame(source_path, midpoint, sample)
        face_box = detect_speaker_bbox(sample)
    finally:
        try:
            sample.unlink()
        except FileNotFoundError:
            pass
        try:
            sample_dir.rmdir()
        except OSError:
            pass
    return compute_speaker_locked_crop(source_w, source_h, face_box)


def _ffmpeg_reframe(
    in_path: Path,
    out_path: Path,
    *,
    crop: tuple[int, int, int, int],
    srt_path: Path | None,
) -> None:
    """One-pass reframe + optional caption burn.

    Filter chain: ``crop=...,scale=1080:1920`` plus an optional
    ``subtitles=...`` step. The subtitles filter is appended *after*
    scale so subtitle text is rendered at the final pixel size (no
    upscaling artifacts on the type).

    Path-escaping: subtitles filter parses ``:`` and ``=`` specially;
    we sidestep the issue by running ffmpeg with cwd set to the SRT's
    parent dir and passing only the basename. That's the simplest
    pattern that survives spaces and other shell-unfriendly chars in
    the highlight's parent path.
    """
    crop_w, crop_h, crop_x, crop_y = crop
    filters = [
        f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y}",
        f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}",
    ]
    cwd: Path | None = None
    if srt_path is not None:
        cwd = srt_path.parent
        filters.append(
            f"subtitles={srt_path.name}:force_style='{_CAPTION_FORCE_STYLE}'"
        )

    ffmpeg = get_ffmpeg_path()
    cmd = [
        str(ffmpeg),
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(in_path.resolve()),
        "-vf",
        ",".join(filters),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        str(out_path.resolve()),
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=1800, cwd=str(cwd) if cwd else None
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg reframe pass failed (rc={result.returncode}): "
            f"{result.stderr[-400:]}"
        )


__all__ = [
    "FaceDetectionError",
    "HighlightRenderMetadata",
    "OUTPUT_HEIGHT",
    "OUTPUT_WIDTH",
    "build_caption_srt",
    "compute_center_crop",
    "compute_speaker_locked_crop",
    "detect_speaker_bbox",
    "render_highlight",
    "words_in_span",
]
