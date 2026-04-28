"""Stable error codes for the MCP server's tool surface.

These string codes are part of the contract — clients (Claude, or any
other MCP consumer) branch on them. Don't rename or repurpose once 6b
starts depending on them; add new codes instead.

The MCP protocol carries an integer ``code`` field on errors (JSON-RPC
convention) plus a free-form ``message`` and optional ``data`` payload.
We map every server-side failure to one of the codes below; the integer
is always :data:`mcp.types.INVALID_PARAMS` for client-fixable errors and
:data:`mcp.types.INTERNAL_ERROR` for "the server tried, the worker
failed" errors. The string code lives in ``message`` (prefix) and
``data["code"]`` so clients have a stable key to switch on.
"""

from __future__ import annotations

from typing import Any, NoReturn

from mcp import types
from mcp.shared.exceptions import McpError

# Client-fixable: bad path, bad schema, malformed request.
FILE_NOT_FOUND = "FILE_NOT_FOUND"
INVALID_DOCUMENT = "INVALID_DOCUMENT"
UNSUPPORTED_SCHEMA = "UNSUPPORTED_SCHEMA"
WORD_BOUNDARY_VIOLATION = "WORD_BOUNDARY_VIOLATION"
CUT_INVALID = "CUT_INVALID"

# Server-side: a worker raised. Surface the underlying message so the
# client (or the human reading the chat) has something to act on.
TRANSCRIPTION_FAILED = "TRANSCRIPTION_FAILED"
RENDER_FAILED = "RENDER_FAILED"

_CLIENT_CODES = frozenset(
    {
        FILE_NOT_FOUND,
        INVALID_DOCUMENT,
        UNSUPPORTED_SCHEMA,
        WORD_BOUNDARY_VIOLATION,
        CUT_INVALID,
    }
)


def raise_mcp(code: str, message: str, data: dict[str, Any] | None = None) -> NoReturn:
    """Raise :class:`McpError` with our stable code + a JSON-RPC integer.

    ``message`` is prefixed with the string code so a client that doesn't
    parse ``data`` still gets a recognizable label. ``data["code"]`` is
    the canonical machine-readable channel.
    """
    rpc_code = types.INVALID_PARAMS if code in _CLIENT_CODES else types.INTERNAL_ERROR
    payload: dict[str, Any] = {"code": code}
    if data:
        payload.update(data)
    raise McpError(
        types.ErrorData(
            code=rpc_code,
            message=f"{code}: {message}",
            data=payload,
        )
    )
