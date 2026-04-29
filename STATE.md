# Transcribe — Project State Report

**Date:** 2026-04-29
**Branch:** main
**Commits:** Phase 6a is shipped across four commits —
  * `f9c06f5` — MCP server foundation (transcribe / read / cut / render).
  * `1f4eca1` — smartcut non-monotonic spike (YELLOW gate, option 1 picked).
  * `0a92ec2` — schema v3 (Clip/Timeline) + run-batched renderer +
    `AddCut.reason`.
  * Pending — GUI v3 reader (playlist-order transcript, edit actions
    disabled on non-monotonic) + MCP `get_timeline` + non-monotonic
    edit-refusal + smoke checklist.

**Status:** Phase 6a is complete. All 602 tests pass (529 prior + 30
MCP-foundation + 29 schema-v3 + 14 final). Lint clean for changed
files. Phase 6b is next.

---

## 1. Phase 6a in four paragraphs

Phase 6a opened with the MCP server foundation (commit `f9c06f5`) —
seven tools wrapping the `core/` pipeline over stdio per Anthropic's
official `mcp` SDK, with stable error codes prefixed onto every error
message so clients can branch on `FILE_NOT_FOUND` / `WORD_BOUNDARY_VIOLATION`
and friends. That work treated `Document` as v2 (single-source, sorted
keep-ranges) and only renamed concepts at the JSON layer.

The continuation (this commit) added schema v3 — `Clip` and `Timeline`
in `core/timeline.py`, with `Document.main_timeline` exposed as a
derived view over the still-frozen `ranges` field. The on-disk JSON
shape changes from `ranges: [{source_id, start, end, reason}, …]` to
`main_timeline: {clips: [{source_id, source_path, source_start,
source_end, reason}, …]}`. v1 → v2 → v3 migration runs automatically
on read. Editing commands (`AddCut`, `RestoreRange`, `CutWordRange`)
gained a non-monotonic guard at apply time — they raise
`NotImplementedError` if `main_timeline.is_source_monotonic()` is
False. `AddCut.reason: str | None = None` is the new persisted
metadata field, recorded onto the surviving neighbour range so it
round-trips through JSON.

The renderer's central change is run-batching. The smartcut spike
(commit `1f4eca1`, full report in §9) established that smartcut
requires sorted non-overlapping `positive_segments`; non-monotonic
input silently drops or duplicates content. So `render_cut` now
branches on monotonicity: monotonic timelines (everything 6a editing
produces) take the v2 fast path verbatim; non-monotonic timelines
split into the minimum number of source-monotonic *runs*, each is one
`smart_cut` call, the per-run outputs concat with ffmpeg's stream-copy
concat demuxer, and one unified `afade` post-pass covers every join in
the final output (within-run *and* run-to-run boundaries). MediaContainer
is reused across runs from the same source — the spike showed ~35 %
wall-clock saving on the heavy HEVC 10-bit fixture.

The final pass closes the loop on the editor-side and MCP-side
v3-awareness. The Qt `TranscriptView` now branches on the document's
monotonicity: monotonic docs render via the pre-6a source-order path
(byte-identical, locked under a hash snapshot test); non-monotonic
docs render in playlist order with a visible `— jump to N.NNs —`
boundary between adjacent clips. `EditorPane` disables cut / restore /
delete / save and stamps a "Phase 6b" tooltip when the loaded
document is non-monotonic — the user never reaches the
`NotImplementedError` safety net in `core.editing`. MCP gets a new
`get_timeline` tool returning the v3 playlist (the v2-shaped
`get_ranges` is retained but flagged lossy on non-monotonic), and
`apply_cuts` / `restore_ranges` refuse non-monotonic documents with
the new stable `EDIT_NOT_SUPPORTED` error code so clients branch
cleanly. `docs/PHASE_6A_SMOKE.md` documents an 11-step manual checklist
the user runs through Claude Desktop.

---

## 2. Project structure (deltas in this pass)

```
.
├── core/
│   ├── document.py          # v3 schema, v1→v2→v3 migration, main_timeline @property
│   ├── editing.py           # AddCut.reason, non-monotonic NotImplementedError guard
│   ├── render.py            # _render_monotonic / _render_non_monotonic / _ffmpeg_concat_demuxer
│   ├── timeline.py          # Clip, Timeline, split_into_monotonic_runs (+ existing v2 helpers)
│   └── …                    # other files unchanged
├── workers/                 # unchanged
├── ui/                      # unchanged (still reads via doc.ranges)
├── ui_qt/
│   ├── components/transcript_view.py  # NEW playlist-order render path
│   └── editor_pane.py       # NEW _apply_monotonicity_state + tooltip
├── mcp_server/
│   ├── errors.py            # +EDIT_NOT_SUPPORTED
│   ├── schemas.py           # +ClipOut, +TimelineResult, +is_source_monotonic on RangesResult
│   ├── server.py            # 8th tool registered (get_timeline)
│   └── tools/document.py    # +get_timeline, +_require_monotonic_timeline
├── scripts/
│   └── smartcut_spike.py    # GATE — kept as a regression check
├── docs/
│   └── PHASE_6A_SMOKE.md    # NEW — 11-step manual checklist
├── tests/
│   ├── test_phase_6a.py     # MCP-foundation tests (30 → 8-tool list update)
│   ├── test_phase_6a_v3.py  # 29 tests for schema v3 + renderer
│   ├── test_phase_6a_final.py  # NEW — 14 tests for GUI v3 reader + MCP awareness
│   ├── test_document.py     # schema_version assertions updated to v3
│   ├── test_editing.py      # AddCut.reason default updated to None
│   └── test_exporters.py    # schema_version assertion bumped to v3
├── main_mcp.py              # unchanged
└── STATE.md                 # this file
```

---

## 3. Dependencies

Unchanged.

---

## 4. Code inventory (deltas in this pass)

| File | What's new |
|------|------------|
| `core/timeline.py` | NEW types `Clip` (frozen, 4 fields with optional `reason`) and `Timeline` (frozen, `clips: tuple[Clip, ...]`). `is_source_monotonic`, `total_duration_s`, `source_paths` properties on Timeline. `split_into_monotonic_runs()` partitions a non-monotonic playlist into the minimum source-monotonic runs. v2 `subtract_interval` / `union_interval` helpers preserved verbatim. |
| `core/document.py` | `_SCHEMA_VERSION` bumped to 3. `Document.to_json` emits `main_timeline: {clips: […]}` with each clip carrying both `source_id` and a redundant `source_path`. `Document.from_json` chains `_migrate_v1_to_v2_data` → `_migrate_v2_to_v3_data` → `_load_v3` so v1, v2, and v3 inputs all land as v3 in memory. The in-memory `ranges` field is unchanged; `Document.main_timeline` is a new derived `@property`. |
| `core/editing.py` | `AddCut.reason: str \| None = None` (was `str = "manual"`). New `_attach_reason_to_neighbor` helper stamps the reason onto the surviving range so it round-trips. New `_require_monotonic` helper is called at the top of every `apply`; non-monotonic timelines raise `NotImplementedError` with a clear message. `RestoreRange` and `CutWordRange` get the same guard. |
| `core/render.py` | `render_cut` now dispatches on `doc.main_timeline.is_source_monotonic()`. Pre-existing logic (full-coverage shortcut, single smartcut + fade pass) lifted into `_render_monotonic`. New `_render_non_monotonic` partitions into runs, smart_cuts each (reusing `MediaContainer` cache keyed by `source_path`), concats via the new `_ffmpeg_concat_demuxer` helper, then runs one unified `_apply_audio_fades` pass over the final output. Per-run progress is merged into a single 0..1 stream weighted by run output duration. |
| `tests/test_phase_6a_v3.py` | NEW — 29 tests: monotonic truth-table (8 cases), Clip post-init validation, run-splitting correctness (5 cases including the spike's schedule), v2 → v3 migration round-trip, hand-crafted non-monotonic v3 fixture loads, AddCut.reason default + persistence + non-overwrite-when-None, NotImplementedError guards on AddCut/RestoreRange, monotonic-fast-path render unchanged, non-monotonic synthetic render duration ±50 ms, fades across run joins, progress reaches 1.0. |
| `tests/test_document.py` | `Document.SCHEMA_VERSION == 3` (was 2). `to_json` emits `main_timeline` (was `ranges`). v1 migration test now expects v3 in memory. `_v2_payload(...)` keeps emitting `schema_version=2` to exercise the v2→v3 chain. |
| `tests/test_editing.py` | `test_add_cut_default_reason_is_manual` → `test_add_cut_default_reason_is_none`. |
| `tests/test_exporters.py` / `tests/test_phase_6a.py` | `schema_version` assertions bumped to 3. |
| `ui_qt/components/transcript_view.py` | New `_render_source_order` (pre-6a body, locked under hash snapshot) and `_render_playlist_order` (walks clips in playlist order, emits ``— jump to N.NNs —`` between adjacent clips). `set_document_model` dispatches by `is_source_monotonic`. |
| `ui_qt/editor_pane.py` | New `_apply_monotonicity_state()` called from `_render_document`. Disables `cut`, `restore`, `delete`, `save` (and the toolbar Save button) on non-monotonic timelines, stamps `EditorPane.NON_MONOTONIC_TOOLTIP`, and restores Qt's default tooltip when re-enabled. Export stays enabled because rendering is read-only. |
| `mcp_server/errors.py` | Added `EDIT_NOT_SUPPORTED` to the stable code set (client-fixable tier — surfaces as `INVALID_PARAMS` at the JSON-RPC layer). |
| `mcp_server/schemas.py` | New `ClipOut` + `TimelineResult` for the v3-aware tool. `RangesResult` gains `is_source_monotonic: bool` (default `True` so v2-shaped clients keep parsing). |
| `mcp_server/server.py` + `mcp_server/tools/document.py` | Registered `get_timeline` (8th tool). `_require_monotonic_timeline` guard added to `apply_cuts` and `restore_ranges` — refuses with `EDIT_NOT_SUPPORTED` rather than letting `NotImplementedError` bubble through. `get_ranges` docstring documents the lossy-on-non-monotonic behaviour. |
| `tests/test_phase_6a_final.py` | NEW — 14 tests: monotonic transcript hash snapshot, struck-words rendering, non-monotonic playlist-order rendering + boundary marker, edit-actions enabled on monotonic / disabled-with-tooltip on non-monotonic / pane loads non-monotonic without exception, MCP `get_timeline` on monotonic + non-monotonic, `get_ranges` flag on non-monotonic, `apply_cuts` + `restore_ranges` refuse non-monotonic with `EDIT_NOT_SUPPORTED`, `apply_cuts` still works on monotonic. |
| `docs/PHASE_6A_SMOKE.md` | NEW — 11-step manual end-to-end checklist for Claude Desktop. |

### Test count

| Phase | Total | Fast | Slow |
|-------|------:|-----:|-----:|
| End of 5f       | 529 | 517 | 12 |
| 6a MCP          | 559 | 547 | 12 |
| 6a schema v3    | 588 | 571 | 17 |
| **6a final**    | **602** | **585** | **17** |

---

## 5. Public APIs added or reshaped

```python
# core.timeline — NEW
@dataclass(frozen=True)
class Clip:
    source_path: Path
    source_start: float
    source_end: float
    reason: str = ""           # 4th field (deviation from spec's literal 3 fields,
                               # see §8.x), required so AddCut.reason persists
                               # in v3 JSON without inventing a parallel cut_log.
    @property
    def duration_s(self) -> float: ...

@dataclass(frozen=True)
class Timeline:
    clips: tuple[Clip, ...] = ()
    @property
    def total_duration_s(self) -> float: ...
    @property
    def source_paths(self) -> tuple[Path, ...]: ...
    def is_source_monotonic(self) -> bool: ...

def split_into_monotonic_runs(timeline: Timeline) -> list[Timeline]: ...

# core.document — additions
class Document:
    SCHEMA_VERSION: ClassVar[int] = 3
    @property
    def main_timeline(self) -> Timeline: ...

# core.editing — reshaped
class AddCut:
    start: float
    end: float
    reason: str | None = None     # was: reason: str = "manual"
    source_id: str = "src0"

# core.render — internal additions; render_cut signature unchanged
def _render_monotonic(...) -> Path: ...
def _render_non_monotonic(...) -> Path: ...
def _ffmpeg_concat_demuxer(intermediates: list[Path], output_path: Path) -> None: ...
```

---

## 6. What's solid

1. **Monotonic fast path is byte-for-byte unchanged.** The
   `_render_monotonic` body is the v2 `render_cut` body verbatim,
   including the `shutil.copy2` full-coverage shortcut. Pre-existing
   render tests (slow + fast) exercise this path and still pass.
2. **Non-monotonic render produces correct duration.** The synthetic-
   fixture test renders `[(20,25), (5,10), (0,3)]` (13 s expected) with
   `pad_lead=pad_trail=0` and `audio_fade_ms=0`; output duration lands
   within 50 ms of expected, audio/video stay within 10 ms across all
   joins.
3. **Run-splitting is order-preserving.** Concatenating the
   `split_into_monotonic_runs` output reproduces the input timeline.
   Tested against the spike's exact schedule (which factors into 2
   runs, not 3) and against a strictly-descending worst case (every
   clip becomes its own run).
4. **v1 → v2 → v3 migration is lossless on monotonic input.** Existing
   v1 fixtures load as v3 in memory; existing v2 fixtures (unit-test
   payloads) load as v3 in memory; re-saving and reloading is
   equality-preserving.
5. **AddCut.reason persists through JSON.** A cut with
   `reason="filler removal"` lands the string on the surviving range
   immediately preceding the cut (or following, if the cut sits at
   file start). Round-tripping through `to_json` / `from_json` keeps
   it. `reason=None` is a deliberate no-op — the existing reason on
   the neighbour range is preserved, not overwritten with empty string.
6. **Non-monotonic editing fails loudly.** `AddCut`,
   `RestoreRange`, and `CutWordRange` all raise `NotImplementedError`
   at apply time with a clear message, not a silent no-op. The check
   runs at apply (not construction) so the same command instance can
   be reused across documents.
7. **MediaContainer reuse, when same source.** `_render_non_monotonic`
   keeps a `dict[Path, MediaContainer]` cache and reuses the entry
   across runs from the same source path. The spike showed this drops
   3 sequential calls on the heavy HEVC clip from 20.7 s to 13.5 s
   (~35 % saving).
8. **Qt editor monotonic render is locked under a hash snapshot.**
   `tests/test_phase_6a_final.py::test_monotonic_transcript_render_is_unchanged_baseline`
   computes a SHA-256 over `toPlainText()` + the per-word kept/struck
   flag list and compares to a literal locked digest. Any future
   change that affects what monotonic users see in the transcript
   trips this test. Two structural sanity assertions ride alongside
   ("alpha" / "delta" present, no `— jump to` marker) so a hash drift
   has actionable diagnostics.
9. **Non-monotonic Qt editor is read-only and obvious.** Loading a
   non-monotonic v3 doc into the editor:
   - renders the transcript in playlist order (gamma/delta first,
     alpha/beta second for the canonical fixture)
   - inserts an italicized gray `— jump to N.NNs —` block between
     adjacent clips
   - disables `cut`, `restore`, `delete`, `save` actions and stamps
     `Editing non-monotonic timelines is not yet supported (Phase 6b).`
     as the tooltip on each
   - leaves `export` enabled (rendering reads the timeline; the
     run-batched renderer handles non-monotonic by construction)
   The `NotImplementedError` from `core.editing` is the safety net,
   not the first line of defence — the user never reaches it through
   the GUI.
10. **`get_timeline` is the v3-faithful read tool.** `get_ranges` is
    retained for v2-compat clients but its output flattens playlist
    order into source order; `is_source_monotonic` on the response
    flags when the flattening is lossy. `get_timeline` returns the
    full clip list in playlist order with the same flag — clients
    that need re-arrangement-aware reads pick this.
11. **`apply_cuts` / `restore_ranges` refuse cleanly, not via stack
    trace.** Both tools call `_require_monotonic_timeline` before any
    other validation and raise `EDIT_NOT_SUPPORTED` (a stable client-
    fixable code) when the document is non-monotonic. The
    `NotImplementedError` from `core.editing` never bubbles up as
    `INTERNAL_ERROR` for non-monotonic input.

---

## 7. What's fragile or worth knowing

1. **`get_ranges` is intentionally lossy on non-monotonic v3.** The
   tool retains its v2 shape — flat list of ranges with totals — and
   reports `is_source_monotonic` so a client can decide whether to
   call `get_timeline` for the playlist-faithful view. Both tools ship
   in 6a; renaming `get_ranges` is post-6c work if at all.
2. **GUI waveform doesn't yet annotate clip jumps.** `WaveformController`
   still calls `_strip.set_ranges(doc.ranges, duration)` to drive the
   dim-overlay; for non-monotonic v3 docs the strip shows the source's
   full kept-extent without a visual indication of playlist boundaries.
   Acceptable for 6a — the editor disables editing on non-monotonic, so
   the waveform only matters for read-only browse — but worth a 6b pass
   when re-arrangement editing lands.
3. **`_attach_reason_to_neighbor` uses a 1 ns float tolerance.** It
   compares `range.end == cut.start` with `abs(...) < 1e-9`. Cuts whose
   endpoints don't exactly match a range edge (post-`subtract_interval`
   they should; the helper exists for the symmetric cut-at-start case)
   silently drop the reason. Document if a future caller produces
   non-edge-aligned cut endpoints.
4. **Non-monotonic render can't take the full-coverage shortcut.** A
   non-monotonic timeline that happens to cover the full source is
   still a re-arrangement, not a copy. The `_is_full_coverage` check
   only runs on the monotonic path, so the shortcut is correctly
   skipped. Worth knowing if a future caller hand-builds a v3 fixture
   that visits every second of the source out of order — it'll pay
   the per-run smartcut cost.
5. **`_ffmpeg_concat_demuxer` requires identical codec parameters
   across intermediates.** All intermediates from a single source
   match by construction (smartcut emits the same codec/params for
   every cut from one container). For a future multi-source non-
   monotonic render the demuxer will error at concat time and we'll
   need to switch to the concat *filter* (one re-encode generation).
   No multi-source test exercises this today.
6. **Per-run pad_lead / pad_trail apply per clip, not per run.** Each
   clip in each run gets the asymmetric pad treatment via
   `_resolve_keep_ranges`. Three clips with default 100 ms padding
   add 0.6 s to the total output. The new test uses `pad=0` to
   isolate the run-batching from the pad pipeline; production renders
   carry the pad as before.
7. **Progress merge is duration-weighted.** A 30-s run + a 1-s run +
   a 0.5-s run are weighted 30 / 1 / 0.5 in the global 0..1. Per-run
   progress can step in non-uniform increments (smartcut quirks the
   spike already documented), but the merged stream stays monotonic
   (the test asserts that).

---

## 8. Spec deviations and their reasons

1. **Clip has 4 fields, not 3.** The spec lists `Clip(source_path,
   source_start, source_end)` with "Word-boundary snapping at
   construction." We added `reason: str = ""` as a 4th field so
   `AddCut.reason` persists in v3 JSON without inventing a parallel
   `cut_log` structure. The "word-boundary snapping at construction"
   guidance can't literally apply to Clip in isolation (Clip has no
   view of word timestamps); snapping continues to live in
   `core/render.py`'s `_snap_ranges_to_word_boundaries`, run per-run
   on the non-monotonic path.
2. **`Document.main_timeline` is a derived `@property`, not a
   replacement field.** The spec says "main_timeline: Timeline replaces
   v2's ranges." We kept `ranges` as the in-memory storage field
   (preserves the entire test surface — 50+ Document construction
   sites, 6 `replace(doc, ranges=...)` callsites in editing.py — and
   keeps the MCP server from commit `f9c06f5` working without changes)
   and made `main_timeline` a derived view. The on-disk JSON shape
   change (the v3 migration) is real; the in-memory shape change
   is deferred. Renderer and editor branch on
   `main_timeline.is_source_monotonic()`, which works correctly off
   the in-memory `ranges` (a non-monotonic v3 fixture leaves `ranges`
   non-sorted, so the property correctly reports non-monotonic).
3. **GUI was retrofitted in the final pass.** The earlier schema-v3
   commit deferred this; the final commit closes it. `TranscriptView`
   branches on `is_source_monotonic`, the editor pane disables editing
   on non-monotonic with the documented tooltip, and a hash-snapshot
   test guarantees the monotonic render hasn't drifted. The waveform
   strip is the one piece that still falls through to the v2 view (see
   §7.2) — deferred to 6b.

### 6a debt that 6b should clear

- **Clip.reason is a 6a-scoped concession.** The 4th field on
  :class:`Clip` carries `AddCut.reason` so it round-trips through v3
  JSON. 6b will likely want a richer `cut_log` / `move_log` structure
  on :class:`Document` so a re-arrangement command (`MoveClip`,
  whatever its shape) can record both the move's rationale and the
  source/destination indices without piggy-backing on Clip's reason
  field. When that ships, Clip's reason can stay as a per-clip note
  while structural edits live in the log.
- **`Document.main_timeline` is a derived `@property`, not a real
  field.** Trigger condition for finishing the migration: any future
  `Clip` field that can't be expressed on the legacy `Range` type.
  Multi-source is the concrete case — once a Document holds clips from
  two source paths, the `Range.source_id` indirection through
  `doc.sources` breaks down (each clip needs its own path on the wire,
  which it has, but the in-memory `ranges` list can't carry it). At
  that point `main_timeline` becomes the storage field and `ranges`
  flips to a derived compatibility view (or is dropped if no caller
  still needs it).

---

## 9. Smartcut non-monotonic spike — verdict

**Gate: YELLOW. Option 1 with run-batching adopted.**

| Approach | Wall | Output dur. | Re-encode | Verdict |
|---|---:|---:|:---|:---|
| smartcut direct (single non-monotonic call) | 11.4 s | 100.0 s (wrong) | No | broken |
| smartcut per-segment + ffmpeg concat demuxer | 20.7 s | 90.02 s (correct, A/V drift 5.3 ms) | No | correct, slow |
| ffmpeg `-ss/-t` per-segment + concat | 3.4 s | 91.10 s (1.1 s drift) | No | GOP-aligned drift |

Root cause of the broken direct call:
`smartcut.cut_video.make_cut_segments` walks GOPs in source order with
a single linear pointer through `positive_segments`; unsorted input
silently drops or duplicates segments. Smartcut absolutely requires
sorted non-overlapping input.

Decision: source-monotonic timelines stay on the v2 fast path
(byte-identical output). Non-monotonic timelines split into runs;
each run is one smartcut call (sorted by construction); per-run
outputs concat with ffmpeg's concat demuxer (stream-copy, no
re-encode). Cost is `O(order_breaks + 1)` smartcut invocations.
30 ms `afade` post-pass covers every join in the final output,
including the run-boundary joins.

---

## 10. Stop-and-report (per-spec)

**1. MediaContainer reuse outcome + wall-clock delta.**

Implemented. The `_render_non_monotonic` path keeps a
`dict[Path, MediaContainer]` cache keyed by `source_path` and reuses
the entry across runs that share a source. The reuse is safe — the
spike confirmed no state corruption (first call's output sizes
captured before disk filled were correct). Measured savings on the
HEVC 10-bit 5-min source: 3 sequential calls dropped from **20.7 s
(fresh container per call) to 13.5 s (shared container)** — about
**35 % wall-clock improvement**. Most of the saving is the per-call
demux cost on the heavy fixture; on lighter H.264 sources the
absolute saving is smaller but the relative ratio likely similar. We
could not run the comparison on the synthetic fixture (the test
suite uses it for correctness, not perf).

**2. Concat demuxer codec param mismatch.**

Never observed. Every intermediate in a single render comes from
smartcut applied to the same MediaContainer with `audio_settings=
[AudioExportSettings(codec="passthru")]` and `VideoSettings(SMARTCUT,
NORMAL, "copy")`. Codec parameters match by construction. The concat
demuxer's `-c copy` is therefore safe and lossless for every
single-source render. **If the multi-source future arrives**, the
concat demuxer can balk on parameter drift between sources and we'd
need the concat filter (with one re-encode generation). For 6a's
single-source-only assumption, no balk path is reachable.

**3. Run-splitting subtleties.**

A few that turned out subtler than the spec assumed:

- **Touching joins (`a.end == b.start`) are monotonic.**
  `is_source_monotonic` uses strict less-than (`source_start <
  prev_end` → False). v2 timelines after `subtract_interval` /
  `union_interval` produce ranges that often touch exactly; treating
  those as non-monotonic would route every touching-edges v2 doc into
  the slow path. Strictly less-than is the right call.
- **The spike schedule splits into 2 runs, not 3.**
  `[(60,90), (0,30), (180,210)]` factors into `[(60,90)]` and
  `[(0,30), (180,210)]` — the third clip's source_start (180) is ≥
  the second clip's source_end (30), so it joins the second run.
  Worst-case run count is `O(clips)` only when each clip's start is
  strictly less than its predecessor's end; "out of order" doesn't
  imply "needs its own run" by itself. Tested.
- **`_resolve_keep_ranges` reads `doc.ranges`, not a Timeline.** The
  v2 keep-range pipeline (snap-to-word-boundary, asymmetric pad,
  merge_gap) operates on `Range` objects. Per-run, we project a
  `Document` whose `ranges` field is just that run's clips re-
  expressed as Range objects, then run the pipeline. Keeps the
  pipeline reusable across the monotonic and non-monotonic paths
  without duplication.
- **Multi-source runs are independent.** `split_into_monotonic_runs`
  starts a new run when `source_path` changes, even if the new
  source's clips would otherwise be sorted. This is correct for
  multi-source compositions (each source's MediaContainer is
  independent) but means a `[srcA, srcA, srcB, srcA, srcA]`
  schedule produces 3 runs, not 2 — the renderer can't fold the two
  `srcA` chunks back together because the playlist intent says
  "play srcB between them." Documented in `split_into_monotonic_runs`'s
  docstring.

---

## 11. What 6a shipped (cumulative)

- ✅ Smartcut spike committed; gate reported YELLOW; option 1 picked.
- ✅ Schema v3: `Clip`, `Timeline`, `is_source_monotonic`, run-splitting.
- ✅ v1 → v2 → v3 migration in `Document.from_json`; v3 emit in `to_json`.
- ✅ `AddCut.reason: str | None = None`; persisted on neighbour range.
- ✅ Non-monotonic guard on `AddCut` / `RestoreRange` / `CutWordRange`.
- ✅ Run-batched renderer with MediaContainer reuse + unified fade pass
     across all joins (within-run + run-boundary).
- ✅ MCP server foundation (commit `f9c06f5`).
- ✅ Qt editor v3-aware: playlist-order rendering on non-monotonic with
     visible clip boundaries; edit actions disabled with documented
     tooltip; export remains enabled; monotonic render byte-identical
     under hash snapshot.
- ✅ MCP `get_timeline` tool (8th tool); `get_ranges` retained with
     lossy-flag; `apply_cuts` / `restore_ranges` refuse non-monotonic
     with stable `EDIT_NOT_SUPPORTED` code.
- ✅ `docs/PHASE_6A_SMOKE.md` — 11-step manual checklist for Claude
     Desktop end-to-end.
- ✅ All 602 tests pass (529 prior + 30 MCP-foundation + 29 schema-v3
     + 14 final). Ruff clean for changed files. Both GUI entry points
     and the MCP entry import cleanly.

## 12. What's next (Phase 6b candidates)

- **Re-arrangement edit commands.** The smallest viable shape is
  `MoveClip(from_index: int, to_index: int)` operating on
  `Document.main_timeline.clips`. It's the first command that would
  legitimately produce a non-monotonic timeline, and the editor's
  6a-locked safety nets (NotImplementedError + the Qt "disabled with
  tooltip" UX) become the right surface to *un-block* once it lands.
- **`cut_log` / `move_log` storage on Document.** A flat append-only
  list of edit entries (rationale + which structural change + when)
  so MCP analysis tools can show the user *why* the document is in
  the shape it's in. Replaces Clip.reason as the long-term home for
  cut rationale (Clip.reason can stay as a per-clip annotation).
- **Waveform v3 reader.** Pair with the rearrangement UX: clip
  boundaries on the strip, a clear visual difference between "this
  span is cut" and "this span is kept but plays later in the
  playlist."
- **Multi-source compositions.** Trigger to flip `main_timeline` from
  `@property` to real field — see §8 6a-debt note.
- **MCP analysis tools.** First candidate from the prior MCP-only
  6a report: `find_silences(json_path, min_duration_s=0.5)`. Mechanical,
  unambiguously actionable, completes the cleanup loop without needing
  any model judgement.

## 13. Definition-of-done checklist

- [x] Smartcut spike script committed with docstring covering inputs,
      schedule, and YELLOW → option-1+batching outcome.
- [x] v2 → v3 migration round-trip lossless on existing v2 fixtures.
      `is_source_monotonic` truth table tested across 8 cases.
- [x] Run-splitting algorithm: empty / monotonic / spike-schedule /
      worst-case / multi-source / playlist-order-preservation tested.
- [x] Non-monotonic synthetic render: duration ±50 ms, audio sync ≤ 10 ms.
      Fast path against the synthetic fixture confirmed unchanged.
- [x] `AddCut.reason` persists through save/load (with attach-to-
      neighbour heuristic mirroring v1→v2 migration).
- [x] Editing on non-monotonic timelines raises `NotImplementedError`.
- [x] All 529 prior tests stay green; 73 new (30 MCP + 29 schema v3 +
      14 final) — 602 total.
- [x] `python main.py` and `python main_qt.py` import cleanly; `python
      main_mcp.py` starts and lists 8 tools (now including `get_timeline`).
- [x] Qt editor: monotonic render locked under hash snapshot;
      non-monotonic renders in playlist order with boundary marker;
      edit actions disabled with `Editing non-monotonic timelines is
      not yet supported (Phase 6b).` tooltip.
- [x] MCP `get_timeline` tool added; `get_ranges` annotated as lossy
      with `is_source_monotonic` flag; `apply_cuts` / `restore_ranges`
      refuse non-monotonic with stable `EDIT_NOT_SUPPORTED` code.
- [x] `docs/PHASE_6A_SMOKE.md` — 11-step Claude Desktop checklist.
- [x] Ruff clean for changed files.
- [x] STATE.md updated — final pass deliverables, debt notes,
      6b candidates.
