"""Phase 6a final — GUI v3 reader + MCP v3 awareness.

Closes 6a:

- TranscriptView renders monotonic v3 docs identically to v2 (hash
  snapshot guard).
- TranscriptView renders non-monotonic v3 docs in playlist order with
  a visible boundary marker between clips.
- EditorPane disables cut/restore/save/delete actions and applies a
  tooltip when the loaded document's timeline is non-monotonic.
- MCP ``get_timeline`` returns clips in playlist order with the
  ``is_source_monotonic`` flag.
- MCP ``apply_cuts`` / ``restore_ranges`` refuse non-monotonic
  documents with the stable ``EDIT_NOT_SUPPORTED`` code.

Qt tests gracefully skip when ``WHISPER_NO_QT`` is set or PySide6 is
missing — same opt-out the other ``ui_qt`` tests use.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from mcp.shared.exceptions import McpError

from core.document import Document, MediaSource, Range, Segment, Word
from mcp_server import errors as mcp_errors
from mcp_server.schemas import (
    ApplyCutsRequest,
    CutRequest,
    JsonPathRequest,
    RestoreRangesRequest,
    RestoreRequestItem,
)
from mcp_server.tools.document import (
    apply_cuts,
    get_ranges,
    get_timeline,
    restore_ranges,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_WAV = REPO_ROOT / "tests" / "fixtures" / "sample.wav"


# ---------------------------------------------------------------------------
# Fixture builders shared between Qt and MCP tests
# ---------------------------------------------------------------------------


def _segments_for_synthetic() -> list[Segment]:
    """Two segments × 2 words each, on tidy 1-second boundaries.

    Word layout:
        seg 0: alpha (0.0–0.5)  beta (0.5–1.0)
        seg 1: gamma (4.0–4.5)  delta (4.5–5.0)
    """
    return [
        Segment(
            text="alpha beta",
            start=0.0,
            end=1.0,
            words=(
                Word(text="alpha", start=0.0, end=0.5),
                Word(text=" beta", start=0.5, end=1.0),
            ),
        ),
        Segment(
            text="gamma delta",
            start=4.0,
            end=5.0,
            words=(
                Word(text="gamma", start=4.0, end=4.5),
                Word(text=" delta", start=4.5, end=5.0),
            ),
        ),
    ]


def _monotonic_doc() -> Document:
    """A v3-monotonic Document covering the whole 5-s synthetic source."""
    return Document(
        sources={"src0": MediaSource(id="src0", path=SAMPLE_WAV, duration=5.0)},
        segments=_segments_for_synthetic(),
        ranges=[Range(source_id="src0", start=0.0, end=5.0)],
        language="en",
        created_at=datetime(2026, 4, 29, 0, 0, 0, tzinfo=UTC),
        model_name="tiny",
        source_hash=None,
    )


def _non_monotonic_doc() -> Document:
    """A v3-non-monotonic Document: clip [4,5] before clip [0,1]."""
    return Document(
        sources={"src0": MediaSource(id="src0", path=SAMPLE_WAV, duration=5.0)},
        segments=_segments_for_synthetic(),
        # Playlist order: gamma/delta first, alpha/beta second.
        ranges=[
            Range(source_id="src0", start=4.0, end=5.0),
            Range(source_id="src0", start=0.0, end=1.0),
        ],
        language="en",
        created_at=datetime(2026, 4, 29, 0, 0, 0, tzinfo=UTC),
        model_name="tiny",
        source_hash=None,
    )


def _write_doc_json(doc: Document, path: Path) -> Path:
    path.write_text(json.dumps(doc.to_json(), indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Qt tests — only run when PySide6 is present
# ---------------------------------------------------------------------------


if os.environ.get("WHISPER_NO_QT"):  # pragma: no cover — opt-in headless
    pytest.skip("WHISPER_NO_QT set", allow_module_level=True)

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")


# ---------------------------------------------------------------------------
# Hash-snapshot: monotonic v3 transcript text unchanged by 6a final
# ---------------------------------------------------------------------------


def _transcript_plain_hash(view) -> str:
    """Stable hash of the transcript widget's plain text + per-word state.

    Hashing toPlainText alone misses the strikethrough state that v2
    rendering puts on cut words. The render-time WordRef list carries
    the ``kept`` flag, so we fold both into the hash. That's what makes
    this a real regression guard — text + visual struck-state both have
    to match.
    """
    body = view.toPlainText()
    flags = "".join("k" if w.kept else "s" for w in view.words)
    h = hashlib.sha256()
    h.update(body.encode("utf-8"))
    h.update(b"\x00")
    h.update(flags.encode("ascii"))
    return h.hexdigest()


def test_monotonic_transcript_render_is_unchanged_baseline(qtbot):
    """Snapshot the monotonic render. Locks in the v2-equivalent shape
    so future GUI changes that accidentally affect monotonic rendering
    show up as a hash mismatch here.
    """
    from ui_qt.components.transcript_view import TranscriptView

    view = TranscriptView()
    qtbot.addWidget(view)
    view.set_document_model(_monotonic_doc())
    digest = _transcript_plain_hash(view)
    # Locked-in snapshot. If you change the monotonic render path,
    # regenerate this digest deliberately and explain why in the PR.
    # The hash covers plain text + per-word kept/struck flags so any
    # change to either dimension trips the assertion.
    assert digest == (
        "77f6934a1717c273b767c0d11f2f30cb09993b263c2e3d840a3180649de0102f"
    )
    # In addition to the hash, assert a couple of structural invariants
    # that are easier to debug if the hash ever drifts.
    assert "alpha" in view.toPlainText()
    assert "delta" in view.toPlainText()
    assert all(w.kept for w in view.words)
    # No clip-boundary markers in monotonic render — that string lives
    # only on the non-monotonic path.
    assert "— jump to" not in view.toPlainText()


def test_monotonic_with_cut_renders_struck_words(qtbot):
    """Pre-6a behaviour: cut words appear with strikethrough; this
    must not regress on 6a-monotonic docs.
    """
    from ui_qt.components.transcript_view import TranscriptView

    view = TranscriptView()
    qtbot.addWidget(view)
    doc = _monotonic_doc()
    # Cut "beta" (0.5–1.0): single split-range timeline, still monotonic.
    object.__setattr__(
        doc,
        "ranges",
        [
            Range(source_id="src0", start=0.0, end=0.5),
            Range(source_id="src0", start=1.0, end=5.0),
        ],
    )
    assert doc.main_timeline.is_source_monotonic()
    view.set_document_model(doc)
    refs_by_text = {r.word.text.strip(): r for r in view.words}
    assert refs_by_text["alpha"].kept is True
    assert refs_by_text["beta"].kept is False  # struck
    assert refs_by_text["gamma"].kept is True
    assert refs_by_text["delta"].kept is True


def test_non_monotonic_transcript_renders_in_playlist_order(qtbot):
    """Words appear in playlist order — gamma/delta first, alpha/beta
    second — and each is marked kept (not struck).
    """
    from ui_qt.components.transcript_view import TranscriptView

    view = TranscriptView()
    qtbot.addWidget(view)
    view.set_document_model(_non_monotonic_doc())
    word_texts = [r.word.text.strip() for r in view.words]
    # Playlist order, not source order:
    assert word_texts == ["gamma", "delta", "alpha", "beta"]
    assert all(r.kept for r in view.words)


def test_non_monotonic_transcript_inserts_clip_boundary_marker(qtbot):
    from ui_qt.components.transcript_view import TranscriptView

    view = TranscriptView()
    qtbot.addWidget(view)
    view.set_document_model(_non_monotonic_doc())
    text = view.toPlainText()
    # Boundary appears between the two clips — not before the first,
    # not after the last.
    assert text.count("— jump to") == 1
    # The boundary references the second clip's source_start (0.00s).
    assert "0.00s" in text


# ---------------------------------------------------------------------------
# EditorPane: editing actions disabled on non-monotonic
# ---------------------------------------------------------------------------


@pytest.fixture
def _qt_settings_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_SETTINGS_DIR", str(tmp_path))
    return tmp_path


def test_editor_pane_enables_actions_on_monotonic_doc(qtbot, _qt_settings_dir):
    from core.settings import Settings
    from ui_qt.editor_pane import EditorPane

    settings = Settings(output_dir=str(_qt_settings_dir))
    pane = EditorPane(_monotonic_doc(), settings=settings)
    qtbot.addWidget(pane)
    try:
        actions = pane.actions_bundle
        for act in (actions.cut, actions.restore, actions.delete, actions.save):
            assert act.isEnabled()
            assert act.toolTip() != EditorPane.NON_MONOTONIC_TOOLTIP
    finally:
        pane.release()


def test_editor_pane_disables_editing_on_non_monotonic(qtbot, _qt_settings_dir):
    from core.settings import Settings
    from ui_qt.editor_pane import EditorPane

    settings = Settings(output_dir=str(_qt_settings_dir))
    pane = EditorPane(_non_monotonic_doc(), settings=settings)
    qtbot.addWidget(pane)
    try:
        actions = pane.actions_bundle
        # Cut / restore / delete / save are all disabled.
        for act in (actions.cut, actions.restore, actions.delete, actions.save):
            assert act.isEnabled() is False
            assert act.toolTip() == EditorPane.NON_MONOTONIC_TOOLTIP
        # Export stays enabled (read-only render works).
        assert actions.export_.isEnabled()
        # The toolbar Save button mirrors the action.
        assert pane._save_btn.isEnabled() is False  # noqa: SLF001
        assert pane._save_btn.toolTip() == EditorPane.NON_MONOTONIC_TOOLTIP  # noqa: SLF001
    finally:
        pane.release()


def test_editor_pane_loads_non_monotonic_without_exception(qtbot, _qt_settings_dir):
    """The pane initialises, renders the transcript, and stays alive
    even though the timeline is non-monotonic. No NotImplementedError
    leaks through to the constructor.
    """
    from core.settings import Settings
    from ui_qt.editor_pane import EditorPane

    settings = Settings(output_dir=str(_qt_settings_dir))
    pane = EditorPane(_non_monotonic_doc(), settings=settings)
    qtbot.addWidget(pane)
    try:
        # Transcript got rendered with playlist-order words.
        assert [r.word.text.strip() for r in pane.transcript_view.words] == [
            "gamma",
            "delta",
            "alpha",
            "beta",
        ]
        # Document session is intact.
        assert pane.session.document.main_timeline.is_source_monotonic() is False
    finally:
        pane.release()


# ---------------------------------------------------------------------------
# MCP get_timeline + apply_cuts/restore_ranges refusal
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def test_mcp_get_timeline_monotonic(tmp_path):
    media = tmp_path / "fake.mp4"
    media.write_bytes(b"")
    doc = _monotonic_doc()
    object.__setattr__(
        doc,
        "sources",
        {"src0": MediaSource(id="src0", path=media, duration=5.0)},
    )
    path = _write_doc_json(doc, tmp_path / "x.transcribe.json")
    res = _run(get_timeline(JsonPathRequest(json_path=str(path))))
    assert res.is_source_monotonic is True
    assert len(res.clips) == 1
    assert res.clips[0].source_path == str(media)
    assert res.clips[0].source_start_s == 0.0
    assert res.clips[0].source_end_s == 5.0
    assert res.total_duration_s == 5.0


def test_mcp_get_timeline_non_monotonic_preserves_playlist_order(tmp_path):
    media = tmp_path / "fake.mp4"
    media.write_bytes(b"")
    doc = _non_monotonic_doc()
    object.__setattr__(
        doc,
        "sources",
        {"src0": MediaSource(id="src0", path=media, duration=5.0)},
    )
    path = _write_doc_json(doc, tmp_path / "x.transcribe.json")
    res = _run(get_timeline(JsonPathRequest(json_path=str(path))))
    assert res.is_source_monotonic is False
    # Playlist order, NOT source order.
    assert [(c.source_start_s, c.source_end_s) for c in res.clips] == [
        (4.0, 5.0),
        (0.0, 1.0),
    ]
    assert res.total_duration_s == 2.0


def test_mcp_get_ranges_flags_non_monotonic(tmp_path):
    """get_ranges retains its v2 shape but reports the lossy flag."""
    media = tmp_path / "fake.mp4"
    media.write_bytes(b"")
    doc = _non_monotonic_doc()
    object.__setattr__(
        doc,
        "sources",
        {"src0": MediaSource(id="src0", path=media, duration=5.0)},
    )
    path = _write_doc_json(doc, tmp_path / "x.transcribe.json")
    res = _run(get_ranges(JsonPathRequest(json_path=str(path))))
    assert res.is_source_monotonic is False
    # Flat list; the playlist order is no longer recoverable from this
    # tool — that's why is_source_monotonic exists.
    assert len(res.ranges) == 2


def test_mcp_apply_cuts_refuses_non_monotonic_with_stable_code(tmp_path):
    media = tmp_path / "fake.mp4"
    media.write_bytes(b"")
    doc = _non_monotonic_doc()
    object.__setattr__(
        doc,
        "sources",
        {"src0": MediaSource(id="src0", path=media, duration=5.0)},
    )
    path = _write_doc_json(doc, tmp_path / "x.transcribe.json")
    before = path.read_bytes()
    with pytest.raises(McpError) as exc:
        _run(
            apply_cuts(
                ApplyCutsRequest(
                    json_path=str(path),
                    cuts=[CutRequest(start_s=4.0, end_s=4.5)],
                )
            )
        )
    assert exc.value.error.data["code"] == mcp_errors.EDIT_NOT_SUPPORTED
    # File on disk is unchanged.
    assert path.read_bytes() == before


def test_mcp_restore_ranges_refuses_non_monotonic(tmp_path):
    media = tmp_path / "fake.mp4"
    media.write_bytes(b"")
    doc = _non_monotonic_doc()
    object.__setattr__(
        doc,
        "sources",
        {"src0": MediaSource(id="src0", path=media, duration=5.0)},
    )
    path = _write_doc_json(doc, tmp_path / "x.transcribe.json")
    with pytest.raises(McpError) as exc:
        _run(
            restore_ranges(
                RestoreRangesRequest(
                    json_path=str(path),
                    ranges=[RestoreRequestItem(start_s=1.0, end_s=4.0)],
                )
            )
        )
    assert exc.value.error.data["code"] == mcp_errors.EDIT_NOT_SUPPORTED


def test_mcp_apply_cuts_still_works_on_monotonic(tmp_path):
    """The non-monotonic guard doesn't accidentally block monotonic edits."""
    media = tmp_path / "fake.mp4"
    media.write_bytes(b"")
    doc = _monotonic_doc()
    object.__setattr__(
        doc,
        "sources",
        {"src0": MediaSource(id="src0", path=media, duration=5.0)},
    )
    path = _write_doc_json(doc, tmp_path / "x.transcribe.json")
    res = _run(
        apply_cuts(
            ApplyCutsRequest(
                json_path=str(path),
                cuts=[CutRequest(start_s=0.5, end_s=1.0, reason="filler")],
            )
        )
    )
    assert res.applied_count == 1


def test_mcp_get_timeline_listed_in_tool_surface():
    from mcp_server.server import _tool_descriptors

    names = [d.name for d in _tool_descriptors()]
    assert "get_timeline" in names
    assert "get_ranges" in names  # both retained — additive contract
