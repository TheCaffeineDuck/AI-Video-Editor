"""Tool implementations for the MCP server.

Each tool is an async function that takes a validated Pydantic input
model and returns a Pydantic output model. The dispatch table in
:mod:`mcp_server.server` maps tool names to handlers.
"""
