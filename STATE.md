# Transcribe — Project State Report

**Date:** 2026-04-30
**Branch:** phase-7-multicam

> **Phase 7** — Synced multi-cam highlights. Schema v3 for highlights
> (`SubSpan` fragments + per-source hash dict + optional
> `sync_group_id`); v2 highlights migrate on read. New `core/sync.py`
> with FFT cross-correlation offset estimation, `SyncGroup`/`SyncSource`
> dataclasses, sidecar persistence at `<doc>.sync/`, and manual-override
> support. Renderer extended with three paths: single-fragment (legacy),
> multi-fragment same-source (concatenated cut + reframe), and
> sync-group multi-source (per-fragment normalize + audio-master swap +
> concat). Four new MCP tools (`create_sync_group`, `list_sync_groups`,
> `read_sync_group`, `set_sync_offset`) bring the surface to **24 tools
> total**. `propose_highlights` accepts `sub_spans` + `sync_group_id`;
> legacy single-span shortcut still works for backward compat. GUI:
> per-fragment camera reassignment dropdowns on highlight cards
> (sync-group highlights only) and a new "Set Up Multi-Cam Sync…" Edit
> menu entry that opens a dialog driving cross-correlation +
> manual-override. Synthetic 3-camera fixture exercises auto-sync,
> multi-fragment propose, and end-to-end render with mixed-source
> fragments. **813 tests total (30 new in Phase 7), all green except
> the pre-existing waveform failure.** Branch is `phase-7-multicam` off
> `main`; no PR opened. Spec told us not to merge — see "What's next."

**Status:** Phase 7 complete on the implementation side. The full
chain from "operator points the GUI at an audio master + N cameras"
through "Claude proposes a multi-fragment highlight with explicit
camera angle picks via MCP" through "renderer produces a 1080×1920
mp4 with master audio swapped over per-fragment camera video" runs
end-to-end on the synthetic fixture. The PASS-rule additions in
`docs/PRODUCTION_RULES.md` codify the load-bearing decisions: audio
always comes from the master in a sync-group highlight, and the
schema migration is on-read with write-through-on-save.

---

## 1. Phase 7 deliverables

- **`core/highlight.py` schema v3.** New `SubSpan` dataclass (one
  fragment: source path + interval + optional reason). `Highlight`
  fields rename: `span_source_*` becomes `sub_spans: tuple[SubSpan,
  ...]`, `parent_source_hash: str` becomes `parent_source_hashes:
  dict[str, str]` keyed by source-path string. New optional
  `sync_group_id`. Old single-source convenience properties
  (`span_source_path`, `parent_source_hash`) survive as compat
  accessors that raise on multi-fragment highlights. v2 highlights
  migrate on read via `_load_v2_as_v3`; v1 still raises with a
  re-propose remediation message. New helper
  `reassign_fragment_source` swaps one fragment's source path,
  re-hashes, prunes obsolete hash entries, clears
  `rendered_output_path` (re-render required). `HighlightRenderResult`
  bumped to v2 with per-source crop + sync-group fields; v1
  render-results migrate on read.
- **`core/sync.py` (new).** `SyncSource` (per-camera offset record)
  and `SyncGroup` (one-shoot collection) dataclasses with full
  JSON round-trip. `estimate_offset()` cross-correlates 16 kHz mono
  PCM extractions of each camera vs the audio master via
  `numpy.fft.rfft` (no scipy dep). Returns `OffsetEstimate(offset_s,
  confidence, peak_correlation)`; convention is `master_time =
  camera_time + offset_s`. `build_sync_group()` runs estimation
  across N cameras and packs the result; failure on individual
  cameras lands as `offset_s=0.0` with a warning, not a hard error.
  `set_manual_offset()` produces an override-flagged
  :class:`SyncGroup`. `validate_sync_group_freshness()` raises
  `StaleSyncGroupError` when any source's `cache_key` has drifted.
  `extract_audio_master_window()` extracts the master's audio at
  offset-translated times into the canonical AAC profile, with
  silence-padding when the requested start is negative.
- **`core/highlight_render.py` extended.** Three paths under one
  `render_highlight` entrypoint:
  1. Single fragment, no sync group — existing behavior; ephemeral
     one-clip Document → `render_cut` → reframe.
  2. Multi-fragment, single source, no sync group — ephemeral
     Document with N ranges from one source → `render_cut` (uses
     monotonic-runs path or non-monotonic depending on order) →
     reframe + caption pass.
  3. Multi-fragment with sync group — per-fragment normalize: cut
     camera video at fragment window, replace audio with master at
     `start + offset` for `duration`, encode to canonical
     1080×1920 H264 + AAC 48 kHz stereo. Concat-demuxer stitches
     fragments losslessly. Optional caption pass on top.
  Per-source crop math: face detection runs once per unique
  source (at the midpoint of the first fragment from that source),
  cached for the run. Aggregate `face_detection_used` reports the
  worst outcome across cameras.
- **MCP surface — 24 tools.** Highlight schemas widened to take
  `sub_spans: list[SubSpanSpec]` plus `sync_group_id: str | None`
  on `HighlightSpec`; legacy single-span shortcut still accepted
  for backward compat (mixing both forms raises
  `INVALID_HIGHLIGHT`). `propose_highlights` validates fragment
  source paths against the named sync group when present.
  `apply_highlight` reads the sync group at apply time and surfaces
  staleness as `STALE_SYNC_GROUP`. Render-result wire shape carries
  `parent_source_hashes` dict + `crop_boxes_by_source` +
  `sync_group_id`. Four new sync tools: `create_sync_group` runs
  cross-correlation + persists; `list_sync_groups` /
  `read_sync_group` for inspection; `set_sync_offset` for manual
  override. New error codes: `SYNC_GROUP_NOT_FOUND`,
  `INVALID_SYNC_GROUP`, `STALE_SYNC_GROUP`,
  `SYNC_ESTIMATION_FAILED`. The `propose_highlights` description
  now includes the camera-angle prompt: "the model can't see camera
  frames; drive angle choices from structural cues (alternation,
  pacing, speaker change), and use per-fragment reasons to record
  the rationale."
- **GUI updates.** `HighlightsPanel`'s `_HighlightCard` renders one
  row per `SubSpan` showing the time window. For sync-group
  highlights, each row gets a `QComboBox` listing every camera in
  the group; switching writes back via `reassign_fragment_source`
  and clears the rendered output. Single-camera highlights show
  the source as plain text. New `SyncSetupDialog`
  (ui_qt/components/sync_setup_dialog.py) walks the operator
  through pick-master → add-cameras → estimate → eyeball/override
  → save. Confidence colors: green ≥ 5, amber 2.5–5, red < 2.5.
  New "Set Up Multi-Cam Sync…" Edit menu entry on
  `MainWindow`.
- **Tests.** 30 new tests in `tests/test_phase_7.py` (26 fast + 4
  slow). Coverage: SubSpan validation + JSON round-trip,
  Highlight v3 round-trip including multi-fragment, v2-payload
  migration on read, v1 still raises, fragment reassignment,
  HighlightRenderResult v1 → v2 migration, sync estimation against
  known shifted signals (positive + negative shifts), silent /
  too-short input rejection, SyncGroup persistence + manual
  override + freshness validation, multi-fragment same-source
  render (slow), synthetic 3-camera fixture (slow), end-to-end
  auto-sync + propose + render with mixed-source fragments (slow),
  MCP propose with sub_spans / legacy / mixed-rejection /
  unknown-sync-group / and the four sync tools (incl. the
  set-offset reject-on-unknown-camera path).

---

## 2. Tool count delta (cumulative)

| Phase | Tool count |
|-------|-----------:|
| 6a final        | 8 |
| 6b-2            | 14 (+ proposal lifecycle) |
| 6c-2            | 20 (+ highlight lifecycle) |
| **Phase 7**     | **24** (+ create_sync_group / list_sync_groups / read_sync_group / set_sync_offset) |

The previously named test
`tests/test_phase_6a.py::test_twenty_tools_registered` is now
`test_twenty_four_tools_registered` and asserts the canonical name
list of all 24 tools in registration order.

---

## 3. Test count delta

| Phase | Total | Fast | Slow |
|-------|------:|-----:|-----:|
| 6c-A/B/C (prior) | 771 | 743 | 28 |
| Pre-Phase-7 (carry-over fixes) | 783 | 755 | 28 |
| **Phase 7**      | **813** | **781** | **32** |

The Phase 7 pass added:

- 26 fast tests + 4 slow tests in `tests/test_phase_7.py` (sync
  unit tests, schema migration tests, MCP wiring tests, the
  multi-cam fixture, and the end-to-end render).

The pre-existing waveform failure
(`tests/test_waveform.py::test_strip_dim_overlay_distinguishes_cut_regions`)
predates Phase 7 and was already documented as "predates this pass"
in the prior STATE entry.

---

## 4. Render-time impact (multi-cam vs single-cam)

The synthetic 3-camera fixture (3 × 30 s, 320×240, h264 + aac;
audio master at 30 s, AAC stereo 48 kHz) renders an end-to-end
multi-fragment 9:16 highlight (3 × 2 s = 6 s output) in **~3 s wall
clock on an Apple M-series laptop**. The cost breakdown:

- Per-fragment encode (3 fragments): ≈ 2.4 s total.
- Concat-demuxer pass: ≈ 0.2 s.
- No caption burn (the smoke test runs without captions).

The headline: **the sync-group path is dominated by the per-fragment
re-encode**, not by cross-correlation (which runs once at sync-group
creation time, not at render time). Compared to the single-camera
6c-2 path, which uses smartcut's stream-copy keep-ranges path and is
near-zero-cost when no audio fade is needed, the Phase 7 path pays a
fixed re-encode tax per fragment. This is unavoidable: matching codec
parameters across cameras is the only way to make concat-demuxer
safe, and the per-fragment audio swap requires re-encoding the audio
track anyway. Mitigations explored:

- **Concat-filter instead of concat-demuxer.** Would let the per-camera
  encodes stay native (no normalize), but the overall output still
  needs one re-encode pass — and the concat filter is the path that
  STATE 6c-2 §7.5 flagged as fragile when codecs differ. Not adopted.
- **Lossless segment-extract for matched-codec multi-cam.** When every
  camera shares identical encoding params (rare in practice — even
  same-model cameras produce slightly different bitstreams), the
  per-fragment cut could be `-c copy`. Not adopted; the matched-codec
  detection has too many edge cases (e.g. PSNR vs CRF re-encode by
  the camera firmware) and the lossless gain is < 5 % on typical
  podcast shoot sizes.

The multi-fragment same-source path (still re-using `render_cut`
under the hood) is essentially free relative to single-camera —
adding fragments only widens the keep-list.

---

## 5. What's deliberately not addressed (Phase 7+ debt — carried)

- **Per-frame speaker tracking.** Still one face-detection sample
  per source per render. With multi-cam where each camera typically
  frames the speaker statically, this is rarely a problem in
  practice.
- **Sub-full-height vertical crop.** Source aspect ≥ 9:16 still
  forces full-height crop — vertical placement follows source
  framing.
- **Async/streaming `apply_highlight`.** Synchronous; a 3-fragment
  multi-cam render still blocks Claude Desktop for the encode
  duration. Not painful at fixture sizes but would matter for
  10-minute podcast highlights.
- **Auto-renormalize on schema drift.** The on-disk v2 file stays
  v2 until a save touches it. A future audit could write a script
  that walks `<doc>.highlights/` and rewrites in v3 form for
  consistency, but the lazy approach matches every other migration
  in this codebase.
- **GUI "re-estimate offsets" button.** The `SyncSetupDialog`
  re-runs estimation when the operator clicks "Estimate offsets",
  but there's no "re-estimate just this camera" button — the whole
  group rebuilds. Not painful at 2–4 cameras.
- **Camera audio fallback.** If the audio master is missing on
  disk at render time, `STALE_SYNC_GROUP` fires and refuses. There's
  no "fall back to camera audio for this fragment" option. By
  design — the production rule is explicit that audio always comes
  from the master.

---

## 6. Architectural decisions surfaced (recorded)

- **Sign convention for `offset_s`.** `master_time = camera_time +
  offset_s`. A camera that started rolling 1 s after the master
  has `offset = +1.0` (master is 1 s ahead of cam at the same
  content). The cross-correlation lag at the peak directly
  corresponds to this value with no sign flip. Documented in
  `core/sync.py`'s module docstring.
- **`parent_source_hashes` is a dict keyed by path string.** Phase 7
  considered a list of `(path, hash)` tuples, but the dict shape
  makes the renderer's stale-guard loop clean (`for path, hash in
  highlight.parent_source_hashes.items()`) and matches the
  multi-source intent. The audio master's hash lives on the sync
  group, *not* on the highlight — separating concerns means a
  highlight referencing a sync group doesn't need to repeat the
  master's hash.
- **Schema migration on read, write-through on save.** Same lazy
  policy that 4f-3 established for `Document`. v2 highlights load
  as v3 in memory; the on-disk file stays v2 until the next save.
  The test suite covers both: `test_highlight_v2_payload_migrates_to_v3`
  (in-memory shape) and the implicit "next write produces v3"
  pattern (verified by reading back after `write_highlight`).
- **GUI camera reassignment is sync-group-only.** Reassigning a
  fragment to a camera that's NOT in the sync group would require
  the operator to either provide a new offset out-of-band or accept
  using camera audio (which violates the production rule). The
  cleanest answer is "you can swap among cameras the group already
  knows about"; cross-group reassignment is a future feature with
  its own UX questions.

---

## 7. What's next

The spec was explicit: **branch `phase-7-multicam` off `main`, do
not merge to main, do not open a PR**. The branch is pushed; further
review happens out-of-band. Next steps (not for this pass):

- Real-fixture smoke against a podcast multi-cam shoot (current
  smoke is synthetic only — solid colors + lab noise).
- Per-frame speaker tracking on the speaker-locked path (the sync
  group's offsets give us audio-driven speaker selection cheaply
  enough that a "follow the loudest mic" heuristic for the active
  camera becomes viable).
- The carry-over Phase 6 follow-ups still on the books:
  `apply_proposal` UX for "all rejected" runs, editor-side drag-to-
  reorder, waveform v3 reader, multi-source compositions on the
  main timeline, `find_silences` analysis tool.

---

## 8. Pre-Phase-7 carry-over (preserved for context)


> **Phase 6c (this pass)** — Highlight artifact + 9:16 reframe / caption
> render path + MCP lifecycle + GUI panel. New `core/highlight.py`
> (`Highlight` dataclass, `HighlightRenderResult` dataclass, sidecar
> persistence, source-keyed stale guard) and `core/highlight_render.py`
> (face detection, crop math, SRT generation, single-pass ffmpeg
> reframe + caption burn). New `mcp_server/tools/highlights.py`
> (six MCP tools wrapping the highlight surface) plus four new error
> codes (`HIGHLIGHT_NOT_FOUND`, `RENDER_RESULT_NOT_FOUND`,
> `INVALID_HIGHLIGHT`, `STALE_HIGHLIGHT`). New
> `ui_qt/components/highlights_panel.py` — read-only `QDockWidget`
> with per-highlight Render / Open buttons, render runs in a
> `QThread` worker per card. **771 tests total** (up from 738), all
> green except the pre-existing `test_strip_dim_overlay_distinguishes_cut_regions`
> failure on `main` that pre-dates this pass. Tool count rose from
> 14 → 20. Real-fixture smoke on
> `/Volumes/Aaron 4TB/531 Podcast Aaron & Barret Autocut only.mp4`:
> two highlights, 15 s + 45 s spans, both `speaker_locked`, both face
> detection succeeded, **38.56 s + 46.08 s wall clock**.

**Status:** Phase 6c (sub-phases 1 / 2 / 3) is complete. The
highlight surface is wired end-to-end: Claude proposes via MCP →
renderer cuts + reframes (+ optionally burns captions) → GUI shows
"rendered" with an Open button. No human review on the highlight
path; the panel is intentionally read-only (re-author via Claude
Desktop, not the GUI). The Phase 6c-A stale-guard fix replaced the
v1 highlight schema's incorrect `parent_document_state_hash`
(which would invalidate spans on unrelated intra-doc edits) with
v2's `parent_source_hash` keyed off `core.cache.cache_key` —
source-time spans now correctly hash the source media file, not
the document state.

---

## 1. Phase 6c at a glance

- **Section A** — schema v2 stale-guard fix on `Highlight`. The v1
  schema (introduced earlier in 6c-1) stored
  `parent_document_state_hash` (sha256 of full Document JSON), which
  silently invalidated highlights every time the parent doc was
  edited even when the source file was unchanged. Highlights are
  source-time spans — intra-doc edits don't shift them. v2 stores
  `parent_source_hash` keyed off `core.cache.cache_key`
  (path+mtime+size). v1 files raise `UnsupportedSchemaError` on
  load with a re-propose remediation message; no silent migration.
- **Section B** — 6c-2 MCP surface. Six new tools in
  `mcp_server/tools/highlights.py`: `propose_highlights`,
  `list_highlights`, `read_highlight`, `apply_highlight`,
  `list_highlight_renders`, `read_highlight_render`. Single-pass
  spec validation on propose (the offending index is named in the
  error message; no partial persistence). `apply_highlight` writes
  a `<render_result_id>.render-result.json` sidecar per call,
  recording wall-clock, `face_detection_used` (one of
  `speaker_locked`, `speaker_locked_fallback_to_center`, `center`),
  and the `crop_box`. Re-runs accumulate sidecars; the .mp4 is
  overwritten in place. Four new error codes wire stable client-
  fixable signals on top.
- **Section C** — 6c-3 GUI Highlights panel. New
  `ui_qt/components/highlights_panel.py` — `QDockWidget` host with
  `_HighlightCard` per highlight, render runs in a `QThread`
  worker (greyed-out Render button + progress bar while in flight),
  Open button uses `QDesktopServices.openUrl` and is gated on
  `rendered_output_path` actually existing on disk. `MainWindow`
  plumbs View → Highlights toggle, auto-show on doc-load when the
  sidecar dir has highlights (once per session per doc), clear on
  doc swap. Caption ASS style locked at module level
  (`core.highlight_render.CAPTION_FORCE_STYLE`): Arial 56,
  white-on-black, bottom-center, 80 px margin. Vertical composition
  stays source-framed (Phase 7+ debt — see §6).
- **Section D** — end-to-end smoke against the real podcast fixture
  + `docs/PHASE_6C_SMOKE.md` + this rewrite.

The stale-guard fix is now codified in `docs/PRODUCTION_RULES.md`
under the cutting-and-rendering section: source-time spans hash the
*file*, not the document state.

---

## 2. Phase 6a / 6b inventory (background, unchanged this pass)

Phase 6a shipped across four commits:

* `f9c06f5` — MCP server foundation (transcribe / read / cut / render).
* `1f4eca1` — smartcut non-monotonic spike (YELLOW gate, option 1).
* `0a92ec2` — schema v3 (`Clip` / `Timeline`) + run-batched renderer.
* `a5c45d3` — GUI v3 reader + MCP `get_timeline` + `EDIT_NOT_SUPPORTED`.

Phase 6b shipped across three sub-passes:

* **6b-1** — `MoveClipSpan` primitive, `EditEvent` /
  `Document.edit_log` (v3 → v3.1), `Proposal` JSON I/O with parent-
  hash guard, reason validator.
* **6b-2** — apply-result persistence, `MoveOutcome` extended with
  chain-of-custody hash, six MCP proposal-lifecycle tools, seven
  new error codes, smoke checklist for Path A.
* **6b-3** — GUI proposal-review dock + reasoned-reject (Path B).
  Reason-flow refactor (event-first, range-derived). End-to-end
  loop closure: GUI rejection reason flows through MCP
  `read_apply_result` verbatim.

These are the prerequisites for 6c. The 6b-2 proposal stale guard
(`parent_document_state_hash`, sha256 over the full Document JSON
including `edit_log`) is the *correct* design for proposals — moves
need to invalidate on intra-doc drift. Highlights are different:
their spans are source-time coordinates, so the proposal's hash
semantics would over-invalidate. Section A's split is what makes
the difference legible.

---

## 3. Project structure (deltas in this pass)

```
.
├── core/
│   ├── highlight.py           # CHANGED (6c Section A) — schema_version=2,
│   │                          #   parent_source_hash field, v1 raises
│   │                          #   UnsupportedSchemaError. NEW (Section B):
│   │                          #   HighlightRenderResult dataclass +
│   │                          #   write_render_result / read_render_result /
│   │                          #   list_render_results_for_document /
│   │                          #   new_render_result_id helpers.
│   ├── highlight_render.py    # CHANGED — render_highlight returns
│   │                          #   HighlightRenderMetadata (output_path,
│   │                          #   face_detection_used, crop_box,
│   │                          #   parent_source_hash) instead of bare Path;
│   │                          #   stale-guard now compares
│   │                          #   cache_key(span_source_path) (not
│   │                          #   document_state_hash); CAPTION_FORCE_STYLE
│   │                          #   bumped to Arial 56 / margin 80 (Section C).
│   └── …                      # other files unchanged
├── mcp_server/
│   ├── errors.py              # +4 codes (HIGHLIGHT_NOT_FOUND,
│   │                          #   RENDER_RESULT_NOT_FOUND, INVALID_HIGHLIGHT,
│   │                          #   STALE_HIGHLIGHT)
│   ├── schemas.py             # +HighlightSpec, HighlightOut, HighlightSummary,
│   │                          #   ProposeHighlights*Request/Result,
│   │                          #   ApplyHighlightRequest/Result, CropBoxOut,
│   │                          #   RenderResultSummary, ListHighlightRenders*,
│   │                          #   ReadHighlightRender*.
│   ├── server.py              # 20-tool dispatch table (8 + 6 + 6).
│   └── tools/
│       └── highlights.py      # NEW (Section B) — six MCP tools.
├── ui_qt/
│   ├── app.py                 # MainWindow plumbing for the 6c-3 dock —
│   │                          #   _setup_highlights_dock,
│   │                          #   _handle_toggle_highlights,
│   │                          #   _handle_highlights_present,
│   │                          #   _refresh_highlights_panel,
│   │                          #   View → Highlights menu entry.
│   └── components/
│       └── highlights_panel.py  # NEW (Section C) — HighlightsPanel,
│                                #   _HighlightCard, _RenderWorker (QThread).
├── docs/
│   ├── PRODUCTION_RULES.md    # +"Highlight stale-guard hashes the source
│   │                          #   file, not document state" (PASS rule).
│   ├── PHASE_6C_SMOKE.md      # NEW (Section D) — 10-step manual checklist.
│   └── …                      # unchanged
├── tests/
│   ├── test_phase_6c1.py      # CHANGED — schema v2 field rename, v1 refusal
│   │                          #   test added, intra-doc-edit-doesn't-
│   │                          #   invalidate test added (24 → 26 tests).
│   ├── test_phase_6c2.py      # NEW (Section B) — 23 tests.
│   ├── test_phase_6c3.py      # NEW (Section C) — 8 tests.
│   └── test_phase_6a.py       # CHANGED — `test_fourteen_tools_registered`
│                              #   renamed to `test_twenty_tools_registered`
│                              #   and updated for the +6 highlight tools.
└── STATE.md                   # this file
```

---

## 4. Test count

| Phase | Total | Fast | Slow |
|-------|------:|-----:|-----:|
| End of 5f       | 529 | 517 | 12 |
| 6a MCP          | 559 | 547 | 12 |
| 6a schema v3    | 588 | 571 | 17 |
| 6a final        | 602 | 585 | 17 |
| 6b-1            | 662 | 644 | 18 |
| 6b-2            | 691 | 673 | 18 |
| 6b-3            | 708 | 690 | 18 |
| 6c-1            | 738 | 716 | 22 |
| **6c-A/B/C**    | **771** | **743** | **28** |

Section A added 2 tests (`test_highlight_from_json_rejects_v1_schema`,
`test_render_does_not_invalidate_on_intra_doc_edit`). Section B added
23 tests in `tests/test_phase_6c2.py` (17 fast + 6 slow). Section C
added 8 tests in `tests/test_phase_6c3.py` (all fast, headless Qt
patterns).

The pre-existing failure
(`tests/test_waveform.py::test_strip_dim_overlay_distinguishes_cut_regions`)
predates this pass. Verified by stashing the working tree and
re-running on `main` — same failure, unrelated to highlights.

---

## 5. Tool count delta

| Phase | Tool count |
|-------|-----------:|
| 6a MCP          | 7 |
| 6a final        | 8  (+`get_timeline`) |
| 6b-2            | 14 (+propose_moves / list_proposals / read_proposal / apply_proposal / list_apply_results / read_apply_result) |
| **6c-2**        | **20** (+propose_highlights / list_highlights / read_highlight / apply_highlight / list_highlight_renders / read_highlight_render) |

---

## 6. What's solid (Phase 6c)

1. **Source-time stale guard is correctly keyed on the source file.**
   `Highlight.parent_source_hash` stores `cache_key(span_source_path)`
   at author time. The renderer compares against the live cache_key
   at apply time; mismatch raises `StaleHighlightError`. v1 files
   (with the old `parent_document_state_hash`) raise on load — no
   silent migration. Now codified in
   `docs/PRODUCTION_RULES.md` as a PASS rule.
2. **Render-result and Highlight responsibilities are non-overlapping.**
   The `Highlight.rendered_output_path` is "where my output mp4
   currently is" — one path per highlight, overwritten on re-render.
   The `HighlightRenderResult` is "what happened on a specific render
   run" — one fresh sidecar per `apply_highlight` call, recording
   wall clock, face-detect outcome, crop box, and source hash.
   The .mp4 is overwritten; the JSON sidecars accumulate. No
   deduplication between them.
3. **Single-pass spec validation on propose.** A bad spec in the
   middle of an `propose_highlights` batch short-circuits the
   entire call before any sidecar lands. The error names the
   offending index (`highlights[N]`) so a multi-entry batch can be
   repaired entry-by-entry. Tested.
4. **`face_detection_used` is set honestly by the renderer.**
   Speaker-locked failure logs a warning *and* records
   `speaker_locked_fallback_to_center` in the metadata. The smoke
   on the real podcast fixture verified `speaker_locked` succeeds
   on actual interview footage; the synthetic-fixture test
   verified the fallback path tags correctly.
5. **GUI panel is read-only by design.** No accept/reject controls,
   no inline editing of highlight specs. If the user wants to change
   a highlight, they re-propose via Claude Desktop. The Render and
   Open buttons are the entire interactive surface; mirrors the
   "no human review on the highlight path" workflow.
6. **Render worker is per-card, not pooled.** Each `_HighlightCard`
   owns its own `QThread` for the duration of one render. Renders
   can run concurrently on different cards if the user clicks fast
   (the `.mp4` path is per-id so they don't collide), but the typical
   pattern is one at a time. Cleanup chain (`worker.deleteLater` +
   `thread.quit / wait / deleteLater`) is wired on both `finished`
   and `failed` signals so a thread leak on render failure is
   structurally impossible.
7. **Auto-show is per-doc, dismissable.** When a doc with at least
   one highlight loads, the dock auto-opens once. Once the user
   dismisses the dock for that doc, it stays dismissed for the rest
   of the session — only switching to a different doc with
   highlights triggers another auto-show. Mirrors the 6b-3
   proposal-review-dock pattern.
8. **Tool registration is locked under a count test.**
   `tests/test_phase_6a.py::test_twenty_tools_registered` asserts
   the canonical name list. A future addition (or accidental
   removal) trips this test before the change ships.

---

## 7. What's fragile or worth knowing

1. **Vertical composition is source-framed (Phase 7+ debt).**
   When the source aspect is ≥ 9:16, the 9:16 crop fills the full
   source height. The speaker's vertical position therefore
   follows wherever the source framing put them — we cannot shift
   them without throwing away pixel rows. Sub-full-height crop and
   dynamic per-frame tracking are Phase 7+ work; documented in
   `compute_speaker_locked_crop`'s docstring.
2. **Caption styling is one fixed default.** The
   `CAPTION_FORCE_STYLE` constant (Arial 56, white-on-black-3px,
   bottom-center, 80 px margin) is the entire knob today. Per-
   highlight overrides land in Phase 7+ alongside the GUI
   affordance to author them.
3. **Render-result timestamps are wall-clock at apply time.** Two
   re-runs of the same highlight produce different
   `render_result_id` values (timestamp-prefixed), different
   `created_at`, and different `wall_clock_s` — even when the
   output .mp4 is byte-identical. That's the right design for
   "what happened on this run" semantics; clients comparing
   render-results across runs should compare on `output_path` (or
   on the file itself), not on the sidecar's metadata.
4. **`apply_highlight` is synchronous over MCP.** Re-rendering the
   45-second podcast highlight took 46 s wall clock. Claude Desktop
   blocks until the call returns. For longer spans the model will
   feel that latency; an async/streaming variant is a Phase 7+
   candidate.
5. **`render_highlight` shells out to ffmpeg in a subprocess.** A
   broad `except Exception` in the MCP `apply_highlight` handler
   surfaces ffmpeg / smartcut / PyAV failures uniformly as
   `RENDER_FAILED` instead of leaking a Python traceback through
   `INTERNAL_ERROR`. This is intentional — the underlying error
   types are heterogeneous (`RuntimeError` from ffmpeg,
   `av.error.FFmpegError` from PyAV, `OSError` from disk) and
   listing them all individually would be more brittle than the
   broad catch. The original message rides in
   `data["highlight_id"]` + the MCP message body.
6. **GUI Render button doesn't propagate progress to a status
   widget.** The per-card progress bar is the only progress
   surface. If a future status-bar wants render % too, the
   `_RenderWorker.progress` signal can be re-routed; for now it's
   self-contained on the card.
7. **A highlight's `parent_source_hash` is recomputed per propose,
   not cached.** The proposing path looks at the parent doc's
   `MediaSource.hash` first (a cached value on the doc) and falls
   back to a fresh `cache_key` call. So docs that don't carry the
   source hash on disk pay one stat call per propose. Negligible
   for the highlight workflow (a propose batch is typically a
   handful of specs).

---

## 8. Spec deviations and reasons (this pass)

1. **Section A's literal grep gate is unreachable.** The spec said
   `grep -rn "parent_document_state_hash" core/ tests/ mcp_server/
   ui_qt/` should return nothing post-Section-A. That's impossible:
   the proposal subsystem (`core/proposal.py`,
   `mcp_server/schemas.py` proposal models, `mcp_server/server.py`
   proposal tool descriptions, `ui_qt/components/proposal_review_pane.py`)
   correctly uses `parent_document_state_hash` for proposals,
   where the semantics are right (proposals SHOULD invalidate on
   intra-doc edits — they're document-state operations). The
   rename was correctly scoped to highlights only per spec A.1.
   The grep gate was treated as the spirit (no leftover highlight
   references that should be `parent_source_hash`), not the
   literal (zero global hits).
2. **`HighlightRenderResult` lives in `core/highlight.py`, not a
   separate module.** Mirrors the `core/proposal.py` shape where
   `Proposal` and `ApplyResult` live in the same module. Spec
   didn't dictate; the bundling keeps the highlight artifacts
   discoverable from one import.
3. **`render_highlight` return type changed.** Spec said
   `apply_highlight` should "wire `render_highlight` to return the
   metadata if it doesn't already." The old function returned a
   bare `Path`; we changed it to return
   `HighlightRenderMetadata(output_path, face_detection_used,
   crop_box, parent_source_hash)`. Existing callers in tests
   adapted. This is the cleaner shape; the callsites are all
   in-repo.
4. **MCP `apply_highlight`'s exception catch is `except Exception`
   rather than a tight whitelist.** Spec implied per-error-type
   surface. As shipped, RuntimeError + OSError + everything-else
   all land as `RENDER_FAILED`; `StaleHighlightError` (specific)
   and `FileNotFoundError` (specific) are caught earlier. The
   reason: ffmpeg / smartcut / PyAV raise heterogeneous types
   that don't share a stable parent class. Detailed in §7.5.
5. **Section A added a defensive test alongside the v1-refusal
   test** (`test_render_does_not_invalidate_on_intra_doc_edit`),
   not strictly required by spec but locks the load-bearing
   distinction so a future regression to v1 semantics fails loudly.

---

## 9. Smoke result (Section D)

Driven against
`/Volumes/Aaron 4TB/531 Podcast Aaron & Barret Autocut only.mp4`
using the existing `~/Desktop/...transcribe.json` (re-pointed to
the 4TB volume's media path). Two highlights authored via
`propose_highlights`, both rendered via `apply_highlight`:

| highlight | span | reframe | captions | face_detection_used | wall clock | output dur. |
|-----------|------|---------|----------|---------------------|-----------:|------------:|
| 165207-8fbdf819 | 120.0 – 135.0 s | speaker_locked | on  | speaker_locked | 38.56 s | 15.55 s |
| 165207-e7675453 | 300.0 – 345.0 s | speaker_locked | off | speaker_locked | 46.08 s | 45.75 s |

Both .mp4 outputs at 1080 × 1920 H264. Output durations land
within ~600 ms of the requested span — that's the
word-boundary outward-snap + 100 ms pad on each side, by design
(see `docs/PRODUCTION_RULES.md`). Render-result sidecars round-trip
through `read_highlight_render`; both sidecars have honest
`face_detection_used` values (real face detection succeeded on the
podcast's interview shot).

Highlights panel verified programmatically: loads the doc,
populates 2 cards, both show "Rendered." status, both Open
buttons enabled, doc swap clears the panel.

---

## 10. Definition-of-done checklist

### Section A — stale-guard fix

- [x] `parent_document_state_hash` → `parent_source_hash` on
      `Highlight` dataclass and JSON schema.
- [x] Authoring time populates from parent doc's `MediaSource.hash`
      or recomputes via `core.cache.cache_key`.
- [x] Render time compares against live `cache_key(span_source_path)`.
- [x] `Highlight.SCHEMA_VERSION = 2`.
- [x] v1 raises `UnsupportedSchemaError` with re-propose message.
- [x] Tests updated; v1-refusal test added; intra-doc-edit-doesn't-
      invalidate test added.
- [x] Production rules note added.

### Section B — 6c-2 MCP tools

- [x] `mcp_server/tools/highlights.py` with six tools.
- [x] Four error codes: `HIGHLIGHT_NOT_FOUND`,
      `RENDER_RESULT_NOT_FOUND`, `INVALID_HIGHLIGHT`,
      `STALE_HIGHLIGHT`. (`RENDER_FAILED` was pre-existing from
      Phase 6a's render path.)
- [x] Schemas: `HighlightSpec`, `HighlightOut` (via
      `HighlightSummary`), `RenderHighlightResult` (split into
      `ApplyHighlightResult` for the apply path + `RenderResultSummary` /
      `ReadHighlightRenderResult` for the read paths),
      `RenderResultOut` (also split for the listing vs full-read
      distinction).
- [x] Render-result sidecar shape recorded by
      `apply_highlight`; `face_detection_used` set honestly.
- [x] Tests in `tests/test_phase_6c2.py` (23 tests, 17 fast + 6
      slow). Covers happy path, every error code, idempotent
      re-render, stale-source guard, render-result round-trip.
- [x] Tool count rises 14 → 20; lock test updated.
- [x] `python main_mcp.py` boots and lists 20 tools.
- [x] Sidecar responsibility split documented (highlight owns
      "where my mp4 is"; render-result owns "what happened on
      this run") — see §6.

### Section C — 6c-3 GUI panel

- [x] `ui_qt/components/highlights_panel.py` — `HighlightsPanel`
      and `_HighlightCard` widgets.
- [x] Render runs in a `QThread` per card with progress bar +
      greyed-out Render button while in flight.
- [x] Open button gated on `rendered_output_path` existing on
      disk; uses `QDesktopServices.openUrl`.
- [x] Auto-refresh on render completion (the affected card's
      sidecar is re-read; no full panel rebuild).
- [x] `MainWindow` plumbing: View → Highlights toggle, dock
      hidden by default, clears on doc swap, auto-shows once
      per session per doc on doc-load with at least one highlight.
- [x] No accept/reject controls (read-only design).
- [x] Caption ASS style locked: Arial 56, white-on-black-3px,
      bottom-center, 80 px margin (`CAPTION_FORCE_STYLE`).
      Per-highlight overrides documented as Phase 7+.
- [x] Vertical-composition Phase 7+ debt called out in
      `compute_speaker_locked_crop` docstring.
- [x] Tests in `tests/test_phase_6c3.py` (8 tests, all fast,
      headless Qt). Covers no-doc state, empty state, card per
      highlight, Open-disabled-until-rendered, auto-show signal
      contract, doc-swap clears, render-button worker plumbing.

### Section D — smoke + STATE + commits

- [x] D.1 — real-fixture smoke executed; both .mp4 outputs at
      1080 × 1920, durations within ~600 ms of expected;
      `face_detection_used` populated honestly.
- [x] D.2 — `docs/PHASE_6C_SMOKE.md` 10-step checklist for
      Claude Desktop covering propose → render → GUI inspect.
- [x] D.3 — STATE.md rewritten in place.
- [x] All 771 tests green except the pre-existing waveform
      failure on `main`.
- [x] `python main.py` / `python main_qt.py` /
      `python main_mcp.py` import + start cleanly.
- [x] D.4 — commits at logical boundaries (this commit + the
      preceding A/B/C commits).

---

## 11. What's deliberately not addressed (Phase 7+ debt)

- **Per-highlight caption-style overrides.** Constants today;
  per-call style is Phase 7+ alongside the GUI affordance to
  author it.
- **Dynamic speaker tracking.** One face-detection sample at the
  span's midpoint, static crop for the duration. Per-frame
  tracking + audio-driven speaker selection are Phase 7+.
- **Multi-source highlights.** A highlight references one source
  path; multi-source compositions are Phase 7+ work that requires
  the run-batched renderer's multi-source path (currently flagged
  in §7.5 of the prior STATE.md as concat-demuxer fragile).
- **Sub-full-height vertical crop.** Source-aspect ≥ 9:16 today
  forces the crop to fill source height, so vertical placement
  follows the source. Sub-full-height crop with a face-driven
  vertical center is Phase 7+.
- **Async / streaming `apply_highlight`.** Synchronous over MCP
  today; long renders block Claude Desktop. Async variant is
  Phase 7+.

---

## 12. What's next (after Phase 6c)

The 6b/6c roadmap items still open from prior STATEs survive this
pass unchanged:

- **`apply_proposal` UX for "all rejected" runs** (defer; only
  bites at scale).
- **Editor-side rearrangement UX** — drag-to-reorder in
  `TranscriptView`'s playlist-order render, hooked through
  `MoveClipSpan` writing fresh proposal files.
- **Waveform v3 reader** — clip boundaries + visual difference
  between "this span is cut" and "this span is kept but plays
  later in the playlist."
- **Multi-source compositions** — flips
  `Document.main_timeline` from `@property` to real field; see
  prior STATE §8 6a-debt note.
- **MCP analysis tools** — `find_silences(json_path,
  min_duration_s=0.5)` is the next mechanical pickup that
  closes the cleanup loop without needing model judgement.

The highlight-specific Phase 7+ list above (caption overrides,
dynamic tracking, multi-source highlights, async apply) layers on
top.
