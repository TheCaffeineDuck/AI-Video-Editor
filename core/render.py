"""Render a cut Document to a media file via smartcut.

Pipeline:

1. Empty-cuts shortcut: if ``doc.cuts`` is empty, ``shutil.copy2`` the
   source. Smartcut is never invoked.
2. Snap each cut's boundaries to the nearest word boundary (Phase 4f-1
   C). Cuts that don't overlap any word — pure-silence cuts between
   segments — pass through unchanged.
3. Pre-merge cuts using :class:`~core.editing.MergeAdjacentCuts` with
   ``threshold=merge_gap``. This absorbs cuts that are within
   ``merge_gap`` of each other AND collapses any tiny keep-range
   sandwiched between two close cuts.
4. Invert merged cuts to keep-ranges across ``[0, duration]``.
5. Pad each keep-range by ``pad_lead`` on the start side and
   ``pad_trail`` on the end side (Phase 4f-1 A; asymmetric pads enable
   different leading/trailing breath defaults), clamped to
   ``[0, duration]``.
6. Merge keep-ranges that overlap or touch after padding.
7. If the final keep-ranges list is empty (cuts covered the entire
   media), raise :class:`ValueError`. We don't write a zero-length file.
8. Convert keep-ranges to :class:`~fractions.Fraction`
   (``limit_denominator=1000``; millisecond precision matches Whisper
   word timestamp resolution) and hand to
   ``smartcut.cut_video.smart_cut`` with audio passthru.
9. If ``audio_fade_ms > 0`` and the output has internal joins
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
import shutil
import subprocess
import warnings
from collections.abc import Callable
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

from core.audio import get_ffmpeg_path
from core.document import CutMark, Document, Segment
from core.editing import MergeAdjacentCuts

_LOG = logging.getLogger(__name__)


def _to_fraction_seconds(value: float) -> Fraction:
    """Convert a float-seconds timestamp to a millisecond-precise Fraction.

    Higher precision than 1ms wastes bytes in smartcut's bookkeeping
    without buying anything (Whisper word timestamps are quantized to
    20–50 ms in practice). Lower precision drops sub-frame accuracy.
    """
    return Fraction(value).limit_denominator(1000)


def _invert_cuts_to_keep_ranges(
    cuts: list[CutMark], duration: float
) -> list[tuple[float, float]]:
    """Compute the complement of ``cuts`` over ``[0, duration]``.

    ``cuts`` must already be sorted by ``start`` and free of overlap
    (use :class:`~core.editing.MergeAdjacentCuts` first to guarantee that).
    Cuts beyond ``duration`` are clamped. Returns a list of
    ``(start, end)`` keep-ranges with ``end > start``.
    """
    keeps: list[tuple[float, float]] = []
    cursor = 0.0
    for c in cuts:
        cs = max(0.0, min(duration, c.start))
        ce = max(0.0, min(duration, c.end))
        if cs > cursor:
            keeps.append((cursor, cs))
        cursor = max(cursor, ce)
        if cursor >= duration:
            break
    if cursor < duration:
        keeps.append((cursor, duration))
    return keeps


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


def _snap_cuts_to_word_boundaries(
    cuts: list[CutMark], segments: list[Segment]
) -> list[CutMark]:
    """Snap each cut's start/end to the nearest word start/end boundary.

    A cut that doesn't overlap any word's ``[start, end]`` interval is
    left untouched (e.g. pure-silence cuts between segments). For overlapping
    cuts, ``cut.start`` snaps to the nearest word start and ``cut.end`` to
    the nearest word end. On ties, snap outward (smaller start, larger end)
    so we err on cutting more, never less.
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
        return list(cuts)

    out: list[CutMark] = []
    for c in cuts:
        overlaps = any(
            ws <= c.end and we >= c.start for (ws, we) in word_intervals
        )
        if not overlaps:
            out.append(c)
            continue
        snapped_start = _snap_to_word_start(c.start, word_starts)
        snapped_end = _snap_to_word_end(c.end, word_ends)
        if snapped_end <= snapped_start:
            # Pathological snap: leave the cut untouched rather than emit
            # a degenerate range that the inverter would silently drop.
            out.append(c)
            continue
        out.append(CutMark(start=snapped_start, end=snapped_end, reason=c.reason))
    return out


def _snap_to_word_start(value: float, word_starts: list[float]) -> float:
    """Snap a cut start to the nearest word start; tie → earlier (cut more)."""
    best = word_starts[0]
    best_dist = abs(best - value)
    for s in word_starts[1:]:
        d = abs(s - value)
        if d < best_dist or (d == best_dist and s < best):
            best = s
            best_dist = d
    return best


def _snap_to_word_end(value: float, word_ends: list[float]) -> float:
    """Snap a cut end to the nearest word end; tie → later (cut more)."""
    best = word_ends[0]
    best_dist = abs(best - value)
    for e in word_ends[1:]:
        d = abs(e - value)
        if d < best_dist or (d == best_dist and e > best):
            best = e
            best_dist = d
    return best


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

    Snaps cuts to word boundaries before inversion (Phase 4f-1 C) so the
    "Never cut inside a word" invariant holds even for low-level callers
    that bypass :class:`~core.editing.CutWordRange`.
    """
    if not doc.cuts:
        return [(0.0, doc.duration)]
    snapped = _snap_cuts_to_word_boundaries(doc.cuts, doc.segments)
    pre_merged = MergeAdjacentCuts(merge_gap).apply(
        replace(doc, cuts=sorted(snapped, key=lambda c: c.start))
    )
    keeps = _invert_cuts_to_keep_ranges(pre_merged.cuts, doc.duration)
    return _pad_and_merge_keep_ranges(keeps, pad_lead, pad_trail, doc.duration)


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
    """Render ``doc`` with its cuts applied to ``output_path``.

    ``pad_lead`` widens each kept range on its start side; ``pad_trail`` on
    its end side. Both default to 100ms. The asymmetric defaults are
    intentional: leading time before a kept range is "breath before the
    next word" (too much drags pacing), trailing time is "decay of the
    previous word" (too little clips consonants). Phase 4f-1 surfaces them
    independently so per-project tuning is possible.

    ``audio_fade_ms`` (default 30) controls a linear ``afade`` applied at
    every internal segment join, fade-out before the join and fade-in
    after. 30ms is below conscious "fade" perception but above the
    threshold needed to suppress click/pop discontinuities at sample
    boundaries. Values >50 are flagged with a warning ("audible as a
    dissolve, not a click suppressor"). 0 disables fades entirely.

    ``pad`` is deprecated as of Phase 4f-1: passing a non-``None`` value
    sets both ``pad_lead`` and ``pad_trail`` to it and emits a
    :class:`DeprecationWarning`.

    Empty cuts → byte-for-byte copy of the source. All cuts covering
    the full duration → ``ValueError``. Otherwise, hand the merged +
    padded keep-ranges to smartcut, which stream-copies most frames
    and re-encodes only at cut boundaries; then optionally apply audio
    fades via a second ffmpeg pass.
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

    if not doc.media_path.is_file():
        raise FileNotFoundError(
            f"Document.media_path does not exist: {doc.media_path}"
        )

    output_path = Path(output_path)

    if not doc.cuts:
        shutil.copy2(doc.media_path, output_path)
        if on_progress is not None:
            on_progress(1.0)
        return output_path

    final_keeps = _resolve_keep_ranges(
        doc, pad_lead=pad_lead, pad_trail=pad_trail, merge_gap=merge_gap
    )
    if not final_keeps:
        raise ValueError(
            "Cuts cover the entire media duration; nothing to render"
        )

    # Smartcut imports are deferred so importing core.render is cheap in
    # tests that exercise only the helpers.
    from smartcut.cut_video import (
        AudioExportInfo,
        AudioExportSettings,
        VideoExportMode,
        VideoExportQuality,
        VideoSettings,
        smart_cut,
    )
    from smartcut.media_container import MediaContainer

    container = MediaContainer(str(doc.media_path))
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
