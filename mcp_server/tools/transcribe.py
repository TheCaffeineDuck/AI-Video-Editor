"""``transcribe`` tool — wraps :class:`workers.transcription.TranscriptionWorker`.

Synchronous from the client's perspective: the call blocks until the
worker finishes. We run the worker's ``run()`` directly on a worker
thread (via ``anyio.to_thread.run_sync``) so the MCP event loop stays
responsive to keepalive traffic. No streaming progress in 6a — flagged
in the README for 6c.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import anyio

from core.document import Document
from core.settings import Settings, load_settings
from mcp_server import errors
from mcp_server.schemas import TranscribeRequest, TranscribeResult
from workers.events import DoneEvent, ErrorEvent, WorkerEvent
from workers.transcription import (
    TranscriptionWorker,
    candidate_cache_path,
    try_load_cached_document,
)

_LOG = logging.getLogger(__name__)


def _resolve_settings(model: str | None, language: str | None) -> Settings:
    """Load on-disk Settings, then override the two fields the tool exposes.

    The MCP server reads Settings via the existing loader — the same one
    the GUIs use. It does not write Settings (that's GUI territory). When
    a tool kwarg is provided, it overrides the on-disk default for this
    call only.
    """
    s = load_settings()
    if model is not None:
        s.default_model = model
    if language is not None:
        s.default_language = language
    return s


async def transcribe(req: TranscribeRequest) -> TranscribeResult:
    source_path = Path(req.source_path)
    if not source_path.is_file():
        errors.raise_mcp(
            errors.FILE_NOT_FOUND,
            f"source_path does not exist or is not a file: {source_path}",
            data={"source_path": str(source_path)},
        )

    settings = _resolve_settings(req.model, req.language)
    requested_output: Path | None = (
        Path(req.output_path) if req.output_path is not None else None
    )

    # Cache fast-path. The GUI does this before constructing a Transcriber
    # so the model never downloads on a cache hit; we mirror it.
    cached = try_load_cached_document(settings, source_path)
    if cached is not None:
        cache_path = candidate_cache_path(settings, source_path)
        out_path = _ensure_output_at(cache_path, requested_output, copy_only=True)
        return _build_result(cached, out_path, cache_hit=True)

    # Cache miss — run the worker on a thread so the MCP event loop
    # keeps spinning. The worker writes the JSON sidecar to its
    # candidate path; if the user wanted it elsewhere we move it after.
    events: list[WorkerEvent] = []

    def on_event(ev: WorkerEvent) -> None:
        events.append(ev)

    worker = TranscriptionWorker(
        settings=settings,
        media_path=source_path,
        model_name=settings.default_model,
        language=settings.default_language,
        formats=["json"],
        on_event=on_event,
    )

    try:
        await anyio.to_thread.run_sync(worker.run)
    except Exception as exc:  # noqa: BLE001 — surface any unexpected raise
        errors.raise_mcp(
            errors.TRANSCRIPTION_FAILED,
            f"transcription worker raised: {exc}",
            data={"source_path": str(source_path)},
        )

    err = next((e for e in events if isinstance(e, ErrorEvent)), None)
    if err is not None:
        errors.raise_mcp(
            errors.TRANSCRIPTION_FAILED,
            err.message,
            data={"source_path": str(source_path)},
        )

    done = next((e for e in events if isinstance(e, DoneEvent)), None)
    if done is None or done.document is None:
        errors.raise_mcp(
            errors.TRANSCRIPTION_FAILED,
            "transcription worker finished without emitting a Document",
            data={"source_path": str(source_path)},
        )
    assert done is not None  # narrowing for type-checkers; raised above otherwise
    assert done.document is not None

    written_json = done.output_files.get("json")
    if written_json is None:
        errors.raise_mcp(
            errors.TRANSCRIPTION_FAILED,
            "transcription worker did not produce a JSON output file",
            data={"source_path": str(source_path)},
        )
    out_path = _ensure_output_at(written_json, requested_output, copy_only=False)
    return _build_result(done.document, out_path, cache_hit=False)


def _ensure_output_at(
    actual: Path, requested: Path | None, *, copy_only: bool
) -> Path:
    """Place the JSON at ``requested`` if asked, else return ``actual``.

    On cache hit ``copy_only=True`` keeps the original cache file
    untouched (the user might still want it where the GUI expects it).
    On cache miss ``copy_only=False`` moves the freshly-written file to
    the requested location and removes the worker's intermediate.
    """
    if requested is None or requested == actual:
        return actual
    requested.parent.mkdir(parents=True, exist_ok=True)
    if copy_only:
        shutil.copyfile(actual, requested)
    else:
        # ``shutil.move`` falls back to copy+unlink across filesystems.
        shutil.move(str(actual), str(requested))
    return requested


def _build_result(
    doc: Document, output_path: Path, *, cache_hit: bool
) -> TranscribeResult:
    duration = (
        float(next(iter(doc.sources.values())).duration) if doc.sources else 0.0
    )
    word_count = sum(len(seg.words) for seg in doc.segments)
    return TranscribeResult(
        output_path=str(output_path),
        duration_s=duration,
        word_count=word_count,
        language_detected=doc.language,
        cache_hit=cache_hit,
    )
