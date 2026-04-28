"""Stdio entry point for the Transcribe MCP server.

Run from the repo root via ``./.venv/bin/python main_mcp.py``. The
process speaks MCP over stdin/stdout per Anthropic's official Python
SDK; logs go to stderr. See ``mcp_server/README.md`` for the
Claude Desktop integration recipe.
"""

from __future__ import annotations

from mcp_server.server import main

if __name__ == "__main__":
    raise SystemExit(main())
