"""Editable-feeling transcript view backed by a read-only QTextEdit.

Phase 5b rendered only KEPT words. Phase 5c renders **every** word and
visualises cut words with strikethrough — the Decision 2 model
(strikethrough is reversible; the source text is never destroyed).

Three orthogonal per-word formats compose:

1. **Strikethrough** — set when the word's time range falls outside
   every keep-range. Driven by the Document.
2. **Selection background** — pale-blue translucent fill, drawn live
   during a click-drag. Cleared on click-without-drag and on every
   successful Document mutation.
3. **Now-playing** — bold weight when the playhead is inside the
   word's [start, end). Recomputed per ``positionChanged``. Composes
   freely with strikethrough + selection because it's a font-weight
   change rather than another background.

Click → ``word_clicked(int)``. Drag → ``selection_changed(start, end)``
inclusive (or ``None`` after a clear). Cut request is signalled
upstream via ``cut_requested(start_word_idx, end_word_idx)`` — the
EditorPane decides whether that means cut or restore based on whether
those words are currently struck.

Widget choice (carry-over from 5b's report): read-only ``QTextEdit``
with per-word ``QTextCharFormat`` carrying ``WORD_INDEX_PROPERTY``.
``cursorForPosition`` resolves a click to a cursor; the cursor's
``charFormat().property(WORD_INDEX_PROPERTY)`` resolves the cursor to
a word index. Same machinery handles drag selection and playhead
lookup.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QMouseEvent,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import QTextEdit, QWidget

from core.document import Document, Range, Segment, Word

# Custom QTextCharFormat property id. Qt uses 0x100000+ as the user range.
# Value is the index into :attr:`TranscriptView.words`.
WORD_INDEX_PROPERTY = 0x100001

# Visual constants. Pale blue selection composes legibly over both
# normal and struck text; the now-playing bold reads cleanly without
# fighting the selection background.
_SELECTION_COLOR = QColor(59, 130, 246, 70)  # blue-500 @ 27% alpha
# Word-level "no playhead at this position" (gap between words / before first).
_NO_PLAYHEAD = -1


@dataclass(frozen=True)
class WordRef:
    """A rendered word + its source-segment coordinates + kept flag.

    ``kept`` is computed at render time against the Document's ranges.
    Cut words still appear in the transcript (Decision 2) — they just
    render with strikethrough. The flag is what the cut/restore code
    consults to decide which command to push.
    """

    seg_idx: int
    word_idx: int
    word: Word
    kept: bool = True


def _word_in_any_range(w: Word, ranges: Iterable[Range]) -> bool:
    """A word is "kept" if its time range strictly overlaps any keep-range.

    Strict inequality is load-bearing: a cut at time ``[1.0, 2.0]`` on
    a timeline that originally contained one full-duration range
    produces ``[Range(0, 1), Range(2, 3)]``. The word ``(1.0, 1.5)``
    sits entirely in the gap; the loose overlap ``end >= range.start``
    would mark it kept (because ``1.0 == 1.0``), which is wrong — the
    user just cut it.

    The strict semantic ``end > range.start and start < range.end``
    treats boundary contact ("touching, not overlapping") as not
    overlapping. This matches what every cut/restore test expects.
    """
    for r in ranges:
        if w.end > r.start and w.start < r.end:
            return True
    return False


def _segment_is_kept(seg: Segment, ranges: Iterable[Range]) -> bool:
    """Used for fallback rendering when a kept segment has no word timings."""
    for r in ranges:
        if seg.end > r.start and seg.start < r.end:
            return True
    return False


def collect_words(document: Document) -> list[WordRef]:
    """Walk every word in every segment in document order.

    Returns one :class:`WordRef` per word, with ``kept`` set per the
    Document's ranges. Phase 5c needs the cut words too (for the
    strikethrough render); filter on ``ref.kept`` if you only want
    kept words (e.g. for a derived plain-text export).
    """
    refs: list[WordRef] = []
    for seg_idx, seg in enumerate(document.segments):
        for word_idx, w in enumerate(seg.words):
            refs.append(
                WordRef(
                    seg_idx=seg_idx,
                    word_idx=word_idx,
                    word=w,
                    kept=_word_in_any_range(w, document.ranges),
                )
            )
    return refs


class TranscriptView(QTextEdit):
    """Read-only QTextEdit rendering every word; cuts via strikethrough.

    Signals:
        word_clicked(int): a click landed on the word at this index.
        selection_changed(object): emits ``(start_idx, end_idx)``
            tuple while the selection is live, or ``None`` when
            cleared. Tuple endpoints are inclusive word indices into
            :attr:`words`.
        cut_requested(int, int): user pressed the cut shortcut on
            ``[start_idx, end_idx]`` inclusive. EditorPane picks
            cut vs restore based on the words' current ``kept`` flags.
        seek_requested(int): user wants the player to jump to the
            given position in milliseconds.
    """

    word_clicked = Signal(int)
    selection_changed = Signal(object)
    cut_requested = Signal(int, int)
    seek_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setAcceptRichText(False)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        # Disable Qt's character-level text selection — we draw our own
        # word-grain selection. Without this, dragging shows a glyph-
        # level highlight under our background and competes for focus.
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        font = self.font()
        font.setPointSize(max(13, font.pointSize()))
        self.setFont(font)

        self._words: list[WordRef] = []
        self._word_starts: list[float] = []  # cached for binary search

        # Selection state (inclusive word indices).
        self._selection_anchor: int | None = None  # mouse-press word index
        self._selection_start: int | None = None
        self._selection_end: int | None = None
        self._dragged: bool = False  # press → release with movement

        # Now-playing index (or _NO_PLAYHEAD).
        self._playhead_word: int = _NO_PLAYHEAD

    # ----- accessors -----

    @property
    def words(self) -> list[WordRef]:
        """Rendered words in display order. Indexed by ``WORD_INDEX_PROPERTY``."""
        return self._words

    @property
    def selection(self) -> tuple[int, int] | None:
        """Current selection as ``(start, end)`` inclusive, or ``None``."""
        if self._selection_start is None or self._selection_end is None:
            return None
        lo, hi = sorted((self._selection_start, self._selection_end))
        return (lo, hi)

    @property
    def playhead_word(self) -> int:
        """Word index currently containing the playhead, or -1 for none."""
        return self._playhead_word

    # ----- render -----

    def set_document_model(self, document: Document) -> None:
        """Re-render the transcript from ``document``.

        Called on initial load and after every successful Document
        mutation. Selection and playhead state are reset because the
        word indices are no longer the same after a re-render.
        """
        self._words = []
        self._selection_anchor = None
        self._selection_start = None
        self._selection_end = None
        self._dragged = False
        self._playhead_word = _NO_PLAYHEAD
        self.clear()
        cursor = self.textCursor()
        cursor.beginEditBlock()
        try:
            for seg_idx, seg in enumerate(document.segments):
                if not seg.words:
                    if _segment_is_kept(seg, document.ranges):
                        self._insert_text_run(cursor, seg.text)
                        cursor.insertBlock()
                    continue
                emitted_in_segment = False
                for word_idx, word in enumerate(seg.words):
                    is_kept = _word_in_any_range(word, document.ranges)
                    ref = WordRef(
                        seg_idx=seg_idx,
                        word_idx=word_idx,
                        word=word,
                        kept=is_kept,
                    )
                    self._words.append(ref)
                    self._insert_word(
                        cursor,
                        word.text,
                        len(self._words) - 1,
                        struck=not is_kept,
                    )
                    emitted_in_segment = True
                if emitted_in_segment:
                    cursor.insertBlock()
        finally:
            cursor.endEditBlock()
        self._word_starts = [r.word.start for r in self._words]
        self.moveCursor(QTextCursor.MoveOperation.Start)

    # ----- selection -----

    def clear_selection(self) -> None:
        """Drop the live selection and re-paint affected words."""
        prev = self.selection
        self._selection_anchor = None
        self._selection_start = None
        self._selection_end = None
        self._dragged = False
        if prev is not None:
            self._repaint_word_range(prev[0], prev[1], selected=False)
            self.selection_changed.emit(None)

    def _set_selection(self, anchor: int, current: int) -> None:
        """Apply a press/drag selection: ``[min(a, c), max(a, c)]`` inclusive."""
        prev = self.selection
        if prev is not None and prev != tuple(sorted((anchor, current))):
            # Clear old highlights before drawing new ones — otherwise
            # a shrinking drag leaves stale background on the trailing
            # words.
            self._repaint_word_range(prev[0], prev[1], selected=False)
        lo, hi = sorted((anchor, current))
        self._selection_start = anchor
        self._selection_end = current
        self._repaint_word_range(lo, hi, selected=True)
        self.selection_changed.emit((lo, hi))

    # ----- mouse handling -----

    def _word_at_position(self, event_pos) -> int | None:
        cursor = self.cursorForPosition(event_pos)
        # cursorForPosition gives a cursor *between* characters; reading
        # the format at that cursor returns the format of the character
        # immediately *before* it. For clicks at the very start of the
        # text we want the format of the character *after*, so check both.
        for fmt_cursor in (cursor, _cursor_at_or_after(cursor)):
            fmt = fmt_cursor.charFormat()
            if fmt.hasProperty(WORD_INDEX_PROPERTY):
                return int(fmt.property(WORD_INDEX_PROPERTY))
        return None

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt API)
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        word_idx = self._word_at_position(event.position().toPoint())
        if word_idx is None:
            self.clear_selection()
            super().mousePressEvent(event)
            return
        self._dragged = False
        # Reset previous selection visually before starting a fresh one.
        prev = self.selection
        self._selection_anchor = word_idx
        self._selection_start = word_idx
        self._selection_end = word_idx
        if prev is not None:
            self._repaint_word_range(prev[0], prev[1], selected=False)
        # A bare press is itself a one-word "selection-in-progress" but
        # we don't fire selection_changed yet — the selection becomes
        # real on movement or release.
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt API)
        if self._selection_anchor is None:
            super().mouseMoveEvent(event)
            return
        word_idx = self._word_at_position(event.position().toPoint())
        if word_idx is None:
            super().mouseMoveEvent(event)
            return
        self._dragged = True
        if word_idx == self._selection_end:
            super().mouseMoveEvent(event)
            return
        self._set_selection(self._selection_anchor, word_idx)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt API)
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        if self._selection_anchor is None:
            super().mouseReleaseEvent(event)
            return
        anchor = self._selection_anchor
        self._selection_anchor = None
        if not self._dragged:
            # Click without drag → clear any prior selection and seek.
            self.clear_selection()
            self.word_clicked.emit(anchor)
            self.seek_requested.emit(int(self._words[anchor].word.start * 1000))
        # else: drag selection persists. mouseMove already emitted
        # selection_changed; nothing to do on release beyond resetting
        # the anchor.
        super().mouseReleaseEvent(event)

    def request_cut_for_selection(self) -> bool:
        """Emit ``cut_requested`` for the live selection. Returns False if none."""
        sel = self.selection
        if sel is None:
            return False
        self.cut_requested.emit(sel[0], sel[1])
        return True

    # ----- playhead -----

    def set_playhead_position(self, position_ms: int) -> None:
        """Update the now-playing highlight to follow ``position_ms``."""
        if not self._words:
            return
        position_s = position_ms / 1000.0
        # Largest index with start <= position_s.
        idx = bisect.bisect_right(self._word_starts, position_s) - 1
        new_word = _NO_PLAYHEAD
        if idx >= 0:
            candidate = self._words[idx].word
            if candidate.start <= position_s < candidate.end:
                new_word = idx
        if new_word == self._playhead_word:
            return
        prev = self._playhead_word
        self._playhead_word = new_word
        if prev != _NO_PLAYHEAD:
            self._repaint_word_range(prev, prev, playhead=False)
        if new_word != _NO_PLAYHEAD:
            self._repaint_word_range(new_word, new_word, playhead=True)
            self._maybe_scroll_to(new_word)

    def _maybe_scroll_to(self, word_idx: int) -> None:
        """Auto-scroll only if the word is outside the viewport.

        Aggressive auto-centering is nauseating during playback;
        ensureCursorVisible-style behaviour ("scroll just enough to
        bring the off-screen word back in") is what we want.
        """
        cursor = self._cursor_for_word(word_idx)
        if cursor is None:
            return
        rect = self.cursorRect(cursor)
        viewport = self.viewport().rect()
        if viewport.contains(rect.topLeft()) and viewport.contains(rect.bottomLeft()):
            return  # already visible
        prev = self.textCursor()
        self.setTextCursor(cursor)
        self.ensureCursorVisible()
        self.setTextCursor(prev)

    # ----- format helpers -----

    def _insert_word(
        self,
        cursor: QTextCursor,
        text: str,
        word_index: int,
        *,
        struck: bool,
    ) -> None:
        fmt = QTextCharFormat()
        fmt.setProperty(WORD_INDEX_PROPERTY, word_index)
        if struck:
            fmt.setFontStrikeOut(True)
        cursor.insertText(text, fmt)

    def _insert_text_run(self, cursor: QTextCursor, text: str) -> None:
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Weight.Normal)
        cursor.insertText(text, fmt)

    def _cursor_for_word(self, word_idx: int) -> QTextCursor | None:
        """Return a cursor selecting the character run for ``word_idx``.

        We rebuild the cursor by walking the document's blocks because
        the layout is `(word)(word)\\n(word)…` and Qt doesn't index
        custom-format runs directly.
        """
        if not (0 <= word_idx < len(self._words)):
            return None
        doc = self.document()
        block = doc.firstBlock()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                fragment = it.fragment()
                fmt = fragment.charFormat()
                if (
                    fmt.hasProperty(WORD_INDEX_PROPERTY)
                    and int(fmt.property(WORD_INDEX_PROPERTY)) == word_idx
                ):
                    cursor = QTextCursor(doc)
                    cursor.setPosition(fragment.position())
                    cursor.setPosition(
                        fragment.position() + fragment.length(),
                        QTextCursor.MoveMode.KeepAnchor,
                    )
                    return cursor
                it += 1
            block = block.next()
        return None

    def _repaint_word_range(
        self,
        start_idx: int,
        end_idx: int,
        *,
        selected: bool | None = None,
        playhead: bool | None = None,
    ) -> None:
        """Re-apply the visual format for words in ``[start_idx, end_idx]``.

        Call with ``selected=True/False`` to toggle the selection
        background, ``playhead=True/False`` to toggle the now-playing
        bold. ``None`` for either dimension means "leave unchanged" —
        in which case we recompute that dimension from current state.
        """
        for idx in range(start_idx, end_idx + 1):
            cursor = self._cursor_for_word(idx)
            if cursor is None:
                continue
            ref = self._words[idx]
            fmt = QTextCharFormat()
            # Always reapply word index + strikethrough so we don't lose
            # them through merge-format.
            fmt.setProperty(WORD_INDEX_PROPERTY, idx)
            fmt.setFontStrikeOut(not ref.kept)
            # Selection: explicit toggle wins; else infer from current.
            sel = self.selection
            is_selected = (
                selected
                if selected is not None
                else (sel is not None and sel[0] <= idx <= sel[1])
            )
            if is_selected:
                fmt.setBackground(_SELECTION_COLOR)
            # Playhead: explicit toggle wins; else infer.
            is_playhead = (
                playhead
                if playhead is not None
                else (idx == self._playhead_word)
            )
            if is_playhead:
                fmt.setFontWeight(QFont.Weight.Bold)
            cursor.setCharFormat(fmt)


def _cursor_at_or_after(cursor: QTextCursor) -> QTextCursor:
    """Return a copy of ``cursor`` advanced one position to the right.

    Used to handle cursorForPosition's "between characters" semantics:
    when the position falls right at the start of a word's character
    run, the format-of-prior is empty / non-word, so we step forward
    once and re-read.
    """
    out = QTextCursor(cursor)
    out.movePosition(QTextCursor.MoveOperation.Right)
    return out
