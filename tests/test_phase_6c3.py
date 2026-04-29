"""Phase 6c-3 — GUI Highlights panel.

Read-only counterpart to :class:`~ui_qt.components.proposal_review_pane.ProposalReviewPane`
— there is no human review on the highlight path. Tests check:

- Panel loads against a doc with N highlights, renders N cards.
- Empty state message shown when no highlights exist.
- Render button triggers ``render_highlight`` in a worker thread; on
  completion the card flips to "Rendered." and Open enables.
- Open button is disabled when not rendered, enabled when rendered.
- Auto-show on doc-load when the sidecar dir has highlights (handled
  via the ``highlights_present`` signal — host wires the auto-show).
- Doc swap clears the panel.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.cache import cache_key
from core.document import Document, MediaSource, Range, Segment, Word
from core.highlight import (
    Highlight,
    list_highlights_for_document,
    mark_rendered,
    rendered_output_path_for,
    write_highlight,
)

# ---------------------------------------------------------------------------
# Headless Qt skip
# ---------------------------------------------------------------------------


def _have_qt() -> bool:
    if os.environ.get("WHISPER_NO_QT"):
        return False
    try:
        import PySide6  # noqa: F401
    except ImportError:
        return False
    return True


qt_skip = pytest.mark.skipif(
    not _have_qt(), reason="PySide6 unavailable / WHISPER_NO_QT set"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _doc(media: Path) -> Document:
    src = MediaSource(id="src0", path=media, duration=30.0, hash=cache_key(media))
    return Document(
        sources={"src0": src},
        segments=[
            Segment(
                text="seg",
                start=0.0,
                end=30.0,
                words=tuple(
                    Word(text=f"w{i}", start=float(i), end=float(i + 1))
                    for i in range(30)
                ),
            )
        ],
        ranges=[Range(source_id="src0", start=0.0, end=30.0, reason="manual")],
        language="en",
        created_at=datetime(2026, 4, 26, tzinfo=UTC),
        model_name="tiny",
    )


def _write_doc(doc: Document, path: Path) -> Path:
    path.write_text(json.dumps(doc.to_json(), indent=2), encoding="utf-8")
    return path


def _make_highlight(
    parent_path: Path,
    media: Path,
    *,
    start: float = 5.0,
    end: float = 10.0,
    reason: str = "highlight reel",
    reframe_mode: str = "speaker_locked",
    captions_enabled: bool = False,
) -> Highlight:
    return Highlight(
        highlight_id="",
        created_at=datetime(2026, 4, 29, tzinfo=UTC),
        parent_document_path=parent_path,
        parent_source_hash=cache_key(media),
        span_source_path=media,
        span_source_start=start,
        span_source_end=end,
        reason=reason,
        reframe_mode=reframe_mode,  # type: ignore[arg-type]
        captions_enabled=captions_enabled,
    )


def _scene(tmp_path: Path) -> tuple[Document, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = _doc(media)
    doc_path = tmp_path / "x.transcribe.json"
    _write_doc(doc, doc_path)
    return doc, doc_path, media


# ---------------------------------------------------------------------------
# Panel tests
# ---------------------------------------------------------------------------


@qt_skip
def test_panel_no_document_state(qtbot):
    """No document → placeholder is shown; cards are empty."""
    from ui_qt.components.highlights_panel import HighlightsPanel

    panel = HighlightsPanel()
    qtbot.addWidget(panel)
    panel.set_document(None, None)

    assert panel.cards == []
    assert panel._empty_label.isHidden() is False
    assert panel._scroll.isHidden() is True
    assert "Open a transcript" in panel._empty_label.text()


@qt_skip
def test_panel_empty_state_when_no_highlights(qtbot, tmp_path):
    from ui_qt.components.highlights_panel import HighlightsPanel

    doc, doc_path, _ = _scene(tmp_path)
    panel = HighlightsPanel()
    qtbot.addWidget(panel)
    panel.set_document(doc, doc_path)

    assert panel.cards == []
    assert panel._empty_label.isHidden() is False
    assert "No highlights" in panel._empty_label.text()


@qt_skip
def test_panel_renders_one_card_per_highlight(qtbot, tmp_path):
    from ui_qt.components.highlights_panel import HighlightsPanel

    doc, doc_path, media = _scene(tmp_path)
    h1 = _make_highlight(doc_path, media, start=2.0, end=5.0)
    h2 = _make_highlight(
        doc_path, media, start=10.0, end=15.0, reason="narrative: punchline"
    )
    write_highlight(doc_path, h1)
    write_highlight(doc_path, h2)

    panel = HighlightsPanel()
    qtbot.addWidget(panel)
    panel.set_document(doc, doc_path)

    assert len(panel.cards) == 2
    listed = list_highlights_for_document(doc_path)
    expected_ids = {h.highlight_id for h in listed}
    actual_ids = {c.highlight_id for c in panel.cards}
    assert actual_ids == expected_ids


@qt_skip
def test_panel_open_button_disabled_until_rendered(qtbot, tmp_path):
    """Open button starts disabled (no rendered_output_path on disk),
    flips to enabled once a card's highlight is marked rendered."""
    from ui_qt.components.highlights_panel import HighlightsPanel

    doc, doc_path, media = _scene(tmp_path)
    materialized, _ = write_highlight(
        doc_path, _make_highlight(doc_path, media, start=2.0, end=5.0)
    )

    panel = HighlightsPanel()
    qtbot.addWidget(panel)
    panel.set_document(doc, doc_path)

    card = panel.cards[0]
    assert card._open_btn.isEnabled() is False
    assert "Not yet rendered" in card._status_lbl.text()

    # Simulate a render landing — write an mp4 stub on disk and update
    # the sidecar via mark_rendered. Reload the panel and check.
    out = rendered_output_path_for(
        doc_path.with_name(doc_path.name + ".highlights"),
        materialized.highlight_id,
    )
    out.write_bytes(b"\x00")
    mark_rendered(doc_path, materialized, out)
    panel.reload_highlights()

    refreshed = panel.cards[0]
    assert refreshed._open_btn.isEnabled() is True
    assert "Rendered" in refreshed._status_lbl.text()


@qt_skip
def test_panel_emits_highlights_present_for_auto_show(qtbot, tmp_path):
    """The host wires the highlights_present signal to auto-show the
    dock when a doc with highlights loads. Test the signal contract."""
    from ui_qt.components.highlights_panel import HighlightsPanel

    doc, doc_path, media = _scene(tmp_path)
    materialized, _ = write_highlight(
        doc_path, _make_highlight(doc_path, media, start=2.0, end=5.0)
    )

    panel = HighlightsPanel()
    qtbot.addWidget(panel)

    received: list[tuple[bool, str]] = []
    panel.highlights_present.connect(
        lambda has, latest: received.append((has, latest))
    )
    panel.set_document(doc, doc_path)

    assert received[-1][0] is True
    assert received[-1][1] == materialized.highlight_id


@qt_skip
def test_panel_emits_no_highlights_when_empty(qtbot, tmp_path):
    """A doc without highlights emits ``(False, "")`` so the host knows
    not to auto-show the dock."""
    from ui_qt.components.highlights_panel import HighlightsPanel

    doc, doc_path, _ = _scene(tmp_path)
    panel = HighlightsPanel()
    qtbot.addWidget(panel)
    received: list[tuple[bool, str]] = []
    panel.highlights_present.connect(
        lambda has, latest: received.append((has, latest))
    )
    panel.set_document(doc, doc_path)
    assert received[-1] == (False, "")


@qt_skip
def test_panel_doc_swap_clears_cards(qtbot, tmp_path):
    """Switching to a different doc must drop the previous doc's cards
    (so a stale list doesn't linger across project loads)."""
    from ui_qt.components.highlights_panel import HighlightsPanel

    doc_a, path_a, media_a = _scene(tmp_path / "a")
    write_highlight(path_a, _make_highlight(path_a, media_a, start=2.0, end=5.0))
    doc_b, path_b, _ = _scene(tmp_path / "b")  # no highlights

    panel = HighlightsPanel()
    qtbot.addWidget(panel)
    panel.set_document(doc_a, path_a)
    assert len(panel.cards) == 1

    panel.set_document(doc_b, path_b)
    assert panel.cards == []
    assert panel._empty_label.isHidden() is False


@qt_skip
def test_panel_render_button_runs_worker(qtbot, tmp_path):
    """Click Render → worker is created, runs, emits failed / finished.

    We simulate failure by using an empty mp4 — ffmpeg/PyAV barfs and
    the card flips to the failed state. The path matters more than the
    success because (a) it exercises the QThread plumbing without a
    multi-second real render, and (b) the success path is covered by
    the slow integration test in 6c-2's test_apply_highlight_*.
    """
    from PySide6.QtCore import QThread

    from ui_qt.components.highlights_panel import HighlightsPanel

    doc, doc_path, media = _scene(tmp_path)
    write_highlight(doc_path, _make_highlight(doc_path, media, start=2.0, end=5.0))

    panel = HighlightsPanel()
    qtbot.addWidget(panel)
    panel.set_document(doc, doc_path)

    card = panel.cards[0]
    assert card._render_btn.isEnabled() is True
    card._on_render_clicked()
    # Worker is now in flight; render button is greyed out.
    assert card._render_btn.isEnabled() is False
    assert card._worker is not None
    assert isinstance(card._thread, QThread)

    # Wait until the worker finishes (failure expected on empty mp4).
    qtbot.waitUntil(lambda: card._worker is None, timeout=15000)
    # After failure, render button re-enables.
    assert card._render_btn.isEnabled() is True
    # Status reflects the failure.
    assert "failed" in card._status_lbl.text().lower()
