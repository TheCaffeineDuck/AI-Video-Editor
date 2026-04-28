# Transcribe MCP server

A stdio MCP server that exposes Transcribe's pipeline (transcribe → read
→ cut → render) to any MCP client. The server reads and writes the same
`.transcribe.json` files the GUIs use; it does not talk to the GUIs and
does not need them running.

Phase 6a ships seven tools: lifecycle (`transcribe`, `render`), read
(`load_document`, `get_transcript`, `get_ranges`), and edit (`apply_cuts`,
`restore_ranges`). Analysis tools (filler-word detection, silence
detection, etc.) are 6b. Batch and streaming-progress are 6c if ever.

## Install in Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`
and add an entry under `mcpServers`. Use absolute paths — Claude Desktop
spawns the process from its own working directory, not yours.

```json
{
  "mcpServers": {
    "transcribe": {
      "command": "/Users/<you>/Desktop/Transcribe/.venv/bin/python",
      "args": ["/Users/<you>/Desktop/Transcribe/main_mcp.py"],
      "cwd": "/Users/<you>/Desktop/Transcribe"
    }
  }
}
```

Restart Claude Desktop. The hammer icon next to the chat input should
show seven new tools under "transcribe."

To verify by hand without Claude Desktop, pipe an `initialize` +
`tools/list` request through the script:

```bash
./.venv/bin/python main_mcp.py
# then paste:
{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}
```

The `tools/list` response lists all seven tools with their schemas.

## Tool surface (one-liners)

| Tool              | What it does                                                                |
|-------------------|-----------------------------------------------------------------------------|
| `transcribe`      | Run faster-whisper on a video/audio file → `.transcribe.json`. Cache-aware. |
| `load_document`   | Summarize a `.transcribe.json` (path, duration, word count, range count).   |
| `get_transcript`  | Word-level transcript with timings + per-word `struck` flag.                |
| `get_ranges`      | List the kept ranges and totals for kept/cut seconds.                       |
| `apply_cuts`      | Apply one or more cuts to the timeline. Word-boundary-validated.            |
| `restore_ranges`  | Inverse of `apply_cuts` — re-insert previously cut intervals.               |
| `render`          | Render the document's kept ranges to a media file via smartcut.             |

Cut endpoints must align to word boundaries (or fall in pure silence
between words). Misaligned endpoints raise `WORD_BOUNDARY_VIOLATION`
without writing anything to disk — `apply_cuts` is all-or-nothing.

## Example prompts

In Claude Desktop, with the server installed:

> "Transcribe `/Users/me/Movies/talk.mp4`, then show me the transcript
> and tell me the total duration."

> "Open `/Users/me/Movies/talk.transcribe.json` and cut the first
> sentence (start at 0, end at the timestamp of the first comma)."

> "Render `/Users/me/Movies/talk.transcribe.json` to
> `~/Desktop/talk.cut.mp4`."

## Error codes

Stable strings the client can branch on. The MCP SDK converts every
handler exception into a `CallToolResult` with `isError: true` and a
text content block — that text content always starts with the code
followed by a colon (`FILE_NOT_FOUND: …`). Clients parse the prefix to
recover the code; the underlying message gives the human details.

| Code                       | Meaning                                                            |
|----------------------------|--------------------------------------------------------------------|
| `FILE_NOT_FOUND`           | A path argument doesn't exist on disk.                             |
| `INVALID_DOCUMENT`         | JSON parse failure or argument-shape failure.                      |
| `UNSUPPORTED_SCHEMA`       | The document's `schema_version` is null/missing/unknown.           |
| `WORD_BOUNDARY_VIOLATION`  | A cut/restore endpoint sits inside a word.                         |
| `CUT_INVALID`              | A cut interval is geometrically impossible (e.g. end < start).     |
| `TRANSCRIPTION_FAILED`     | The transcription worker raised; original message in `message`.    |
| `RENDER_FAILED`            | The render worker raised; original message in `message`.           |

These codes are part of the contract — they don't change once 6b
starts depending on them.

## Logging

stderr only. stdout is the JSON-RPC channel; logging there breaks the
protocol. Format: `[ISO timestamp] [level] [logger name] message`.
INFO covers tool entry/exit; switch the root level to DEBUG for the
underlying worker chatter.

## Concurrency

Last writer wins. The MCP server and either GUI may both have a
`.transcribe.json` open at the same time, and the server's writes are
not coordinated with the GUI's. If both write at the same moment, one
of the two changes is lost — there is no file lock, no conflict
detection, no merge. The MCP server reads-modifies-writes
atomically-per-tool-call, so a partial JSON cannot land on disk; but a
tool call that started reading before a GUI save finishes will overwrite
the GUI save when it persists. Surface this as a 6c question if the
workflow demands "two writers at once."
