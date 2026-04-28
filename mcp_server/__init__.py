"""MCP server exposing the Transcribe pipeline to MCP clients.

The server is a third consumer of :mod:`core` and :mod:`workers`,
alongside the customtkinter GUI in :mod:`ui` and the PySide6 GUI in
:mod:`ui_qt`. It does not talk to either GUI; it operates on the same
``.transcribe.json`` files via :meth:`core.document.Document.from_json`
and :meth:`core.document.Document.to_json`.

Phase 6a — foundation: read, save, render. No analysis tools yet
(those are 6b); no batch / queue / streaming-progress (6c if ever).
"""

from __future__ import annotations

__version__ = "0.6.0a"
