"""Pure helpers for editing a v2 Document's keep-range timeline.

A v2 Document stores ``ranges`` — a sorted list of :class:`~core.document.Range`
entries representing what to KEEP. Edit commands reduce to one of two
operations on this list:

- :func:`subtract_interval` — remove an interval (the ``AddCut`` semantic).
- :func:`union_interval` — restore an interval (the ``RestoreRange`` semantic).

Both are pure: inputs are not mutated; the returned list is a new list of
new Range objects. Both preserve the canonical invariants of the timeline:

- Sorted by ``start`` (ascending).
- No overlaps between distinct ranges.
- All ranges share the ``source_id`` argument — a precondition the caller
  must satisfy. The helpers raise :class:`ValueError` on violation rather
  than silently producing a malformed list.

The two-helper architecture is the single biggest leverage point in
Phase 4f-3: every ``EditCommand`` reduces to a one-liner against these,
and the v1→v2 migration uses ``subtract_interval`` to derive ranges from
the legacy cuts list.
"""

from __future__ import annotations

from collections.abc import Sequence

from core.document import Range


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
