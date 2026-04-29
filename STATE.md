# Transcribe — Project State Report

**Date:** 2026-04-29
**Branch:** main
**Commits:** Phase 6a in two passes —
  * `f9c06f5` — MCP server foundation (transcribe / read / cut / render).
  * `1f4eca1` — smartcut non-monotonic spike (YELLOW gate, option 1 picked).
  * Pending — schema v3 (Clip/Timeline) + run-batched renderer + AddCut.reason.

**Status:** Phase 6a's foundation is complete. All 588 tests pass (529
prior + 30 MCP-foundation + 29 schema-v3). Lint clean for changed files.
GUI v3 reader and MCP-tool retrofitting (clip-aware naming) are
deferred to later passes.

---

## 1. Phase 6a in three paragraphs

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
├── ui_qt/                   # unchanged in this pass — GUI v3 reader is a later pass
├── mcp_server/              # unchanged in this pass — clip-aware naming is a later pass
├── scripts/
│   └── smartcut_spike.py    # GATE — kept as a regression check
├── tests/
│   ├── test_phase_6a.py     # MCP-foundation tests (30, schema_version assertion bumped to 3)
│   ├── test_phase_6a_v3.py  # NEW — 29 tests for schema v3 + renderer
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

### Test count

| Phase | Total | Fast | Slow |
|-------|------:|-----:|-----:|
| End of 5f       | 529 | 517 | 12 |
| 6a MCP          | 559 | 547 | 12 |
| **6a schema v3** | **588** | **571** | **17** |

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

---

## 7. What's fragile or worth knowing

1. **MCP server still uses v2 vocabulary in its tool surface.** The
   `get_ranges` tool returns `RangesResult { ranges: list[RangeOut],
   total_kept_s, total_cut_s }`. Renaming to clip-shaped output is
   deliberately deferred — clients that already wired against the v2
   shape (including the test suite from commit `f9c06f5`) keep working.
   When 6b ships re-arrangement we'll need to add a parallel
   `get_timeline` tool that returns the playlist with non-monotonic
   ordering preserved; the renaming question can wait until then.
2. **GUI doesn't yet show non-monotonic timelines correctly.**
   `ui_qt/components/transcript_view.py`, `waveform_controller.py`,
   and `editor_pane.py` still iterate `doc.ranges` directly. For
   monotonic v3 documents (everything 6a editing produces) this is
   indistinguishable from v2 behaviour — no visible regression. For a
   hand-crafted non-monotonic v3 fixture, the transcript view will
   render words in source-time order rather than playlist order, and
   the waveform will look strange. Editing actions are also not
   blocked for non-monotonic — they'd fail at the
   `_session.apply(command)` boundary with `NotImplementedError`. The
   GUI v3 reader is a separate pass.
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
3. **GUI not retrofitted in this pass.** The spec called this out as
   a concurrent sub-agent; we deferred it. Monotonic documents render
   identically to v2; non-monotonic documents are not yet user-
   creatable in the GUI flow. See §7.2.

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

## 11. What's done in 6a so far

- ✅ Smartcut spike committed; gate reported YELLOW; option 1 picked.
- ✅ Schema v3: `Clip`, `Timeline`, `is_source_monotonic`, run-splitting.
- ✅ v1 → v2 → v3 migration in `Document.from_json`; v3 emit in `to_json`.
- ✅ `AddCut.reason: str | None = None`; persisted on neighbour range.
- ✅ Non-monotonic guard on `AddCut` / `RestoreRange` / `CutWordRange`.
- ✅ Run-batched renderer with MediaContainer reuse + unified fade pass
     across all joins (within-run + run-boundary).
- ✅ MCP server foundation (commit `f9c06f5`, separate prior pass).
- ✅ All 588 tests pass (529 prior + 30 MCP-foundation + 29 schema-v3).

## 12. What's left for 6a (deferred to later passes)

- ⏳ GUI v3 reader: `ui_qt/components/transcript_view.py`,
  `waveform_controller.py`, `editor_pane.py` still iterate
  `doc.ranges` directly. For non-monotonic documents the transcript
  view should show clips in playlist order with a visual boundary at
  run joins; the waveform should fall back to "show full source as
  kept" gracefully; cut/restore/save actions should be disabled (with
  tooltip explaining the limitation). Existing GUI tests pass
  unchanged because monotonic v3 docs look identical to v2.
- ⏳ MCP `get_ranges` → `get_timeline` (or addition of
  `get_timeline`). The current `RangesResult` shape is fine for
  monotonic documents and has clients (the `f9c06f5` test suite); a
  parallel timeline-aware tool will be added when 6b ships
  re-arrangement.
- ⏳ Manual end-to-end smoke through Claude Desktop on a v3 document.

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
- [x] All 529 prior tests stay green (only assertions touching
      `schema_version` / `AddCut.reason` default needed updates;
      production code unchanged for those tests).
- [x] `python main.py` and `python main_qt.py` import cleanly; `python
      main_mcp.py` starts and lists 7 tools.
- [x] Ruff clean for changed files.
- [x] STATE.md updated in place — schema version, renderer strategy,
      MediaContainer finding, AddCut.reason, what's done so far,
      what's left.
