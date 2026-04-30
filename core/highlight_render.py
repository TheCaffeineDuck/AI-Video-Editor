"""Render a :class:`~core.highlight.Highlight` to a 9:16 vertical clip.

Three paths share one entry point (:func:`render_highlight`):

1. **Single fragment, no sync group.** The 6c-1 path — build an
   ephemeral one-clip Document from the highlight's lone fragment,
   call :func:`~core.render.render_cut`, reframe in one ffmpeg pass.
   Caption SRT is generated from the parent document's transcript.
2. **Multi-fragment, single source, no sync group.** Phase 7 — same
   ephemeral Document approach, but with N ranges instead of one.
   :func:`~core.render.render_cut` already supports concatenating
   multiple ranges from one source via the monotonic-runs path
   (or non-monotonic, if the fragments are out of source order).
   Reframe and captions still happen as a final pass.
3. **Multi-fragment with sync group.** Phase 7 — per-fragment
   normalize-then-concat. Each fragment is cut from its camera
   source, its audio is replaced with the audio master at the
   offset-translated window, then encoded to a uniform target
   (1080×1920 H264 + AAC 48 kHz stereo) with the source's crop
   already applied. Concat-demuxer stitches the fragments
   losslessly. Captions come from the audio master's transcript
   (the parent doc, by convention).

Reframe modes (now per unique source):

- ``"speaker_locked"`` — for each unique source the highlight
  references, sample one frame at the midpoint of the *first*
  fragment from that source, run face detection, place the face
  center at upper-third of the output canvas. Per-source crops are
  cached for the run; multi-fragment renders only pay for face
  detection once per camera.
- ``"center"`` — static center crop per source.

If face detection fails for one source, that source falls back to
``"center"`` independently — other cameras' speaker-lock crops are
unaffected. The render result records the per-source crop choices.

Phase 7 debt explicitly carried forward:

* Dynamic per-frame speaker tracking. Still one detection per source
  per render. Multi-cam highlights with movement will look static
  on the cropped output.
* Sub-full-height vertical crop. When the source aspect ≥ 9:16 the
  vertical crop fills the full source height — same as 6c-3.
* Async render. The pipeline still blocks for the duration; renders
  on long sync-group highlights take longer because of the per-
  fragment re-encode.
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
    SubSpan,
    highlights_dir_for_document,
    mark_rendered,
    rendered_output_path_for,
)
from core.render import render_cut
from core.sync import (
    StaleSyncGroupError,
    SyncGroup,
    extract_audio_master_window,
    read_sync_group,
    validate_sync_group_freshness,
)

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
"""Default ASS style block burned into highlight captions."""

# Internal alias preserved for the existing ffmpeg invocation below.
_CAPTION_FORCE_STYLE = CAPTION_FORCE_STYLE

_CAPTION_LINE_GAP_S = 0.30
_CAPTION_MAX_WORDS = 5
_CAPTION_MIN_WORDS = 3


# Normalize-encode targets used by the sync-group path. Every
# per-fragment intermediate lands at this profile so concat-demuxer
# can stream-copy the result.
_NORMALIZE_VCODEC = "libx264"
_NORMALIZE_PIX_FMT = "yuv420p"
_NORMALIZE_PRESET = "veryfast"
_NORMALIZE_CRF = "20"
_NORMALIZE_ACODEC = "aac"
_NORMALIZE_AUDIO_RATE = "48000"
_NORMALIZE_AUDIO_CHANNELS = "2"
_NORMALIZE_AUDIO_BITRATE = "192k"


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
    """Extract a single frame from ``source_path`` at ``t_seconds`` to ``out_path``."""
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

    Uses OpenCV's Haar cascade frontal-face classifier.
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

    See module docstring for vertical-composition caveat. Boundary-safe.
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


def _probe_video_codec(path: Path) -> str:
    """Return the source's video codec name (lowercase, e.g., 'h264' / 'hevc')."""
    ffprobe = _ffprobe_path()
    cmd = [
        str(ffprobe),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe codec failed: {result.stderr[-200:]}")
    return result.stdout.strip().lower()


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
    """Return ``(text, start, end)`` for words overlapping the span."""
    out: list[tuple[str, float, float]] = []
    for seg in document.segments:
        for w in seg.words:
            if w.end > span_start and w.start < span_end:
                out.append((w.text, float(w.start), float(w.end)))
    return out


def _group_words_into_lines(
    words: list[tuple[str, float, float]],
) -> list[list[tuple[str, float, float]]]:
    """Group a flat word list into caption lines."""
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

    Returns the empty string when no words overlap the span.
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
        _ = _CAPTION_MIN_WORDS  # documented floor; not enforced
        blocks.append(f"{idx}\n{ts}\n{text}\n")
    return "\n".join(blocks)


def build_caption_srt_for_subspans(
    document: Document, sub_spans: tuple[SubSpan, ...]
) -> str:
    """SRT body for a multi-fragment highlight, in output time.

    Each fragment's words are looked up at fragment-source time, then
    translated to *output time* — i.e., zero at the start of the
    rendered file, accumulating fragment durations as we walk forward.
    Lines are grouped per fragment with the existing pause/length
    heuristic; a line cannot straddle a fragment boundary.

    For sync-group highlights the document is the audio master's
    transcript and ``sub_spans`` carry camera-time intervals; the
    caller must pass *audio-master-time* spans (the renderer
    translates camera intervals to master intervals before calling
    this).
    """
    blocks: list[str] = []
    cursor = 0.0
    cue_idx = 1
    for span in sub_spans:
        words = words_in_span(document, span.source_start, span.source_end)
        if not words:
            cursor += span.duration
            continue
        lines = _group_words_into_lines(words)
        for line in lines:
            rel_start = max(0.0, line[0][1] - span.source_start) + cursor
            rel_end = (
                max(rel_start + 0.05, line[-1][2] - span.source_start + cursor)
            )
            ts = (
                f"{format_srt_timestamp(rel_start)} --> "
                f"{format_srt_timestamp(rel_end)}"
            )
            text = " ".join(w[0].strip() for w in line).strip()
            if not text:
                continue
            blocks.append(f"{cue_idx}\n{ts}\n{text}\n")
            cue_idx += 1
        cursor += span.duration
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# Ephemeral document for the cut step
# ---------------------------------------------------------------------------


def _ephemeral_doc_for_subspans(
    parent: Document,
    sub_spans: tuple[SubSpan, ...],
) -> Document:
    """Build a Document whose ``ranges`` are exactly ``sub_spans``.

    Used by the no-sync-group renderer: the same parent transcript +
    sources, but the ranges list is replaced with the highlight's
    fragments. All fragments must reference the same source path
    (caller's responsibility — the multi-source path goes through
    the sync-group branch).
    """
    if not sub_spans:
        raise ValueError("_ephemeral_doc_for_subspans: empty sub_spans")
    unique_paths = {str(s.source_path) for s in sub_spans}
    if len(unique_paths) != 1:
        raise ValueError(
            "_ephemeral_doc_for_subspans expects a single source; got "
            f"{sorted(unique_paths)!r}"
        )
    span_path = sub_spans[0].source_path

    sid: str | None = None
    for k, src in parent.sources.items():
        if Path(src.path) == Path(span_path):
            sid = k
            break
    if sid is None:
        sid = "highlight_src"
        new_src = MediaSource(
            id=sid,
            path=Path(span_path),
            duration=max(
                max(s.source_end for s in sub_spans) + 1.0,
                _probe_duration_seconds(span_path),
            ),
        )
        sources = {**parent.sources, sid: new_src}
    else:
        sources = parent.sources

    ranges = [
        Range(
            source_id=sid,
            start=float(s.source_start),
            end=float(s.source_end),
            reason=s.reason,
        )
        for s in sub_spans
    ]
    return _replace(parent, sources=dict(sources), ranges=ranges)


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

    Phase 7 changes:

    - ``crop_box`` is the crop applied to the *first unique source*
      (the historic single-source value). Multi-camera renders also
      populate ``crop_boxes_by_source`` with one entry per camera.
    - ``parent_source_hashes`` replaces the singular
      ``parent_source_hash``. Single-source renders have one entry.
    - ``sync_group_id`` is the group used at render time, or ``None``.
    """

    output_path: Path
    face_detection_used: Literal[
        "speaker_locked",
        "speaker_locked_fallback_to_center",
        "center",
    ]
    crop_box: tuple[int, int, int, int]
    parent_source_hashes: dict[str, str]
    crop_boxes_by_source: dict[str, tuple[int, int, int, int]]
    sync_group_id: str | None = None


# v2 alias kept so 6c-2 callers (mcp_server/tools/highlights.py) and
# any v2 test that imported the old singular field name still work.
def _legacy_metadata_singletons(
    metadata: HighlightRenderMetadata,
) -> tuple[str, tuple[int, int, int, int]]:
    """Return ``(parent_source_hash, crop_box)`` for v2-style consumers."""
    if not metadata.parent_source_hashes:
        return "", metadata.crop_box
    first_path = next(iter(metadata.parent_source_hashes))
    return metadata.parent_source_hashes[first_path], metadata.crop_box


def render_highlight(
    highlight: Highlight,
    parent_document: Document,
    progress_callback: Callable[[float], None] | None = None,
) -> HighlightRenderMetadata:
    """Render a highlight to ``<id>.highlight.mp4``; return render metadata.

    Stale check fires first against every entry in
    ``highlight.parent_source_hashes`` (and against the sync group's
    audio master + cameras when applicable).

    Three render paths share the bookkeeping but differ in the
    middle:

    * Single fragment, no sync group → :func:`_render_single_or_same_source`.
    * Multi-fragment, single source, no sync group →
      :func:`_render_single_or_same_source`.
    * Sync group → :func:`_render_with_sync_group`.

    All three update the highlight's sidecar JSON via
    :func:`~core.highlight.mark_rendered` and return a populated
    :class:`HighlightRenderMetadata`.
    """
    _check_stale_sources(highlight)

    sync_group: SyncGroup | None = None
    if highlight.sync_group_id is not None:
        sync_group = read_sync_group(
            highlight.parent_document_path, highlight.sync_group_id
        )
        validate_sync_group_freshness(sync_group)

    out_dir = highlights_dir_for_document(highlight.parent_document_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    final_path = rendered_output_path_for(out_dir, highlight.highlight_id)

    if sync_group is None:
        return _render_single_or_same_source(
            highlight, parent_document, out_dir, final_path, progress_callback
        )
    return _render_with_sync_group(
        highlight, parent_document, sync_group, out_dir, final_path, progress_callback
    )


def _check_stale_sources(highlight: Highlight) -> None:
    """Validate every per-camera source hash on the highlight.

    Sync-group audio master / camera staleness is handled separately
    (the sync group's own ``validate_sync_group_freshness``). This
    function only walks ``highlight.parent_source_hashes`` so a
    no-sync-group highlight gets the same guard.
    """
    for path_str, expected in highlight.parent_source_hashes.items():
        path = Path(path_str)
        try:
            live = cache_key(path)
        except FileNotFoundError as exc:
            raise StaleHighlightError(
                f"highlight source {path_str!r} is missing on disk: {exc}"
            ) from exc
        if expected != live:
            raise StaleHighlightError(
                f"highlight source {path_str!r} hash drifted "
                f"(expected={expected!r}, live={live!r}); the file has been "
                "replaced. Re-author the highlight against the current source."
            )


# ---------------------------------------------------------------------------
# Path A — no sync group (single-fragment OR multi-fragment same source)
# ---------------------------------------------------------------------------


def _render_single_or_same_source(
    highlight: Highlight,
    parent_document: Document,
    out_dir: Path,
    final_path: Path,
    progress_callback: Callable[[float], None] | None,
) -> HighlightRenderMetadata:
    """Render a no-sync-group highlight via the existing render_cut pipeline."""
    cut_path = out_dir / f"{highlight.highlight_id}.cut.mp4"
    ephemeral_doc = _ephemeral_doc_for_subspans(parent_document, highlight.sub_spans)

    face_detection_used: Literal[
        "speaker_locked",
        "speaker_locked_fallback_to_center",
        "center",
    ]
    crop: tuple[int, int, int, int]
    try:
        render_cut(ephemeral_doc, cut_path, on_progress=progress_callback)

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

        srt_path: Path | None = None
        if highlight.captions_enabled:
            srt_body = build_caption_srt_for_subspans(
                parent_document, highlight.sub_spans
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
    primary_path = next(iter(highlight.parent_source_hashes))
    return HighlightRenderMetadata(
        output_path=final_path,
        face_detection_used=face_detection_used,
        crop_box=(crop_x, crop_y, crop_w, crop_h),
        parent_source_hashes=dict(highlight.parent_source_hashes),
        crop_boxes_by_source={
            primary_path: (crop_x, crop_y, crop_w, crop_h),
        },
        sync_group_id=None,
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


def _resolve_speaker_locked_crop_at(
    source_path: Path,
    source_w: int,
    source_h: int,
    sample_t: float,
) -> tuple[int, int, int, int]:
    """Like :func:`_resolve_speaker_locked_crop` but at a caller-chosen time.

    Used by the sync-group path: the crop is derived against a frame
    inside the highlight (at the midpoint of the first fragment from
    that camera) rather than the camera file's overall midpoint.
    """
    sample_dir = source_path.parent / f".{source_path.stem}.sample"
    sample_dir.mkdir(exist_ok=True)
    sample = sample_dir / "frame.png"
    try:
        _sample_frame(source_path, sample_t, sample)
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
    """One-pass reframe + optional caption burn (no-sync-group path)."""
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
        _NORMALIZE_VCODEC,
        "-pix_fmt",
        _NORMALIZE_PIX_FMT,
        "-preset",
        _NORMALIZE_PRESET,
        "-crf",
        _NORMALIZE_CRF,
        "-c:a",
        _NORMALIZE_ACODEC,
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


# ---------------------------------------------------------------------------
# Path B — sync group (multi-source, audio-master-driven)
# ---------------------------------------------------------------------------


def _render_with_sync_group(
    highlight: Highlight,
    parent_document: Document,
    sync_group: SyncGroup,
    out_dir: Path,
    final_path: Path,
    progress_callback: Callable[[float], None] | None,
) -> HighlightRenderMetadata:
    """Per-fragment normalize-then-concat with audio-master swap.

    For each fragment:

    1. Look up the per-source crop (cached so face detection happens
       once per camera, not once per fragment).
    2. Cut the fragment's *video* from the camera (no audio); apply
       crop + scale to 1080×1920; encode to the canonical normalize
       profile.
    3. Extract the audio master's window at offset-translated
       times to a uniform AAC track.
    4. Mux the fragment video with the master audio. The result
       lands at the canonical profile.

    Then concat all fragments via the concat demuxer (now lossless
    because every intermediate matches profile by construction).
    Finally, optionally burn captions in a re-encode pass; if no
    captions, the concat output is the final file.

    Crop choice per source: a single sample at the midpoint of the
    first fragment from that camera (in *camera time*). Future
    iterations may sample multiple fragments and average; for now
    one detection per camera matches 6c-1 semantics.
    """
    # Compute per-source crops up-front so the per-fragment encode
    # below is a tight loop without face-detection branching.
    per_source_crop, face_status_per_source = _resolve_per_source_crops(
        highlight, sync_group
    )
    # Aggregate face_detection_used: report the "worst" outcome —
    # if any source fell back, the metadata's singular field reflects
    # the fallback; otherwise it matches the requested mode.
    face_detection_used = _aggregate_face_status(
        highlight.reframe_mode, face_status_per_source
    )

    # Build the sub_spans expressed in audio-master time so caption
    # generation matches the swapped audio's transcript.
    master_aligned_spans: tuple[SubSpan, ...] = tuple(
        SubSpan(
            source_path=sync_group.audio_master_path,
            source_start=s.source_start + sync_group.offset_for(s.source_path),
            source_end=s.source_end + sync_group.offset_for(s.source_path),
            reason=s.reason,
        )
        for s in highlight.sub_spans
    )

    intermediates: list[Path] = []
    try:
        for i, span in enumerate(highlight.sub_spans):
            crop = per_source_crop[str(span.source_path)]
            inter = out_dir / f"{highlight.highlight_id}.frag{i:03d}.mp4"
            offset = sync_group.offset_for(span.source_path)
            _build_fragment(
                span,
                sync_group.audio_master_path,
                offset,
                crop,
                out_path=inter,
                workdir=out_dir,
                fragment_id=f"{highlight.highlight_id}.frag{i:03d}",
            )
            intermediates.append(inter)
            if progress_callback is not None:
                progress_callback(min(1.0, (i + 1) / max(1, len(highlight.sub_spans))))

        # Concat (lossless: every intermediate matches profile).
        concat_target = (
            out_dir / f"{highlight.highlight_id}.concat.mp4"
            if highlight.captions_enabled
            else final_path
        )
        _ffmpeg_concat_demuxer(intermediates, concat_target)

        if highlight.captions_enabled:
            try:
                srt_body = build_caption_srt_for_subspans(
                    parent_document, master_aligned_spans
                )
                if srt_body:
                    srt_path = out_dir / f"{highlight.highlight_id}.captions.srt"
                    srt_path.write_text(srt_body, encoding="utf-8")
                    _ffmpeg_burn_captions(concat_target, final_path, srt_path)
                    try:
                        srt_path.unlink()
                    except FileNotFoundError:
                        pass
                else:
                    # No words landed in the span — copy concat to final.
                    concat_target.replace(final_path)
            finally:
                if concat_target != final_path:
                    try:
                        concat_target.unlink()
                    except FileNotFoundError:
                        pass
    finally:
        for inter in intermediates:
            try:
                inter.unlink()
            except FileNotFoundError:
                pass

    mark_rendered(highlight.parent_document_path, highlight, final_path)
    primary_path = next(iter(highlight.parent_source_hashes))
    primary_crop = per_source_crop.get(primary_path)
    if primary_crop is None:
        primary_crop = next(iter(per_source_crop.values()))
    return HighlightRenderMetadata(
        output_path=final_path,
        face_detection_used=face_detection_used,
        crop_box=primary_crop,
        parent_source_hashes=dict(highlight.parent_source_hashes),
        crop_boxes_by_source=dict(per_source_crop),
        sync_group_id=highlight.sync_group_id,
    )


def _resolve_per_source_crops(
    highlight: Highlight,
    sync_group: SyncGroup,
) -> tuple[
    dict[str, tuple[int, int, int, int]],
    dict[str, Literal["speaker_locked", "speaker_locked_fallback_to_center", "center"]],
]:
    """Compute one crop window per unique source on a multi-cam highlight.

    For ``speaker_locked``, we sample one frame per source at the
    midpoint of the first sub_span that uses that source (in *camera*
    time, not master time — the camera file's clock is what
    ``_probe_dimensions`` and ``_sample_frame`` see).
    """
    crops: dict[str, tuple[int, int, int, int]] = {}
    face_status: dict[
        str,
        Literal["speaker_locked", "speaker_locked_fallback_to_center", "center"],
    ] = {}

    # Pick first sub_span per source to drive the crop sampling.
    first_span_by_source: dict[str, SubSpan] = {}
    for s in highlight.sub_spans:
        first_span_by_source.setdefault(str(s.source_path), s)

    for path_str, span in first_span_by_source.items():
        path = Path(path_str)
        source_w, source_h = _probe_dimensions(path)
        if highlight.reframe_mode == "speaker_locked":
            sample_t = (span.source_start + span.source_end) / 2.0
            try:
                crop = _resolve_speaker_locked_crop_at(
                    path, source_w, source_h, sample_t
                )
                face_status[path_str] = "speaker_locked"
            except FaceDetectionError as exc:
                _LOG.warning(
                    "speaker-lock fallback for source %s in highlight %s: %s",
                    path_str,
                    highlight.highlight_id,
                    exc,
                )
                crop = compute_center_crop(source_w, source_h)
                face_status[path_str] = "speaker_locked_fallback_to_center"
        else:
            crop = compute_center_crop(source_w, source_h)
            face_status[path_str] = "center"
        crops[path_str] = crop
    # Defend against an unused sync_group reference at type-check
    # time — the sync group is exercised in caption-time math but we
    # intentionally don't gate the per-source crop on it.
    _ = sync_group
    return crops, face_status


def _aggregate_face_status(
    requested_mode: str,
    face_status_per_source: dict[str, str],
) -> Literal[
    "speaker_locked", "speaker_locked_fallback_to_center", "center",
]:
    """Reduce per-source statuses to one summary value for the metadata.

    Rule: if the requested mode was ``center``, the summary is
    ``center``. If any source fell back to center, summary is
    ``speaker_locked_fallback_to_center``. Otherwise
    ``speaker_locked``.
    """
    if requested_mode == "center":
        return "center"
    if any(
        v == "speaker_locked_fallback_to_center"
        for v in face_status_per_source.values()
    ):
        return "speaker_locked_fallback_to_center"
    return "speaker_locked"


def _build_fragment(
    span: SubSpan,
    audio_master_path: Path,
    offset_s: float,
    crop: tuple[int, int, int, int],
    *,
    out_path: Path,
    workdir: Path,
    fragment_id: str,
) -> None:
    """Cut camera video [start, end], mux master audio at offset, normalize.

    Output is the canonical normalize profile (1080×1920 H264 +
    AAC 48 kHz stereo). The video is decoded from the camera, cropped
    + scaled, then re-encoded; the audio master is extracted to AAC
    via :func:`extract_audio_master_window`; ffmpeg muxes them.

    A single-pass implementation would chain crop+scale+amerge in
    one filtergraph — we deliberately split into two ffmpeg calls
    (extract audio, then mux) because the audio extraction needs to
    handle negative offsets (silence padding at head) which is its
    own filter graph.
    """
    crop_w, crop_h, crop_x, crop_y = crop
    duration = span.duration
    audio_temp = workdir / f"{fragment_id}.audio.m4a"
    extract_audio_master_window(
        audio_master_path,
        audio_temp,
        start_s=span.source_start + offset_s,
        duration_s=duration,
    )
    try:
        ffmpeg = get_ffmpeg_path()
        cmd = [
            str(ffmpeg),
            "-y",
            "-loglevel",
            "error",
            # Camera video, seek to the fragment window.
            "-ss",
            f"{span.source_start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(span.source_path),
            # Pre-extracted audio master window.
            "-i",
            str(audio_temp),
            # Map: video from input 0, audio from input 1.
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-vf",
            f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y},"
            f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}",
            "-c:v",
            _NORMALIZE_VCODEC,
            "-pix_fmt",
            _NORMALIZE_PIX_FMT,
            "-preset",
            _NORMALIZE_PRESET,
            "-crf",
            _NORMALIZE_CRF,
            "-c:a",
            "copy",
            "-ar",
            _NORMALIZE_AUDIO_RATE,
            "-ac",
            _NORMALIZE_AUDIO_CHANNELS,
            "-shortest",
            str(out_path),
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=1800
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg fragment build failed (rc={result.returncode}): "
                f"{result.stderr[-400:]}"
            )
    finally:
        try:
            audio_temp.unlink()
        except FileNotFoundError:
            pass


def _ffmpeg_concat_demuxer(intermediates: list[Path], output_path: Path) -> None:
    """Glue ``intermediates`` into ``output_path`` via stream-copy.

    By construction every intermediate has the canonical normalize
    profile, so concat-demuxer is safe and lossless.
    """
    if not intermediates:
        raise ValueError("ffmpeg concat called with no intermediates")
    list_path = output_path.parent / f"{output_path.stem}.concat.txt"
    list_path.write_text(
        "\n".join(f"file '{p.resolve()}'" for p in intermediates) + "\n",
        encoding="utf-8",
    )
    ffmpeg = get_ffmpeg_path()
    cmd = [
        str(ffmpeg),
        "-y",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    finally:
        try:
            list_path.unlink()
        except FileNotFoundError:
            pass
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg concat demuxer failed (rc={result.returncode}): "
            f"{result.stderr[-400:]}"
        )


def _ffmpeg_burn_captions(in_path: Path, out_path: Path, srt_path: Path) -> None:
    """Burn captions onto an already-1080×1920 file via subtitles filter."""
    ffmpeg = get_ffmpeg_path()
    cwd = srt_path.parent
    cmd = [
        str(ffmpeg),
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(in_path.resolve()),
        "-vf",
        f"subtitles={srt_path.name}:force_style='{_CAPTION_FORCE_STYLE}'",
        "-c:v",
        _NORMALIZE_VCODEC,
        "-pix_fmt",
        _NORMALIZE_PIX_FMT,
        "-preset",
        _NORMALIZE_PRESET,
        "-crf",
        _NORMALIZE_CRF,
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(out_path.resolve()),
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=1800, cwd=str(cwd)
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg caption burn-in failed (rc={result.returncode}): "
            f"{result.stderr[-400:]}"
        )


# Re-export so tests / mcp tools can grab StaleSyncGroupError without
# needing a second import line.
__all__ = [
    "CAPTION_FORCE_STYLE",
    "FaceDetectionError",
    "HighlightRenderMetadata",
    "OUTPUT_HEIGHT",
    "OUTPUT_WIDTH",
    "StaleSyncGroupError",
    "build_caption_srt",
    "build_caption_srt_for_subspans",
    "compute_center_crop",
    "compute_speaker_locked_crop",
    "detect_speaker_bbox",
    "render_highlight",
    "words_in_span",
]
