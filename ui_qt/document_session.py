"""Editable Document state for the Qt editor pane.

Wraps the immutable :class:`core.document.Document` plus the existing
:class:`core.editing.CommandStack` plus dirty-state tracking, behind a
QObject-based interface so the editor pane can react to changes via Qt
signals.

Why a separate class: 5b's EditorPane already does layout, splitter,
video-source wiring, and the layout toggle. 5c adds command application,
undo/redo, and dirty tracking. Moving those into a focused helper keeps
EditorPane readable and lets tests exercise edit semantics without
spinning up the whole pane.

Dirty tracking — id-based saved-document pointer:
``mark_saved`` records ``id(self._document)``. ``is_dirty`` returns
``True`` iff the current document object is not the one we recorded.
This handles every stack-shape correctly:

- Edit → ``id`` changes → dirty.
- Save → record current id → not dirty.
- Edit → undo → ``before`` is the same Python object as the saved
  document → not dirty (undo-back-to-pristine clears the marker).
- Save A → undo to B → edit to C (pushes a new entry, clears redo so
  A is unreachable) → ``id(C) != id(A)`` → stays dirty as it should.

The trick works because ``CommandStack.undo`` returns the exact
``before`` Document instance we pushed (not a copy), and
:func:`dataclasses.replace` always returns a fresh Document so
post-edit ``id`` is always new.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from core.document import Document
from core.editing import CommandStack, EditCommand


class DocumentSession(QObject):
    """Owns the editable Document, the CommandStack, and dirty state.

    Signals:
        document_changed(Document): the active document was replaced.
            Fires after every successful apply, undo, or redo.
        dirty_changed(bool): the dirty flag flipped. Fires on edits,
            on undo/redo, and on ``mark_saved``. Quieter than
            ``document_changed`` — only fires on actual transitions.
    """

    document_changed = Signal(Document)
    dirty_changed = Signal(bool)

    def __init__(
        self,
        document: Document,
        *,
        max_undo_depth: int = 100,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._document = document
        self._stack = CommandStack(max_depth=max_undo_depth)
        self._saved_doc_id: int = id(document)
        self._was_dirty: bool = False

    # ----- accessors -----

    @property
    def document(self) -> Document:
        return self._document

    @property
    def stack(self) -> CommandStack:
        return self._stack

    @property
    def can_undo(self) -> bool:
        return self._stack.can_undo

    @property
    def can_redo(self) -> bool:
        return self._stack.can_redo

    @property
    def is_dirty(self) -> bool:
        return id(self._document) != self._saved_doc_id

    # ----- mutations -----

    def apply(self, command: EditCommand) -> bool:
        """Apply ``command`` to the current Document. Push on real change.

        A "real change" is a non-equal ranges list — applying a cut over
        an interval that's already cut produces an equal Document and
        we drop it on the floor (the spec calls this out for already-cut
        words). Returns ``True`` when something actually changed.
        """
        before = self._document
        after = command.apply(before)
        if after.ranges == before.ranges:
            return False
        self._stack.push(command, before, after)
        self._set_document(after)
        return True

    def undo(self) -> bool:
        """Undo the most recent change. Returns ``True`` if anything happened."""
        prev = self._stack.undo()
        if prev is None:
            return False
        self._set_document(prev)
        return True

    def redo(self) -> bool:
        """Redo the most recently undone change. Returns ``True`` on success."""
        nxt = self._stack.redo()
        if nxt is None:
            return False
        self._set_document(nxt)
        return True

    def mark_saved(self) -> None:
        """Record the current document as the saved checkpoint."""
        self._saved_doc_id = id(self._document)
        self._refresh_dirty()

    # ----- internal -----

    def _set_document(self, doc: Document) -> None:
        self._document = doc
        self.document_changed.emit(doc)
        self._refresh_dirty()

    def _refresh_dirty(self) -> None:
        d = self.is_dirty
        if d != self._was_dirty:
            self._was_dirty = d
            self.dirty_changed.emit(d)
