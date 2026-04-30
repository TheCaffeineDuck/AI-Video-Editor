"""GUI pane for inspecting + rendering highlights.

Largely read-only — Claude proposes highlights via the MCP
``propose_highlights`` tool and the renders run via ``apply_highlight``.
The panel surfaces what's been proposed, lets the operator trigger a
render with one click, and opens the rendered .mp4 in the OS player.

Phase 7 additions:

* Each highlight card lists every *fragment* (one per :class:`SubSpan`),
  showing the camera assigned to it.
* For sync-group highlights, every fragment gets a small drop-down
  letting the operator reassign the camera before render. The
  reassignment writes the new source path (and hash) back to the
  sidecar JSON via :func:`core.highlight.reassign_fragment_source`
  and clears any prior render output — re-render is required.
* Single-camera (no sync group) highlights show the source as plain
  text without a drop-down — there's no other camera to swap to.

Per-card actions are unchanged from 6c-3:

* **Render** — runs :func:`core.highlight_render.render_highlight` in
  a :class:`QThread` so the GUI stays responsive.
* **Open** — opens the rendered .mp4 with the OS default player via
  :class:`QDesktopServices`. Disabled until the highlight has been
  rendered.

State machine:

- **Empty** — no document loaded, or the doc has no highlights. The
  pane shows a short placeholder; controls are hidden.
- **Loaded** — at least one highlight; cards render in chronological
  order (highlight_id is timestamp-prefixed).
- **Rendering** — a single render is in flight; that card's Render
  button is disabled and a spinner-shape progress bar replaces the
  status line.
- **Rendered** — render completed successfully; the card flips to
  show "rendered" and Open enables.

The ``proposal_review_pane`` worker pattern doesn't apply here —
:class:`~core.proposal.apply_proposal_with_human_decisions` is fast
enough to run synchronously, but ``render_highlight`` shells out to
ffmpeg for tens of seconds and would freeze the UI. We use a
``QThread`` per render rather than a thread pool because renders are
exclusive (each one writes the same .mp4 path) and a queue
discipline isn't needed at this scale.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.cache import cache_key
from core.document import Document
from core.highlight import (
    Highlight,
    SubSpan,
    list_highlights_for_document,
    read_highlight,
    reassign_fragment_source,
)
from core.highlight_render import render_highlight
from core.sync import read_sync_group
from ui_qt.style import ACCENT, DANGER, MUTED, SUCCESS

_LOG = logging.getLogger(__name__)

DOCK_TARGET_WIDTH = 360


# ---------------------------------------------------------------------------
# Worker — runs render_highlight on a QThread
# ---------------------------------------------------------------------------


class _RenderWorker(QObject):
    """Runs :func:`render_highlight` off the GUI thread.

    Lives on a dedicated :class:`QThread` and emits progress + done
    signals for the card to consume. The card owns the worker's
    lifecycle and is responsible for cleaning up the thread (calling
    ``quit()`` + ``wait()``) once :attr:`finished` fires.
    """

    progress = Signal(float)
    finished = Signal(object)  # carries the HighlightRenderMetadata
    failed = Signal(str)

    def __init__(self, highlight: Highlight, document: Document) -> None:
        super().__init__()
        self._highlight = highlight
        self._document = document

    def run(self) -> None:
        try:
            metadata = render_highlight(
                self._highlight,
                self._document,
                progress_callback=self._on_progress,
            )
        except Exception as exc:  # noqa: BLE001 — surface anything
            self.failed.emit(str(exc))
            return
        self.finished.emit(metadata)

    def _on_progress(self, value: float) -> None:
        # progress_callback runs on the worker thread; emit is queued
        # back to the GUI thread automatically because progress is a
        # Qt signal.
        try:
            self.progress.emit(float(value))
        except (TypeError, ValueError):
            pass


# ---------------------------------------------------------------------------
# HighlightCard — one per highlight
# ---------------------------------------------------------------------------


def _format_duration(start: float, end: float) -> str:
    span = max(0.0, end - start)
    return f"{start:.1f}s – {end:.1f}s ({span:.1f}s)"


def _camera_label(path: Path) -> str:
    """Short label for a camera path (filename stem)."""
    return Path(path).name


class _HighlightCard(QFrame):
    """Outline group for a single highlight: header, status, controls.

    Rendering happens in a worker thread; signals from the worker drive
    the UI updates here. The card owns the worker + its thread; on
    completion the card cleans them both up.
    """

    rendered = Signal(str)  # emits highlight_id when render completes

    def __init__(
        self,
        highlight: Highlight,
        document: Document,
        document_path: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._highlight = highlight
        self._document = document
        self._document_path = document_path
        self._worker: _RenderWorker | None = None
        self._thread: QThread | None = None
        self._build_ui()
        self._refresh_open_state()

    # ----- public surface --------------------------------------------------

    @property
    def highlight_id(self) -> str:
        return self._highlight.highlight_id

    def set_highlight(self, highlight: Highlight) -> None:
        """Refresh the card's underlying highlight (e.g., after a render)."""
        self._highlight = highlight
        self._refresh_open_state()

    # ----- build -----------------------------------------------------------

    def _build_ui(self) -> None:
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFrameShadow(QFrame.Shadow.Plain)
        self.setLineWidth(1)
        self.setObjectName("HighlightCard")
        self.setStyleSheet(
            "QFrame#HighlightCard { "
            f"border: 1px solid {MUTED}; border-radius: 6px; "
            "padding: 4px; margin: 4px 0; }"
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)

        header = QLabel(self._format_header())
        header.setWordWrap(True)
        header.setStyleSheet("font-weight: bold;")
        outer.addWidget(header)

        # Fragment list — one row per SubSpan, with optional camera
        # reassignment combo for sync-group highlights.
        self._fragment_combos: list[QComboBox | None] = []
        self._available_cameras: list[Path] = self._resolve_available_cameras()
        for idx, span in enumerate(self._highlight.sub_spans):
            row = self._build_fragment_row(idx, span)
            outer.addLayout(row)

        reason_lbl = QLabel(f"Reason: {self._highlight.reason}")
        reason_lbl.setWordWrap(True)
        reason_lbl.setStyleSheet(f"color: {MUTED};")
        outer.addWidget(reason_lbl)

        self._status_lbl = QLabel(self._initial_status_text())
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet(f"color: {self._initial_status_color()};")
        outer.addWidget(self._status_lbl)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setVisible(False)
        outer.addWidget(self._progress)

        controls = QHBoxLayout()
        controls.setSpacing(6)
        self._render_btn = QPushButton("Render")
        self._render_btn.clicked.connect(self._on_render_clicked)
        self._render_btn.setStyleSheet(
            f"QPushButton:enabled {{ background-color: {ACCENT}; color: white; "
            "padding: 4px 12px; border-radius: 4px; }}"
        )
        controls.addWidget(self._render_btn)

        self._open_btn = QPushButton("Open")
        self._open_btn.clicked.connect(self._on_open_clicked)
        controls.addWidget(self._open_btn)
        controls.addStretch(1)
        outer.addLayout(controls)

    def _resolve_available_cameras(self) -> list[Path]:
        """Return the list of cameras the user can swap a fragment to.

        For sync-group highlights, this is every camera registered in
        the group. For non-sync-group highlights this is empty (the
        only camera is the highlight's existing source path; reassignment
        across unrelated sources is intentionally not supported here
        because there's no offset translation to fall back on).
        """
        if self._highlight.sync_group_id is None:
            return []
        try:
            group = read_sync_group(
                self._document_path, self._highlight.sync_group_id
            )
        except (FileNotFoundError, ValueError) as exc:
            _LOG.warning(
                "highlights panel: could not load sync group %s: %s",
                self._highlight.sync_group_id,
                exc,
            )
            return []
        return [cam.source_path for cam in group.cameras.values()]

    def _build_fragment_row(self, idx: int, span: SubSpan) -> QHBoxLayout:
        """Build the row for one SubSpan: time label + camera label/combo."""
        row = QHBoxLayout()
        row.setSpacing(6)
        time_lbl = QLabel(
            f"#{idx + 1} {_format_duration(span.source_start, span.source_end)}"
        )
        time_lbl.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        row.addWidget(time_lbl)
        if self._available_cameras:
            combo = QComboBox()
            for cam in self._available_cameras:
                combo.addItem(_camera_label(cam), userData=str(cam))
            current_str = str(span.source_path)
            for i in range(combo.count()):
                if combo.itemData(i) == current_str:
                    combo.setCurrentIndex(i)
                    break
            combo.currentIndexChanged.connect(
                lambda _idx, fragment_index=idx, c=combo: self._on_camera_changed(
                    fragment_index, c
                )
            )
            row.addWidget(combo, 1)
            self._fragment_combos.append(combo)
        else:
            cam_lbl = QLabel(_camera_label(span.source_path))
            cam_lbl.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
            row.addWidget(cam_lbl, 1)
            self._fragment_combos.append(None)
        return row

    def _on_camera_changed(self, fragment_index: int, combo: QComboBox) -> None:
        """Persist a fragment camera change and surface the dirty state."""
        new_path_str = combo.currentData()
        if not new_path_str:
            return
        new_path = Path(new_path_str)
        if self._highlight.sub_spans[fragment_index].source_path == new_path:
            return
        try:
            new_hash = cache_key(new_path)
        except FileNotFoundError as exc:
            _LOG.warning(
                "highlights panel: cannot reassign — camera missing: %s", exc
            )
            return
        self._highlight = reassign_fragment_source(
            self._document_path,
            self._highlight,
            fragment_index=fragment_index,
            new_source_path=new_path,
            new_source_hash=new_hash,
        )
        # The reassignment cleared rendered_output_path; reflect that.
        self._refresh_open_state()
        if self._is_rendered():
            return
        self._status_lbl.setText("Reassigned — needs render.")
        self._status_lbl.setStyleSheet(f"color: {ACCENT};")

    def _format_header(self) -> str:
        captions = " · captions on" if self._highlight.captions_enabled else ""
        sync = (
            f" · sync:{self._highlight.sync_group_id[:8]}"
            if self._highlight.sync_group_id
            else ""
        )
        nfrag = (
            f" · {len(self._highlight.sub_spans)} fragments"
            if len(self._highlight.sub_spans) > 1
            else ""
        )
        return (
            f"{self._highlight.reframe_mode}{captions}{sync}{nfrag} · "
            f"{self._highlight.highlight_id[:15]}"
        )

    def _initial_status_text(self) -> str:
        if self._is_rendered():
            return "Rendered."
        return "Not yet rendered."

    def _initial_status_color(self) -> str:
        if self._is_rendered():
            return SUCCESS
        return MUTED

    # ----- state helpers --------------------------------------------------

    def _is_rendered(self) -> bool:
        rp = self._highlight.rendered_output_path
        return rp is not None and Path(rp).is_file()

    def _refresh_open_state(self) -> None:
        rendered = self._is_rendered()
        self._open_btn.setEnabled(rendered)
        if rendered:
            self._status_lbl.setText("Rendered.")
            self._status_lbl.setStyleSheet(f"color: {SUCCESS};")
        elif self._worker is None:
            self._status_lbl.setText("Not yet rendered.")
            self._status_lbl.setStyleSheet(f"color: {MUTED};")

    # ----- actions ---------------------------------------------------------

    def _on_render_clicked(self) -> None:
        if self._worker is not None:
            return
        self._render_btn.setEnabled(False)
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._status_lbl.setText("Rendering…")
        self._status_lbl.setStyleSheet(f"color: {ACCENT};")

        self._thread = QThread(self)
        self._worker = _RenderWorker(self._highlight, self._document)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        # Cleanup chain: worker + thread teardown in lockstep.
        self._worker.finished.connect(self._cleanup_worker)
        self._worker.failed.connect(self._cleanup_worker)
        self._thread.start()

    def _on_progress(self, value: float) -> None:
        pct = int(round(max(0.0, min(1.0, value)) * 100))
        self._progress.setValue(pct)

    def _on_finished(self, _metadata) -> None:
        # Reload the highlight's sidecar JSON to pick up the new
        # rendered_output_path that render_highlight wrote via mark_rendered.
        try:
            self._highlight = read_highlight(self._document_path, self._highlight.highlight_id)
        except FileNotFoundError:
            # Sidecar gone (unlikely) — leave the in-memory copy alone
            # and rely on the metadata's output_path.
            pass
        self._progress.setVisible(False)
        self._refresh_open_state()
        self._render_btn.setEnabled(True)
        self.rendered.emit(self._highlight.highlight_id)

    def _on_failed(self, message: str) -> None:
        self._progress.setVisible(False)
        self._status_lbl.setText(f"Render failed: {message}")
        self._status_lbl.setStyleSheet(f"color: {DANGER};")
        self._render_btn.setEnabled(True)

    def _cleanup_worker(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
            self._thread.deleteLater()
            self._thread = None
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None

    def _on_open_clicked(self) -> None:
        rp = self._highlight.rendered_output_path
        if rp is None:
            return
        # Use QDesktopServices.openUrl with file:// — opens in the OS
        # default player (QuickTime on macOS, Movies & TV on Windows).
        url = QUrl.fromLocalFile(str(Path(rp).resolve()))
        QDesktopServices.openUrl(url)


# ---------------------------------------------------------------------------
# HighlightsPanel
# ---------------------------------------------------------------------------


class HighlightsPanel(QWidget):
    """The full highlights surface: title + scrollable card list.

    Read-only — no accept/reject, no editing of highlight specs.
    Re-proposing happens via Claude Desktop, not the GUI.

    Signals:
        highlights_present(bool, str): Emitted after every reload with
            ``(has_highlights, latest_highlight_id)``. Hosts use this
            to auto-show the dock when a document with highlights
            loads (mirrors the proposal-review pane's auto-show logic).
            ``latest_highlight_id`` is empty when the doc has none.
        rendered(str): Forwarded from any card's ``rendered`` signal so
            the host can react (e.g., refresh some external listing).
    """

    highlights_present = Signal(bool, str)
    rendered = Signal(str)

    EMPTY_TEXT = (
        "No highlights for this document.\n\n"
        "Use the MCP `propose_highlights` tool (e.g. via Claude Desktop) "
        "to author highlights, then re-open this pane."
    )

    NO_DOCUMENT_TEXT = "Open a transcript to view highlights."

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._doc: Document | None = None
        self._doc_path: Path | None = None
        self._cards: list[_HighlightCard] = []
        self._build_ui()
        self.setMinimumWidth(260)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

    # ----- build -----------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        title = QLabel("Highlights")
        title_font: QFont = title.font()
        title_font.setBold(True)
        title_font.setPointSize(title_font.pointSize() + 1)
        title.setFont(title_font)
        outer.addWidget(title)

        self._empty_label = QLabel(self.NO_DOCUMENT_TEXT)
        self._empty_label.setWordWrap(True)
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setStyleSheet(f"color: {MUTED}; padding: 20px;")
        outer.addWidget(self._empty_label, 1)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._container = QWidget()
        self._container_layout = QVBoxLayout(self._container)
        self._container_layout.setContentsMargins(0, 0, 0, 0)
        self._container_layout.setSpacing(2)
        self._container_layout.addStretch(1)
        self._scroll.setWidget(self._container)
        self._scroll.setVisible(False)
        outer.addWidget(self._scroll, 1)

    # ----- public surface --------------------------------------------------

    def set_document(self, doc: Document | None, doc_path: Path | None) -> None:
        """Bind the panel to ``doc`` (or unbind on ``None``).

        ``doc_path`` is the on-disk location of the .transcribe.json so
        the panel can locate ``<doc_path>.highlights/``. When either is
        None the panel reverts to the no-document state.
        """
        self._doc = doc
        self._doc_path = doc_path
        self.reload_highlights()

    def reload_highlights(self) -> None:
        """Re-scan ``<doc>.highlights/`` and refresh the card list.

        Called on document load and whenever the host suspects the
        sidecar dir has changed (e.g., a new highlight landed via the
        MCP path, or a render just completed).
        """
        # Drop existing cards.
        for card in self._cards:
            card.setParent(None)
            card.deleteLater()
        self._cards = []

        if self._doc is None or self._doc_path is None:
            self._empty_label.setText(self.NO_DOCUMENT_TEXT)
            self._empty_label.setVisible(True)
            self._scroll.setVisible(False)
            self.highlights_present.emit(False, "")
            return

        items = list_highlights_for_document(self._doc_path)
        if not items:
            self._empty_label.setText(self.EMPTY_TEXT)
            self._empty_label.setVisible(True)
            self._scroll.setVisible(False)
            self.highlights_present.emit(False, "")
            return

        # Rebuild card list. Insert before the trailing stretch.
        insert_at = self._container_layout.count() - 1
        for h in items:
            card = _HighlightCard(
                h, self._doc, self._doc_path, parent=self._container
            )
            card.rendered.connect(self._on_card_rendered)
            self._cards.append(card)
            self._container_layout.insertWidget(insert_at, card)
            insert_at += 1

        self._empty_label.setVisible(False)
        self._scroll.setVisible(True)
        latest = items[-1].highlight_id  # list is sorted chronologically
        self.highlights_present.emit(True, latest)

    @property
    def cards(self) -> list[_HighlightCard]:
        """Test surface — exposes the per-card list for assertions."""
        return list(self._cards)

    # ----- signals ---------------------------------------------------------

    def _on_card_rendered(self, highlight_id: str) -> None:
        """Refresh the affected card from disk after its render finishes.

        We don't tear down the panel — only the highlight whose render
        completed needs its sidecar re-read for the new
        ``rendered_output_path``. Mirrors the proposal pane's
        ``mark_applied`` minimal-disturb pattern.
        """
        if self._doc_path is None:
            return
        for card in self._cards:
            if card.highlight_id == highlight_id:
                try:
                    fresh = read_highlight(self._doc_path, highlight_id)
                except FileNotFoundError:
                    return
                card.set_highlight(fresh)
                break
        self.rendered.emit(highlight_id)
