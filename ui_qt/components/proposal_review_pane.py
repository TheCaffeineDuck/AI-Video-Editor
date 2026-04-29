"""GUI pane for human-in-the-loop proposal review (Phase 6b-3).

The :class:`ProposalReviewPane` is the human side of the iterative
editing loop. It shows the proposals an LLM (or another author) has
written for the active document, lets the reviewer accept/reject each
move with rationale, and applies the result via Path B
(:func:`core.proposal.apply_proposal_with_human_decisions`). The
on-disk apply-result file mirrors Path A's schema, so the LLM can read
the human's verdict back via the existing MCP ``read_apply_result``
tool on its next turn.

The pane is a regular :class:`QWidget`; the host (typically a
:class:`QDockWidget` owned by :class:`ui_qt.app.MainWindow`) is
responsible for placement, visibility, and connecting the
``apply_completed`` / ``apply_failed`` signals to "reload the editor's
document" after a successful apply.

State machine, per spec:

- **Empty** — no document loaded, or document has no proposals. The
  pane shows a short placeholder message; controls are hidden.
- **Loaded** — proposals exist, the latest is selected, every move
  starts pending, Apply is disabled.
- **Reviewing** — at least one move has been accepted or rejected.
  Apply enables only when *every* move has a decision.
- **Applying** — Apply was clicked; controls are temporarily disabled
  while the host runs Path B and writes the apply-result.
- **Applied** — host calls :meth:`mark_applied` to clear state, after
  which the host typically dismisses the pane.

Decision shape: a per-move ``Decision(accept: bool,
rejection_reason: str | None)`` (see :class:`core.proposal.Decision`).
The pane builds the dict ``{move_id: Decision}`` it needs from the
in-widget state and hands it to the host on Apply via the
``apply_requested`` signal.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.document import Document
from core.edit_events import is_valid_reason
from core.editing import MoveClipSpan
from core.proposal import (
    Decision,
    Proposal,
    document_state_hash,
    latest_apply_result_id_for,
    list_apply_results_for_document,
    list_proposals,
)
from ui_qt.style import ACCENT, DANGER, MUTED, SUCCESS

# Width target for the dock — see spec stop-and-report. Real content
# (transcript snippets, reason text) wraps; this is a hint.
DOCK_TARGET_WIDTH = 360

# Per spec: rejection reasons must be at least 8 chars. Mirrors
# core.edit_events._MIN_FREEFORM_LEN.
MIN_REJECTION_REASON_CHARS = 8

# How many words to surface on each end of a clip's transcript snippet.
# Seven words on each end keeps the snippet short enough to fit in the
# 360px dock without truncation in most cases; prose with very long
# words may still wrap, which is fine.
SNIPPET_WORDS_PER_END = 6


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def clip_word_texts(doc: Document, source_start: float, source_end: float) -> list[str]:
    """Return the words in ``doc`` whose timing intersects ``[start, end)``.

    Mirrors the per-word filter inside
    :meth:`ui_qt.components.transcript_view.TranscriptView._render_playlist_order`.
    Pulled out so the proposal review pane can render a clip snippet
    without depending on the QTextEdit machinery — both paths share the
    same monotonic-source convention (a word "belongs to" a clip iff
    its time interval overlaps the clip's source-time interval).

    Words on segments without per-word timestamps are skipped (the
    snippet still renders; it just shows whatever words *are* timed).
    """
    out: list[str] = []
    for seg in doc.segments:
        if seg.end <= source_start or seg.start >= source_end:
            continue
        if not seg.words:
            continue
        for word in seg.words:
            if word.end <= source_start or word.start >= source_end:
                continue
            out.append(word.text.strip())
    return out


def clip_snippet(doc: Document, source_start: float, source_end: float) -> str:
    """Return a "first 6 / last 6 words" preview for a clip, or fallback.

    Format: ``"foo bar baz qux quux corge … xyz wibble wobble wubble fred plugh"``
    when the clip has more than 12 words; the full word list (joined
    with single spaces) when 12 or fewer. Returns a placeholder when
    the clip has no timed words at all.
    """
    words = clip_word_texts(doc, source_start, source_end)
    if not words:
        return "(no transcript)"
    if len(words) <= SNIPPET_WORDS_PER_END * 2:
        return " ".join(words)
    head = " ".join(words[:SNIPPET_WORDS_PER_END])
    tail = " ".join(words[-SNIPPET_WORDS_PER_END:])
    return f"{head} … {tail}"


@dataclass
class _MoveContext:
    """Per-move bookkeeping used to render an outline group.

    Pulled into a dataclass so the rendering helpers don't pass six
    positional args around. Constructed by :func:`_resolve_move_context`
    against the live document.
    """

    move: MoveClipSpan
    move_id: str
    span_indices: list[int]      # positional indices in doc.main_timeline.clips
    target_index: int | None     # None when target is "to end" or unresolvable
    span_resolved: bool          # False when the span anchors don't match the doc
    target_resolved: bool        # False when target anchor is set but not found


def _resolve_move_context(move: MoveClipSpan, move_id: str, doc: Document) -> _MoveContext:
    """Best-effort resolution of a move against ``doc`` for rendering.

    The renderer must not raise on an unresolvable move — STALE-style
    failures should still produce a usable outline (the apply path will
    surface the failure as ``rejected_system`` later). So we resolve
    each anchor independently and mark what we found.
    """
    clips = list(doc.main_timeline.clips)
    span_indices: list[int] = []
    span_resolved = True
    for anchor in move.span:
        match = next((k for k, c in enumerate(clips) if anchor.matches(c)), None)
        if match is None:
            span_resolved = False
            break
        span_indices.append(match)
    # Contiguity check: only count as resolved if span indices form a run.
    if span_resolved and span_indices:
        for offset in range(1, len(span_indices)):
            if span_indices[offset] != span_indices[offset - 1] + 1:
                span_resolved = False
                break

    if move.target is None:
        target_index = None
        target_resolved = True
    else:
        target_index = next(
            (k for k, c in enumerate(clips) if move.target.matches(c)),
            None,
        )
        target_resolved = target_index is not None

    return _MoveContext(
        move=move,
        move_id=move_id,
        span_indices=span_indices if span_resolved else [],
        target_index=target_index,
        span_resolved=span_resolved,
        target_resolved=target_resolved,
    )


def _format_time_span(start: float, end: float) -> str:
    return f"{start:.1f}s — {end:.1f}s"


# ---------------------------------------------------------------------------
# MoveCard — one per proposal move
# ---------------------------------------------------------------------------


class _MoveCard(QFrame):
    """Outline group for a single move: header, clip rows, decision controls.

    Owns its own decision state (pending / accepted / rejected). Emits
    ``decision_changed`` when the state shifts so the parent can refresh
    the Apply button and summary count.

    Visual conventions:

    - Move boundary is a thin border around the whole card so the
      reviewer can see "these clips form one move."
    - Accept (✓ green) / Reject (✗ red) buttons toggle the card's mode.
    - When Reject is active, an inline QLineEdit appears for the reason;
      the Submit button enables only when ``is_valid_reason`` returns
      True and the typed length is at least
      :data:`MIN_REJECTION_REASON_CHARS`.
    """

    decision_changed = Signal()  # emitted whenever the committed decision changes

    def __init__(self, ctx: _MoveContext, doc: Document, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._doc = doc
        self._decision: Decision | None = None  # pending until set
        self._reject_active: bool = False  # True when reject was clicked but not yet committed
        self._build_ui()
        self._refresh_button_state()

    # ----- public surface --------------------------------------------------

    @property
    def move_id(self) -> str:
        return self._ctx.move_id

    @property
    def decision(self) -> Decision | None:
        return self._decision

    def reset_decision(self) -> None:
        """Clear committed decision and any in-progress reject input."""
        self._decision = None
        self._reject_active = False
        self._reason_input.clear()
        self._reason_input.setVisible(False)
        self._reason_submit.setVisible(False)
        self._refresh_button_state()
        self.decision_changed.emit()

    # ----- build -----------------------------------------------------------

    def _build_ui(self) -> None:
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFrameShadow(QFrame.Shadow.Plain)
        self.setLineWidth(1)
        # Move boundary delineation: a thin border on the card. We use
        # ``setObjectName`` + an objectName-keyed selector because
        # underscore-prefixed class names confuse Qt's stylesheet
        # parser (it warns "Could not parse stylesheet").
        self.setObjectName("ProposalMoveCard")
        self.setStyleSheet(
            "QFrame#ProposalMoveCard { "
            f"border: 1px solid {MUTED}; border-radius: 6px; "
            "padding: 4px; margin: 4px 0; }"
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)

        # Header — move target description + reason text.
        header = QLabel(self._format_move_header())
        header.setWordWrap(True)
        header.setStyleSheet("font-weight: bold;")
        outer.addWidget(header)

        reason_lbl = QLabel(f"Reason: {self._ctx.move.reason}")
        reason_lbl.setWordWrap(True)
        reason_lbl.setStyleSheet(f"color: {MUTED};")
        outer.addWidget(reason_lbl)

        if not self._ctx.span_resolved:
            warn = QLabel("⚠ Span anchors don't resolve in the current document.")
            warn.setWordWrap(True)
            warn.setStyleSheet(f"color: {DANGER};")
            outer.addWidget(warn)

        # Clip rows.
        if self._ctx.span_resolved and self._ctx.span_indices:
            clips = list(self._doc.main_timeline.clips)
            for idx in self._ctx.span_indices:
                if 0 <= idx < len(clips):
                    outer.addWidget(self._make_clip_row(clips[idx], badge_idx=idx))
        else:
            for anchor in self._ctx.move.span:
                snippet = clip_snippet(self._doc, anchor.source_start, anchor.source_end)
                outer.addWidget(self._make_clip_row_from_anchor(anchor, snippet))

        # Decision controls.
        controls = QHBoxLayout()
        controls.setSpacing(6)
        self._accept_btn = QPushButton("✓ Accept")
        self._accept_btn.clicked.connect(self._on_accept_clicked)
        self._accept_btn.setStyleSheet(
            f"QPushButton {{ color: {SUCCESS}; padding: 4px 10px; }}"
        )
        controls.addWidget(self._accept_btn)

        self._reject_btn = QPushButton("✗ Reject")
        self._reject_btn.clicked.connect(self._on_reject_clicked)
        self._reject_btn.setStyleSheet(
            f"QPushButton {{ color: {DANGER}; padding: 4px 10px; }}"
        )
        controls.addWidget(self._reject_btn)
        controls.addStretch(1)
        outer.addLayout(controls)

        # Inline reject-reason input + submit. Hidden until Reject is clicked.
        self._reason_input = QLineEdit()
        self._reason_input.setPlaceholderText(
            "Why reject? (≥8 chars or use a category prefix)"
        )
        self._reason_input.textChanged.connect(self._on_reason_text_changed)
        self._reason_input.returnPressed.connect(self._on_submit_reject)
        self._reason_input.setVisible(False)
        outer.addWidget(self._reason_input)

        self._reason_submit = QPushButton("Submit rejection")
        self._reason_submit.clicked.connect(self._on_submit_reject)
        self._reason_submit.setEnabled(False)
        self._reason_submit.setVisible(False)
        outer.addWidget(self._reason_submit)

        # Status line — reflects the committed decision (read-only).
        self._status_lbl = QLabel("")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet(f"color: {MUTED}; font-style: italic;")
        outer.addWidget(self._status_lbl)

    # ----- header / row helpers -------------------------------------------

    def _format_move_header(self) -> str:
        n = len(self._ctx.move.span)
        word = "clip" if n == 1 else "clips"
        if self._ctx.move.target is None:
            destination = "→ end of playlist"
        elif self._ctx.target_index is not None:
            target_clips = list(self._doc.main_timeline.clips)
            if 0 <= self._ctx.target_index < len(target_clips):
                t = target_clips[self._ctx.target_index]
                destination = (
                    f"→ before clip at {t.source_start:.1f}s "
                    f"(position {self._ctx.target_index + 1})"
                )
            else:
                destination = "→ before unresolved target"
        else:
            destination = "→ ⚠ target not found in current playlist"
        return f"Move {n} {word} {destination}"

    def _make_clip_row(self, clip, badge_idx: int) -> QWidget:
        """Render one resolved clip with position badge + snippet + time span."""
        row = QFrame()
        layout = QVBoxLayout(row)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(2)
        if self._ctx.move.target is None:
            badge = f"↓ moving from position {badge_idx + 1} to end"
        elif self._ctx.target_index is not None:
            arrow = "↑" if self._ctx.target_index < badge_idx else "↓"
            badge = (
                f"{arrow} moving from position {badge_idx + 1} "
                f"to position {self._ctx.target_index + 1}"
            )
        else:
            badge = f"position {badge_idx + 1}"
        badge_lbl = QLabel(badge)
        badge_lbl.setStyleSheet(f"color: {ACCENT}; font-size: 11px;")
        layout.addWidget(badge_lbl)

        time_lbl = QLabel(_format_time_span(clip.source_start, clip.source_end))
        time_lbl.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        layout.addWidget(time_lbl)

        snippet = clip_snippet(self._doc, clip.source_start, clip.source_end)
        snippet_lbl = QLabel(snippet)
        snippet_lbl.setWordWrap(True)
        layout.addWidget(snippet_lbl)
        return row

    def _make_clip_row_from_anchor(self, anchor, snippet: str) -> QWidget:
        """Fallback rendering for a clip whose anchor doesn't resolve."""
        row = QFrame()
        layout = QVBoxLayout(row)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(2)
        time_lbl = QLabel(
            f"⚠ {_format_time_span(anchor.source_start, anchor.source_end)} (unresolved)"
        )
        time_lbl.setStyleSheet(f"color: {DANGER}; font-size: 11px;")
        layout.addWidget(time_lbl)
        snippet_lbl = QLabel(snippet)
        snippet_lbl.setWordWrap(True)
        layout.addWidget(snippet_lbl)
        return row

    # ----- decision handlers ----------------------------------------------

    def _on_accept_clicked(self) -> None:
        self._decision = Decision(accept=True, rejection_reason=None)
        self._reject_active = False
        self._reason_input.setVisible(False)
        self._reason_submit.setVisible(False)
        self._status_lbl.setText("Accepted.")
        self._status_lbl.setStyleSheet(f"color: {SUCCESS}; font-style: italic;")
        self._refresh_button_state()
        self.decision_changed.emit()

    def _on_reject_clicked(self) -> None:
        # Reveal the inline reason input. Decision is not yet committed
        # — committing requires the Submit click (or Enter in the field)
        # with a valid reason.
        self._reject_active = True
        # If a previous reject was committed, clear it — user is editing.
        if self._decision is not None and not self._decision.accept:
            self._decision = None
        self._reason_input.setVisible(True)
        self._reason_submit.setVisible(True)
        self._reason_input.setFocus()
        self._on_reason_text_changed(self._reason_input.text())
        self._status_lbl.setText("")
        self._status_lbl.setStyleSheet(f"color: {MUTED}; font-style: italic;")
        self._refresh_button_state()
        self.decision_changed.emit()

    def _on_reason_text_changed(self, text: str) -> None:
        self._reason_submit.setEnabled(self._reason_is_valid(text))

    def _on_submit_reject(self) -> None:
        text = self._reason_input.text().strip()
        if not self._reason_is_valid(text):
            return
        self._decision = Decision(accept=False, rejection_reason=text)
        self._reject_active = False
        self._status_lbl.setText(f"Rejected: {text}")
        self._status_lbl.setStyleSheet(f"color: {DANGER}; font-style: italic;")
        self._refresh_button_state()
        self.decision_changed.emit()

    def _reason_is_valid(self, text: str) -> bool:
        s = text.strip()
        if len(s) < MIN_REJECTION_REASON_CHARS:
            return False
        return is_valid_reason(s)

    def _refresh_button_state(self) -> None:
        # Visual emphasis on the active branch. Plain when pending or
        # opposite-active; emphasized when this branch holds the decision.
        accept_active = self._decision is not None and self._decision.accept
        reject_active = (
            self._reject_active
            or (self._decision is not None and not self._decision.accept)
        )
        self._accept_btn.setStyleSheet(
            f"QPushButton {{ color: {'white' if accept_active else SUCCESS}; "
            f"background-color: {SUCCESS if accept_active else 'transparent'}; "
            "padding: 4px 10px; border-radius: 4px; }}"
        )
        self._reject_btn.setStyleSheet(
            f"QPushButton {{ color: {'white' if reject_active else DANGER}; "
            f"background-color: {DANGER if reject_active else 'transparent'}; "
            "padding: 4px 10px; border-radius: 4px; }}"
        )

    def set_controls_enabled(self, enabled: bool) -> None:
        """Disable everything during the host's apply phase."""
        self._accept_btn.setEnabled(enabled)
        self._reject_btn.setEnabled(enabled)
        self._reason_input.setEnabled(enabled)
        self._reason_submit.setEnabled(enabled and self._reason_is_valid(self._reason_input.text()))


# ---------------------------------------------------------------------------
# ProposalReviewPane
# ---------------------------------------------------------------------------


class ProposalReviewPane(QWidget):
    """The full review surface: picker + banner + outline + apply.

    Signals:
        apply_requested(object, object): Emitted on Apply click. Args
            are ``(Proposal, dict[str, Decision])``. The host runs Path
            B and is responsible for everything from there: writing the
            apply-result file, rewriting the document JSON, refreshing
            the editor pane. The pane stays in "applying" state (all
            controls disabled) until the host calls
            :meth:`mark_applied` or :meth:`mark_apply_failed`.
        proposals_changed(int, str): Emitted after every reload with
            ``(count, latest_fresh_proposal_id)``. ``latest_fresh_proposal_id``
            is the proposal_id of the most recently authored proposal
            whose ``created_at`` is newer than the latest apply-result
            for the document — or empty string when no fresh proposal
            exists. Hosts use this for "auto-show dock when a new
            proposal arrives" behaviour; they're responsible for
            tracking which ids they've already surfaced so a dismiss-
            and-reload doesn't re-trigger.
    """

    apply_requested = Signal(object, object)
    proposals_changed = Signal(int, str)

    EMPTY_TEXT = (
        "No proposals for this document.\n\n"
        "Use the MCP `propose_moves` tool (e.g. via Claude Desktop) to "
        "author a proposal, then re-open this pane."
    )

    NO_DOCUMENT_TEXT = (
        "Open a transcript to review proposals."
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._doc: Document | None = None
        self._doc_path: Path | None = None
        self._proposals: list[Proposal] = []
        self._current_proposal: Proposal | None = None
        self._move_cards: list[_MoveCard] = []
        self._latest_apply_result_ids: dict[str, str | None] = {}
        self._build_ui()
        self.setMinimumWidth(260)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

    # ----- build -----------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        title = QLabel("Proposal Review")
        title_font: QFont = title.font()
        title_font.setBold(True)
        title_font.setPointSize(title_font.pointSize() + 1)
        title.setFont(title_font)
        outer.addWidget(title)

        # Picker — only shown when more than one proposal exists.
        self._picker = QComboBox()
        self._picker.currentIndexChanged.connect(self._on_picker_changed)
        outer.addWidget(self._picker)

        # Staleness banner — shown when proposal.parent_document_state_hash
        # != document_state_hash(doc). Read-only display per spec; apply
        # is not blocked.
        self._staleness_banner = QLabel("")
        self._staleness_banner.setWordWrap(True)
        self._staleness_banner.setStyleSheet(
            f"color: {DANGER}; background: #FEF2F2; padding: 6px; border-radius: 4px;"
        )
        self._staleness_banner.setVisible(False)
        outer.addWidget(self._staleness_banner)

        # Empty / no-document state — replaced by the outline scroll
        # area when a proposal is loaded.
        self._empty_label = QLabel(self.NO_DOCUMENT_TEXT)
        self._empty_label.setWordWrap(True)
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setStyleSheet(f"color: {MUTED}; padding: 20px;")
        outer.addWidget(self._empty_label, 1)

        # Outline scroll area — populated per proposal.
        self._outline_scroll = QScrollArea()
        self._outline_scroll.setWidgetResizable(True)
        self._outline_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._outline_container = QWidget()
        self._outline_layout = QVBoxLayout(self._outline_container)
        self._outline_layout.setContentsMargins(0, 0, 0, 0)
        self._outline_layout.setSpacing(2)
        self._outline_layout.addStretch(1)
        self._outline_scroll.setWidget(self._outline_container)
        self._outline_scroll.setVisible(False)
        outer.addWidget(self._outline_scroll, 1)

        # Apply summary + button.
        self._apply_summary = QLabel("")
        self._apply_summary.setStyleSheet(f"color: {MUTED};")
        outer.addWidget(self._apply_summary)

        self._apply_btn = QPushButton("Apply")
        self._apply_btn.setEnabled(False)
        self._apply_btn.clicked.connect(self._on_apply_clicked)
        self._apply_btn.setStyleSheet(
            f"QPushButton:enabled {{ background-color: {ACCENT}; color: white; "
            "padding: 8px; border-radius: 4px; font-weight: bold; }}"
        )
        outer.addWidget(self._apply_btn)

        self._update_picker_visibility()

    # ----- public surface --------------------------------------------------

    def set_document(self, doc: Document | None, doc_path: Path | None) -> None:
        """Bind the pane to ``doc`` (or unbind on ``None``).

        ``doc_path`` is the on-disk location of the .transcribe.json so
        the pane can locate ``<doc_path>.proposals/``. When either arg
        is None the pane reverts to the no-document state.
        """
        self._doc = doc
        self._doc_path = doc_path
        self.reload_proposals()

    def reload_proposals(self) -> None:
        """Re-scan the sidecar dir and refresh the picker.

        Called on document load and whenever the host suspects the
        proposals directory has changed (e.g., a new ``.proposal.json``
        file landed via the MCP path while the user kept the editor
        open).
        """
        if self._doc is None or self._doc_path is None:
            self._proposals = []
            self._current_proposal = None
            self._render_outline()
            self._empty_label.setText(self.NO_DOCUMENT_TEXT)
            self._empty_label.setVisible(True)
            self._outline_scroll.setVisible(False)
            self._update_picker_visibility()
            self._update_apply_state()
            self.proposals_changed.emit(0, "")
            return

        self._proposals = list_proposals(self._doc_path)
        self._latest_apply_result_ids = {
            p.proposal_id or "": (
                latest_apply_result_id_for(self._doc_path, p.proposal_id)
                if p.proposal_id is not None
                else None
            )
            for p in self._proposals
        }
        self._refresh_picker_items()
        latest_fresh = self._latest_fresh_proposal_id()

        if not self._proposals:
            self._current_proposal = None
            self._render_outline()
            self._empty_label.setText(self.EMPTY_TEXT)
            self._empty_label.setVisible(True)
            self._outline_scroll.setVisible(False)
            self._update_picker_visibility()
            self._update_apply_state()
            self.proposals_changed.emit(0, "")
            return

        # Default to most recent. proposals are already sorted
        # chronologically by ``list_proposals``, so the last entry is
        # most recent.
        self._select_proposal_by_index(len(self._proposals) - 1)
        self.proposals_changed.emit(len(self._proposals), latest_fresh)

    def _latest_fresh_proposal_id(self) -> str:
        """Return the most recent proposal_id newer than any apply-result.

        Returns empty string when no proposal qualifies. A proposal is
        "fresh" if (a) no apply-result exists for the document at all,
        or (b) its ``created_at`` is strictly newer than the most
        recent apply-result's ``created_at``. This is the signal the
        host uses to auto-show the review dock — once an apply-result
        catches up, the proposal stops being fresh.
        """
        if not self._proposals or self._doc_path is None:
            return ""
        all_results = list_apply_results_for_document(self._doc_path)
        latest_apply_at = None
        if all_results:
            latest_apply_at = max(ar.created_at for ar in all_results)
        fresh: list[Proposal] = []
        for p in self._proposals:
            if p.created_at is None or p.proposal_id is None:
                continue
            if latest_apply_at is None or p.created_at > latest_apply_at:
                fresh.append(p)
        if not fresh:
            return ""
        # proposals are already sorted chronologically; the tail is most recent.
        return fresh[-1].proposal_id or ""

    def mark_applied(self) -> None:
        """Host calls this after a successful Path-B apply.

        Resets the pane to a clean state and reloads proposals (the
        applied proposal stays on disk; a fresh apply-result file is
        now present, surfacing in the picker subtitle).
        """
        # The host has rewritten the doc on disk; the pane caller will
        # also push a fresh ``set_document`` with the new doc once the
        # editor reloads. Re-enabling controls here is just so the
        # interim render isn't frozen.
        self._set_all_cards_enabled(True)
        self._apply_btn.setEnabled(False)

    def mark_apply_failed(self, message: str = "") -> None:
        """Host calls this if the Path-B apply raises before completing.

        Re-enables controls so the user can re-try or change decisions.
        """
        self._set_all_cards_enabled(True)
        self._update_apply_state()
        if message:
            self._apply_summary.setText(f"Apply failed: {message}")
            self._apply_summary.setStyleSheet(f"color: {DANGER};")

    @property
    def current_proposal(self) -> Proposal | None:
        return self._current_proposal

    def collected_decisions(self) -> dict[str, Decision]:
        """Return the dict the host needs for ``apply_proposal_with_human_decisions``.

        Pending moves (no Accept / no committed Reject) are *omitted*
        — Path B treats missing decisions as ``skipped``. The Apply
        button only enables once every move has a committed decision,
        so this only returns a complete dict in practice; tests poke
        the helper directly.
        """
        out: dict[str, Decision] = {}
        for card in self._move_cards:
            if card.decision is not None:
                out[card.move_id] = card.decision
        return out

    # ----- picker ----------------------------------------------------------

    def _refresh_picker_items(self) -> None:
        self._picker.blockSignals(True)
        self._picker.clear()
        for p in self._proposals:
            label = self._format_picker_label(p)
            self._picker.addItem(label, userData=p.proposal_id)
        self._picker.blockSignals(False)
        self._update_picker_visibility()

    def _update_picker_visibility(self) -> None:
        # Hide the dropdown when there's nothing to pick. We always
        # render a single proposal directly without dropdown clutter.
        self._picker.setVisible(len(self._proposals) > 1)

    def _format_picker_label(self, p: Proposal) -> str:
        pid = p.proposal_id or "?"
        short = pid[:15]
        created = p.created_at.strftime("%Y-%m-%d %H:%M") if p.created_at else "?"
        latest = self._latest_apply_result_ids.get(pid)
        suffix = f" — last apply: {latest[:15]}" if latest else " — never applied"
        return f"{short} · {created} · {len(p.moves)} move(s){suffix}"

    def _on_picker_changed(self, idx: int) -> None:
        if 0 <= idx < len(self._proposals):
            self._select_proposal_by_index(idx)

    def _select_proposal_by_index(self, idx: int) -> None:
        proposal = self._proposals[idx]
        self._picker.blockSignals(True)
        self._picker.setCurrentIndex(idx)
        self._picker.blockSignals(False)
        self._current_proposal = proposal
        self._render_outline()
        self._update_staleness_banner()
        self._update_apply_state()
        self._empty_label.setVisible(False)
        self._outline_scroll.setVisible(True)
        # Reset summary message after a previous apply.
        self._apply_summary.setStyleSheet(f"color: {MUTED};")

    # ----- outline ---------------------------------------------------------

    def _render_outline(self) -> None:
        # Clear existing card widgets.
        for card in self._move_cards:
            card.setParent(None)
            card.deleteLater()
        self._move_cards = []

        if self._current_proposal is None or self._doc is None:
            return

        # Insert cards before the trailing stretch.
        # The layout has one stretch at the end; we insert at the
        # second-to-last position so the stretch stays trailing.
        insert_at = self._outline_layout.count() - 1
        for i, move in enumerate(self._current_proposal.moves):
            move_id = move.move_id if move.move_id is not None else f"m{i:03d}"
            ctx = _resolve_move_context(move, move_id, self._doc)
            card = _MoveCard(ctx, self._doc, parent=self._outline_container)
            card.decision_changed.connect(self._update_apply_state)
            self._move_cards.append(card)
            self._outline_layout.insertWidget(insert_at, card)
            insert_at += 1

    def _update_staleness_banner(self) -> None:
        if (
            self._current_proposal is not None
            and self._doc is not None
            and self._current_proposal.schema_version >= 2
            and self._current_proposal.parent_document_state_hash is not None
            and self._current_proposal.parent_document_state_hash
            != document_state_hash(self._doc)
        ):
            self._staleness_banner.setText(
                "⚠ Proposal may be stale — the document has changed since "
                "this proposal was authored. Apply will report per-move "
                "outcomes, but rejected_system errors are likely."
            )
            self._staleness_banner.setVisible(True)
        else:
            self._staleness_banner.setVisible(False)

    # ----- apply state ----------------------------------------------------

    def _update_apply_state(self) -> None:
        decided = sum(1 for c in self._move_cards if c.decision is not None)
        accepted = sum(1 for c in self._move_cards if c.decision is not None and c.decision.accept)
        rejected = sum(1 for c in self._move_cards if c.decision is not None and not c.decision.accept)
        total = len(self._move_cards)
        if total == 0:
            self._apply_summary.setText("")
            self._apply_btn.setEnabled(False)
            return
        if decided < total:
            self._apply_summary.setText(
                f"Apply: {decided}/{total} moves decided "
                f"({accepted} accept, {rejected} reject)"
            )
        else:
            self._apply_summary.setText(
                f"Apply: {accepted} accepted, {rejected} rejected"
            )
        self._apply_btn.setEnabled(decided == total)

    def _on_apply_clicked(self) -> None:
        if self._current_proposal is None:
            return
        decisions = self.collected_decisions()
        # Disable controls during the apply. The host re-enables via
        # mark_applied / mark_apply_failed.
        self._set_all_cards_enabled(False)
        self._apply_btn.setEnabled(False)
        self.apply_requested.emit(self._current_proposal, decisions)

    def _set_all_cards_enabled(self, enabled: bool) -> None:
        for card in self._move_cards:
            card.set_controls_enabled(enabled)
