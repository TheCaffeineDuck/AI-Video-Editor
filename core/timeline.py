"""Timeline primitives and pure helpers for editing the keep-range timeline.

Phase 6a (schema v3) introduces :class:`Clip` and :class:`Timeline` as
the canonical "what plays in what order" abstraction. A :class:`Clip`
references a single source by path with a half-open ``[start, end)``
interval; a :class:`Timeline` is an ordered tuple of clips representing
playlist order (NOT necessarily source order). The earlier
:class:`~core.document.Range` type stays for in-memory storage on
:class:`~core.document.Document.ranges` (kept frozen for backward
compat) — :class:`Timeline` is exposed as a derived view via
:attr:`~core.document.Document.main_timeline`.

The two range-arithmetic helpers from v2 stay: edit commands reduce
to either :func:`subtract_interval` (the ``AddCut`` semantic) or
:func:`union_interval` (the ``RestoreRange`` semantic). Both operate
on :class:`~core.document.Range` lists and preserve the canonical v2
invariants — sorted, non-overlapping, single-source. The v3 editing
commands continue to use these helpers; they raise
:class:`NotImplementedError` upstream when the timeline is
non-monotonic, so the helpers never have to deal with such input.

Run-splitting (:func:`split_into_monotonic_runs`) is the new piece of
6a's renderer: it walks a non-monotonic playlist and partitions it
into the smallest number of source-monotonic runs, each of which is
one ``smart_cut`` invocation. The smartcut spike (YELLOW) established
that smartcut requires sorted non-overlapping ``positive_segments``;
splitting into runs and concatenating the per-run outputs is the
workaround that preserves codec parity (no re-encode).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from core.document import Range

# ---------------------------------------------------------------------------
# v3 timeline types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Clip:
    """A half-open ``[source_start, source_end)`` slice of a single source.

    The fields match the v3 schema's on-disk shape (``source_path`` /
    ``source_start`` / ``source_end``). An optional ``reason`` field
    carries a free-form rationale string when one is set by an edit
    command — empty string is the default and the absence-of-reason
    sentinel.

    Construction-time validation is intentionally minimal: ``end > start``
    (an empty clip is meaningless on the playlist). Word-boundary
    snapping is the renderer's responsibility, not Clip's, because
    Clip alone has no view of word timestamps.
    """

    source_path: Path
    source_start: float
    source_end: float
    reason: str = ""

    def __post_init__(self) -> None:
        if self.source_end <= self.source_start:
            raise ValueError(
                f"Clip source_end ({self.source_end}) must be > "
                f"source_start ({self.source_start})"
            )

    @property
    def duration_s(self) -> float:
        return float(self.source_end - self.source_start)


@dataclass(frozen=True)
class Timeline:
    """Ordered playlist of :class:`Clip` instances.

    The clip order is *playlist* order — what the renderer plays from
    first to last. For a v2-derived timeline (single source, sorted by
    source-time, non-overlapping) this happens to match source order;
    for a hand-built non-monotonic v3 fixture it does not.

    The class is frozen and the ``clips`` field is a :class:`tuple`
    so callers cannot accidentally mutate the playlist. Construct a
    new Timeline to express a change.
    """

    clips: tuple[Clip, ...] = ()

    @property
    def total_duration_s(self) -> float:
        return sum(c.duration_s for c in self.clips)

    @property
    def source_paths(self) -> tuple[Path, ...]:
        """Distinct source paths referenced by clips, first-seen order."""
        seen: list[Path] = []
        for c in self.clips:
            if c.source_path not in seen:
                seen.append(c.source_path)
        return tuple(seen)

    def is_source_monotonic(self) -> bool:
        """True iff the playlist visits a single source in source-time order
        with no overlaps.

        Empty timelines and one-clip timelines are vacuously monotonic.
        Multi-source timelines are non-monotonic by definition (there
        is no single source to be ordered against).
        """
        if len(self.clips) <= 1:
            return True
        first_path = self.clips[0].source_path
        prev_end = self.clips[0].source_end
        for c in self.clips[1:]:
            if c.source_path != first_path:
                return False
            if c.source_start < prev_end:
                # Strictly less-than: a clip starting exactly where the
                # previous ended is a touching join, which v2 timelines
                # produce after subtract/union and is monotonic.
                return False
            prev_end = c.source_end
        return True


def split_into_monotonic_runs(timeline: Timeline) -> list[Timeline]:
    """Partition a playlist into the minimum number of monotonic runs.

    A "run" is a maximal contiguous slice of the playlist whose clips
    all share one ``source_path`` and are sorted ascending by
    ``source_start`` with no overlap. The returned list preserves
    playlist order; concatenating the runs back yields the input
    timeline.

    Empty input → empty list. Already-monotonic input → ``[timeline]``.
    The non-monotonic renderer feeds each run to ``smart_cut`` as one
    sorted call; the per-run outputs concat with ffmpeg's
    stream-copy concat demuxer.
    """
    if not timeline.clips:
        return []
    runs: list[list[Clip]] = []
    current: list[Clip] = []
    for clip in timeline.clips:
        if not current:
            current.append(clip)
            continue
        last = current[-1]
        same_source = clip.source_path == last.source_path
        in_order = clip.source_start >= last.source_end
        if same_source and in_order:
            current.append(clip)
        else:
            runs.append(current)
            current = [clip]
    if current:
        runs.append(current)
    return [Timeline(clips=tuple(run)) for run in runs]


# ---------------------------------------------------------------------------
# v2-style range helpers (preserved verbatim; the v3 in-memory truth is
# still ``Document.ranges`` and the editing commands operate on it).
# ---------------------------------------------------------------------------


def _check_single_source(ranges: Sequence[Range], source_id: str) -> None:
    for r in ranges:
        if r.source_id != source_id:
            raise ValueError(
                f"range {r!r} has source_id != {source_id!r}; this helper "
                "expects a single-source input — filter or partition first."
            )


def _check_interval(interval: tuple[float, float]) -> None:
    a, b = interval
    if b < a:
        raise ValueError(f"interval end ({b}) < start ({a})")


def subtract_interval(
    ranges: Sequence[Range],
    interval: tuple[float, float],
    source_id: str,
) -> list[Range]:
    """Subtract ``interval`` from each overlapping range in ``ranges``.

    For each existing range:

    - No overlap → kept verbatim.
    - Interval fully contains the range → range dropped.
    - Interval truncates one end → range shrinks on that side.
    - Interval is strictly inside the range → range splits in two.

    Range ``reason`` carries onto the surviving piece(s); when a split
    produces two halves both inherit the parent's reason.

    A degenerate ``interval`` where ``start == end`` is a no-op (returns
    a copy of the input).

    Raises :class:`ValueError` on inverted intervals or mismatched
    ``source_id``.
    """
    _check_interval(interval)
    _check_single_source(ranges, source_id)
    a, b = interval
    if b == a:
        return list(ranges)

    out: list[Range] = []
    for r in ranges:
        if b <= r.start or a >= r.end:
            out.append(r)
            continue
        if a <= r.start and b >= r.end:
            continue  # range fully consumed
        if a > r.start and b >= r.end:
            out.append(Range(source_id=r.source_id, start=r.start, end=a, reason=r.reason))
            continue
        if a <= r.start and b < r.end:
            out.append(Range(source_id=r.source_id, start=b, end=r.end, reason=r.reason))
            continue
        # Strict split
        out.append(Range(source_id=r.source_id, start=r.start, end=a, reason=r.reason))
        out.append(Range(source_id=r.source_id, start=b, end=r.end, reason=r.reason))
    return out


def union_interval(
    ranges: Sequence[Range],
    interval: tuple[float, float],
    source_id: str,
) -> list[Range]:
    """Insert ``interval`` into ``ranges``, merging with any overlapping or
    touching existing ranges.

    Touching = adjacent with no gap (``range.end == interval.start`` or
    vice versa). A range that is already wholly covered by the interval
    contributes nothing extra. If the interval is wholly covered by an
    existing range, the input is returned unchanged (modulo the new-list
    contract).

    The merged range's ``reason`` is taken from the leftmost range that
    participated in the merge; if there were no overlapping ranges, the
    new range's reason is empty. Phase 4f-3's commands and migration code
    don't rely on a specific tie-break here — the editor view in Phase 5
    can re-author reasons as needed.

    A degenerate ``interval`` where ``start == end`` is a no-op.

    Raises :class:`ValueError` on inverted intervals or mismatched
    ``source_id``.
    """
    _check_interval(interval)
    _check_single_source(ranges, source_id)
    a, b = interval
    if b == a:
        return list(ranges)

    before: list[Range] = []
    overlapping: list[Range] = []
    after: list[Range] = []
    for r in ranges:
        if r.end < a:
            before.append(r)
        elif r.start > b:
            after.append(r)
        else:
            overlapping.append(r)

    if overlapping:
        merged_start = min(a, *(r.start for r in overlapping))
        merged_end = max(b, *(r.end for r in overlapping))
        leftmost = min(overlapping, key=lambda r: r.start)
        merged_reason = leftmost.reason
    else:
        merged_start, merged_end = a, b
        merged_reason = ""

    merged = Range(
        source_id=source_id,
        start=merged_start,
        end=merged_end,
        reason=merged_reason,
    )
    return [*before, merged, *after]
