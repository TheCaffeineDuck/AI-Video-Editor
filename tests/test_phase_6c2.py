"""Phase 6c-2 — highlight lifecycle MCP tools.

Covers the six-tool surface added by ``mcp_server/tools/highlights.py``:

- ``propose_highlights`` — single-pass spec validation per error code
  (INVALID_HIGHLIGHT for out-of-bounds / zero-duration / unknown
  reframe_mode / unrecognized reason); persists each accepted spec
  with auto-assigned ``highlight_id`` + ``parent_source_hash``.
- ``list_highlights`` — empty / single / multiple in chronological
  order; includes ``rendered_output_path`` after a render.
- ``read_highlight`` — by-id round-trip; HIGHLIGHT_NOT_FOUND on miss.
- ``apply_highlight`` — happy path with face-detection fallback,
  STALE_HIGHLIGHT on source-file replacement, RENDER_FAILED bubble-up
  on ffmpeg errors, render-result sidecar shape, idempotent re-runs
  produce a fresh sidecar.
- ``list_highlight_renders`` / ``read_highlight_render`` — directory
  scan, per-highlight filter, by-id read with RENDER_RESULT_NOT_FOUND
  on miss.
- Round-trip: propose → list → read → apply → list_renders → read_render.
- Sidecar responsibility split: the highlight owns
  ``rendered_output_path`` (one per highlight); render-result owns
  per-run metadata. They don't duplicate each other.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from mcp.shared.exceptions import McpError

from core.cache import cache_key
from core.document import Document, MediaSource, Range, Segment, Word
from core.highlight import (
    Highlight,
    list_highlights_for_document,
    list_render_results_for_document,
    write_highlight,
)
from core.highlight import (
    read_highlight as core_read_highlight,
)
from core.highlight import (
    read_render_result as core_read_render_result,
)
from mcp_server import errors as mcp_errors
from mcp_server.schemas import (
    ApplyHighlightRequest,
    HighlightSpec,
    ListHighlightRendersRequest,
    ListHighlightsRequest,
    ProposeHighlightsRequest,
    ReadHighlightRenderRequest,
    ReadHighlightRequest,
)
from mcp_server.tools.highlights import (
    apply_highlight,
    list_highlight_renders,
    list_highlights,
    propose_highlights,
    read_highlight,
    read_highlight_render,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FFMPEG = REPO_ROOT / "resources" / "bin" / "ffmpeg-mac"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _doc(media: Path, duration: float = 30.0) -> Document:
    src = MediaSource(id="src0", path=media, duration=duration, hash=cache_key(media))
    n_words = max(2, int(duration))
    seg = Segment(
        text=" ".join(f"w{i}" for i in range(n_words)),
        start=0.0,
        end=duration,
        words=tuple(
            Word(text=f"w{i}", start=float(i), end=float(i) + 0.9)
            for i in range(n_words)
        ),
    )
    return Document(
        sources={"src0": src},
        segments=[seg],
        ranges=[Range(source_id="src0", start=0.0, end=duration, reason="manual")],
        language="en",
        created_at=datetime(2026, 4, 29, tzinfo=UTC),
        model_name="tiny",
    )


def _write_doc(doc: Document, path: Path) -> Path:
    path.write_text(json.dumps(doc.to_json(), indent=2), encoding="utf-8")
    return path


def _doc_pair(tmp_path: Path, *, duration: float = 30.0) -> tuple[Path, Path]:
    media = tmp_path / "src.mp4"
    media.write_bytes(b"")
    doc = _doc(media, duration=duration)
    doc_path = tmp_path / "src.transcribe.json"
    _write_doc(doc, doc_path)
    return doc_path, media


def _spec(
    media: Path,
    *,
    start: float = 5.0,
    end: float = 15.0,
    reason: str = "highlight reel",
    reframe_mode: str = "speaker_locked",
    captions_enabled: bool = False,
) -> HighlightSpec:
    return HighlightSpec(
        source_path=str(media),
        source_start_s=start,
        source_end_s=end,
        reason=reason,
        reframe_mode=reframe_mode,
        captions_enabled=captions_enabled,
    )


# ---------------------------------------------------------------------------
# propose_highlights
# ---------------------------------------------------------------------------


def test_propose_highlights_writes_sidecar(tmp_path):
    doc_path, media = _doc_pair(tmp_path)
    result = _run(
        propose_highlights(
            ProposeHighlightsRequest(
                json_path=str(doc_path),
                highlights=[_spec(media, start=5.0, end=15.0)],
            )
        )
    )
    assert len(result.highlights) == 1
    entry = result.highlights[0]
    assert entry.highlight_id != ""
    assert Path(entry.json_path).is_file()

    on_disk = list_highlights_for_document(doc_path)
    assert len(on_disk) == 1
    assert on_disk[0].highlight_id == entry.highlight_id
    assert on_disk[0].parent_source_hash == cache_key(media)
    assert on_disk[0].reframe_mode == "speaker_locked"


def test_propose_highlights_persists_multiple_specs(tmp_path):
    doc_path, media = _doc_pair(tmp_path)
    result = _run(
        propose_highlights(
            ProposeHighlightsRequest(
                json_path=str(doc_path),
                highlights=[
                    _spec(media, start=2.0, end=8.0, reason="highlight: hook"),
                    _spec(
                        media,
                        start=15.0,
                        end=20.0,
                        reason="narrative: punchline",
                        captions_enabled=True,
                    ),
                ],
            )
        )
    )
    assert len(result.highlights) == 2
    on_disk = list_highlights_for_document(doc_path)
    assert len(on_disk) == 2
    captions = sorted(h.captions_enabled for h in on_disk)
    assert captions == [False, True]


def test_propose_highlights_rejects_zero_duration(tmp_path):
    doc_path, media = _doc_pair(tmp_path)
    with pytest.raises(McpError) as exc_info:
        _run(
            propose_highlights(
                ProposeHighlightsRequest(
                    json_path=str(doc_path),
                    highlights=[_spec(media, start=10.0, end=10.0)],
                )
            )
        )
    assert mcp_errors.INVALID_HIGHLIGHT in str(exc_info.value)
    assert "highlights[0]" in str(exc_info.value)


def test_propose_highlights_rejects_out_of_bounds_end(tmp_path):
    doc_path, media = _doc_pair(tmp_path, duration=30.0)
    with pytest.raises(McpError) as exc_info:
        _run(
            propose_highlights(
                ProposeHighlightsRequest(
                    json_path=str(doc_path),
                    highlights=[_spec(media, start=5.0, end=999.0)],
                )
            )
        )
    assert mcp_errors.INVALID_HIGHLIGHT in str(exc_info.value)


def test_propose_highlights_rejects_negative_start(tmp_path):
    doc_path, media = _doc_pair(tmp_path)
    with pytest.raises(McpError) as exc_info:
        _run(
            propose_highlights(
                ProposeHighlightsRequest(
                    json_path=str(doc_path),
                    highlights=[_spec(media, start=-1.0, end=5.0)],
                )
            )
        )
    assert mcp_errors.INVALID_HIGHLIGHT in str(exc_info.value)


def test_propose_highlights_rejects_unknown_reframe_mode(tmp_path):
    doc_path, media = _doc_pair(tmp_path)
    with pytest.raises(McpError) as exc_info:
        _run(
            propose_highlights(
                ProposeHighlightsRequest(
                    json_path=str(doc_path),
                    highlights=[
                        _spec(media, start=2.0, end=8.0, reframe_mode="dynamic_track"),
                    ],
                )
            )
        )
    assert mcp_errors.INVALID_HIGHLIGHT in str(exc_info.value)


def test_propose_highlights_rejects_invalid_reason(tmp_path):
    doc_path, media = _doc_pair(tmp_path)
    with pytest.raises(McpError) as exc_info:
        _run(
            propose_highlights(
                ProposeHighlightsRequest(
                    json_path=str(doc_path),
                    highlights=[_spec(media, start=2.0, end=8.0, reason="x")],
                )
            )
        )
    assert mcp_errors.INVALID_HIGHLIGHT in str(exc_info.value)


def test_propose_highlights_no_partial_persistence_on_failure(tmp_path):
    """Spec says ``On any spec failure, raises INVALID_HIGHLIGHT``. The
    failure must short-circuit before any sidecar is written; otherwise
    the user has to clean up half-persisted state."""
    doc_path, media = _doc_pair(tmp_path)
    with pytest.raises(McpError):
        _run(
            propose_highlights(
                ProposeHighlightsRequest(
                    json_path=str(doc_path),
                    highlights=[
                        _spec(media, start=2.0, end=8.0),  # valid
                        _spec(media, start=999.0, end=1000.0),  # out of bounds
                    ],
                )
            )
        )
    assert list_highlights_for_document(doc_path) == []


def test_propose_highlights_indexes_offending_spec(tmp_path):
    """The error message names the specific failing spec index so a
    multi-entry batch can be repaired one entry at a time."""
    doc_path, media = _doc_pair(tmp_path)
    with pytest.raises(McpError) as exc_info:
        _run(
            propose_highlights(
                ProposeHighlightsRequest(
                    json_path=str(doc_path),
                    highlights=[
                        _spec(media, start=2.0, end=8.0),
                        _spec(media, start=15.0, end=20.0),
                        _spec(media, start=25.0, end=24.0),  # zero/inverted
                    ],
                )
            )
        )
    assert "highlights[2]" in str(exc_info.value)


# ---------------------------------------------------------------------------
# list_highlights / read_highlight
# ---------------------------------------------------------------------------


def test_list_highlights_empty(tmp_path):
    doc_path, _ = _doc_pair(tmp_path)
    result = _run(list_highlights(ListHighlightsRequest(json_path=str(doc_path))))
    assert result.highlights == []


def test_list_highlights_returns_summaries(tmp_path):
    doc_path, media = _doc_pair(tmp_path)
    _run(
        propose_highlights(
            ProposeHighlightsRequest(
                json_path=str(doc_path),
                highlights=[_spec(media, start=2.0, end=8.0)],
            )
        )
    )
    result = _run(list_highlights(ListHighlightsRequest(json_path=str(doc_path))))
    assert len(result.highlights) == 1
    s = result.highlights[0]
    assert s.source_start_s == 2.0
    assert s.source_end_s == 8.0
    assert s.reason == "highlight reel"
    assert s.reframe_mode == "speaker_locked"
    assert s.rendered_output_path is None


def test_read_highlight_round_trips(tmp_path):
    doc_path, media = _doc_pair(tmp_path)
    propose = _run(
        propose_highlights(
            ProposeHighlightsRequest(
                json_path=str(doc_path),
                highlights=[
                    _spec(media, start=12.345, end=18.678, reason="best-take")
                ],
            )
        )
    )
    hid = propose.highlights[0].highlight_id
    summary = _run(
        read_highlight(ReadHighlightRequest(json_path=str(doc_path), highlight_id=hid))
    )
    assert summary.highlight_id == hid
    assert summary.source_start_s == 12.345
    assert summary.source_end_s == 18.678
    assert summary.reason == "best-take"


def test_read_highlight_not_found(tmp_path):
    doc_path, _ = _doc_pair(tmp_path)
    with pytest.raises(McpError) as exc_info:
        _run(
            read_highlight(
                ReadHighlightRequest(
                    json_path=str(doc_path), highlight_id="nope-12345678"
                )
            )
        )
    assert mcp_errors.HIGHLIGHT_NOT_FOUND in str(exc_info.value)


# ---------------------------------------------------------------------------
# apply_highlight (slow — needs ffmpeg + a real source file)
# ---------------------------------------------------------------------------


def _propose_against(synthetic_video, tmp_path: Path, **spec_kwargs) -> tuple[Path, str]:
    """Helper: copy synthetic_video to tmp, write a doc, propose, return id."""
    media_dst = tmp_path / "src.mp4"
    shutil.copy2(synthetic_video, media_dst)
    doc_path = tmp_path / "src.transcribe.json"
    duration = _probe_duration(media_dst)
    doc = _doc(media_dst, duration=duration)
    _write_doc(doc, doc_path)
    propose = _run(
        propose_highlights(
            ProposeHighlightsRequest(
                json_path=str(doc_path),
                highlights=[_spec(media_dst, **spec_kwargs)],
            )
        )
    )
    return doc_path, propose.highlights[0].highlight_id


def _probe_duration(path: Path) -> float:
    out = subprocess.run(
        [
            "/opt/homebrew/bin/ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(out.stdout.strip())


def _probe_dimensions(path: Path) -> tuple[int, int]:
    out = subprocess.run(
        [
            "/opt/homebrew/bin/ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=s=x:p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    w, h = out.stdout.strip().split("x")
    return int(w), int(h)


@pytest.mark.slow
def test_apply_highlight_writes_render_result(synthetic_video, tmp_path):
    doc_path, hid = _propose_against(
        synthetic_video, tmp_path, start=2.0, end=6.0, reframe_mode="center"
    )
    result = _run(
        apply_highlight(
            ApplyHighlightRequest(json_path=str(doc_path), highlight_id=hid)
        )
    )
    assert Path(result.output_path).is_file()
    w, h = _probe_dimensions(Path(result.output_path))
    assert (w, h) == (1080, 1920)

    # Render-result sidecar exists and round-trips.
    rr = core_read_render_result(doc_path, result.render_result_id)
    assert rr.highlight_id == hid
    assert rr.face_detection_used == "center"
    assert rr.crop_box[2] > 0 and rr.crop_box[3] > 0
    assert rr.wall_clock_s >= 0.0
    # The highlight's sidecar now points at the rendered file.
    h_disk = core_read_highlight(doc_path, hid)
    assert h_disk.rendered_output_path == Path(result.output_path)


@pytest.mark.slow
def test_apply_highlight_records_speaker_lock_fallback(synthetic_video, tmp_path):
    """testsrc has no face — speaker_locked must fall back to center
    silently and the render-result must record the fallback honestly."""
    doc_path, hid = _propose_against(
        synthetic_video, tmp_path, start=2.0, end=5.0, reframe_mode="speaker_locked"
    )
    result = _run(
        apply_highlight(
            ApplyHighlightRequest(json_path=str(doc_path), highlight_id=hid)
        )
    )
    rr = core_read_render_result(doc_path, result.render_result_id)
    assert rr.face_detection_used == "speaker_locked_fallback_to_center"


@pytest.mark.slow
def test_apply_highlight_idempotent_re_render_produces_fresh_sidecar(
    synthetic_video, tmp_path
):
    """Re-running on the same id produces a new render-result file; the
    .mp4 output is overwritten in place."""
    doc_path, hid = _propose_against(
        synthetic_video, tmp_path, start=2.0, end=5.0, reframe_mode="center"
    )
    first = _run(
        apply_highlight(ApplyHighlightRequest(json_path=str(doc_path), highlight_id=hid))
    )
    second = _run(
        apply_highlight(ApplyHighlightRequest(json_path=str(doc_path), highlight_id=hid))
    )
    assert first.render_result_id != second.render_result_id
    assert first.output_path == second.output_path
    rrs = list_render_results_for_document(doc_path)
    assert len(rrs) == 2


def test_apply_highlight_stale_when_source_replaced(tmp_path):
    """STALE_HIGHLIGHT fires when the source file has been replaced.

    Simulates by minting a highlight against a doc, then bumping the
    source file's mtime + content (cache_key shifts)."""
    doc_path, media = _doc_pair(tmp_path)
    # Build a highlight directly with the current source hash, then
    # mutate the file so the live cache_key drifts.
    h = Highlight(
        highlight_id="20260429T100000-deadbeef",
        created_at=datetime(2026, 4, 29, tzinfo=UTC),
        parent_document_path=doc_path,
        parent_source_hash=cache_key(media),
        span_source_path=media,
        span_source_start=2.0,
        span_source_end=8.0,
        reason="highlight reel",
        reframe_mode="center",
    )
    write_highlight(doc_path, h)
    # Mutate: rewrite content + bump mtime far enough that int(mtime) shifts.
    media.write_bytes(b"different content")
    import os
    new_mtime = media.stat().st_mtime + 100
    os.utime(media, (new_mtime, new_mtime))

    with pytest.raises(McpError) as exc_info:
        _run(
            apply_highlight(
                ApplyHighlightRequest(
                    json_path=str(doc_path), highlight_id=h.highlight_id
                )
            )
        )
    assert mcp_errors.STALE_HIGHLIGHT in str(exc_info.value)


def test_apply_highlight_render_failed_bubbles_through(tmp_path):
    """An empty source file makes ffmpeg's smartcut step blow up. The
    error should surface as RENDER_FAILED, not as a raw RuntimeError."""
    doc_path, media = _doc_pair(tmp_path)
    propose = _run(
        propose_highlights(
            ProposeHighlightsRequest(
                json_path=str(doc_path),
                highlights=[_spec(media, start=2.0, end=8.0, reframe_mode="center")],
            )
        )
    )
    hid = propose.highlights[0].highlight_id
    with pytest.raises(McpError) as exc_info:
        _run(
            apply_highlight(
                ApplyHighlightRequest(json_path=str(doc_path), highlight_id=hid)
            )
        )
    # Either RENDER_FAILED (if smartcut raised RuntimeError) or
    # FILE_NOT_FOUND (if ffprobe reported no container) — both are
    # acceptable in this synthetic scenario.
    msg = str(exc_info.value)
    assert (
        mcp_errors.RENDER_FAILED in msg or mcp_errors.FILE_NOT_FOUND in msg
    ), msg


# ---------------------------------------------------------------------------
# list_highlight_renders / read_highlight_render
# ---------------------------------------------------------------------------


def test_list_highlight_renders_empty(tmp_path):
    doc_path, _ = _doc_pair(tmp_path)
    result = _run(
        list_highlight_renders(
            ListHighlightRendersRequest(json_path=str(doc_path))
        )
    )
    assert result.render_results == []


def test_read_highlight_render_not_found(tmp_path):
    doc_path, _ = _doc_pair(tmp_path)
    with pytest.raises(McpError) as exc_info:
        _run(
            read_highlight_render(
                ReadHighlightRenderRequest(
                    json_path=str(doc_path),
                    render_result_id="nope-87654321",
                )
            )
        )
    assert mcp_errors.RENDER_RESULT_NOT_FOUND in str(exc_info.value)


@pytest.mark.slow
def test_read_highlight_render_round_trips(synthetic_video, tmp_path):
    doc_path, hid = _propose_against(
        synthetic_video, tmp_path, start=2.0, end=6.0, reframe_mode="center"
    )
    apply_result = _run(
        apply_highlight(
            ApplyHighlightRequest(json_path=str(doc_path), highlight_id=hid)
        )
    )
    rr = _run(
        read_highlight_render(
            ReadHighlightRenderRequest(
                json_path=str(doc_path),
                render_result_id=apply_result.render_result_id,
            )
        )
    )
    assert rr.render_result_id == apply_result.render_result_id
    assert rr.highlight_id == hid
    assert rr.output_path == apply_result.output_path
    assert rr.face_detection_used == "center"
    assert rr.crop_box.w > 0 and rr.crop_box.h > 0
    assert rr.parent_source_hash != ""


@pytest.mark.slow
def test_list_highlight_renders_filter_by_highlight_id(synthetic_video, tmp_path):
    media_dst = tmp_path / "src.mp4"
    shutil.copy2(synthetic_video, media_dst)
    doc_path = tmp_path / "src.transcribe.json"
    duration = _probe_duration(media_dst)
    _write_doc(_doc(media_dst, duration=duration), doc_path)

    propose = _run(
        propose_highlights(
            ProposeHighlightsRequest(
                json_path=str(doc_path),
                highlights=[
                    _spec(media_dst, start=2.0, end=5.0, reframe_mode="center"),
                    _spec(media_dst, start=10.0, end=13.0, reframe_mode="center"),
                ],
            )
        )
    )
    hid_a = propose.highlights[0].highlight_id
    hid_b = propose.highlights[1].highlight_id
    _run(
        apply_highlight(
            ApplyHighlightRequest(json_path=str(doc_path), highlight_id=hid_a)
        )
    )
    _run(
        apply_highlight(
            ApplyHighlightRequest(json_path=str(doc_path), highlight_id=hid_b)
        )
    )
    all_results = _run(
        list_highlight_renders(
            ListHighlightRendersRequest(json_path=str(doc_path))
        )
    )
    assert len(all_results.render_results) == 2
    just_a = _run(
        list_highlight_renders(
            ListHighlightRendersRequest(json_path=str(doc_path), highlight_id=hid_a)
        )
    )
    assert len(just_a.render_results) == 1
    assert just_a.render_results[0].highlight_id == hid_a


# ---------------------------------------------------------------------------
# Round-trip + sidecar responsibility split
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_round_trip_propose_to_render_result(synthetic_video, tmp_path):
    """End-to-end: propose → list → read → apply → list_renders → read_render."""
    doc_path, hid = _propose_against(
        synthetic_video, tmp_path, start=2.0, end=5.0, reframe_mode="center"
    )

    listing = _run(list_highlights(ListHighlightsRequest(json_path=str(doc_path))))
    assert any(s.highlight_id == hid for s in listing.highlights)

    summary = _run(
        read_highlight(
            ReadHighlightRequest(json_path=str(doc_path), highlight_id=hid)
        )
    )
    assert summary.highlight_id == hid

    apply_res = _run(
        apply_highlight(
            ApplyHighlightRequest(json_path=str(doc_path), highlight_id=hid)
        )
    )
    assert Path(apply_res.output_path).is_file()

    renders = _run(
        list_highlight_renders(
            ListHighlightRendersRequest(json_path=str(doc_path), highlight_id=hid)
        )
    )
    assert len(renders.render_results) == 1
    rr_summary = renders.render_results[0]
    assert rr_summary.render_result_id == apply_res.render_result_id

    rr_full = _run(
        read_highlight_render(
            ReadHighlightRenderRequest(
                json_path=str(doc_path),
                render_result_id=apply_res.render_result_id,
            )
        )
    )
    assert rr_full.output_path == apply_res.output_path

    # Responsibility split — the highlight points at the .mp4; the
    # render-result records the per-run metadata. They share output_path
    # by design (the highlight's pointer should land on the most-recent
    # render's output) but everything else is distinct.
    h_after = _run(
        read_highlight(
            ReadHighlightRequest(json_path=str(doc_path), highlight_id=hid)
        )
    )
    assert h_after.rendered_output_path == apply_res.output_path
    # Highlight does not carry crop_box / wall_clock — only the render-
    # result does.
    assert not hasattr(h_after, "crop_box")
    assert not hasattr(h_after, "wall_clock_s")
