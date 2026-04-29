# Phase 6a smoke checklist

Manual end-to-end verification through Claude Desktop. Targets the
seven 6a tools (`transcribe`, `load_document`, `get_transcript`,
`get_ranges`, `get_timeline`, `apply_cuts`, `restore_ranges`,
`render`) plus the v3 schema migration and the run-batched renderer.

The unit tests prove the server is correct. This checklist proves it's
*usable* end-to-end — that Claude on the other side of the MCP wire
understands the tool descriptions, picks the right tool, and produces
the documented output.

## Prerequisites

- Phase 6a has been built and committed. `python main_mcp.py` boots
  cleanly from a manual stdin handshake (the README documents how).
- `~/Library/Application Support/Claude/claude_desktop_config.json`
  has the entry from `mcp_server/README.md`. Restart Claude Desktop
  after editing the config — it spawns MCP servers on startup, not
  per-message.
- A short test source video exists. Either:
  - The repo's `tests/fixtures/synthetic.mp4` (30 s, H.264 + AAC,
    fast). Best for the cut/render test path.
  - A real podcast clip ≤ 5 minutes. Better for hearing audio
    fades at joins.

The transcript JSON sidecar will land at
`<source_dir>/<source_stem>.transcribe.json`.

## Steps

Run these as sequential prompts in a single Claude Desktop chat.
Expected outputs are paraphrased — Claude's exact wording will vary,
but the load-bearing values (paths, counts, durations) are what to
check against.

### 1. Confirm the server is registered

Prompt:

> "Which Transcribe tools are available?"

Expected: Claude lists fourteen tools (post-6b). The eight 6a tools
— `transcribe`, `load_document`, `get_transcript`, `get_ranges`,
`get_timeline`, `apply_cuts`, `restore_ranges`, `render` — plus the
six 6b-2 proposal-lifecycle tools (`propose_moves`, `list_proposals`,
`read_proposal`, `apply_proposal`, `list_apply_results`,
`read_apply_result`). If Claude says it has no Transcribe tools, the
config snippet didn't take or the server isn't booting; check
`~/Library/Logs/Claude/mcp*.log`.

### 2. Transcribe a fixture

Prompt:

> "Transcribe `<absolute path to your test video>` and tell me the
> language detected and total duration."

Expected:
- Claude calls `transcribe(source_path=...)`. First call runs
  inference (~seconds for synthetic.mp4, ~tens of seconds on `base`
  model for a 5-min podcast).
- Claude reports `output_path` (`<dir>/<stem>.transcribe.json`),
  `duration_s`, `word_count`, `language_detected`, and
  `cache_hit: false`.
- `<dir>/<stem>.transcribe.json` exists on disk afterwards, JSON-
  parseable, with `schema_version: 3.1` (post-6b-1).

### 3. Cache hit on second call

Prompt:

> "Transcribe the same file again — should be instant this time."

Expected: Claude calls `transcribe` again; response includes
`cache_hit: true`, `output_path` unchanged, no inference happens
(stderr log on the server side shows no model load).

### 4. Load and summarize

Prompt:

> "Load the .transcribe.json file you just produced and tell me how
> many words and how many ranges it has."

Expected: Claude calls `load_document`. Response: `path`,
`source_path`, `duration_s`, `word_count` matching step 2,
`range_count: 1` (a fresh transcript has one full-duration keep
range), `schema_version: 3.1`.

### 5. Read the timeline (v3 view)

Prompt:

> "Show me the timeline for that document."

Expected: Claude calls `get_timeline`. Response: a single clip whose
`source_start_s == 0.0` and `source_end_s` matches the source
duration. `is_source_monotonic: true`. `total_duration_s` matches the
source duration.

### 6. Read the ranges (v2-shaped view, lossy on rearranged docs)

Prompt:

> "What kept-ranges does that document have?"

Expected: Claude calls `get_ranges`. Response shape:
`{ranges: [{start_s, end_s, reason}], total_kept_s, total_cut_s,
is_source_monotonic: true}`. The flag here matches the timeline's.

### 7. Read part of the transcript

Prompt:

> "What are the first ten words of the transcript with their
> timestamps?"

Expected: Claude calls `get_transcript(json_path=..., include_struck=true)`
or similar, then summarizes. Returned shape: list of
`{word, start_s, end_s, segment_idx, struck}`. All `struck: false`
because nothing has been cut yet.

### 8. Apply a cut

Prompt:

> "Cut from the start of word 5 to the end of word 7 with reason
> 'pilot edit', and tell me the new range count."

Expected: Claude reads the relevant words' start/end (or asks for
clarification), calls `apply_cuts(json_path=..., cuts=[...])`.
Response: `applied_count: 1`, `skipped_count: 0`, two ranges in
`ranges_after`. The `.transcribe.json` on disk now has two clips.
Note: the cut's `reason` ("pilot edit") attaches to the kept range
*adjacent* to the cut (the range immediately preceding the cut, or
the range immediately following when the cut starts at t=0) — not
to the cut itself. v2 stores ranges, not cuts, so the reason rides
on the surviving keep-range.

### 9. Word-boundary violation

Prompt:

> "Cut between 0.13 seconds and 0.27 seconds." (Pick a span that
> definitely sits inside a word.)

Expected: Claude calls `apply_cuts` with non-word-boundary endpoints
and gets back an error. The response text starts with
`WORD_BOUNDARY_VIOLATION: …`. The `.transcribe.json` on disk is
unchanged from step 8.

### 10. Render

Prompt:

> "Render the cut version to `~/Desktop/transcribe-smoke.mp4`."

Expected: Claude calls `render(json_path=..., output_path=...)`.
Response includes `output_path`, `duration_s` (slightly less than
source duration because of the cut), `file_size_bytes > 0`,
`render_time_s` typically <1 s for synthetic.mp4 / a few seconds for
the podcast clip.

Verify the rendered file plays and is shorter than the source by
roughly the cut duration.

### 11. (Optional) Non-monotonic refusal

This step requires hand-editing the `.transcribe.json` to put a clip
out of source-time order — e.g., swap the first two clips' positions
in the `main_timeline.clips` array. Save, then prompt Claude:

> "Apply a cut from 0.0 to 0.5 to that document."

Expected: Claude calls `apply_cuts` and receives an error response
beginning with `EDIT_NOT_SUPPORTED: …`. The `.transcribe.json` is
unchanged. `get_timeline` on the same file shows
`is_source_monotonic: false`.

This step is the one that proves 6a's foundation for 6b — non-monotonic
documents load, are readable through MCP, and the editing surface
refuses cleanly rather than crashing.

## What success looks like

All steps land their expected outputs. Claude's tool selection should
feel obvious — the descriptions are explicit enough that the model
doesn't need user prodding to pick `get_timeline` over `get_ranges`
when playlist order matters.

If a step misses its expected output:
- Check `stderr` logs from the MCP server (Claude Desktop's
  per-server log file is the easiest source).
- Re-read the tool description to see if the model's reasoning is
  suggesting a different tool. If the wrong tool keeps getting picked,
  the description needs tightening (this is a real outcome — feedback
  for tightening Phase 6b's tool surface).

## Out of scope for this checklist

- Streaming progress for long renders (deferred to 6c if ever).
- Multi-source documents (post-6a).
- Re-arrangement commands (`apply_cuts` of `MoveClip` etc — Phase 6b).
- Performance benchmarking — see `scripts/smartcut_spike.py` for the
  baseline measurement against the heavy HEVC fixture.
