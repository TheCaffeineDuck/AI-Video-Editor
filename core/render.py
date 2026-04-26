"""Render a cut Document to a media file via smartcut.

Pipeline:

1. Empty-cuts shortcut: if ``doc.cuts`` is empty, ``shutil.copy2`` the
   source. Smartcut is never invoked.
2. Pre-merge cuts using :class:`~core.editing.MergeAdjacentCuts` with
   ``threshold=merge_gap``. This absorbs cuts that are within
   ``merge_gap`` of each other AND collapses any tiny keep-range
   sandwiched between two close cuts (the "two adjacent cuts produce a
   sub-merge_gap keep-range" case).
3. Invert merged cuts to keep-ranges across ``[0, duration]``.
4. Pad each keep-range by ``pad`` on both sides, clamped to
   ``[0, duration]`` so we never hand smartcut a negative start.
5. Merge keep-ranges that overlap or touch after padding.
6. If the final keep-ranges list is empty (cuts covered the entire
   media), raise :class:`ValueError`. We don't write a zero-length file.
7. Convert keep-ranges to :class:`~fractions.Fraction` (limit_denominator=1000;
   millisecond precision matches Whisper word timestamp resolution) and
   hand to ``smartcut.cut_video.smart_cut`` with the same
   stream-copy-friendly settings the smartcut CLI uses by default.

Progress contract:
    ``on_progress: Callable[[float], None] | None`` fires monotonically,
    clamped to ``[0.0, 1.0]``. ``finalize()`` ensures the last call is
    1.0 on success even if smartcut's increments don't sum to its
    announced total.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

from core.document import CutMark, Document
from core.editing import MergeAdjacentCuts


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
    keeps: list[tuple[float, float]], pad: float, duration: float
) -> list[tuple[float, float]]:
    """Pad each keep-range by ``pad`` on each side, clamp to ``[0, duration]``,
    then merge any padded ranges that now overlap or touch.
    """
    if not keeps:
        return []
    if pad < 0:
        raise ValueError(f"pad must be >= 0 (got {pad})")
    padded: list[tuple[float, float]] = []
    for start, end in keeps:
        ps = max(0.0, start - pad)
        pe = min(duration, end + pad)
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
    doc: Document, *, pad: float, merge_gap: float
) -> list[tuple[float, float]]:
    """The full keep-range computation, factored out for direct testing."""
    if not doc.cuts:
        return [(0.0, doc.duration)]
    pre_merged = MergeAdjacentCuts(merge_gap).apply(
        replace(doc, cuts=sorted(doc.cuts, key=lambda c: c.start))
    )
    keeps = _invert_cuts_to_keep_ranges(pre_merged.cuts, doc.duration)
    return _pad_and_merge_keep_ranges(keeps, pad, doc.duration)


def render_cut(
    doc: Document,
    output_path: Path,
    on_progress: Callable[[float], None] | None = None,
    *,
    pad: float = 0.10,
    merge_gap: float = 0.30,
) -> Path:
    """Render ``doc`` with its cuts applied to ``output_path``.

    Empty cuts → byte-for-byte copy of the source. All cuts covering
    the full duration → ``ValueError``. Otherwise, hand the merged +
    padded keep-ranges to smartcut, which stream-copies most frames
    and re-encodes only at cut boundaries.
    """
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

    final_keeps = _resolve_keep_ranges(doc, pad=pad, merge_gap=merge_gap)
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

    adapter = _ProgressAdapter(on_progress)
    try:
        exc = smart_cut(
            container,
            fraction_keeps,
            str(output_path),
            audio_export_info=audio_export_info,
            video_settings=video_settings,
            progress=adapter,
            log_level="error",
        )
        if exc is not None:
            raise exc
    finally:
        adapter.finalize()

    return output_path
