"""Edit operations and undo/redo command stack for transcript Documents.

Documents are immutable from the operations' perspective: every
``apply`` / ``revert`` returns a *new* :class:`~core.document.Document`
via :func:`dataclasses.replace`, and the ``cuts`` / ``segments`` fields
are always rebuilt as fresh lists rather than mutated in place. The
Document itself is ``frozen=True`` (see :mod:`core.document`), which
prevents accidental ``doc.cuts = [...]`` reassignment but does *not*
prevent ``doc.cuts.append(...)`` — every command in this module is
responsible for never appending to an existing Document's lists.

Command stack semantics — fork discards redo:

When the user does A → B → C, undoes back to A, then performs a new
command D, the redo branch (B, C) is discarded permanently. This is the
standard behavior of every text editor and browser; users can't redo
their way back to an abandoned timeline. Implemented explicitly here so
nobody is tempted to "preserve" the redo branch on push.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from typing import Protocol, runtime_checkable

from core.document import CutMark, Document


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
# Cut-list commands
# ---------------------------------------------------------------------------


@dataclass
class AddCut:
    """Append a new :class:`CutMark` with the given range and reason."""

    start: float
    end: float
    reason: str = "manual"

    @property
    def description(self) -> str:
        return f"Add cut [{self.start:.2f}–{self.end:.2f}]"

    def _target(self) -> CutMark:
        return CutMark(start=self.start, end=self.end, reason=self.reason)

    def apply(self, doc: Document) -> Document:
        return replace(doc, cuts=[*doc.cuts, self._target()])

    def revert(self, doc: Document) -> Document:
        target = self._target()
        new_cuts = list(doc.cuts)
        for i in range(len(new_cuts) - 1, -1, -1):
            if new_cuts[i] == target:
                new_cuts.pop(i)
                return replace(doc, cuts=new_cuts)
        raise ValueError(f"AddCut.revert: no matching cut to remove ({target!r})")


@dataclass
class RemoveCut:
    """Remove the :class:`CutMark` at ``index`` and remember it for revert.

    ``revert`` re-inserts the captured cut at its original index, so a
    round-trip ``apply → revert`` produces an equal Document.
    """

    index: int
    _captured: CutMark | None = field(default=None, repr=False, compare=False)

    @property
    def description(self) -> str:
        return f"Remove cut #{self.index}"

    def apply(self, doc: Document) -> Document:
        if not (0 <= self.index < len(doc.cuts)):
            raise IndexError(
                f"cut index {self.index} out of range (have {len(doc.cuts)} cuts)"
            )
        self._captured = doc.cuts[self.index]
        new_cuts = [c for i, c in enumerate(doc.cuts) if i != self.index]
        return replace(doc, cuts=new_cuts)

    def revert(self, doc: Document) -> Document:
        if self._captured is None:
            raise RuntimeError("RemoveCut.revert called before apply")
        new_cuts = list(doc.cuts)
        new_cuts.insert(self.index, self._captured)
        return replace(doc, cuts=new_cuts)


@dataclass
class MergeAdjacentCuts:
    """Collapse cuts that are within ``threshold_seconds`` of each other.

    Mergeability rule (deterministic, order-independent):

    * Sort all cuts by ``start`` time first.
    * For consecutive cuts ``A`` (already in the merged list) and ``B``
      (next in sorted order), they merge iff ``B.start - A.end <= threshold``.
    * Overlapping cuts (``B.start < A.end``) always merge — the gap is
      negative, which is ``<= threshold`` for any non-negative threshold.
    * Merged cut: ``start = A.start``, ``end = max(A.end, B.end)``,
      ``reason = A.reason`` (the first cut's reason wins, deterministically).

    Captures the pre-apply ``cuts`` list so ``revert`` can restore it
    exactly. (We can't recompute the inverse — merging is destructive.)
    """

    threshold_seconds: float
    _captured: list[CutMark] | None = field(default=None, repr=False, compare=False)

    @property
    def description(self) -> str:
        return f"Merge cuts within {self.threshold_seconds:.2f}s"

    def apply(self, doc: Document) -> Document:
        self._captured = list(doc.cuts)
        sorted_cuts = sorted(doc.cuts, key=lambda c: c.start)
        merged: list[CutMark] = []
        for cut in sorted_cuts:
            if merged:
                last = merged[-1]
                gap = cut.start - last.end
                if gap <= self.threshold_seconds:
                    merged[-1] = CutMark(
                        start=last.start,
                        end=max(last.end, cut.end),
                        reason=last.reason,
                    )
                    continue
            merged.append(cut)
        return replace(doc, cuts=merged)

    def revert(self, doc: Document) -> Document:
        if self._captured is None:
            raise RuntimeError("MergeAdjacentCuts.revert called before apply")
        return replace(doc, cuts=list(self._captured))


@dataclass
class CutWordRange:
    """Add a :class:`CutMark` spanning a range of words within a single segment.

    Indexing rule — INCLUSIVE on both ends:
        Cutting words 3..5 of a segment removes words 3, 4, AND 5.
        ``CutMark.start = segments[seg_idx].words[word_start_idx].start``
        ``CutMark.end   = segments[seg_idx].words[word_end_idx].end``

    Cross-segment ranges raise :class:`ValueError` — a future
    ``CutWordRangeMultiSegment`` command will handle that case once the
    editor view requires it.
    """

    seg_idx: int
    word_start_idx: int
    word_end_idx: int
    reason: str = "manual"

    @property
    def description(self) -> str:
        return (
            f"Cut words {self.word_start_idx}–{self.word_end_idx} "
            f"of segment {self.seg_idx}"
        )

    def _resolve(self, doc: Document) -> CutMark:
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
        return CutMark(
            start=seg.words[self.word_start_idx].start,
            end=seg.words[self.word_end_idx].end,
            reason=self.reason,
        )

    def apply(self, doc: Document) -> Document:
        cut = self._resolve(doc)
        return replace(doc, cuts=[*doc.cuts, cut])

    def revert(self, doc: Document) -> Document:
        cut = self._resolve(doc)
        new_cuts = list(doc.cuts)
        for i in range(len(new_cuts) - 1, -1, -1):
            if new_cuts[i] == cut:
                new_cuts.pop(i)
                return replace(doc, cuts=new_cuts)
        raise ValueError(f"CutWordRange.revert: cut not found ({cut!r})")


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
