# Transcribe — Project State Report

**Date:** 2026-04-29
**Branch:** main
**Commit:** Phase 6a — MCP server foundation (transcribe, read, cut, render)
**Status:** Phase 6a complete. All 559 tests passing (529 prior + 30 6a).
Lint clean for changed files.

---

## 1. Phase 6a in two paragraphs

Phase 6a stands up an MCP server next to the existing tkinter and Qt
GUIs. It is a third consumer of `core/` and `workers/`, not a peer of
either GUI; it speaks JSON-RPC over stdio per Anthropic's official
Python `mcp` SDK and operates on the same `.transcribe.json` files the
editor reads and writes. Seven tools land — `transcribe`, `load_document`,
`get_transcript`, `get_ranges`, `apply_cuts`, `restore_ranges`, `render`
— covering the lifecycle (transcribe → render), the read side (summary,
words, ranges), and the edit side (cut, restore). Every tool's
input/output is a Pydantic model whose JSON Schema becomes the SDK's
`inputSchema`/`outputSchema`. The MCP layer reuses `TranscriptionWorker`
and `RenderWorker` directly, off-loading their synchronous `run()`
calls to a worker thread via `anyio.to_thread.run_sync` so the protocol
event loop stays live.

The architecture decision was to keep `core/` strictly read-only from
the MCP layer (same rule as Phase 5). The `reason` field that the spec
flagged as a possible `core/` change turned out to already exist on
`AddCut` (default `"manual"`) — no edit to `core/editing.py` was
needed. The MCP server adds a layer of stable error codes
(`FILE_NOT_FOUND`, `INVALID_DOCUMENT`, `UNSUPPORTED_SCHEMA`,
`WORD_BOUNDARY_VIOLATION`, `CUT_INVALID`, `TRANSCRIPTION_FAILED`,
`RENDER_FAILED`) that prefix every error message — clients branch on
the prefix because the MCP SDK collapses tool exceptions into
`isError: true` content blocks rather than into JSON-RPC-level errors.

---

## 2. Project structure (deltas from 5f)

```
.
├── core/                            # unchanged in 6a
├── workers/                         # unchanged in 6a
├── ui/                              # unchanged
├── ui_qt/                           # unchanged
├── mcp_server/                      # NEW (6a)
│   ├── __init__.py
│   ├── server.py                    # tool registry, build_server, run_stdio
│   ├── schemas.py                   # Pydantic input/output models per tool
│   ├── errors.py                    # stable error codes + raise_mcp helper
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── transcribe.py            # wraps TranscriptionWorker
│   │   ├── document.py              # load, transcript, ranges, cuts, restore
│   │   └── render.py                # wraps RenderWorker
│   └── README.md                    # Claude Desktop install + tool surface
├── main_mcp.py                      # NEW (6a) — stdio entry, mirrors main_qt.py
├── tests/test_phase_6a.py           # NEW (6a) — 30 tests
├── main.py / main_qt.py             # unchanged; both still launch
├── requirements.txt                 # +mcp>=1.27
└── …
```

---

## 3. Dependencies (delta)

`mcp>=1.27` added (pulls in pydantic v2 ≥2.7, anyio, jsonschema —
which were already transitively present via PySide6 / faster-whisper /
smartcut).

---

## 4. Code inventory (deltas from 5f)

| File | Lines | What's new in 6a |
|------|------:|------------------|
| `mcp_server/__init__.py` | 15 | NEW — `__version__ = "0.6.0a"` and a one-paragraph package docstring. |
| `mcp_server/server.py` | 293 | NEW — `ToolDef` dataclass; `TOOLS` registry of 7 tools; `_tool_descriptors()` marshals each Pydantic model to `mcp.types.Tool`; `build_server()` registers `list_tools` and `call_tool` decorators with input validation, dispatch, and structured-content marshalling; `configure_stderr_logging()` routes logs off stdout; `run_stdio()` is the async entry. |
| `mcp_server/schemas.py` | 259 | NEW — Pydantic models for every tool's request and response; all use `ConfigDict(extra="forbid")` so `additionalProperties: false` propagates into Claude's tool-call validator. |
| `mcp_server/errors.py` | 63 | NEW — frozen string codes; `raise_mcp(code, msg, data)` builds an `McpError` with a JSON-RPC integer code (INVALID_PARAMS for client-fixable, INTERNAL_ERROR for worker failures) and a `<CODE>: ...` message prefix that survives the SDK's exception-to-content collapse. |
| `mcp_server/tools/document.py` | 414 | NEW — `_load_document` / `_save_document` shared helpers; `_word_boundary_set` + `_is_word_boundary` enforce the "Never cut inside a word" invariant at the MCP layer (silence between words is allowed); `apply_cuts` validates every cut before mutating, then runs them through `AddCut` on a fresh `CommandStack`, with skip semantics for cuts inside existing cuts; `restore_ranges` is the symmetric inverse via `RestoreRange`. |
| `mcp_server/tools/transcribe.py` | 159 | NEW — wraps `TranscriptionWorker` with `formats=["json"]`, off-loads `worker.run()` via `anyio.to_thread.run_sync`; cache-hit fast-path mirrors the GUI's `try_load_cached_document`; `output_path` override copies (cache hit) or moves (cache miss) the produced file to the requested location. |
| `mcp_server/tools/render.py` | 108 | NEW — wraps `RenderWorker` similarly; pad/fade kwargs override Settings per-call; output metadata (file size, duration, render time) populated post-write. |
| `mcp_server/README.md` | 114 | NEW — Claude Desktop integration recipe (`claude_desktop_config.json` snippet), tool one-liners, error-code table, logging/concurrency notes. |
| `main_mcp.py` | 14 | NEW — sync entry that imports `mcp_server.server.main`. |
| `tests/test_phase_6a.py` | 667 | NEW — 30 tests covering tool registration, schema marshalling, every error path, all-or-nothing semantics for `apply_cuts`/`restore_ranges`, cache-hit and cache-miss for `transcribe` (worker mocked), happy-path and failure-path for `render` (worker mocked). |
| `requirements.txt` | 6 | `+mcp>=1.27`. |

### Test count

| Phase | Total | Fast | Slow |
|-------|------:|-----:|-----:|
| End of 5e   | 501 | 489 | 12 |
| End of 5f   | 529 | 517 | 12 |
| **End of 6a** | **559** | **547** | **12** |

`pytest -q` runs all 559 green in ~15 s on this M4.

---

## 5. Git history

```
phase 6a: mcp server foundation (transcribe, read, cut, render)  (this commit)
phase 5f: macos polish, menu bar, quit guard, render ux
phase 5e: render export, autosave, splitter persistence, settings completion
phase 5d: waveform strip with cache and dim regions
phase 5c: transcript interactivity, cuts, undo, save
phase 5b: editor pane skeleton + qmediaplayer wiring
phase 5a: qt scaffold + port transcribe flow
…
```

---

## 6. Public APIs added in Phase 6a

```python
# mcp_server — top-level
__version__: str  # "0.6.0a"

# mcp_server.server
SERVER_NAME: str  # "transcribe"
TOOLS: tuple[ToolDef, ...]
def build_server() -> mcp.server.Server: ...
def configure_stderr_logging(level: int = logging.INFO) -> None: ...
async def run_stdio() -> None: ...
def main() -> int: ...

@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    input_model: type[pydantic.BaseModel]
    output_model: type[pydantic.BaseModel]
    handler: Callable[[BaseModel], Awaitable[BaseModel]]

# mcp_server.errors — string codes (frozen contract)
FILE_NOT_FOUND, INVALID_DOCUMENT, UNSUPPORTED_SCHEMA: str
WORD_BOUNDARY_VIOLATION, CUT_INVALID: str
TRANSCRIPTION_FAILED, RENDER_FAILED: str
def raise_mcp(code: str, message: str, data: dict | None = None) -> NoReturn: ...

# mcp_server.schemas — Pydantic models, one per tool I/O
class TranscribeRequest / TranscribeResult: ...
class JsonPathRequest / DocumentSummary: ...
class GetTranscriptRequest / TranscriptResult / TranscriptWord: ...
class RangesResult / RangeOut: ...
class ApplyCutsRequest / CutRequest / ApplyCutsResult: ...
class RestoreRangesRequest / RestoreRequestItem / RestoreResult: ...
class RenderRequest / RenderResult: ...

# mcp_server.tools.{transcribe,document,render} — async handlers
async def transcribe(req: TranscribeRequest) -> TranscribeResult: ...
async def load_document(req: JsonPathRequest) -> DocumentSummary: ...
async def get_transcript(req: GetTranscriptRequest) -> TranscriptResult: ...
async def get_ranges(req: JsonPathRequest) -> RangesResult: ...
async def apply_cuts(req: ApplyCutsRequest) -> ApplyCutsResult: ...
async def restore_ranges(req: RestoreRangesRequest) -> RestoreResult: ...
async def render(req: RenderRequest) -> RenderResult: ...
```

---

## 7. What's solid

1. **Tool registration is data-driven.** The `TOOLS` tuple is the
   single source of truth — `_tool_descriptors()` derives both the
   advertised `inputSchema`/`outputSchema` (from Pydantic) and the
   dispatch table. Adding a 6b tool is a new `ToolDef` row; no
   server-bootstrap edit required.
2. **Stable error codes survive the SDK's collapse.** The MCP SDK
   converts every handler exception into `CallToolResult(isError=True,
   content=[TextContent(...)])` rather than into a JSON-RPC error
   response. Clients can't read `data.code`, but the message always
   begins `<CODE>: ...`, so prefix-parsing keeps the contract intact.
   The unit tests assert on the prefix; the README documents it.
3. **Word-boundary validation matches the renderer's invariant.**
   `_is_word_boundary` accepts a timestamp if it (a) matches a word
   start or end within 1 ms, or (b) falls in pure silence between
   words. This mirrors `core.render._snap_cuts_to_word_boundaries`'s
   behaviour ("cuts that don't overlap any word pass through
   unchanged") so MCP cuts that pass validation here are guaranteed to
   render cleanly.
4. **All-or-nothing for `apply_cuts` and `restore_ranges`.** Validation
   over every requested interval runs *before* any mutation; if any
   one fails, the file on disk is untouched. The tests confirm this by
   reading the bytes pre-call and asserting equality post-error.
5. **Workers run off the event loop.** `anyio.to_thread.run_sync`
   pushes `TranscriptionWorker.run()` and `RenderWorker.run()` to a
   thread, so the MCP read loop keeps draining stdin during a long
   render. No new thread management code lives in `mcp_server/` —
   anyio handles it.
6. **End-to-end stdio handshake verified.** Manually piped
   `initialize` + `tools/list` + `tools/call(load_document)` through
   `python main_mcp.py` and read structured JSON back. All 7 tools
   list, load_document round-trips, and the FILE_NOT_FOUND error path
   surfaces with the documented prefix.

---

## 8. What's fragile or worth knowing (6a additions)

1. **`output_schema` validation can reject string-typed fields if
   strict.** The MCP SDK runs `jsonschema.validate` against the
   `outputSchema` after the handler returns. Pydantic emits
   `additionalProperties: false`; if the handler ever forgot a field
   on a response model, validation would fail with a generic message.
   The tests cover happy paths but not "missing field" — adding an
   output validation regression test if 6b tweaks any model is cheap
   insurance.
2. **Cache hit assumes Settings.output_dir is None.** When
   `Settings.output_dir` is set (user has chosen a custom output
   folder in the GUI), `try_load_cached_document` looks there for the
   sidecar. The MCP server inherits the same Settings, so a user with
   a non-default output_dir gets cache hits keyed off that folder —
   consistent with the GUI's behaviour, but worth a flag if a future
   tool wants source-relative caching irrespective of Settings.
3. **`output_path` override on cache hit copies, not moves.** If the
   user passes `output_path` for `transcribe` while a cache file
   exists at the candidate path, the MCP server copies the cache file
   to the requested path, leaving the original in place. That's the
   right behaviour for the editor's "load from your normal cache path"
   workflow but might surprise a workflow that expects a single
   canonical location. Documented in the schema's field description.
4. **No streaming progress in 6a.** Synchronous from the client's
   perspective. A long transcribe (25-min podcast on `medium` model
   ~ 8 min) blocks the chat with no in-progress feedback beyond the
   stderr log on the server side. 6c spec: surface progress through
   MCP's `notifications/progress` channel.
5. **Multi-source documents are rejected at the cut/restore layer.**
   `_primary_source_id` raises `INVALID_DOCUMENT` if `len(doc.sources)
   != 1`. Same constraint `core.render` enforces. Multi-source MCP
   support waits on multi-source render (Phase 5+'s deferred scope).
6. **`render` reports `render_time_s` from the MCP-layer wall clock,
   not the worker's elapsed.** The two diverge by the thread-handoff
   overhead (~milliseconds). Close enough for client telemetry; if a
   future tool wants exact worker-elapsed, plumb `RenderComplete.elapsed`
   through.
7. **`mcp_server/server.py` does not advertise `serverCapabilities.tools.listChanged=True`.**
   The tool list is static at process start; 6b/6c can flip this on
   when dynamic registration lands.

---

## 9. Phase 6a stop-and-report (per spec)

**1. `core/` change for `reason` field.**

No `core/` change. `AddCut` already had `reason: str = "manual"`
(see [core/editing.py:83](core/editing.py)). The MCP `apply_cuts`
handler propagates `CutRequest.reason` straight into the
`AddCut(..., reason=...)` constructor; if the client omits it we keep
the existing `"manual"` default. The spec's flag was conservative —
worth confirming.

**2. Settings load path.**

The existing `core.settings.load_settings()` Just Worked. It honours
`WHISPER_SETTINGS_DIR` env var → falls back to
`~/Library/Application Support/WhisperTranscriber/settings.json` on
macOS. No refactor was needed; the loader has no GUI dependencies, so
the MCP layer calls it directly. Per-tool override kwargs (`model`,
`language`, `pad_lead`, etc.) mutate the loaded Settings dataclass
in-place for that one call — Settings is a dataclass, not a singleton,
so there's no cross-call leakage.

**3. Worker-in-MCP integration.**

`anyio.to_thread.run_sync(worker.run)`. The worker's `run()` is
synchronous and CPU-bound (transcription) or subprocess-bound (render),
so a thread is the right primitive. Cancellation: there is no
client-driven cancellation in 6a. If Claude Desktop kills the MCP
process mid-render, the OS reaps the parent, the worker thread (a
daemon) dies with it, and any ffmpeg subprocesses inherit the
parent-PID-died signal — they get SIGTERM/SIGHUP and exit, leaving
partial output behind. That partial-file cleanup is what
`workers.render.RenderWorker._cleanup_partial` exists to do, but it
only runs on the *cancelled* path, not on process kill. A 6c improvement:
register an atexit hook that unlinks any partial render output, or
ship a separate cancellation tool.

**4. Manual end-to-end smoke.**

Stdio handshake verified at the protocol level (see §7.6 above) —
piped `initialize`/`tools/list`/`tools/call(load_document)` and read
back structured JSON. Errors round-trip through the documented
prefix path (FILE_NOT_FOUND on a bogus path).

End-to-end via Claude Desktop **is the spec's required smoke** but
requires the user to (a) restart Claude Desktop with the
`claude_desktop_config.json` entry and (b) drive the workflow
themselves. The README has the config snippet ready; this commit's
done-when leaves the literal "open Claude Desktop and try it" step
to the user as the final acceptance test. The proxy I ran (raw stdio
RPC) exercises the same protocol surface a Claude Desktop session
would; if anything failed in the chat-driven flow, it'd be a UX layer
above the protocol, not a wire-format issue.

**5. Tool schema surprises.**

Pydantic v2 `model_json_schema()` produces clean draft-2020 schemas;
the MCP SDK accepts them verbatim. One concrete improvement worth
shipping in 6b: descriptive examples in `Field(..., examples=[...])`.
Claude reads tool descriptions to decide which tool to call but
doesn't currently see examples on individual fields. The MCP spec
will eventually surface these; until then, embedding example values
in the field's `description` text is the pragmatic workaround.

The other gotcha: `extra="forbid"` strict-mode is non-default.
Without it, the JSON Schema would emit `additionalProperties:
true`, and Claude would happily pass through extra keys (like a
`reason` on a `RestoreRequestItem` that doesn't accept one) without
the SDK's input validator catching them. Forbidding extras at the
Pydantic layer surfaces those issues at request time instead of as
silent ignored fields.

**6. Concurrency / file-locking.**

Failure modes considered:
- GUI saves while MCP `apply_cuts` reads: MCP gets the pre-save
  Document, applies cuts, writes to disk. The GUI's save lands first
  (or the MCP's, depending on timing); whichever wrote second wins.
  The user's GUI undo stack has no record of the MCP edit, so on the
  next dirty-save the GUI overwrites the MCP edit silently.
- MCP `apply_cuts` mid-write while GUI saves: file is briefly empty
  (Python's `Path.write_text` is non-atomic). If GUI tries to read
  during that window, it'd see an empty file and fail to parse —
  but the GUI's save path is `write_text`, not load, so this specific
  collision only matters if the GUI is *autosaving* and the autosave
  reads the on-disk version. Today's autosave doesn't read; it writes
  the in-memory Document. So the failure mode is "MCP writes nothing,
  GUI overwrites with its in-memory copy" — i.e., MCP edits get lost.
- Two MCP tool calls simultaneously: the SDK serializes calls per
  session, so this can't happen with one client. With two clients
  (e.g., two Claude Desktop chats), it can — and the same "last
  writer wins" applies. No mutex.

For 6a "last writer wins" is acceptable. If the workflow demands
"two writers at once" in 6c, the right tool is a sidecar `.lock` file
(or, less desirably, fcntl flock — which is per-process so the Qt and
MCP processes would actually have to cooperate).

**7. Smallest analysis tool 6b should ship.**

`find_silences(json_path, min_duration_s=0.5) -> list[{start_s, end_s, duration_s}]`.

Reasoning: every other interesting analysis (filler-word detection,
redundant-take detection, sentence-level pacing) requires either an
external model or a non-trivial heuristic, and most of them are
"sometimes correct" tools where the user has to verify the output
anyway. Silence detection, by contrast, is mechanical — gaps between
word.end and the next word.start where the gap exceeds a threshold
— and the result is unambiguously actionable. If 6b ships only this
one tool, the workflow becomes: "transcribe → ask Claude to find
silences over 0.5 s → apply_cuts on the list → render." That's the
core podcast-cleanup loop with zero subjective judgement, and it's
the demonstrably-useful thing the MCP layer adds beyond what the GUI
already has. Filler detection is a more impressive demo but harder
to get right; silence detection is the no-arguments minimum viable
analysis tool.

---

## 10. Definition-of-done checklist (6a)

- [x] `mcp_server/` package implemented; all 7 tools functional.
- [x] `python main_mcp.py` boots a stdio server (verified by piping
      `initialize` + `tools/list` and reading the response).
- [x] Installable in Claude Desktop via `mcp_server/README.md`'s
      recipe. (Verified via raw stdio handshake; in-Claude smoke is
      the user's final acceptance test, see §9.4.)
- [x] All 529 prior tests pass plus 30 new = 559.
- [x] `python main.py` and `python main_qt.py` still launch (import
      smoke verified).
- [x] No `core/` changes (the `reason` field already existed; see §9.1).
- [x] Ruff clean for changed files (`mcp_server/`, `main_mcp.py`,
      `tests/test_phase_6a.py`).
- [x] STATE.md overwritten — Phase 6a complete.
- [x] Single commit: `phase 6a: mcp server foundation
      (transcribe, read, cut, render)`.
