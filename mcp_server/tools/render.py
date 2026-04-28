"""``render`` tool — wraps :class:`workers.render.RenderWorker`.

Synchronous from the client's perspective; the worker's ``run()``
executes on a worker thread via ``anyio.to_thread.run_sync``. No
streaming progress in 6a; no cancellation-from-client (Claude Desktop
killing the process is the only abort path, and ffmpeg cleanup is
covered by :class:`workers.render.RenderWorker._cleanup_partial`).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import anyio

from core.audio import get_duration
from core.settings import Settings, load_settings
from mcp_server import errors
from mcp_server.schemas import RenderRequest, RenderResult
from mcp_server.tools.document import _load_document
from workers.render import (
    RenderComplete,
    RenderError,
    RenderEvent,
    RenderWorker,
)

_LOG = logging.getLogger(__name__)


def _resolve_settings(req: RenderRequest) -> Settings:
    s = load_settings()
    if req.pad_lead is not None:
        s.default_pad_lead = float(req.pad_lead)
    if req.pad_trail is not None:
        s.default_pad_trail = float(req.pad_trail)
    if req.audio_fade_ms is not None:
        s.default_audio_fade_ms = int(req.audio_fade_ms)
    return s


async def render(req: RenderRequest) -> RenderResult:
    doc, _, _ = _load_document(req.json_path)
    settings = _resolve_settings(req)
    output_path = Path(req.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    events: list[RenderEvent] = []

    def on_event(ev: RenderEvent) -> None:
        events.append(ev)

    worker = RenderWorker(
        document=doc,
        output_path=output_path,
        settings=settings,
        on_event=on_event,
    )

    started = time.monotonic()
    try:
        await anyio.to_thread.run_sync(worker.run)
    except Exception as exc:  # noqa: BLE001
        errors.raise_mcp(
            errors.RENDER_FAILED,
            f"render worker raised: {exc}",
            data={"output_path": str(output_path)},
        )

    err = next((e for e in events if isinstance(e, RenderError)), None)
    if err is not None:
        errors.raise_mcp(
            errors.RENDER_FAILED,
            err.message,
            data={"output_path": str(output_path)},
        )

    complete = next((e for e in events if isinstance(e, RenderComplete)), None)
    if complete is None:
        errors.raise_mcp(
            errors.RENDER_FAILED,
            "render worker finished without emitting RenderComplete "
            "(may have been cancelled or short-circuited)",
            data={"output_path": str(output_path)},
        )

    if not output_path.is_file():
        errors.raise_mcp(
            errors.RENDER_FAILED,
            f"render reported complete but output file is missing: {output_path}",
            data={"output_path": str(output_path)},
        )

    try:
        duration = float(get_duration(output_path))
    except Exception:  # noqa: BLE001 — duration probe is best-effort
        duration = 0.0
    file_size = output_path.stat().st_size
    elapsed = time.monotonic() - started

    return RenderResult(
        output_path=str(output_path),
        duration_s=duration,
        file_size_bytes=file_size,
        render_time_s=elapsed,
    )
