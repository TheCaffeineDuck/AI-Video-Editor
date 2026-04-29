# Phase 6c smoke checklist (highlight propose → render → GUI inspect)

Manual end-to-end verification of the highlight lifecycle: an LLM
proposes, the renderer cuts + reframes + (optionally) burns captions,
the GUI shows the result, the user opens the .mp4. Targets the six
6c-2 highlight-lifecycle tools (`propose_highlights`,
`list_highlights`, `read_highlight`, `apply_highlight`,
`list_highlight_renders`, `read_highlight_render`) plus the GUI
Highlights panel from 6c-3 and the v2 highlight schema's
source-keyed stale guard from Section A.

The highlight path is **read-only at the GUI**: the user does not
review highlights for accept/reject. Claude proposes, Claude (or the
user via the panel) renders, the user opens the mp4. If a highlight
is wrong the user re-proposes via Claude Desktop, not via the GUI.

The unit + integration tests in `tests/test_phase_6c1.py` /
`tests/test_phase_6c2.py` / `tests/test_phase_6c3.py` cover the
mechanical surface. This checklist proves a remote agent (Claude
Desktop) can drive the lifecycle end-to-end and the GUI surfaces
the result without manual fix-up.

## Prerequisites

- Phase 6c is built and committed. `python main_mcp.py` boots
  cleanly and lists 20 tools (8 from 6a + 6 from 6b-2 + 6 from
  6c-2). `python main_qt.py` opens.
- Claude Desktop's `claude_desktop_config.json` has the
  `transcribe` MCP server configured, restarted since the 6c
  build.
- A test `.transcribe.json` exists on disk for a media file that
  contains identifiable faces in roughly-frontal framing (any
  podcast / interview shot will do). The 6c smoke fixture used
  `/Volumes/Aaron 4TB/531 Podcast Aaron & Barret Autocut only.mp4`,
  but anything with a clear face works.

## Steps

### 1. Confirm twenty tools are registered

Prompt:

> "Which Transcribe tools are available?"

Expected: Claude lists 20. Section the names:

- 8 from 6a: `transcribe`, `load_document`, `get_transcript`,
  `get_ranges`, `get_timeline`, `apply_cuts`, `restore_ranges`,
  `render`.
- 6 from 6b-2: `propose_moves`, `list_proposals`, `read_proposal`,
  `apply_proposal`, `list_apply_results`, `read_apply_result`.
- 6 from 6c-2: `propose_highlights`, `list_highlights`,
  `read_highlight`, `apply_highlight`, `list_highlight_renders`,
  `read_highlight_render`.

If the count is 14, the new build hasn't been picked up — quit
and re-launch Claude Desktop.

### 2. Author two highlights at once

Prompt:

> "Propose two highlights against `<doc path>`: a 15-second hook
> around 2:00 and a 45-second narrative around 5:00. Use
> speaker-locked reframing for both; turn captions on for the
> first one only."

Expected: Claude calls `propose_highlights` once with two specs
in the array. The response carries an `highlights` list with two
entries; each has a `highlight_id` (timestamp-prefixed,
`YYYYMMDDTHHMMSS-<8 hex>`) and a `json_path` pointing into
`<doc>.highlights/`.

### 3. List + inspect

Prompt:

> "What highlights have been authored?"

Expected: `list_highlights` returns both ids in chronological
order. Each entry shows source-time span, `reframe_mode`,
`captions_enabled`, `reason`, and `rendered_output_path: null`
(no render has run yet).

Optional sanity check:

> "Read the second highlight by id."

Expected: `read_highlight` returns the same per-entry shape with
`highlight_id` matching, `parent_source_hash` populated (a
sha256-hex string from `core.cache.cache_key`).

### 4. Render both highlights

Prompt:

> "Render both highlights."

Expected: Claude calls `apply_highlight` twice (once per id).
Each call blocks until the .mp4 lands; observe `wall_clock_s`
in the response (smoke run: 38.56s for the 15s span, 46.08s
for the 45s span on a 23GB H264 source). Each response has
`render_result_id` and `output_path`. The .mp4 lives at
`<doc>.highlights/<highlight_id>.highlight.mp4`.

Verify the outputs play cleanly:

```sh
ffprobe -v error -select_streams v:0 -show_entries stream=width,height \
        -show_entries format=duration -of default=noprint_wrappers=1 \
        "<output path>"
```

Expected: `width=1080`, `height=1920`, `duration` within ~600 ms
of the requested span (the renderer's word-boundary outward-snap
+ 100 ms pad on each side widens the cut a bit; this is by
design, see `docs/PRODUCTION_RULES.md` "Pad direction expands
keep-ranges").

### 5. Inspect the render-result sidecars

Prompt:

> "What render-results have been written for this document?"

Expected: `list_highlight_renders` returns two entries with
`render_result_id`, `highlight_id`, `created_at`,
`output_path`, `wall_clock_s`, `face_detection_used`. The
`face_detection_used` field is one of:

- `speaker_locked` — the dominant face was detected at the span
  midpoint and the crop was face-centered.
- `speaker_locked_fallback_to_center` — the caller asked for
  speaker-locked but face detection failed; the renderer
  silently fell back to a centered crop.
- `center` — the caller asked for center mode.

For the podcast fixture used in the smoke (interview shot, both
hosts visible), expect `speaker_locked` for both.

Optional read of one full record:

> "Read the most recent render-result by id."

Expected: `read_highlight_render` returns the full record
including `parent_source_hash` (the cache_key matched at render
time) and `crop_box` (`{x, y, w, h}` of the 9:16 window taken
from the source frame *before* the scale to 1080×1920).

### 6. Open the GUI

Quit Claude Desktop's render activity if any is in flight, then:

```sh
python main_qt.py
```

Open the same `<doc path>`. Expected on load:

- The Highlights dock auto-shows on the right edge once per
  session per document (mirrors the 6b-3 proposal-review
  auto-show). If you dismissed it earlier this session for the
  same doc, it stays dismissed.
- Both highlights appear as cards in chronological order. Each
  card shows the reframe mode + captions flag + first 15 chars
  of the highlight_id, the source-time span, and the reason.
- Status line on each card reads "Rendered." (because step 4
  already rendered them).
- "Open" button enabled, "Render" button enabled (re-rendering
  is fine — produces a fresh render-result file and overwrites
  the .mp4).

### 7. Open one of the rendered .mp4s

Click the "Open" button on the first card. Expected: the OS
default player opens the file (QuickTime on macOS, Movies & TV
on Windows). The video plays at 1080×1920, captions visible
(first highlight had `captions_enabled=true`), faces centered
in the frame. No truncation, no glitches at the cut boundaries.

### 8. Re-render in the GUI to exercise the worker

Click the "Render" button on the second card. Expected:

- The card's status flips to "Rendering…" with a progress bar.
- The Render button greys out for the duration of the render.
- The other card stays interactive (the worker is per-card).
- On completion the bar disappears, status flips back to
  "Rendered.", and a *new* render-result file lands in the
  sidecar dir (`list_highlight_renders` from MCP would now
  return three entries, two for the second card).
- The .mp4 at `<id>.highlight.mp4` is overwritten in place
  (same path as before; old contents replaced).

### 9. Verify the stale-guard

Drop the source media file out (or rename it on disk; restoring
afterward) and try `apply_highlight` again on either id.

Expected: the tool returns `STALE_HIGHLIGHT` with a clear
message — the live `cache_key` no longer matches the
`parent_source_hash` that was recorded at author time. The
.mp4 is *not* overwritten; the prior render stands.

Move the file back, retry — the call succeeds. (`cache_key` is
absolute-path + mtime + size; if you put the file back at the
exact same path with mtime preserved, the hash matches; if the
mtime shifted, you'll need to re-propose.)

### 10. Verify intra-doc edits do NOT invalidate

This is the load-bearing distinction from Phase 6b proposals.
Apply a `cut` to the parent document via `apply_cuts` (or via
the GUI editor pane), then call `apply_highlight` on either
existing id.

Expected: the render proceeds. The highlight's
`parent_source_hash` only tracks the source media file, not
the document state — intra-doc edits don't shift the source-
time coordinates of a highlight span, so they don't invalidate.

This is the production rule the Phase 6c-A fix established:
**source-time spans hash the file, not the document state.**
A regression here means the v1 schema has crept back in.

## Common failures

- **Wrong `face_detection_used`.** If the smoke fixture you
  picked has the speaker off-frame at the span midpoint
  (cutaways, b-roll), the detector falls back to center. Check
  the source by sampling a frame at midpoint with `ffmpeg
  -ss <mid> -i <src> -frames:v 1 frame.png` and inspecting
  visually — if there's no clear face there, the fallback is
  correct.
- **Captions cut off / hard to read.** Phase 6c-3 settled on
  Arial 56 with a 80 px bottom margin (see
  `core.highlight_render.CAPTION_FORCE_STYLE`). If the captions
  are visually wrong, override the constant and re-render —
  per-highlight overrides land in Phase 7+.
- **Vertical composition surprises.** When the source is
  16:9 the 9:16 crop fills the full source height, so vertical
  placement of the speaker follows wherever the source framing
  put them. Sub-full-height crop and dynamic per-frame tracking
  are Phase 7+ work; the smoke is correct.
- **Tool count is 14 instead of 20.** Restart Claude Desktop
  with the new build present. The MCP server picks up new tool
  registrations only at server start.
