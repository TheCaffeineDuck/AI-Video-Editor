"""MCP server bootstrap — registers every tool, runs over stdio.

The server is a thin shim:

1. Build a :class:`mcp.server.Server` instance.
2. Register each tool's input schema (derived from a Pydantic model)
   and dispatch handler.
3. Run it on stdio under :func:`run_stdio`.

Logging goes to stderr — stdio MCP servers can't use stdout for logs
(it's the protocol channel). The format is
``[ISO timestamp] [level] [logger name] message``.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import anyio
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from pydantic import BaseModel, ValidationError

from mcp_server import __version__
from mcp_server.errors import raise_mcp
from mcp_server.schemas import (
    ApplyCutsRequest,
    ApplyCutsResult,
    DocumentSummary,
    GetTranscriptRequest,
    JsonPathRequest,
    RangesResult,
    RenderRequest,
    RenderResult,
    RestoreRangesRequest,
    RestoreResult,
    TimelineResult,
    TranscribeRequest,
    TranscribeResult,
    TranscriptResult,
)
from mcp_server.tools.document import (
    apply_cuts,
    get_ranges,
    get_timeline,
    get_transcript,
    load_document,
    restore_ranges,
)
from mcp_server.tools.render import render
from mcp_server.tools.transcribe import transcribe

_LOG = logging.getLogger(__name__)

SERVER_NAME = "transcribe"


@dataclass(frozen=True)
class ToolDef:
    """One row in the tool dispatch table.

    ``handler`` takes the validated input model and returns an output
    model; the wrapper in :func:`build_server` does the marshalling.
    """

    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: Callable[[BaseModel], Awaitable[BaseModel]]


# Build the dispatch table once. The order is the order tools appear in
# ``tools/list``; clients (Claude included) read the descriptions to
# decide when to call each tool, so put the lifecycle tools first.
TOOLS: tuple[ToolDef, ...] = (
    ToolDef(
        name="transcribe",
        description=(
            "Transcribe a video or audio file with faster-whisper, producing "
            "a .transcribe.json document that the editor and the other MCP "
            "tools can operate on. Synchronous: blocks until the worker "
            "finishes. On a cache hit (existing .transcribe.json with a "
            "matching source_hash) returns immediately without re-running "
            "inference."
        ),
        input_model=TranscribeRequest,
        output_model=TranscribeResult,
        handler=transcribe,  # type: ignore[arg-type]
    ),
    ToolDef(
        name="load_document",
        description=(
            "Read a .transcribe.json sidecar and return its summary "
            "metadata (path, source, duration, word count, range count, "
            "schema version, created-at). Use this to confirm a file is "
            "valid before calling get_transcript or get_ranges. Returns "
            "metadata only — the full transcript comes from get_transcript."
        ),
        input_model=JsonPathRequest,
        output_model=DocumentSummary,
        handler=load_document,  # type: ignore[arg-type]
    ),
    ToolDef(
        name="get_transcript",
        description=(
            "Return the word-level transcript with timings as a flat list. "
            "Each entry has word text, start_s, end_s, segment_idx, and a "
            "`struck` flag indicating whether the word's timing falls "
            "outside the current kept ranges (i.e., would be removed by "
            "render). Pass include_struck=False to omit struck words. "
            "This is the load-bearing tool for analysis tasks."
        ),
        input_model=GetTranscriptRequest,
        output_model=TranscriptResult,
        handler=get_transcript,  # type: ignore[arg-type]
    ),
    ToolDef(
        name="get_ranges",
        description=(
            "Return the kept ranges (the timeline that survives a render) "
            "with totals for kept and cut seconds. Use this to see what's "
            "already been edited before applying further cuts. NOTE: this "
            "tool flattens the playlist into source-time order and is lossy "
            "for non-monotonic (rearranged) v3 documents — the response's "
            "is_source_monotonic flag tells you whether the flattening "
            "discarded ordering information. For a faithful playlist view, "
            "use get_timeline instead."
        ),
        input_model=JsonPathRequest,
        output_model=RangesResult,
        handler=get_ranges,  # type: ignore[arg-type]
    ),
    ToolDef(
        name="get_timeline",
        description=(
            "Return the v3 main_timeline as an ordered list of clips in "
            "playlist order. Each clip carries source_path, "
            "source_start_s, source_end_s, and an optional reason string "
            "set by an edit command. is_source_monotonic indicates whether "
            "the playlist visits a single source in source-time order — "
            "True for every doc 6a editing produces, False for hand-built "
            "or 6b-rearranged fixtures."
        ),
        input_model=JsonPathRequest,
        output_model=TimelineResult,
        handler=get_timeline,  # type: ignore[arg-type]
    ),
    ToolDef(
        name="apply_cuts",
        description=(
            "Apply one or more cuts to a .transcribe.json document, "
            "writing the result back to the same path. Each cut is a "
            "{start_s, end_s, reason?} interval. Cut endpoints must align "
            "to word boundaries (or fall in pure silence between words) — "
            "WORD_BOUNDARY_VIOLATION is raised otherwise. All-or-nothing: "
            "if any cut fails validation, no file is written. Cuts that "
            "fully overlap an already-cut region are skipped (not errors). "
            "Refuses with EDIT_NOT_SUPPORTED when the document's timeline "
            "is non-monotonic (Phase 6b will lift this restriction)."
        ),
        input_model=ApplyCutsRequest,
        output_model=ApplyCutsResult,
        handler=apply_cuts,  # type: ignore[arg-type]
    ),
    ToolDef(
        name="restore_ranges",
        description=(
            "Re-insert one or more previously-cut intervals into the kept "
            "timeline of a .transcribe.json document. Inverse of apply_cuts; "
            "same word-boundary constraint and same all-or-nothing semantics. "
            "Restores that are already fully kept are skipped. Refuses with "
            "EDIT_NOT_SUPPORTED on non-monotonic timelines."
        ),
        input_model=RestoreRangesRequest,
        output_model=RestoreResult,
        handler=restore_ranges,  # type: ignore[arg-type]
    ),
    ToolDef(
        name="render",
        description=(
            "Render a .transcribe.json document's kept ranges to a media "
            "file at output_path. Wraps smartcut + the audio-fade pass; "
            "container is inferred from the output extension. pad_lead / "
            "pad_trail / audio_fade_ms default to the user's Settings; "
            "pass them to override per-call."
        ),
        input_model=RenderRequest,
        output_model=RenderResult,
        handler=render,  # type: ignore[arg-type]
    ),
)


def _tool_descriptors() -> list[types.Tool]:
    """Marshal each :class:`ToolDef` into an :class:`mcp.types.Tool`.

    ``inputSchema`` and ``outputSchema`` are produced by Pydantic's
    JSON-Schema emitter directly. Pydantic emits draft-2020 schemas with
    ``additionalProperties: false`` (because every model declares
    ``ConfigDict(extra="forbid")``) so Claude's tool-call validator
    rejects unexpected keys eagerly.
    """
    out: list[types.Tool] = []
    for tool in TOOLS:
        out.append(
            types.Tool(
                name=tool.name,
                description=tool.description,
                inputSchema=tool.input_model.model_json_schema(),
                outputSchema=tool.output_model.model_json_schema(),
            )
        )
    return out


def build_server() -> Server:
    """Construct the MCP server with every tool registered.

    Pulled out of :func:`run_stdio` so tests can introspect tool
    metadata and exercise dispatch without spinning the stdio transport.
    """
    server: Server = Server(SERVER_NAME, version=__version__)
    by_name = {t.name: t for t in TOOLS}

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return _tool_descriptors()

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = by_name.get(name)
        if tool is None:
            raise_mcp(
                "UNKNOWN_TOOL",
                f"unknown tool: {name!r}",
                data={"tool": name},
            )
        try:
            req = tool.input_model.model_validate(arguments)
        except ValidationError as exc:
            raise_mcp(
                "INVALID_DOCUMENT",
                f"invalid arguments for {name}: {exc}",
                data={"tool": name, "errors": exc.errors()},
            )
        _LOG.info("tool=%s start", name)
        try:
            result = await tool.handler(req)
        except McpError:
            raise
        except Exception:
            _LOG.exception("tool=%s unhandled exception", name)
            raise
        _LOG.info("tool=%s done", name)
        if not isinstance(result, tool.output_model):
            raise TypeError(
                f"tool {name!r} returned {type(result).__name__}, "
                f"expected {tool.output_model.__name__}"
            )
        return result.model_dump(mode="json")

    return server


def configure_stderr_logging(level: int = logging.INFO) -> None:
    """Route logs to stderr in the documented format.

    Idempotent: re-running it on a configured logger replaces the
    handler rather than stacking duplicates. stdout is reserved for the
    JSON-RPC protocol channel — log there and the client misparses every
    message.
    """
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    root.addHandler(handler)
    root.setLevel(level)


async def run_stdio() -> None:
    """Run the server over stdio until the client disconnects.

    Used by :mod:`main_mcp` as the process entry. Tests don't invoke
    this — they call :func:`build_server` and exercise the dispatch
    handlers directly.
    """
    configure_stderr_logging()
    server = build_server()
    _LOG.info("starting %s mcp server (v%s)", SERVER_NAME, __version__)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> int:
    """Sync entry point. ``main_mcp.py`` calls into here."""
    try:
        anyio.run(run_stdio)
    except KeyboardInterrupt:  # pragma: no cover — interactive only
        return 130
    return 0
