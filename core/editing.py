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
from typing import Protocol, runtime_checkable

from core.document import Document, Range
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


@dataclass
class AddCut:
    """Remove the interval ``[start, end]`` from the timeline.

    Reduces to :func:`~core.timeline.subtract_interval`. The legacy v1
    name is preserved so callers (and undo-history descriptions) read
    the same; the implementation now operates on keep-ranges instead of
    appending a CutMark.

    ``revert`` restores the pre-apply ``ranges`` list verbatim (captured
    on apply) — simpler and obviously-correct than inverting the
    subtraction in the multi-range / split case.
    """

    start: float
    end: float
    reason: str = "manual"
    source_id: str = _DEFAULT_SOURCE_ID
    _captured: list[Range] | None = field(default=None, repr=False, compare=False)

    @property
    def description(self) -> str:
        return f"Add cut [{self.start:.2f}–{self.end:.2f}]"

    def apply(self, doc: Document) -> Document:
        self._captured = list(doc.ranges)
        new_ranges = subtract_interval(
            doc.ranges, (self.start, self.end), self.source_id
        )
        return replace(doc, ranges=new_ranges)

    def revert(self, doc: Document) -> Document:
        if self._captured is None:
            raise RuntimeError("AddCut.revert called before apply")
        return replace(doc, ranges=list(self._captured))


@dataclass
class RestoreRange:
    """Re-insert the interval ``[start, end]`` into the timeline.

    The inverse of :class:`AddCut` for the case where a user un-cuts
    something. Reduces to :func:`~core.timeline.union_interval`. The
    captured pre-apply ``ranges`` drives ``revert``.

    Replaces v1's ``RemoveCut(index=…)``. v1 indexed into the cut list;
    v2 has no cut list, so the API is now interval-based — same shape as
    :class:`AddCut`, opposite operation.
    """

    start: float
    end: float
    source_id: str = _DEFAULT_SOURCE_ID
    _captured: list[Range] | None = field(default=None, repr=False, compare=False)

    @property
    def description(self) -> str:
        return f"Restore range [{self.start:.2f}–{self.end:.2f}]"

    def apply(self, doc: Document) -> Document:
        self._captured = list(doc.ranges)
        new_ranges = union_interval(
            doc.ranges, (self.start, self.end), self.source_id
        )
        return replace(doc, ranges=new_ranges)

    def revert(self, doc: Document) -> Document:
        if self._captured is None:
            raise RuntimeError("RestoreRange.revert called before apply")
        return replace(doc, ranges=list(self._captured))


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
    _captured: list[Range] | None = field(default=None, repr=False, compare=False)

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
        interval = self._resolve_interval(doc)
        self._captured = list(doc.ranges)
        new_ranges = subtract_interval(doc.ranges, interval, self.source_id)
        return replace(doc, ranges=new_ranges)

    def revert(self, doc: Document) -> Document:
        if self._captured is None:
            raise RuntimeError("CutWordRange.revert called before apply")
        return replace(doc, ranges=list(self._captured))


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
