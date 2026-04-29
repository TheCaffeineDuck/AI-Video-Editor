"""Phase 6a — MCP server tool surface.

These tests exercise the MCP layer only: tool registration, dispatch,
schema marshalling, error codes, and the all-or-nothing semantics of
the editing tools. The underlying ``core/`` and ``workers/`` behaviours
are already covered elsewhere — we mock or short-circuit them here.

The handlers are async; ``anyio.from_thread.run`` (via
``asyncio.run``) is the simplest way to call them from sync test
bodies.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from mcp.shared.exceptions import McpError

from core.document import Document, MediaSource, Range, Segment, Word
from mcp_server import errors as mcp_errors
from mcp_server.schemas import (
    ApplyCutsRequest,
    CutRequest,
    GetTranscriptRequest,
    JsonPathRequest,
    RestoreRangesRequest,
    RestoreRequestItem,
    TranscribeRequest,
)
from mcp_server.server import TOOLS, _tool_descriptors, build_server
from mcp_server.tools.document import (
    apply_cuts,
    get_ranges,
    get_transcript,
    load_document,
    restore_ranges,
)
from mcp_server.tools.transcribe import transcribe
from workers import transcription as transcription_mod
from workers.events import DoneEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async handler synchronously in a fresh event loop."""
    return asyncio.run(coro)


def _build_doc(
    media_path: Path,
    *,
    duration: float = 4.0,
    source_hash: str | None = None,
) -> Document:
    """Two-segment, four-word Document. Words sit on tidy boundaries
    so cut tests can target word edges without floating-point fuss.

    Word layout (seconds):
        0.0–0.5   hello       seg 0
        0.5–1.0    world      seg 0
        2.0–2.5   foo         seg 1
        2.5–3.0    bar        seg 1
    Silence between 1.0 and 2.0; tail silence 3.0–4.0.
    """
    seg0 = Segment(
        text="hello world",
        start=0.0,
        end=1.0,
        words=(
            Word(text="hello", start=0.0, end=0.5),
            Word(text=" world", start=0.5, end=1.0),
        ),
    )
    seg1 = Segment(
        text="foo bar",
        start=2.0,
        end=3.0,
        words=(
            Word(text="foo", start=2.0, end=2.5),
            Word(text=" bar", start=2.5, end=3.0),
        ),
    )
    return Document(
        sources={"src0": MediaSource(id="src0", path=media_path, duration=duration)},
        segments=[seg0, seg1],
        ranges=[Range(source_id="src0", start=0.0, end=duration)],
        language="en",
        created_at=datetime.now(UTC),
        model_name="tiny",
        source_hash=source_hash,
    )


def _write_doc(doc: Document, path: Path) -> Path:
    path.write_text(json.dumps(doc.to_json(), indent=2), encoding="utf-8")
    return path


def _make_doc_file(tmp_path: Path) -> Path:
    media = tmp_path / "fake.mp4"
    media.write_bytes(b"")
    doc = _build_doc(media)
    return _write_doc(doc, tmp_path / "fake.transcribe.json")


# ---------------------------------------------------------------------------
# 1. Tool registration / schema marshalling
# ---------------------------------------------------------------------------


def test_eight_tools_registered():
    """Phase 6a final: ``get_timeline`` joins the surface as the v3-aware
    counterpart to ``get_ranges``."""
    descs = _tool_descriptors()
    names = [d.name for d in descs]
    assert names == [
        "transcribe",
        "load_document",
        "get_transcript",
        "get_ranges",
        "get_timeline",
        "apply_cuts",
        "restore_ranges",
        "render",
    ]


def test_each_tool_has_input_schema():
    for desc in _tool_descriptors():
        assert isinstance(desc.inputSchema, dict)
        assert desc.inputSchema.get("type") == "object"
        assert "properties" in desc.inputSchema
        # extra=forbid → additionalProperties: false propagates through
        assert desc.inputSchema.get("additionalProperties") is False


def test_each_tool_has_output_schema():
    for desc in _tool_descriptors():
        assert isinstance(desc.outputSchema, dict)
        assert desc.outputSchema.get("type") == "object"


def test_build_server_returns_server_instance():
    s = build_server()
    assert s is not None
    # The server's name field is the one we configured.
    assert s.name == "transcribe"


def test_dispatch_table_handlers_are_async():
    import inspect

    for tool in TOOLS:
        assert inspect.iscoroutinefunction(tool.handler), (
            f"{tool.name} handler must be async"
        )


# ---------------------------------------------------------------------------
# 2. load_document
# ---------------------------------------------------------------------------


def test_load_document_returns_summary(tmp_path):
    path = _make_doc_file(tmp_path)
    summary = _run(load_document(JsonPathRequest(json_path=str(path))))
    assert summary.path == str(path)
    assert summary.source_path.endswith("fake.mp4")
    assert summary.duration_s == 4.0
    assert summary.word_count == 4
    assert summary.range_count == 1
    assert summary.schema_version == 3


def test_load_document_missing_file(tmp_path):
    missing = tmp_path / "nope.transcribe.json"
    with pytest.raises(McpError) as exc:
        _run(load_document(JsonPathRequest(json_path=str(missing))))
    assert exc.value.error.data["code"] == mcp_errors.FILE_NOT_FOUND


def test_load_document_invalid_json(tmp_path):
    path = tmp_path / "broken.transcribe.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(McpError) as exc:
        _run(load_document(JsonPathRequest(json_path=str(path))))
    assert exc.value.error.data["code"] == mcp_errors.INVALID_DOCUMENT


def test_load_document_unsupported_schema(tmp_path):
    """A future schema_version trips UNSUPPORTED_SCHEMA, not silent migrate."""
    path = tmp_path / "future.transcribe.json"
    path.write_text(
        json.dumps({"schema_version": 999, "garbage": True}), encoding="utf-8"
    )
    with pytest.raises(McpError) as exc:
        _run(load_document(JsonPathRequest(json_path=str(path))))
    assert exc.value.error.data["code"] == mcp_errors.UNSUPPORTED_SCHEMA


def test_load_document_v1_migrates(tmp_path):
    """v1 sidecars load — Document.from_json runs the migration in memory."""
    media = tmp_path / "old.mp4"
    media.write_bytes(b"")
    v1 = {
        "schema_version": 1,
        "media_path": str(media),
        "duration": 5.0,
        "language": "en",
        "model_name": "tiny",
        "created_at": datetime.now(UTC).isoformat(),
        "segments": [],
        "cuts": [],
    }
    path = tmp_path / "old.transcribe.json"
    path.write_text(json.dumps(v1), encoding="utf-8")
    summary = _run(load_document(JsonPathRequest(json_path=str(path))))
    assert summary.duration_s == 5.0
    assert summary.range_count == 1  # full keep-range derived from no cuts
    assert summary.schema_version == 1  # raw on-disk version reported


# ---------------------------------------------------------------------------
# 3. get_transcript
# ---------------------------------------------------------------------------


def test_get_transcript_all_words(tmp_path):
    path = _make_doc_file(tmp_path)
    res = _run(
        get_transcript(GetTranscriptRequest(json_path=str(path), include_struck=True))
    )
    assert len(res.words) == 4
    assert all(not w.struck for w in res.words)
    assert res.words[0].word == "hello"
    assert res.words[0].segment_idx == 0
    assert res.words[2].segment_idx == 1


def test_get_transcript_marks_struck_after_cut(tmp_path):
    media = tmp_path / "fake.mp4"
    media.write_bytes(b"")
    doc = _build_doc(media)
    # Manually carve out [0.5, 1.0] so the word "world" is struck.
    doc = type(doc)(
        sources=doc.sources,
        segments=doc.segments,
        ranges=[
            Range(source_id="src0", start=0.0, end=0.5),
            Range(source_id="src0", start=1.0, end=4.0),
        ],
        language=doc.language,
        created_at=doc.created_at,
        model_name=doc.model_name,
        source_hash=doc.source_hash,
    )
    path = _write_doc(doc, tmp_path / "cut.transcribe.json")
    res = _run(
        get_transcript(GetTranscriptRequest(json_path=str(path), include_struck=True))
    )
    by_word = {w.word: w for w in res.words}
    assert by_word["hello"].struck is False
    assert by_word[" world"].struck is True


def test_get_transcript_excludes_struck_when_disabled(tmp_path):
    media = tmp_path / "fake.mp4"
    media.write_bytes(b"")
    doc = _build_doc(media)
    doc = type(doc)(
        sources=doc.sources,
        segments=doc.segments,
        ranges=[
            Range(source_id="src0", start=0.0, end=0.5),
            Range(source_id="src0", start=1.0, end=4.0),
        ],
        language=doc.language,
        created_at=doc.created_at,
        model_name=doc.model_name,
        source_hash=doc.source_hash,
    )
    path = _write_doc(doc, tmp_path / "cut.transcribe.json")
    res = _run(
        get_transcript(
            GetTranscriptRequest(json_path=str(path), include_struck=False)
        )
    )
    words = {w.word for w in res.words}
    assert " world" not in words
    assert all(not w.struck for w in res.words)


# ---------------------------------------------------------------------------
# 4. get_ranges
# ---------------------------------------------------------------------------


def test_get_ranges_full_coverage(tmp_path):
    path = _make_doc_file(tmp_path)
    res = _run(get_ranges(JsonPathRequest(json_path=str(path))))
    assert len(res.ranges) == 1
    assert res.ranges[0].start_s == 0.0
    assert res.ranges[0].end_s == 4.0
    assert res.total_kept_s == 4.0
    assert res.total_cut_s == 0.0


def test_get_ranges_after_cut(tmp_path):
    media = tmp_path / "fake.mp4"
    media.write_bytes(b"")
    doc = _build_doc(media)
    doc = type(doc)(
        sources=doc.sources,
        segments=doc.segments,
        ranges=[
            Range(source_id="src0", start=0.0, end=0.5),
            Range(source_id="src0", start=1.0, end=4.0),
        ],
        language=doc.language,
        created_at=doc.created_at,
        model_name=doc.model_name,
        source_hash=doc.source_hash,
    )
    path = _write_doc(doc, tmp_path / "x.transcribe.json")
    res = _run(get_ranges(JsonPathRequest(json_path=str(path))))
    assert len(res.ranges) == 2
    assert res.total_kept_s == pytest.approx(3.5)
    assert res.total_cut_s == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 5. apply_cuts
# ---------------------------------------------------------------------------


def test_apply_cuts_word_aligned(tmp_path):
    path = _make_doc_file(tmp_path)
    req = ApplyCutsRequest(
        json_path=str(path),
        cuts=[CutRequest(start_s=0.5, end_s=1.0, reason="filler")],
    )
    res = _run(apply_cuts(req))
    assert res.applied_count == 1
    assert res.skipped_count == 0
    # File on disk now reflects the cut.
    raw = json.loads(path.read_text(encoding="utf-8"))
    doc2 = Document.from_json(raw)
    assert len(doc2.ranges) == 2
    assert doc2.ranges[0].end == 0.5
    assert doc2.ranges[1].start == 1.0
    # Reason landed on the surviving range whose end matches the cut start
    # (subtract_interval inherits the parent reason; the AddCut.reason
    # field is captured in the command but not auto-attached to ranges
    # in 4f-3 — verify the file is well-formed and the ranges shrink).


def test_apply_cuts_silence_between_words_passes(tmp_path):
    """A cut covering pure silence between segments validates fine."""
    path = _make_doc_file(tmp_path)
    req = ApplyCutsRequest(
        json_path=str(path),
        cuts=[CutRequest(start_s=1.2, end_s=1.8)],
    )
    res = _run(apply_cuts(req))
    assert res.applied_count == 1
    assert res.skipped_count == 0


def test_apply_cuts_word_boundary_violation_blocks_write(tmp_path):
    path = _make_doc_file(tmp_path)
    before_bytes = path.read_bytes()
    # 0.3 sits inside the word "hello" (0.0–0.5) → violation.
    req = ApplyCutsRequest(
        json_path=str(path),
        cuts=[CutRequest(start_s=0.3, end_s=1.0)],
    )
    with pytest.raises(McpError) as exc:
        _run(apply_cuts(req))
    assert exc.value.error.data["code"] == mcp_errors.WORD_BOUNDARY_VIOLATION
    # All-or-nothing — file unchanged.
    assert path.read_bytes() == before_bytes


def test_apply_cuts_one_invalid_blocks_all(tmp_path):
    """All-or-nothing: the valid cut also doesn't land if a sibling fails."""
    path = _make_doc_file(tmp_path)
    before_bytes = path.read_bytes()
    req = ApplyCutsRequest(
        json_path=str(path),
        cuts=[
            CutRequest(start_s=0.5, end_s=1.0),  # valid
            CutRequest(start_s=2.1, end_s=2.5),  # 2.1 mid-word "foo"
        ],
    )
    with pytest.raises(McpError) as exc:
        _run(apply_cuts(req))
    assert exc.value.error.data["code"] == mcp_errors.WORD_BOUNDARY_VIOLATION
    assert path.read_bytes() == before_bytes


def test_apply_cuts_skip_inside_existing_cut(tmp_path):
    """Cut entirely inside an already-cut region → skip, applied_count stays."""
    media = tmp_path / "fake.mp4"
    media.write_bytes(b"")
    doc = _build_doc(media)
    # Pre-cut [0.0, 1.0] so [0.5, 1.0] sits inside the cut already.
    doc = type(doc)(
        sources=doc.sources,
        segments=doc.segments,
        ranges=[Range(source_id="src0", start=2.0, end=4.0)],
        language=doc.language,
        created_at=doc.created_at,
        model_name=doc.model_name,
        source_hash=doc.source_hash,
    )
    path = _write_doc(doc, tmp_path / "c.transcribe.json")
    req = ApplyCutsRequest(
        json_path=str(path),
        cuts=[CutRequest(start_s=0.5, end_s=1.0)],
    )
    res = _run(apply_cuts(req))
    assert res.applied_count == 0
    assert res.skipped_count == 1


def test_apply_cuts_inverted_interval(tmp_path):
    path = _make_doc_file(tmp_path)
    req = ApplyCutsRequest(
        json_path=str(path),
        cuts=[CutRequest(start_s=1.0, end_s=0.5)],
    )
    with pytest.raises(McpError) as exc:
        _run(apply_cuts(req))
    assert exc.value.error.data["code"] == mcp_errors.CUT_INVALID


# ---------------------------------------------------------------------------
# 6. restore_ranges
# ---------------------------------------------------------------------------


def test_restore_ranges_re_inserts_cut(tmp_path):
    media = tmp_path / "fake.mp4"
    media.write_bytes(b"")
    doc = _build_doc(media)
    doc = type(doc)(
        sources=doc.sources,
        segments=doc.segments,
        ranges=[
            Range(source_id="src0", start=0.0, end=0.5),
            Range(source_id="src0", start=1.0, end=4.0),
        ],
        language=doc.language,
        created_at=doc.created_at,
        model_name=doc.model_name,
        source_hash=doc.source_hash,
    )
    path = _write_doc(doc, tmp_path / "x.transcribe.json")
    req = RestoreRangesRequest(
        json_path=str(path),
        ranges=[RestoreRequestItem(start_s=0.5, end_s=1.0)],
    )
    res = _run(restore_ranges(req))
    assert res.applied_count == 1
    raw = json.loads(path.read_text(encoding="utf-8"))
    doc2 = Document.from_json(raw)
    # union_interval merges the restore with the two adjacent kept ranges.
    assert len(doc2.ranges) == 1
    assert doc2.ranges[0].start == 0.0
    assert doc2.ranges[0].end == 4.0


def test_restore_ranges_skip_already_kept(tmp_path):
    path = _make_doc_file(tmp_path)
    req = RestoreRangesRequest(
        json_path=str(path),
        ranges=[RestoreRequestItem(start_s=0.0, end_s=0.5)],
    )
    res = _run(restore_ranges(req))
    assert res.applied_count == 0
    assert res.skipped_count == 1


def test_restore_ranges_word_boundary_violation(tmp_path):
    path = _make_doc_file(tmp_path)
    before = path.read_bytes()
    req = RestoreRangesRequest(
        json_path=str(path),
        ranges=[RestoreRequestItem(start_s=0.3, end_s=1.0)],
    )
    with pytest.raises(McpError) as exc:
        _run(restore_ranges(req))
    assert exc.value.error.data["code"] == mcp_errors.WORD_BOUNDARY_VIOLATION
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# 7. transcribe (worker is mocked)
# ---------------------------------------------------------------------------


class _FakeWorker:
    """Stand-in for TranscriptionWorker that emits a DoneEvent on .run()."""

    def __init__(self, *, settings, media_path, model_name, language, formats, on_event, cancel_event=None):
        self.settings = settings
        self.media_path = media_path
        self.formats = formats
        self.on_event = on_event

    def run(self) -> None:
        # Fabricate a Document and a JSON sidecar at the candidate path.
        from workers.transcription import candidate_cache_path

        doc = _build_doc(self.media_path)
        out_path = candidate_cache_path(self.settings, self.media_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(doc.to_json(), indent=2), encoding="utf-8")
        info = type("Info", (), {"language": "en", "duration": 4.0})()
        self.on_event(
            DoneEvent(
                segments=list(doc.segments),
                info=info,
                output_files={"json": out_path},
                elapsed=0.01,
                document=doc,
            )
        )


def test_transcribe_writes_json_and_returns_metadata(tmp_path, monkeypatch):
    media = tmp_path / "input.mp4"
    media.write_bytes(b"\x00\x00")  # so try_load_cached_document's stat works

    monkeypatch.setattr(
        "mcp_server.tools.transcribe.TranscriptionWorker", _FakeWorker
    )
    # Force a cache miss — the candidate path doesn't exist yet, so the
    # worker runs and writes it.

    res = _run(transcribe(TranscribeRequest(source_path=str(media))))
    assert res.cache_hit is False
    assert Path(res.output_path).is_file()
    assert res.word_count == 4
    assert res.duration_s == 4.0
    assert res.language_detected == "en"


def test_transcribe_cache_hit_skips_worker(tmp_path, monkeypatch):
    """Existing .transcribe.json with matching source_hash → fast path."""
    media = tmp_path / "input.mp4"
    media.write_bytes(b"\x00\x00")
    # Build a Document whose source_hash matches the live file.
    from core.cache import cache_key

    src_hash = cache_key(media)
    doc = _build_doc(media, source_hash=src_hash)
    cache_path = tmp_path / "input.transcribe.json"
    cache_path.write_text(json.dumps(doc.to_json(), indent=2), encoding="utf-8")

    # If the worker were invoked, we'd notice — patch it to a sentinel
    # that raises so the test would fail loudly on the wrong path.
    def _explode(*a, **kw):
        raise AssertionError("worker should not run on cache hit")

    monkeypatch.setattr(
        "mcp_server.tools.transcribe.TranscriptionWorker", _explode
    )

    # Settings.output_dir defaults to None → cache lives next to the
    # source. Make sure the loader points at the right dir.
    monkeypatch.setattr(
        transcription_mod,
        "resolve_output_dir",
        lambda settings, p: p.parent,
    )

    res = _run(transcribe(TranscribeRequest(source_path=str(media))))
    assert res.cache_hit is True
    assert Path(res.output_path) == cache_path
    assert res.word_count == 4


def test_transcribe_missing_source_file(tmp_path):
    missing = tmp_path / "nope.mp4"
    with pytest.raises(McpError) as exc:
        _run(transcribe(TranscribeRequest(source_path=str(missing))))
    assert exc.value.error.data["code"] == mcp_errors.FILE_NOT_FOUND


# ---------------------------------------------------------------------------
# 8. render (worker is mocked)
# ---------------------------------------------------------------------------


def test_render_returns_file_metadata(tmp_path, monkeypatch):
    from mcp_server.schemas import RenderRequest
    from mcp_server.tools.render import render
    from workers import render as render_worker_mod

    path = _make_doc_file(tmp_path)
    out = tmp_path / "out.mp4"

    def _fake_render_cut(doc, output, on_progress=None, **kwargs):
        # Write a small payload so file_size_bytes > 0.
        Path(output).write_bytes(b"\x00" * 1024)
        if on_progress:
            on_progress(1.0)
        return Path(output)

    monkeypatch.setattr(render_worker_mod, "render_cut", _fake_render_cut)
    monkeypatch.setattr(
        "mcp_server.tools.render.get_duration", lambda p: 3.5
    )

    res = _run(
        render(
            RenderRequest(json_path=str(path), output_path=str(out))
        )
    )
    assert Path(res.output_path) == out
    assert res.file_size_bytes == 1024
    assert res.duration_s == 3.5
    assert res.render_time_s >= 0.0


def test_render_failure_raises_render_failed(tmp_path, monkeypatch):
    from mcp_server.schemas import RenderRequest
    from mcp_server.tools.render import render
    from workers import render as render_worker_mod

    path = _make_doc_file(tmp_path)
    out = tmp_path / "out.mp4"

    def _broken_render(*a, **kw):
        raise RuntimeError("smartcut blew up")

    monkeypatch.setattr(render_worker_mod, "render_cut", _broken_render)

    with pytest.raises(McpError) as exc:
        _run(
            render(
                RenderRequest(json_path=str(path), output_path=str(out))
            )
        )
    assert exc.value.error.data["code"] == mcp_errors.RENDER_FAILED


def test_render_missing_document_raises_file_not_found(tmp_path):
    from mcp_server.schemas import RenderRequest
    from mcp_server.tools.render import render

    missing = tmp_path / "nope.transcribe.json"
    with pytest.raises(McpError) as exc:
        _run(
            render(
                RenderRequest(
                    json_path=str(missing),
                    output_path=str(tmp_path / "x.mp4"),
                )
            )
        )
    assert exc.value.error.data["code"] == mcp_errors.FILE_NOT_FOUND
