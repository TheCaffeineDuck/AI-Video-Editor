"""Edit operations and undo/redo command stack for v2 transcript Documents.

A v2 :class:`~core.document.Document` stores ``ranges`` — what to KEEP —
not ``cuts`` — what to remove. Phase 4f-3 rewrote every edit command
against this model. Each command now reduces to a single call to one of
the helpers in :mod:`core.timeline`:

- :class:`AddCut` (remove an interval) → :func:`~core.timeline.subtract_interval`
- :class:`RestoreRange` (re-insert a previously-removed interval) → :func:`~core.timeline.union_interval`
- :class:`CutWordRange` (word-bounded :class:`AddCut`) → :func:`~core.timeline.subtract_interval`

The ``apply``/``revert`` symmetry is preserved by capturing the prior
``ranges`` list on apply and restoring it on revert. This is simpler and
more obviously-correct than computing inverses of the timeline math.

``MergeAdjacentCuts`` from v1 is **gone**, not renamed. v1 used it
internally to canonicalize the cut list before render; v2's timeline is
canonicalized on every edit (the helpers always produce sorted,
non-overlapping output), and the render-time "absorb sub-merge_gap
gaps" behavior moved into :mod:`core.render` as a non-command helper.
The phase 4f-3 final report explains the choice.

Documents are immutable from the operations' perspective: every
``apply`` / ``revert`` returns a *new* Document via
:func:`dataclasses.replace`. The ``ranges`` / ``segments`` fields are
always rebuilt as fresh lists rather than mutated in place.

Command stack semantics — fork discards redo: when the user does
A → B → C, undoes back to A, then performs a new command D, the redo
branch (B, C) is discarded permanently. Standard text-editor /
browser behavior. Implemented explicitly so nobody is tempted to
"preserve" the redo branch on push.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from core.document import Document, Range
from core.edit_events import ClipAnchor, EditEvent, is_valid_reason
from core.timeline import subtract_interval, union_interval

_DEFAULT_SOURCE_ID = "src0"


@runtime_checkable
class EditCommand(Protocol):
    """The contract every edit operation satisfies.

    ``apply`` and ``revert`` are pure: they take a Document and return a
    new Document, never mutating the input. ``description`` is a short
    human-readable label used by undo/redo UI ("Undo: Add cut").
    """

    description: str

    def apply(self, doc: Document) -> Document: ...
    def revert(self, doc: Document) -> Document: ...


# ---------------------------------------------------------------------------
# Range-based commands
# ---------------------------------------------------------------------------


def _require_monotonic(doc: Document, command_name: str) -> None:
    """Raise NotImplementedError if the document's timeline is non-monotonic.

    The v3 timeline allows non-monotonic playlists (clips visiting the
    source out of order); 6a's editing commands explicitly do not
    handle that case yet. The check runs at apply time, not at command
    construction, because a Document might transition from monotonic
    to non-monotonic between construction and apply (e.g., a user
    loaded a different file). Single, explicit failure mode.
    """
    if not doc.main_timeline.is_source_monotonic():
        raise NotImplementedError(
            f"{command_name} is not supported on a non-monotonic timeline; "
            "rearrangement-aware editing is scheduled for a later phase."
        )


def _attach_reason_to_neighbor(
    ranges: list[Range], cut_start: float, cut_end: float, reason: str
) -> list[Range]:
    """Stamp ``reason`` onto the surviving range immediately preceding the
    cut (or following it for cuts at file start), mirroring the v1→v2
    migration's reason-attachment heuristic.

    Returns the same list when no neighbor matches (cut consumed the
    only range that could carry the reason). Empty / blank reason is a
    no-op — callers passing ``None`` should not invoke this helper.
    """
    if not reason:
        return ranges
    preceding_idx: int | None = None
    following_idx: int | None = None
    for i, r in enumerate(ranges):
        if abs(r.end - cut_start) < 1e-9:
            preceding_idx = i
            break
    if preceding_idx is None:
        for i, r in enumerate(ranges):
            if abs(r.start - cut_end) < 1e-9:
                following_idx = i
                break
    target_idx = preceding_idx if preceding_idx is not None else following_idx
    if target_idx is None:
        return ranges
    return [
        *ranges[:target_idx],
        replace(ranges[target_idx], reason=reason),
        *ranges[target_idx + 1 :],
    ]


@dataclass
class AddCut:
    """Remove the interval ``[start, end]`` from the timeline.

    Reduces to :func:`~core.timeline.subtract_interval` followed by an
    optional ``reason`` stamp on the surviving range immediately
    bordering the cut.

    Phase 6a additions:

    - ``reason: str | None``: free-form rationale. ``None`` (default)
      records no reason. A string is persisted onto the surviving
      neighbor range so it round-trips through ``to_json``/``from_json``.
      The previous default of ``"manual"`` is gone — explicit None is
      now the contract for "no reason given," distinguishing a deliberate
      blank from the implicit "manual" stamp v2 added everywhere.
    - Apply-time guard: raises :class:`NotImplementedError` if the
      document's ``main_timeline`` is non-monotonic.

    Phase 6b-1: every successful ``apply`` appends a
    :class:`~core.edit_events.EditEvent` of kind ``"cut"`` to
    ``Document.edit_log``. The event's ``reason`` mirrors what the
    surviving :class:`Range` carries (denormalization), so MCP /
    UI clients can read the rationale off either surface.
    """

    start: float
    end: float
    reason: str | None = None
    source_id: str = _DEFAULT_SOURCE_ID
    _captured: tuple[list[Range], tuple[EditEvent, ...]] | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def description(self) -> str:
        return f"Add cut [{self.start:.2f}–{self.end:.2f}]"

    def apply(self, doc: Document) -> Document:
        _require_monotonic(doc, "AddCut")
        self._captured = (list(doc.ranges), doc.edit_log)
        # Construct the EditEvent first; ``Range.reason`` is derived
        # from ``event.reason``. Single writer (the event) so the two
        # surfaces can never silently desync. See STATE.md §8 (6b-3
        # debt cleanup) for why this matters.
        event = EditEvent(
            kind="cut",
            timestamp=datetime.now(UTC),
            reason=self.reason or "",
            start=float(self.start),
            end=float(self.end),
            source_id=self.source_id,
        )
        new_ranges = subtract_interval(
            doc.ranges, (self.start, self.end), self.source_id
        )
        if event.reason:
            new_ranges = _attach_reason_to_neighbor(
                new_ranges, self.start, self.end, event.reason
            )
        return replace(
            doc,
            ranges=new_ranges,
            edit_log=(*doc.edit_log, event),
        )

    def revert(self, doc: Document) -> Document:
        if self._captured is None:
            raise RuntimeError("AddCut.revert called before apply")
        ranges, log = self._captured
        return replace(doc, ranges=list(ranges), edit_log=log)


@dataclass
class RestoreRange:
    """Re-insert the interval ``[start, end]`` into the timeline.

    The inverse of :class:`AddCut` for the case where a user un-cuts
    something. Reduces to :func:`~core.timeline.union_interval`. The
    captured pre-apply ``ranges`` drives ``revert``.

    Phase 6a: raises :class:`NotImplementedError` at apply time when
    the document's ``main_timeline`` is non-monotonic.

    Phase 6b-1: appends an :class:`~core.edit_events.EditEvent` of
    kind ``"restore"`` on every successful ``apply``. ``RestoreRange``
    has no on-Range reason field of its own, so the event's ``reason``
    always comes from the optional constructor argument (default ``""``).
    """

    start: float
    end: float
    source_id: str = _DEFAULT_SOURCE_ID
    reason: str = ""
    _captured: tuple[list[Range], tuple[EditEvent, ...]] | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def description(self) -> str:
        return f"Restore range [{self.start:.2f}–{self.end:.2f}]"

    def apply(self, doc: Document) -> Document:
        _require_monotonic(doc, "RestoreRange")
        self._captured = (list(doc.ranges), doc.edit_log)
        # Event constructed first per the 6b-3 reason-flow contract.
        # RestoreRange has no Range-side reason write today, but
        # building the event before the new ranges keeps the pattern
        # uniform across the three editing commands.
        event = EditEvent(
            kind="restore",
            timestamp=datetime.now(UTC),
            reason=self.reason,
            start=float(self.start),
            end=float(self.end),
            source_id=self.source_id,
        )
        new_ranges = union_interval(
            doc.ranges, (self.start, self.end), self.source_id
        )
        return replace(
            doc,
            ranges=new_ranges,
            edit_log=(*doc.edit_log, event),
        )

    def revert(self, doc: Document) -> Document:
        if self._captured is None:
            raise RuntimeError("RestoreRange.revert called before apply")
        ranges, log = self._captured
        return replace(doc, ranges=list(ranges), edit_log=log)


@dataclass
class CutWordRange:
    """Cut a range of words within a single segment.

    Indexing rule — INCLUSIVE on both ends:
        Cutting words 3..5 of a segment removes words 3, 4, AND 5.
        Removed interval start = ``segments[seg_idx].words[word_start_idx].start``
        Removed interval end   = ``segments[seg_idx].words[word_end_idx].end``

    Cross-segment ranges raise :class:`ValueError` — a future
    ``CutWordRangeMultiSegment`` command will handle that case once the
    editor view requires it.

    The "Never cut inside a word" rule is enforced two ways: this command
    constructs intervals on word boundaries by definition, and
    :func:`core.render._snap_cuts_to_word_boundaries` snaps low-level
    callers' boundaries at render-time so neither path can violate it.
    """

    seg_idx: int
    word_start_idx: int
    word_end_idx: int
    reason: str = "manual"
    source_id: str = _DEFAULT_SOURCE_ID
    _captured: tuple[list[Range], tuple[EditEvent, ...]] | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def description(self) -> str:
        return (
            f"Cut words {self.word_start_idx}–{self.word_end_idx} "
            f"of segment {self.seg_idx}"
        )

    def _resolve_interval(self, doc: Document) -> tuple[float, float]:
        if not (0 <= self.seg_idx < len(doc.segments)):
            raise ValueError(
                f"seg_idx {self.seg_idx} out of range "
                f"(have {len(doc.segments)} segments)"
            )
        seg = doc.segments[self.seg_idx]
        n_words = len(seg.words)
        if n_words == 0:
            raise ValueError(
                f"segment {self.seg_idx} has no word-level timestamps"
            )
        if not (0 <= self.word_start_idx <= self.word_end_idx < n_words):
            raise ValueError(
                f"word range [{self.word_start_idx}, {self.word_end_idx}] "
                f"out of segment {self.seg_idx} (which has {n_words} words)"
            )
        return (
            seg.words[self.word_start_idx].start,
            seg.words[self.word_end_idx].end,
        )

    def apply(self, doc: Document) -> Document:
        _require_monotonic(doc, "CutWordRange")
        interval = self._resolve_interval(doc)
        self._captured = (list(doc.ranges), doc.edit_log)
        # Event constructed first per the 6b-3 reason-flow contract.
        # CutWordRange does NOT denormalize ``reason`` onto the
        # surviving Range — that surface predates 6b-1's edit_log and
        # carrying the default ``"manual"`` everywhere would surprise
        # existing tests. The event still records the reason so the
        # log surface is complete.
        event = EditEvent(
            kind="cut",
            timestamp=datetime.now(UTC),
            reason=self.reason or "",
            start=float(interval[0]),
            end=float(interval[1]),
            source_id=self.source_id,
        )
        new_ranges = subtract_interval(doc.ranges, interval, self.source_id)
        return replace(
            doc,
            ranges=new_ranges,
            edit_log=(*doc.edit_log, event),
        )

    def revert(self, doc: Document) -> Document:
        if self._captured is None:
            raise RuntimeError("CutWordRange.revert called before apply")
        ranges, log = self._captured
        return replace(doc, ranges=list(ranges), edit_log=log)


# ---------------------------------------------------------------------------
# MoveClipSpan
# ---------------------------------------------------------------------------


class SpanResolutionError(ValueError):
    """Raised when a :class:`MoveClipSpan` can't resolve its span/target.

    Subclass of ``ValueError`` so generic catch-blocks (e.g.,
    ``apply_proposal``'s outcome loop) still see it. The message is
    operator-grade — it identifies which anchor failed and why.
    """


@dataclass
class MoveClipSpan:
    """Move a contiguous span of clips to a new playlist position.

    ``span`` is a tuple of :class:`~core.edit_events.ClipAnchor`
    instances naming the clips to move, in playlist order. The clips
    must be contiguous in the live timeline at apply time — anchor
    ``span[i]`` matches ``clips[k+i]`` for some starting index ``k``.
    ``target`` is an anchor naming the clip that the span should land
    *before*; ``None`` means "move to the end of the playlist."

    The same anchor pattern is used for the inverse: see
    :meth:`inverse`. Anchors are by source identity (path + start +
    end), so a move computed before any other moves still resolves
    cleanly after intervening moves rearrange the clip order.

    Construction validates ``reason`` against
    :func:`~core.edit_events.is_valid_reason`. Empty span and
    ``target == anchor in span`` (a self-cycle) are rejected.

    Apply-time errors are :class:`SpanResolutionError` (a ``ValueError``
    subclass): "span not found", "span not contiguous", "target not
    found", "target inside span". A successful apply appends one
    ``"move"`` :class:`~core.edit_events.EditEvent` to ``doc.edit_log``
    and returns a new Document whose ``ranges`` reflect the new order.
    """

    span: tuple[ClipAnchor, ...]
    target: ClipAnchor | None
    reason: str
    move_id: str | None = None
    """Optional stable identifier for this move within a :class:`Proposal`.

    6b-2 added this so an MCP client can selectively apply a subset of
    a proposal's moves by id (the ``apply_proposal`` tool's
    ``move_ids`` parameter). When ``None``, the proposal writer
    auto-assigns a sequential id (``m000``, ``m001``, …) at
    ``write`` / ``propose_moves`` time. Free-form strings are accepted
    so authors can write descriptive ids (``"swap-take-2-take-3"``).
    """
    _captured: tuple[list[Range], tuple[EditEvent, ...]] | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not self.span:
            raise ValueError("MoveClipSpan.span must not be empty")
        if not is_valid_reason(self.reason):
            raise ValueError(
                f"MoveClipSpan.reason {self.reason!r} is not a valid rationale; "
                "see core.edit_events.is_valid_reason for accepted patterns"
            )
        if self.target is not None and self.target in self.span:
            raise ValueError(
                f"MoveClipSpan.target {self.target!r} is also in span "
                "(self-cycle)"
            )

    @property
    def description(self) -> str:
        n = len(self.span)
        target_label = "end" if self.target is None else "target"
        return f"Move {n} clip{'s' if n != 1 else ''} to {target_label}"

    # -- helpers ----------------------------------------------------------

    def _find_span_indices(self, clips: list) -> list[int]:
        """Locate ``self.span`` as a contiguous run inside ``clips``.

        Returns the matched indices ``[i, i+1, …, i+len(span)-1]``.
        Raises :class:`SpanResolutionError` if the first anchor isn't
        found, or if the run isn't contiguous from there.
        """
        first = self.span[0]
        first_idx: int | None = None
        for k, c in enumerate(clips):
            if first.matches(c):
                first_idx = k
                break
        if first_idx is None:
            raise SpanResolutionError(
                f"span anchor {first!r} not found in timeline (len={len(clips)})"
            )
        for offset, anchor in enumerate(self.span):
            k = first_idx + offset
            if k >= len(clips):
                raise SpanResolutionError(
                    f"span runs past timeline end at anchor[{offset}]={anchor!r}"
                )
            if not anchor.matches(clips[k]):
                raise SpanResolutionError(
                    f"span not contiguous: anchor[{offset}]={anchor!r} does not "
                    f"match clip at index {k} ({clips[k]!r})"
                )
        return list(range(first_idx, first_idx + len(self.span)))

    def _find_target_index(self, clips: list, exclude: set[int]) -> int | None:
        """Return the index of the clip matching ``self.target``, or
        ``None`` when ``self.target is None`` (semantic: "to end").

        Raises :class:`SpanResolutionError` when ``self.target`` is set
        but no clip matches, or when the matched clip's index is in
        ``exclude`` (target inside span — the construction-time check
        catches anchor identity, but a different anchor could still
        land inside the span post-resolution).
        """
        if self.target is None:
            return None
        for k, c in enumerate(clips):
            if self.target.matches(c) and k not in exclude:
                return k
        # If the target's anchor matches a clip but only inside the
        # excluded span, that's a different error than "not found."
        for k, c in enumerate(clips):
            if self.target.matches(c) and k in exclude:
                raise SpanResolutionError(
                    f"target {self.target!r} resolves to a clip inside the span"
                )
        raise SpanResolutionError(
            f"target {self.target!r} not found in timeline (len={len(clips)})"
        )

    # -- command protocol -------------------------------------------------

    def apply(self, doc: Document) -> Document:
        clips = list(doc.main_timeline.clips)
        ranges = list(doc.ranges)
        span_indices = self._find_span_indices(clips)
        i, j = span_indices[0], span_indices[-1]
        target_idx = self._find_target_index(clips, exclude=set(span_indices))

        span_ranges = ranges[i : j + 1]
        remaining = ranges[:i] + ranges[j + 1 :]

        if target_idx is None:
            new_ranges = remaining + span_ranges
        else:
            # ``target_idx`` is an index into the full ``clips`` list;
            # after removing the span (which sits at indices [i..j]),
            # the same logical clip now sits at a smaller index when
            # it was originally to the *right* of the span.
            adjusted = target_idx if target_idx < i else target_idx - len(span_indices)
            new_ranges = remaining[:adjusted] + span_ranges + remaining[adjusted:]

        self._captured = (list(doc.ranges), doc.edit_log)
        event = EditEvent(
            kind="move",
            timestamp=datetime.now(UTC),
            reason=self.reason,
            span=self.span,
            target=self.target,
        )
        return replace(
            doc,
            ranges=new_ranges,
            edit_log=(*doc.edit_log, event),
        )

    def revert(self, doc: Document) -> Document:
        if self._captured is None:
            raise RuntimeError("MoveClipSpan.revert called before apply")
        ranges, log = self._captured
        return replace(doc, ranges=list(ranges), edit_log=log)

    def inverse(self, doc_before: Document) -> MoveClipSpan:
        """Compute a :class:`MoveClipSpan` that undoes ``self`` applied to
        ``doc_before``.

        The inverse moves the same span back to before the clip that
        originally came right after it; if the span sat at the end of
        the playlist, the inverse target is ``None``.

        Inverses round-trip: applying ``self`` to ``doc_before`` then
        applying ``self.inverse(doc_before)`` to the result yields a
        Document equal to ``doc_before`` (modulo the edit_log, which
        accumulates two events — an explicit ``revert`` is the path to
        a byte-identical round-trip on the log).
        """
        clips = list(doc_before.main_timeline.clips)
        span_indices = self._find_span_indices(clips)
        j = span_indices[-1]
        if j + 1 >= len(clips):
            inv_target: ClipAnchor | None = None
        else:
            inv_target = ClipAnchor.from_clip(clips[j + 1])
        return MoveClipSpan(
            span=self.span,
            target=inv_target,
            reason=f"undo: {self.reason}",
            move_id=None,  # inverse is a fresh move; original id stays with the original
        )


# ---------------------------------------------------------------------------
# CommandStack
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StackEntry:
    command: EditCommand
    before: Document
    after: Document


class CommandStack:
    """Bounded undo/redo stack of Document transitions.

    Callers do their own ``apply`` (so they get the new Document and can
    decide whether to push), then call :meth:`push` with the command and
    both endpoints. :meth:`undo` snaps the current Document back to
    ``before``; :meth:`redo` snaps it forward to ``after``.

    Pushing a new entry **always** clears the redo stack — the redo
    branch beyond the current undo position is gone for good. This is
    the standard fork behavior of text editors and browsers.

    The undo stack is bounded by ``max_depth``; the oldest entry is
    dropped when a new one is pushed past the limit.
    """

    def __init__(self, max_depth: int = 100) -> None:
        if max_depth < 1:
            raise ValueError(f"max_depth must be >= 1 (got {max_depth})")
        self._max_depth = max_depth
        self._undo: deque[_StackEntry] = deque()
        self._redo: deque[_StackEntry] = deque()

    @property
    def max_depth(self) -> int:
        return self._max_depth

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def undo_depth(self) -> int:
        return len(self._undo)

    @property
    def redo_depth(self) -> int:
        return len(self._redo)

    def push(self, command: EditCommand, before: Document, after: Document) -> None:
        """Record a transition. Discards the entire redo branch."""
        self._undo.append(_StackEntry(command=command, before=before, after=after))
        self._redo.clear()
        while len(self._undo) > self._max_depth:
            self._undo.popleft()

    def undo(self) -> Document | None:
        """Pop one entry off the undo stack.

        Returns the prior Document (the ``before`` of the most recent
        push), or ``None`` if the stack is empty. The popped entry moves
        onto the redo stack.
        """
        if not self._undo:
            return None
        entry = self._undo.pop()
        self._redo.append(entry)
        return entry.before

    def redo(self) -> Document | None:
        """Pop one entry off the redo stack.

        Returns the next Document (the ``after`` of the popped entry),
        or ``None`` if the redo stack is empty. The entry moves back
        onto the undo stack.
        """
        if not self._redo:
            return None
        entry = self._redo.pop()
        self._undo.append(entry)
        return entry.after

    def clear(self) -> None:
        """Drop both undo and redo stacks. Useful after loading a new project."""
        self._undo.clear()
        self._redo.clear()
