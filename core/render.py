"""Render a v2 Document to a media file via smartcut.

Pipeline:

1. Pick the Document's source. Phase 4f-3 supports single-source
   documents only at render time — multi-source compositing is Phase 5+.
   The source is looked up by ``Range.source_id`` (all ranges must agree)
   in ``doc.sources``.
2. Empty-ranges → :class:`ValueError`. ``ranges == []`` means "nothing
   kept"; we don't write a zero-length file.
3. Full-coverage shortcut: if ``doc.ranges`` covers ``[0, duration]``
   contiguously with no gaps, ``shutil.copy2`` the source. The invariant
   "no edit ⇒ no transcoding" still holds; the detection logic just
   checks gap-free total coverage rather than ``len(cuts) == 0``.
4. Snap each range's boundaries to the nearest word boundary (Phase 4f-1
   C). For a keep-range the snap direction is OUTWARD (earlier word
   start, later word end) — preserve more, cut less, consistent with the
   pad rationale. Ranges that don't overlap any word pass through.
5. Pad each keep-range by ``pad_lead`` on the start side and
   ``pad_trail`` on the end side; clamp to ``[0, duration]``; merge any
   ranges that overlap or touch after padding.
6. Convert keep-ranges to :class:`~fractions.Fraction`
   (``limit_denominator=1000``; millisecond precision matches Whisper
   word timestamp resolution) and hand to
   ``smartcut.cut_video.smart_cut`` with audio passthru.
7. If ``audio_fade_ms > 0`` and the output has internal joins
   (i.e., more than one keep-range), post-process via ffmpeg to apply a
   linear ``afade`` of ``audio_fade_ms`` milliseconds on each side of
   every join. Smartcut has no native fade option (verified at Phase
   4f-1) so this is a re-encode of the audio track only; video stays
   ``-c:v copy``.

Progress contract:
    ``on_progress: Callable[[float], None] | None`` fires monotonically,
    clamped to ``[0.0, 1.0]``. ``finalize()`` ensures the last call is
    1.0 on success even if smartcut's increments don't sum to its
    announced total.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import warnings
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path

from core.audio import get_ffmpeg_path
from core.document import Document, MediaSource, Range, Segment

_LOG = logging.getLogger(__name__)


def _to_fraction_seconds(value: float) -> Fraction:
    """Convert a float-seconds timestamp to a millisecond-precise Fraction.

    Higher precision than 1ms wastes bytes in smartcut's bookkeeping
    without buying anything (Whisper word timestamps are quantized to
    20–50 ms in practice). Lower precision drops sub-frame accuracy.
    """
    return Fraction(value).limit_denominator(1000)


def _select_single_source(doc: Document) -> MediaSource:
    """Phase 4f-3 renders single-source Documents only.

    All ranges must share a ``source_id`` that resolves to one of
    ``doc.sources``. Multi-source composition is Phase 5+ work and will
    require a different render path.
    """
    if not doc.sources:
        raise ValueError("Document has no sources to render")
    source_ids = {r.source_id for r in doc.ranges}
    if len(source_ids) > 1:
        raise ValueError(
            f"Document references multiple sources {source_ids!r}; "
            "render_cut supports single-source documents in Phase 4f"
        )
    if not source_ids:
        # No ranges yet — caller will hit the empty-ranges error below;
        # but if there's exactly one source registered, fall back to it
        # so error messages reference the right path.
        if len(doc.sources) == 1:
            return next(iter(doc.sources.values()))
        raise ValueError(
            "Document has no ranges and multiple sources; "
            "cannot determine which source to render"
        )
    sid = next(iter(source_ids))
    if sid not in doc.sources:
        raise ValueError(
            f"Range references source_id={sid!r} not in doc.sources "
            f"({list(doc.sources)!r})"
        )
    return doc.sources[sid]


def _is_full_coverage(ranges: list[Range], duration: float) -> bool:
    """Return True iff ``ranges`` (sorted, non-overlapping) cover
    ``[0.0, duration]`` contiguously with no gaps.
    """
    if not ranges:
        return False
    sorted_ranges = sorted(ranges, key=lambda r: r.start)
    if sorted_ranges[0].start > 0.0:
        return False
    cursor = sorted_ranges[0].start
    for r in sorted_ranges:
        if r.start > cursor:
            return False
        cursor = max(cursor, r.end)
    return cursor >= duration


def _pad_and_merge_keep_ranges(
    keeps: list[tuple[float, float]],
    pad_lead: float,
    pad_trail: float,
    duration: float,
) -> list[tuple[float, float]]:
    """Pad each keep-range by ``pad_lead`` on the start side and
    ``pad_trail`` on the end side, clamp to ``[0, duration]``, then merge
    any padded ranges that now overlap or touch.
    """
    if not keeps:
        return []
    if pad_lead < 0:
        raise ValueError(f"pad_lead must be >= 0 (got {pad_lead})")
    if pad_trail < 0:
        raise ValueError(f"pad_trail must be >= 0 (got {pad_trail})")
    padded: list[tuple[float, float]] = []
    for start, end in keeps:
        ps = max(0.0, start - pad_lead)
        pe = min(duration, end + pad_trail)
        if pe <= ps:
            continue
        padded.append((ps, pe))
    merged: list[tuple[float, float]] = []
    for s, e in padded:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _snap_ranges_to_word_boundaries(
    ranges: list[Range], segments: list[Segment]
) -> list[Range]:
    """Snap each keep-range's start/end OUTWARD to the nearest word
    boundary.

    For a keep-range, "outward" means: ``range.start`` snaps to a word
    start at or before the current value (preserve the leading edge of
    the first word), and ``range.end`` snaps to a word end at or after
    the current value (preserve the trailing edge of the last word).
    This is the opposite direction from cuts (Phase 4f-1's snap was
    inward to remove more); for ranges we want to preserve more. The
    invariant — "Never cut inside a word" — holds either way.

    A range that doesn't overlap any word is left untouched (silence
    between segments has no boundary to snap to).
    """
    word_starts: list[float] = []
    word_ends: list[float] = []
    word_intervals: list[tuple[float, float]] = []
    for seg in segments:
        for w in seg.words:
            word_starts.append(w.start)
            word_ends.append(w.end)
            word_intervals.append((w.start, w.end))

    if not word_intervals:
        return list(ranges)

    out: list[Range] = []
    for r in ranges:
        overlaps = any(
            ws <= r.end and we >= r.start for (ws, we) in word_intervals
        )
        if not overlaps:
            out.append(r)
            continue
        snapped_start = _snap_outward_start(r.start, word_starts)
        snapped_end = _snap_outward_end(r.end, word_ends)
        if snapped_end <= snapped_start:
            out.append(r)
            continue
        out.append(
            Range(
                source_id=r.source_id,
                start=snapped_start,
                end=snapped_end,
                reason=r.reason,
            )
        )
    return out


def _snap_outward_start(value: float, word_starts: list[float]) -> float:
    """Snap a keep-range start to the nearest word start ≤ value (preserve more).

    If no word start is ≤ value, falls back to the smallest word start
    (this happens when the range starts before any word in the segment).
    """
    candidates_le = [s for s in word_starts if s <= value]
    if candidates_le:
        return max(candidates_le)
    return min(word_starts)


def _snap_outward_end(value: float, word_ends: list[float]) -> float:
    """Snap a keep-range end to the nearest word end ≥ value (preserve more).

    If no word end is ≥ value, falls back to the largest word end.
    """
    candidates_ge = [e for e in word_ends if e >= value]
    if candidates_ge:
        return min(candidates_ge)
    return max(word_ends)


class _ProgressAdapter:
    """Bridge smartcut's ``emit(int)`` Protocol to a float callback.

    Smartcut's first ``emit`` announces the total work units; subsequent
    calls are increments that may be non-uniform and may exceed the
    announced total. We map cumulative-divided-by-total to ``[0, 1]``
    and never go backwards. ``finalize`` guarantees the final emitted
    value is 1.0 on success.
    """

    def __init__(self, on_progress: Callable[[float], None] | None) -> None:
        self._cb = on_progress
        self._total: int | None = None
        self._cumulative = 0
        self._last_emitted = 0.0

    def emit(self, value: int) -> None:
        if self._total is None:
            self._total = max(1, int(value))
            return
        self._cumulative += int(value)
        if self._cb is None:
            return
        fraction = min(1.0, self._cumulative / self._total)
        if fraction > self._last_emitted:
            self._last_emitted = fraction
            self._cb(fraction)

    def finalize(self) -> None:
        if self._cb is not None and self._last_emitted < 1.0:
            self._last_emitted = 1.0
            self._cb(1.0)


def _resolve_keep_ranges(
    doc: Document,
    *,
    pad_lead: float,
    pad_trail: float,
    merge_gap: float,
) -> list[tuple[float, float]]:
    """The full keep-range computation, factored out for direct testing.

    Snaps each Range to word boundaries (Phase 4f-1 C, recast for the v2
    keep-range model), pads each side independently, merges any ranges
    that overlap or touch after padding, then absorbs cross-range gaps
    smaller than ``merge_gap``. The result is a list of ``(start, end)``
    intervals in source time, ready for smartcut.
    """
    if not doc.ranges:
        return []
    source = _select_single_source(doc)
    snapped = _snap_ranges_to_word_boundaries(doc.ranges, doc.segments)
    raw_keeps = sorted(
        ((r.start, r.end) for r in snapped), key=lambda t: t[0]
    )
    padded = _pad_and_merge_keep_ranges(
        raw_keeps, pad_lead, pad_trail, source.duration
    )
    if merge_gap > 0:
        padded = _merge_close_keep_ranges(padded, merge_gap)
    return padded


def _merge_close_keep_ranges(
    keeps: list[tuple[float, float]], merge_gap: float
) -> list[tuple[float, float]]:
    """Absorb gaps smaller than ``merge_gap`` between consecutive keeps.

    A gap of exactly ``merge_gap`` does NOT merge — strict less-than. This
    matches the v1 :class:`~core.editing.MergeAdjacentCuts` semantics that
    the v1 render pipeline used; the behavior moved here in 4f-3 because
    v2 ranges are canonicalized at every edit (no need for an explicit
    edit command anymore).
    """
    if not keeps:
        return []
    out = [keeps[0]]
    for s, e in keeps[1:]:
        ls, le = out[-1]
        if s - le < merge_gap:
            out[-1] = (ls, max(le, e))
        else:
            out.append((s, e))
    return out


def _join_times_in_output(keeps: list[tuple[float, float]]) -> list[float]:
    """Output-timeline times where consecutive keep-ranges meet.

    For N keep-ranges there are N-1 internal joins (the file's outer
    boundaries are not joins). Returns each join as cumulative output
    time of the keep-ranges that precede it.
    """
    joins: list[float] = []
    cumulative = 0.0
    for i, (s, e) in enumerate(keeps):
        cumulative += e - s
        if i < len(keeps) - 1:
            joins.append(cumulative)
    return joins


# Matches the first video stream line in ``ffmpeg -i`` stderr.
# Example: ``Stream #0:0[0x1](und): Video: hevc (Main 10) (hev1 / 0x31766568), ...``
_VIDEO_STREAM_RE = re.compile(
    r"Stream #\d+:\d+.*?: Video: (?P<codec>\w+).*?\((?P<tag>\w{4}) /"
)


def _probe_video_codec_and_tag(path: Path) -> tuple[str | None, str | None]:
    """Return ``(codec_name, codec_tag)`` of the first video stream.

    Parses ``ffmpeg -i`` stderr because we don't ship ffprobe (matching
    ``core.audio.get_duration``'s approach). Both values are lowercased.
    Returns ``(None, None)`` when no video stream is found or the line
    can't be parsed — callers must treat that as "skip retag".
    """
    ffmpeg = get_ffmpeg_path()
    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    for line in result.stderr.splitlines():
        m = _VIDEO_STREAM_RE.search(line)
        if m:
            return m.group("codec").lower(), m.group("tag").lower()
    return None, None


def _ensure_quicktime_compatible(path: Path) -> None:
    """Retag HEVC outputs with ``codec_tag=hvc1`` so QuickTime opens them.

    libavformat defaults HEVC's MP4 codec_tag to ``hev1``; QuickTime
    Player rejects ``hev1`` and requires ``hvc1``. Both tags describe
    the same bitstream — they differ only in whether parameter sets
    (VPS/SPS/PPS) live solely in the sample description (``hvc1``) or
    may also appear inline (``hev1``). Smartcut + libavformat output
    has parameter sets in the sample description, so a metadata-only
    remux is safe and lossless.

    No-op when the video stream isn't HEVC, is already tagged ``hvc1``,
    or can't be probed.
    """
    codec, tag = _probe_video_codec_and_tag(path)
    if codec != "hevc" or tag != "hev1":
        return

    tmp = path.with_name(path.name + ".qt.tmp")
    ffmpeg = get_ffmpeg_path()
    cmd = [
        str(ffmpeg),
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-c",
        "copy",
        "-tag:v",
        "hvc1",
        "-movflags",
        "+faststart",
        str(tmp),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise RuntimeError(
            f"ffmpeg hvc1 retag failed (rc={result.returncode}): "
            f"{result.stderr[-400:]}"
        )
    tmp.replace(path)


def _apply_audio_fades(
    smartcut_output: Path,
    final_output: Path,
    joins: list[float],
    fade_ms: int,
) -> None:
    """Re-mux ``smartcut_output`` into ``final_output`` applying a linear
    ``afade`` on each side of every join.

    Video is stream-copied; the audio track is re-encoded so the filter
    can apply. This is the only re-encode in the pipeline; smartcut's
    audio is passthru.
    """
    fade_s = fade_ms / 1000.0
    chain_parts: list[str] = []
    for t in joins:
        out_start = max(0.0, t - fade_s)
        # ``enable`` gates the filter to its fade window; outside that window
        # the signal passes through unmodified. Without enable, ``afade=t=out``
        # silences everything past ``st+d`` and ``afade=t=in`` silences
        # everything before ``st`` — chaining them silences the entire track.
        chain_parts.append(
            f"afade=t=out:st={out_start:.6f}:d={fade_s:.6f}"
            f":enable='between(t,{out_start:.6f},{t:.6f})'"
        )
        chain_parts.append(
            f"afade=t=in:st={t:.6f}:d={fade_s:.6f}"
            f":enable='between(t,{t:.6f},{t + fade_s:.6f})'"
        )
    af_chain = ",".join(chain_parts)

    ffmpeg = get_ffmpeg_path()
    cmd = [
        str(ffmpeg),
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(smartcut_output),
        "-c:v",
        "copy",
        "-af",
        af_chain,
        str(final_output),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg fade post-process failed (rc={result.returncode}): "
            f"{result.stderr[-400:]}"
        )


def render_cut(
    doc: Document,
    output_path: Path,
    on_progress: Callable[[float], None] | None = None,
    *,
    pad_lead: float = 0.10,
    pad_trail: float = 0.10,
    merge_gap: float = 0.30,
    audio_fade_ms: int = 30,
    pad: float | None = None,
) -> Path:
    """Render ``doc`` with its keep-ranges to ``output_path``.

    ``pad_lead`` widens each kept range on its start side; ``pad_trail``
    on its end side. Both default to 100ms.

    ``audio_fade_ms`` (default 30) controls a linear ``afade`` applied
    at every internal segment join — including the new run-boundary
    joins introduced by the non-monotonic path. 30ms is below conscious
    "fade" perception but above the threshold needed to suppress
    click/pop discontinuities. Values >50 are flagged with a warning.
    0 disables fades entirely.

    ``pad`` is deprecated as of Phase 4f-1.

    Empty ``doc.ranges`` → :class:`ValueError`.
    Phase 6a (schema v3): ``main_timeline.is_source_monotonic()`` decides
    the path. Monotonic timelines (everything 6a editing produces) take
    the legacy single-call smartcut path — byte-for-byte identical to
    pre-6a output, including the full-coverage ``shutil.copy2``
    shortcut. Non-monotonic timelines (only reachable via hand-built v3
    fixtures or 6b's rearrangement commands) split into source-monotonic
    runs; each run is one smartcut call; runs concat with ffmpeg's
    stream-copy concat demuxer; a single ``afade`` post-pass covers
    every join, including the run-boundary ones the spec calls out.
    """
    if pad is not None:
        warnings.warn(
            "render_cut(pad=...) is deprecated; pass pad_lead and pad_trail "
            "explicitly. Setting both to the given value.",
            DeprecationWarning,
            stacklevel=2,
        )
        pad_lead = pad
        pad_trail = pad

    if audio_fade_ms < 0:
        raise ValueError(f"audio_fade_ms must be >= 0 (got {audio_fade_ms})")
    if audio_fade_ms > 50:
        _LOG.warning(
            "audio_fade_ms=%d > 50ms is audible as a dissolve, "
            "not a click-suppressor",
            audio_fade_ms,
        )

    if not doc.ranges:
        raise ValueError("Document has no ranges to render")

    output_path = Path(output_path)

    if doc.main_timeline.is_source_monotonic():
        result = _render_monotonic(
            doc,
            output_path,
            on_progress=on_progress,
            pad_lead=pad_lead,
            pad_trail=pad_trail,
            merge_gap=merge_gap,
            audio_fade_ms=audio_fade_ms,
        )
    else:
        result = _render_non_monotonic(
            doc,
            output_path,
            on_progress=on_progress,
            pad_lead=pad_lead,
            pad_trail=pad_trail,
            merge_gap=merge_gap,
            audio_fade_ms=audio_fade_ms,
        )
    _ensure_quicktime_compatible(result)
    return result


# ---------------------------------------------------------------------------
# Monotonic fast path (the v2 behaviour, lifted into a helper)
# ---------------------------------------------------------------------------


def _render_monotonic(
    doc: Document,
    output_path: Path,
    *,
    on_progress: Callable[[float], None] | None,
    pad_lead: float,
    pad_trail: float,
    merge_gap: float,
    audio_fade_ms: int,
) -> Path:
    """Single-call smartcut path, identical to v2's render_cut body.

    Pre-6a Documents and every 6a-edited Document are source-monotonic
    by construction; this is the path they hit. Output is byte-identical
    to v2's render output for the same input.
    """
    source = _select_single_source(doc)
    if not source.path.is_file():
        raise FileNotFoundError(
            f"MediaSource path does not exist: {source.path}"
        )

    if _is_full_coverage(doc.ranges, source.duration):
        shutil.copy2(source.path, output_path)
        if on_progress is not None:
            on_progress(1.0)
        return output_path

    final_keeps = _resolve_keep_ranges(
        doc, pad_lead=pad_lead, pad_trail=pad_trail, merge_gap=merge_gap
    )
    if not final_keeps:
        raise ValueError(
            "Ranges + padding produced no keep-range to render"
        )

    from smartcut.cut_video import (
        AudioExportInfo,
        AudioExportSettings,
        VideoExportMode,
        VideoExportQuality,
        VideoSettings,
        smart_cut,
    )
    from smartcut.media_container import MediaContainer

    container = MediaContainer(str(source.path))
    audio_settings = [
        AudioExportSettings(codec="passthru") for _ in container.audio_tracks
    ]
    audio_export_info = AudioExportInfo(output_tracks=audio_settings)
    video_settings = VideoSettings(
        VideoExportMode.SMARTCUT, VideoExportQuality.NORMAL, "copy"
    )

    fraction_keeps = [
        (_to_fraction_seconds(s), _to_fraction_seconds(e)) for s, e in final_keeps
    ]

    joins = _join_times_in_output(final_keeps)
    needs_fade = audio_fade_ms > 0 and bool(joins) and bool(container.audio_tracks)
    smartcut_target = (
        output_path.parent / f"{output_path.stem}.smartcut{output_path.suffix}"
        if needs_fade
        else output_path
    )

    adapter = _ProgressAdapter(on_progress)
    try:
        exc = smart_cut(
            container,
            fraction_keeps,
            str(smartcut_target),
            audio_export_info=audio_export_info,
            video_settings=video_settings,
            progress=adapter,
            log_level="error",
        )
        if exc is not None:
            raise exc
    finally:
        adapter.finalize()

    if needs_fade:
        try:
            _apply_audio_fades(smartcut_target, output_path, joins, audio_fade_ms)
        finally:
            try:
                smartcut_target.unlink()
            except FileNotFoundError:
                pass

    return output_path


# ---------------------------------------------------------------------------
# Non-monotonic path (Phase 6a) — split into runs, smartcut each, concat
# ---------------------------------------------------------------------------


def _render_non_monotonic(
    doc: Document,
    output_path: Path,
    *,
    on_progress: Callable[[float], None] | None,
    pad_lead: float,
    pad_trail: float,
    merge_gap: float,
    audio_fade_ms: int,
) -> Path:
    """Run-batched render for non-monotonic v3 timelines.

    Algorithm:

    1. Split ``doc.main_timeline`` into maximal source-monotonic runs.
       Each run is a contiguous slice of the playlist whose clips share
       a source path and walk source-time forward without overlap.
    2. For each run, compute the run's effective keep-ranges (the same
       word-boundary snap + asymmetric pad + merge_gap pipeline as the
       monotonic path) and feed them as one ``smart_cut`` call producing
       a temp file. **Smartcut MediaContainer is reused** across runs
       that share a source path — the YELLOW spike showed each fresh
       container costs ~7s of HEVC 10-bit demux on the heavy fixture;
       reuse drops 3 calls from 20.7s to 13.5s on that source.
    3. Concat the per-run temp files with ffmpeg's concat demuxer
       (stream-copy, no re-encode).
    4. Compute every join time in the *final* output (within-run joins
       across all runs, plus the run-to-run boundary joins) and apply
       one unified ``afade`` pass via :func:`_apply_audio_fades`. This
       satisfies the spec's "concat boundaries are new cut edges and
       need the same 30 ms fade treatment."
    5. Progress is merged across runs weighted by per-run output
       duration so the caller sees a single 0.0–1.0 stream.
    """
    from core.timeline import split_into_monotonic_runs

    timeline = doc.main_timeline
    runs = split_into_monotonic_runs(timeline)
    if not runs:
        raise ValueError("Document has no ranges to render")

    # Per-run resolution: every run reuses the doc's segments + sources
    # for word-boundary snap and source duration lookup; the only thing
    # that changes between runs is which clips participate.
    per_run_keeps: list[list[tuple[float, float]]] = []
    per_run_source_paths: list[Path] = []
    per_run_durations: list[float] = []
    for run_tl in runs:
        # All clips in a run share source_path by construction.
        run_source_path = run_tl.clips[0].source_path
        run_doc = _project_doc_for_run(doc, run_tl)
        keeps = _resolve_keep_ranges(
            run_doc,
            pad_lead=pad_lead,
            pad_trail=pad_trail,
            merge_gap=merge_gap,
        )
        if not keeps:
            raise ValueError(
                f"Ranges + padding produced no keep-range for run "
                f"{run_source_path.name}"
            )
        per_run_keeps.append(keeps)
        per_run_source_paths.append(run_source_path)
        per_run_durations.append(sum(e - s for s, e in keeps))

    total_run_duration = sum(per_run_durations)
    if total_run_duration <= 0:
        raise ValueError("Non-monotonic render produced zero total duration")

    from smartcut.cut_video import (
        AudioExportInfo,
        AudioExportSettings,
        VideoExportMode,
        VideoExportQuality,
        VideoSettings,
        smart_cut,
    )
    from smartcut.media_container import MediaContainer

    video_settings = VideoSettings(
        VideoExportMode.SMARTCUT, VideoExportQuality.NORMAL, "copy"
    )

    # Reusable MediaContainer cache, keyed by source path. The spike
    # confirmed reuse is safe (no observable state corruption) and the
    # demux-cost saving is meaningful on heavy sources.
    containers: dict[Path, MediaContainer] = {}
    has_audio = False  # tracked for the final fade decision

    workdir = output_path.parent
    intermediates: list[Path] = []
    audio_track_count: int = 0

    progress_done = 0.0  # cumulative output seconds completed across runs
    run_progress_carry = [0.0]  # mutable cell for closure access

    def _make_run_progress(run_total_s: float) -> Callable[[float], None] | None:
        """A per-run progress callback that maps the run's 0..1 onto a
        weighted slice of the global progress space.
        """
        if on_progress is None:
            return None
        run_index_total = total_run_duration

        def _emit(fraction: float) -> None:
            run_progress_carry[0] = max(0.0, min(1.0, fraction))
            global_fraction = (
                progress_done + run_progress_carry[0] * run_total_s
            ) / run_index_total
            on_progress(max(0.0, min(1.0, global_fraction)))

        return _emit

    try:
        # Per-run smartcut call.
        for i, (run_keeps, src_path, run_dur) in enumerate(
            zip(per_run_keeps, per_run_source_paths, per_run_durations, strict=False)
        ):
            container = containers.get(src_path)
            if container is None:
                container = MediaContainer(str(src_path))
                containers[src_path] = container

            audio_track_count = max(audio_track_count, len(container.audio_tracks))
            has_audio = has_audio or bool(container.audio_tracks)
            audio_settings = [
                AudioExportSettings(codec="passthru")
                for _ in container.audio_tracks
            ]
            audio_export_info = AudioExportInfo(output_tracks=audio_settings)
            fraction_keeps = [
                (_to_fraction_seconds(s), _to_fraction_seconds(e))
                for s, e in run_keeps
            ]

            inter = workdir / f"{output_path.stem}.run{i}{output_path.suffix}"
            adapter = _ProgressAdapter(_make_run_progress(run_dur))
            try:
                exc = smart_cut(
                    container,
                    fraction_keeps,
                    str(inter),
                    audio_export_info=audio_export_info,
                    video_settings=video_settings,
                    progress=adapter,
                    log_level="error",
                )
                if exc is not None:
                    raise exc
            finally:
                adapter.finalize()
            intermediates.append(inter)
            progress_done += run_dur
            run_progress_carry[0] = 0.0

        # Concat the per-run intermediates via the ffmpeg concat demuxer
        # (stream-copy, no re-encode).
        concat_target = (
            workdir / f"{output_path.stem}.concat{output_path.suffix}"
            if audio_fade_ms > 0 and has_audio
            else output_path
        )
        _ffmpeg_concat_demuxer(intermediates, concat_target)

        # Build the unified join list across all runs in playlist order.
        # Every keep-range edge that isn't the file's outer boundary is a
        # join in the final output — which sweeps in both within-run
        # joins and the run-to-run boundary joins.
        all_keeps_in_playlist_order: list[tuple[float, float]] = []
        for run_keeps in per_run_keeps:
            all_keeps_in_playlist_order.extend(run_keeps)
        joins = _join_times_in_output(all_keeps_in_playlist_order)
        needs_fade = audio_fade_ms > 0 and bool(joins) and has_audio

        if needs_fade:
            try:
                _apply_audio_fades(concat_target, output_path, joins, audio_fade_ms)
            finally:
                try:
                    concat_target.unlink()
                except FileNotFoundError:
                    pass

        if on_progress is not None:
            on_progress(1.0)
        return output_path
    finally:
        for inter in intermediates:
            try:
                inter.unlink()
            except FileNotFoundError:
                pass


def _project_doc_for_run(doc: Document, run_timeline: Timeline) -> Document:  # noqa: F821
    """Build a Document-like projection holding only the run's ranges.

    ``_resolve_keep_ranges`` reads ``doc.ranges`` to compute the
    per-source keep-range pipeline. For the non-monotonic path we want
    the same pipeline applied per-run, so we hand it a Document whose
    ranges field is just this run's clips (in their playlist order,
    which is also source-monotonic by construction). All other fields
    pass through unchanged so segments-driven word-boundary snapping
    still sees every word.
    """
    from dataclasses import replace as _replace

    # Map each Clip back to its source_id by scanning the original
    # ranges. The clip's source_path identifies the source registry
    # entry; we pick the first matching range to read its source_id.
    src_path_to_id: dict[Path, str] = {}
    for sid, src in doc.sources.items():
        src_path_to_id.setdefault(src.path, sid)

    run_ranges: list[Range] = []
    for c in run_timeline.clips:
        sid = src_path_to_id.get(c.source_path)
        if sid is None:
            raise ValueError(
                f"clip references source_path {c.source_path!r} not in doc.sources"
            )
        run_ranges.append(
            Range(source_id=sid, start=c.source_start, end=c.source_end, reason=c.reason)
        )
    return _replace(doc, ranges=run_ranges)


def _ffmpeg_concat_demuxer(intermediates: list[Path], output_path: Path) -> None:
    """Glue ``intermediates`` into ``output_path`` via stream-copy.

    The concat demuxer is the right tool when every intermediate was
    produced from the same source via smartcut: codec parameters match
    by construction, so ``-c copy`` is safe and lossless. If the inputs
    ever differ in codec params (a multi-source future), this errors
    loudly; we'd need to switch to the concat *filter* and accept a
    re-encode for that path.
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
