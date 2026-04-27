"""Read-only transcript view rendered from the v2 Document timeline.

Walks ``doc.ranges`` (the keep-list — Phase 4f-3 production rule
"timeline is the ordered keep-list") and pulls every :class:`Word` whose
``[start, end]`` falls inside one of those ranges. Words from
:class:`Segment` instances that don't intersect any range are skipped.

Widget choice: read-only :class:`QTextEdit` rather than
:class:`QTextBrowser`. Two reasons:

1. **Per-word click targets (5c) want pixel-position → word lookup, not
   anchor clicks.** Clicking a transcript word selects a *cut range*
   that the user can extend with shift-drag; QTextBrowser's
   ``anchorClicked`` is single-click-atom by design, which doesn't
   model "drag from word A to word B" naturally. ``QTextEdit`` exposes
   :meth:`cursorForPosition` and :meth:`document.documentLayout` —
   we'll resolve mouse events to a :class:`QTextCursor`, then read a
   custom property off the inserted character format to identify the
   word. The same machinery handles drag selection.
2. **Strikethrough (5c) is per-word, layered on the existing format.**
   ``QTextCharFormat.setFontStrikeOut(True)`` is the natural way to
   tag cut words; QTextBrowser would render the same way but the
   programmatic application is identical.

The 5b body inserts each word as plain text + a
:class:`QTextCharFormat` carrying a ``WORD_INDEX_PROPERTY`` integer
keyed on the position of the word in :attr:`words`. 5c's interactivity
will read that property to know which word a click landed on.

Range boundaries land as a single ``\\n`` between paragraphs; in 5c
this becomes a stronger visual delimiter when adjacent ranges represent
non-contiguous spans.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import (
    QFont,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import QTextEdit, QWidget

from core.document import Document, Segment, Word

# Custom QTextCharFormat property id. Qt uses 0x100000+ as the user range.
# Value is the index into :attr:`TranscriptView.words`.
WORD_INDEX_PROPERTY = 0x100001


@dataclass(frozen=True)
class WordRef:
    """A rendered word's source-segment index, word-in-segment index, and timing."""

    seg_idx: int
    word_idx: int
    word: Word


def _word_in_any_range(w: Word, ranges) -> bool:
    """A word survives if it overlaps any keep-range.

    Whisper's word boundaries don't always sit perfectly on the keep
    edge, so use overlap (inclusive on the kept side) rather than full
    containment. Words with zero or negative span are still anchored on
    ``start``.
    """
    for r in ranges:
        if w.end >= r.start and w.start <= r.end:
            return True
    return False


def collect_words(document: Document) -> list[WordRef]:
    """Return all words in document order whose timings fall in any range.

    Works against any v2 Document, single- or multi-source. Phase 5b is
    single-source by 5a's invariant; the helper is range-aware so 5c
    can drop in pause-driven cuts without rewriting the renderer.
    """
    refs: list[WordRef] = []
    for seg_idx, seg in enumerate(document.segments):
        for word_idx, w in enumerate(seg.words):
            if _word_in_any_range(w, document.ranges):
                refs.append(WordRef(seg_idx=seg_idx, word_idx=word_idx, word=w))
    return refs


def _segment_is_kept(seg: Segment, ranges) -> bool:
    """Used for fallback rendering when a kept segment has no word timings."""
    for r in ranges:
        if seg.end >= r.start and seg.start <= r.end:
            return True
    return False


class TranscriptView(QTextEdit):
    """Read-only QTextEdit showing every kept word from the Document."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setAcceptRichText(False)
        # Word wrap on; let the splitter dictate width.
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        font = self.font()
        font.setPointSize(max(13, font.pointSize()))
        self.setFont(font)
        self._words: list[WordRef] = []

    @property
    def words(self) -> list[WordRef]:
        """The rendered words in display order. Indexed by ``WORD_INDEX_PROPERTY``."""
        return self._words

    def set_document_model(self, document: Document) -> None:
        """Re-render the transcript from ``document``.

        Method named ``set_document_model`` to avoid clobbering
        :meth:`QTextEdit.setDocument` (which takes a :class:`QTextDocument`).
        """
        self._words = []
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
                    if not _word_in_any_range(word, document.ranges):
                        continue
                    ref = WordRef(seg_idx=seg_idx, word_idx=word_idx, word=word)
                    self._words.append(ref)
                    self._insert_word(cursor, word.text, len(self._words) - 1)
                    emitted_in_segment = True
                if emitted_in_segment:
                    cursor.insertBlock()
        finally:
            cursor.endEditBlock()
        # Reset to top so newly-loaded transcripts open at the start.
        self.moveCursor(QTextCursor.MoveOperation.Start)

    # ----- helpers -----

    def _insert_word(self, cursor: QTextCursor, text: str, word_index: int) -> None:
        fmt = QTextCharFormat()
        fmt.setProperty(WORD_INDEX_PROPERTY, word_index)
        cursor.insertText(text, fmt)

    def _insert_text_run(self, cursor: QTextCursor, text: str) -> None:
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Weight.Normal)
        cursor.insertText(text, fmt)
