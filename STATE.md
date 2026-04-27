# Transcribe — Project State Report

**Date:** 2026-04-27
**Branch:** main
**Commit:** Phase 4f-3 (3/3) — schema v2 multi-clip-ready Document
**Status:** All 385 tests passing (374 fast + 11 slow). Lint clean.

---

## 1. Phase 4 in two paragraphs

Phase 4 (a–e) gave the codebase the data primitives a Descript-style
editor needs: word-level timestamps, a canonical `Document` model with
schema-versioned JSON, lenient SRT parsing, an undo/redo command stack,
and frame-accurate cutting via smartcut. The customtkinter app gained
one new checkbox ("Editable project (.transcribe.json)") and one new
file written alongside each transcription.

Phase 4f closed the gaps a production-rules audit surfaced after 4e and
reshaped the persisted model for the multi-source Phase 5+ editor.
**4f-0** fixed the Document UTC default-factory and added three PASS
rules. **4f-1** split `pad` into `pad_lead`/`pad_trail`, added 30ms
audio fades at internal joins (post-process via ffmpeg afade — smartcut
has no native fade option), and snapped mid-word cut boundaries at
render ingest. **4f-2** added the Document JSON cache: a transcript is
now reusable across runs as long as the source file's path/mtime/size
hash matches. **4f-3** migrated the persisted model to schema v2: a
`Document` now stores `sources: dict[str, MediaSource]` and
`ranges: list[Range]` (what to KEEP) instead of the v1
`media_path` / `duration` / `cuts` (what to REMOVE) triple. v1 sidecars
on disk continue to load via the migration in `Document.from_json`.

---

## 2. Project structure

```
.
├── core/
│   ├── __init__.py
│   ├── audio.py            # ffmpeg path/duration/extract
│   ├── cache.py            # Phase 4f-2: cache_key sha256(path||mtime||size)
│   ├── document.py         # v2 Document, MediaSource, Range, build_document, v1→v2 migration
│   ├── editing.py          # AddCut, RestoreRange, CutWordRange, CommandStack (no MergeAdjacentCuts)
│   ├── exporters.py        # render_txt/srt/vtt + parse_srt + write_outputs
│   ├── languages.py        # 99-language registry
│   ├── model_loader.py     # HF download with progress
│   ├── models.py           # model registry, cache paths
│   ├── render.py           # render_cut consuming v2 ranges
│   ├── settings.py         # JSON-on-disk preferences
│   ├── timeline.py         # Phase 4f-3: subtract_interval / union_interval (pure helpers)
│   └── transcriber.py      # faster-whisper wrapper, returns list[Segment] with words
├── ui/
│   ├── app.py              # main controller (worker checks Document JSON cache before inference)
│   ├── components/
│   │   ├── drop_zone.py
│   │   ├── language_picker.py
│   │   ├── model_picker.py
│   │   ├── output_formats.py
│   │   ├── progress_card.py
│   │   ├── result_card.py
│   │   └── settings_panel.py
│   ├── state.py
│   └── theme.py
├── docs/
│   └── PRODUCTION_RULES.md  # codified decisions; loaded into every session via CLAUDE.md
├── tests/
│   ├── conftest.py             # synthetic_video, probe_duration, is_playable
│   ├── fixtures/
│   │   ├── sample.wav
│   │   ├── srt/                # 8 SRT round-trip fixtures
│   │   └── synthetic.mp4       # gitignored, generated on first run
│   ├── test_audio.py
│   ├── test_bootstrap.py
│   ├── test_cache.py            # Phase 4f-2
│   ├── test_document.py         # v2 round-trip + migration tests
│   ├── test_editing.py          # range-based commands
│   ├── test_exporters.py
│   ├── test_language_picker.py
│   ├── test_model_loader.py
│   ├── test_models.py
│   ├── test_render.py
│   ├── test_settings.py
│   ├── test_settings_panel.py
│   ├── test_state.py
│   ├── test_timeline.py         # Phase 4f-3
│   ├── test_transcriber.py
│   └── test_ui.py
├── scripts/
│   ├── cli_test.py
│   └── word_probe.py
├── resources/
│   ├── bin/ffmpeg-mac
│   ├── fonts/
│   └── icons/
├── main.py
├── pyproject.toml
├── requirements.txt
├── requirements-dev.txt
├── CLAUDE.md
├── STATE.md                    # this file
└── whisper_transcriber_spec.md
```

---

## 3. Dependencies

### requirements.txt (unchanged from Phase 4d-0)

```
customtkinter==5.2.2
tkinterdnd2==0.4.2
faster-whisper==1.2.1
huggingface-hub==0.24.0
smartcut==1.7
```

### Resolved in venv

```
av==16.0.1
ctranslate2==4.7.1
customtkinter==5.2.2
faster-whisper==1.2.1
huggingface-hub==0.24.0
numpy==2.4.4
onnxruntime==1.25.0
smartcut==1.7
tkinterdnd2==0.4.2
tqdm==4.67.3
```

`pip check` exits 0. No new runtime deps added in 4f.

### Python / platform

- Python 3.11.15 (`>=3.11,<3.12`)
- Apple Silicon M4, 16 GB
- CPU-only inference (`compute_type="int8"`, `device="auto"`)

---

## 4. Code inventory

### core/ (2,143 LOC)

| File | Lines | What's new in 4f |
|------|------:|------------------|
| `core/render.py` | 517 | v2 ranges; outward-snap word boundaries; `_merge_close_keep_ranges`; `_apply_audio_fades`; `_join_times_in_output`; `pad_lead`/`pad_trail`/`audio_fade_ms` |
| `core/document.py` | 424 | `MediaSource`, `Range`; v2 `Document` with `sources`+`ranges`; v1→v2 migration in `from_json`; `_attach_cut_reason` migration helper |
| `core/editing.py` | 297 | `AddCut`/`RestoreRange`/`CutWordRange` against v2 ranges; `MergeAdjacentCuts` removed |
| `core/exporters.py` | 261 | unchanged in 4f (v2 `Document` flows through `to_json` transparently) |
| `core/languages.py` | 156 | unchanged |
| `core/timeline.py` | 152 | NEW (4f-3): `subtract_interval`, `union_interval` |
| `core/transcriber.py` | 133 | unchanged |
| `core/audio.py` | 120 | unchanged |
| `core/settings.py` | 105 | unchanged |
| `core/models.py` | 82 | unchanged |
| `core/model_loader.py` | 54 | unchanged |
| `core/cache.py` | 47 | NEW (4f-2): `cache_key` |

### ui/ (1,632 LOC)

| File | Lines | What's new in 4f |
|------|------:|------------------|
| `ui/app.py` | 523 | 4f-2 cache lookup before inference; cache-hit fast path; `_CachedInfo` adapter; v2-aware `_emit_cache_hit_done` (reads `cached.sources["src0"].duration`) |
| `ui/state.py` | 239 | unchanged |
| `ui/components/*` | 870 | unchanged |

### tests/ (4,431 LOC, +957 from 4e end)

| File | Lines | What's new in 4f |
|------|------:|------------------|
| `tests/test_render.py` | 676 | full-coverage detection; asymmetric pad; audio-fade envelope; `_snap_ranges_to_word_boundaries`; v2 `_doc` helper |
| `tests/test_document.py` | 564 | v2 round-trip; v1→v2 migration suite; `MediaSource`/`Range`; cut-at-timestamp-0 edge case; pre-4f-2 v1 file load |
| `tests/test_exporters.py` | 519 | unchanged from 4e (one minor v2 update on `_example_doc`) |
| `tests/test_ui.py` | 449 | 4f-2: cache hit/miss/mtime-invalidate/hash-absent/corrupt-JSON tests |
| `tests/test_editing.py` | 406 | range-based commands; `RestoreRange` round-trip; mixed-command stack flow |
| `tests/test_transcriber.py` | 295 | end-to-end JSON test asserts v2 shape (sources["src0"], ranges) |
| `tests/test_state.py` | 267 | unchanged |
| `tests/test_language_picker.py` | 232 | unchanged |
| `tests/test_timeline.py` | 210 | NEW (4f-3): 29 tests for `subtract_interval` + `union_interval` |
| `tests/test_settings.py` | 135 | unchanged |
| `tests/test_settings_panel.py` | 123 | unchanged |
| `tests/test_audio.py` | 121 | unchanged |
| `tests/test_models.py` | 103 | unchanged |
| `tests/test_bootstrap.py` | 89 | unchanged |
| `tests/test_model_loader.py` | 79 | unchanged |
| `tests/test_cache.py` | 63 | NEW (4f-2): 5 tests for `cache_key` |

### Test count

| Phase | Total | Fast | Slow |
|-------|------:|-----:|-----:|
| End of Phase 4e | 318 (claimed; actual ~319) | 309–315 | 3–9 |
| End of Phase 4f-0 | 319 | 310 | 9 |
| End of Phase 4f-1 | 333 | 321 | 12 |
| End of Phase 4f-2 | 349 | 337 | 12 |
| End of Phase 4f-3 (1/3) | 378 | 366 | 12 |
| **End of Phase 4f-3** | **385** | **374** | **11** |

`pytest -q` (no marker filter) runs all 385 green in ~7 s on this M4 with the model cached.

---

## 5. Git history

```
phase 4f-3 (3/3) — final: docs + STATE.md (this commit)
phase 4f-3 (2/3): schema v2 multi-clip-ready document with v1 migration
phase 4f-3 (1/3): timeline helpers + Range/MediaSource types
phase 4f-2: document json cache via source_hash
docs: tighten audio-passthru rule to reflect single-pass re-encode reality
phase 4f-1: pad_lead/pad_trail + audio fades + render-time boundary snap
phase 4f-0: utc default-factory fix + three doc additions
docs: add production rules + reference from CLAUDE.md
Phase 4 done: refresh STATE.md to current state of main
Phase 4e: write Document JSON next to .srt/.txt; checkbox to toggle
…
```

Each 4f sub-phase landed as its own commit; 4f-3 split into three commits as the spec authorized for that sub-phase only.

---

## 6. Public APIs added or reshaped in Phase 4f

```python
# core.cache (Phase 4f-2)
def cache_key(source_path: Path) -> str: ...
    """sha256(absolute_path_bytes || NUL || mtime_int || NUL || size_int)"""

# core.timeline (Phase 4f-3)
def subtract_interval(
    ranges: Sequence[Range], interval: tuple[float, float], source_id: str,
) -> list[Range]: ...

def union_interval(
    ranges: Sequence[Range], interval: tuple[float, float], source_id: str,
) -> list[Range]: ...

# core.document (Phase 4f-3 — reshaped)
@dataclass(frozen=True)
class MediaSource:
    id: str
    path: Path
    duration: float
    hash: str = ""

@dataclass(frozen=True)
class Range:
    source_id: str
    start: float
    end: float
    reason: str = ""

@dataclass(frozen=True)
class Document:
    SCHEMA_VERSION: ClassVar[int] = 2
    sources: dict[str, MediaSource]
    segments: list[Segment]
    ranges: list[Range]
    language: str | None
    created_at: datetime
    model_name: str = ""
    source_hash: str | None = None
    def to_json(self) -> dict[str, Any]: ...
    @classmethod
    def from_json(cls, data: dict) -> Document: ...   # branches on schema_version

def build_document(
    *, media_path, duration, language, segments, model_name, source_hash=None,
) -> Document: ...   # constructs single-source v2 Document with one full-duration range

# core.editing (Phase 4f-3 — reshaped)
class AddCut:           # subtract_interval
    start: float; end: float; reason: str = "manual"; source_id: str = "src0"
class RestoreRange:     # union_interval — replaces v1's RemoveCut(index=…)
    start: float; end: float; source_id: str = "src0"
class CutWordRange:     # word-bounded subtract_interval
    seg_idx: int; word_start_idx: int; word_end_idx: int
    reason: str = "manual"; source_id: str = "src0"
# MergeAdjacentCuts is REMOVED.

# core.render (Phase 4f-1, ranges in 4f-3)
def render_cut(
    doc: Document, output_path: Path,
    on_progress: Callable[[float], None] | None = None, *,
    pad_lead: float = 0.10, pad_trail: float = 0.10,
    merge_gap: float = 0.30, audio_fade_ms: int = 30,
    pad: float | None = None,   # deprecated, sets both pad_lead and pad_trail
) -> Path: ...
```

---

## 7. Document JSON format (schema v2)

Saved as `<source_stem>.transcribe.json` next to the source media (or under `output_dir`).

```json
{
  "schema_version": 2,
  "sources": {
    "src0": {
      "id": "src0",
      "path": "/path/to/sample.wav",
      "duration": 6.10,
      "hash": "<sha256 of path||mtime||size>"
    }
  },
  "language": "en",
  "model_name": "tiny",
  "created_at": "2026-04-27T15:32:18.123456+00:00",
  "segments": [
    {
      "text": " This is an example sound file...",
      "start": 0.0,
      "end": 6.1,
      "words": [
        {"text": " This", "start": 0.0,  "end": 0.26, "probability": 0.89},
        {"text": " is",   "start": 0.26, "end": 0.52, "probability": 0.99}
      ]
    }
  ],
  "ranges": [
    {"source_id": "src0", "start": 0.0, "end": 6.10, "reason": ""}
  ],
  "source_hash": "<same sha256 as sources.src0.hash>"
}
```

`schema_version` is mandatory. `Document.from_json` raises
`UnsupportedSchemaError` on missing/null/unknown. `schema_version: 1`
is recognized as a migration path, not a coercion: it routes through
`_migrate_v1_to_v2`, which subtracts each v1 cut from a full-source
keep-range and attaches each cut's reason to the surviving range
immediately preceding (or following, for cuts at the file's start) the
removed interval.

---

## 8. What's solid

1. **Boundary types own the contract.** faster-whisper's objects don't
   leak past `core/transcriber.py`. The rest of the system speaks
   `Segment` / `Word` / `Document` — and now `MediaSource` / `Range` —
   exclusively.
2. **Frozen Document + replace-only commands.** Tests prove every
   command's `apply → revert` round-trips to an equal Document. Undo
   stack is correct on the classic fork case.
3. **Timeline helpers are pure.** `subtract_interval` /
   `union_interval` are sorted-in / sorted-out, never mutate inputs,
   raise on inverted intervals or mismatched source_id. Every
   `EditCommand` is a one-liner against them. 29 tests cover the
   exhaustive case list.
4. **render_cut is byte-for-byte under no edit.** Full-coverage ranges
   short-circuit to `shutil.copy2`; no smartcut, no transcoding. The
   "no edit ⇒ no transcoding" invariant survived the v1→v2 reshape
   (the detection logic just moved from `not doc.cuts` to
   `_is_full_coverage(doc.ranges, source.duration)`).
5. **Schema migrations are written, not skipped.** v1 sidecars load
   via `_migrate_v1_to_v2`. Migration is on read; the on-disk file is
   not auto-rewritten — write-through happens on the next save. This
   policy is now codified as a PASS rule.
6. **Cache key is path+mtime+size.** Full content hash is too slow on
   long media; the rsync-style heuristic is wrong only when a user
   replaces a file with byte-identical content at the same mtime, an
   acceptable edge case.
7. **Audio fades use ffmpeg `afade` with `enable=` gating.** Without
   `enable`, chained fades silence the entire track — captured as a
   production rule so future-us doesn't repeat the mistake.

---

## 9. What's fragile or worth knowing

1. **Segments are still source-id-less.** A v2 Document supports
   multiple `MediaSource` entries, but transcripts are still tied to
   "the source" implicitly. `core.render._select_single_source` raises
   if more than one source's ranges appear. Phase 5 multi-source
   compositing will need either a per-segment `source_id` field or a
   different transcript model. The schema accommodates the future
   change; the behavior doesn't yet.
2. **`MergeAdjacentCuts` is gone.** v1's "merge close cuts" command
   had a render-pipeline use (pre-merge before inversion) that v2
   doesn't need (ranges are canonicalized at every edit). The
   `merge_gap` semantics moved into `core.render._merge_close_keep_ranges`
   as a non-command helper. If a future workflow wants user-visible
   merge-of-close-ranges, it should be added as a new EditCommand
   built on `union_interval`, not by resurrecting the old class.
3. **`cached.sources["src0"]` assumption in ui/app.py.** The cache-hit
   path in `_emit_cache_hit_done` reads the primary source's duration
   directly. For multi-source projects this is undefined. Phase 5 will
   need to either pick a "primary" source or compute the timeline
   duration from ranges.
4. **`av==16.0.1` resolves cleanly but is recent.** Unchanged caveat
   from Phase 4d. smartcut forces it; faster-whisper>=1.1.0 permits it.
5. **smartcut's `emit()` is non-monotonic.** Wrapped by
   `_ProgressAdapter`; same caveat as Phase 4d.
6. **Synthetic video fixture is per-developer.** Generated on first
   slow-test run via `ffmpeg-mac`. Tests `pytest.skip` cleanly when the
   binary isn't on disk.
7. **Threading model unchanged.** Single worker thread, single
   transcriber. Phase 5 may revisit.

---

## 10. Definition-of-done checklist (4f)

- [x] **4f-0** UTC default-factory fix; three new PASS rules in `PRODUCTION_RULES.md`.
- [x] **4f-1** `pad_lead`/`pad_trail` split (with deprecated `pad`); `audio_fade_ms` post-process via ffmpeg afade with `enable=` gating; render-time word-boundary snap.
- [x] **4f-2** Document JSON cache; `cache_key` in `core.cache`; `source_hash` field on `Document`; cache lookup in worker; cache-hit doesn't reconstruct `Transcriber`; mtime change invalidates.
- [x] **4f-3** `core/timeline.py` with the two helpers; v2 `Document` shape (`sources` + `ranges`); v1→v2 migration in `from_json`; `EditCommand` rewrite (`AddCut`/`RestoreRange`/`CutWordRange`); `render_cut` consumes ranges; tests migrated; migration edge-case tests including cut-at-timestamp-0.
- [x] All 385 tests pass (`pytest -q`).
- [x] `PRODUCTION_RULES.md` updated (no remaining GAP rules; new PASS rules captured).
- [x] `STATE.md` overwritten in place (this file).

---

## 11. What Phase 5 inherits

- A v2 `Document` with multi-source schema and a keep-range timeline.
- `core.timeline` helpers — every future edit operation reduces to one
  of two function calls, regardless of how many sources or ranges.
- A migration path that lets users open v1 sidecars from any prior
  build without re-transcribing.
- A working render pipeline with frame-accurate cuts, asymmetric pads,
  click-suppression fades, and word-boundary snap — already keyed off
  v2 ranges.
- An undo/redo command stack ready to drive the editor view.
- A Document JSON cache so opening a project is fast even after closing
  and reopening, and timestamps are immutable for the life of the
  project.
- A clean `core/` ↔ `ui/` boundary maintained throughout 4f. The
  PySide6 work in Phase 5 can start on the GUI side without unwinding
  anything in `core/`.

## 12. Phase 4f-3 final report (per spec request)

**1. Total tests, total LOC delta from 4f-0 baseline.**
- Tests: 319 (4f-0 start) → 385 (4f-3 end), +66. Fast 310 → 374 (+64); slow 9 → 11 (+2).
- LOC delta in `core/` from 4f-0 baseline: +473 (new `cache.py`, `timeline.py`; reshaped `document.py`, `editing.py`, `render.py`).
- LOC delta in `tests/` from 4f-0 baseline: +957.
- LOC delta in `ui/`: +109 (cache helpers + cache-hit fast path).

**2. New modules.**
- `core/timeline.py` — pure interval-arithmetic helpers (`subtract_interval`, `union_interval`).
- `core/cache.py` — `cache_key` (added in 4f-2; listed here for completeness).
- `tests/test_timeline.py`, `tests/test_cache.py` — companion tests.

**3. Existing modules that changed shape.**
- `core/document.py`: `Document` lost `media_path` / `duration` / `cuts` fields, gained `sources: dict[str, MediaSource]` and `ranges: list[Range]`. New `MediaSource` and `Range` types. `from_json` now branches on `schema_version` and routes v1 through a migration. `to_json` always emits v2.
- `core/editing.py`: `AddCut` rewritten to subtract from ranges; `RemoveCut(index=…)` replaced by `RestoreRange(start, end)`; `MergeAdjacentCuts` removed entirely; `CutWordRange` rewritten to subtract a word-bounded interval. Each command captures the pre-apply ranges list for `revert`.
- `core/render.py`: consumes `doc.ranges` directly. Empty ranges → `ValueError`. Full-coverage shortcut. `_snap_ranges_to_word_boundaries` reverses snap direction (outward for keeps, vs. v1's inward for cuts). Inversion helper `_invert_cuts_to_keep_ranges` is gone (we don't invert anything in v2). New `_merge_close_keep_ranges` for the render-time merge-gap behavior.
- `ui/app.py`: cache-hit path reads `cached.sources["src0"].duration` instead of `cached.duration`.

**4. Fragile decisions where the prompt left judgment to me.**
- *Whether to keep `Document.media_path` / `Document.duration` as backward-compat properties on v2.* I chose not to — callers had to update to use `sources["src0"]` or `next(iter(sources.values()))`. The blast radius was small (one line in `ui/app.py`, a handful in tests). Adding properties would have invited confusion ("which one is canonical?") without buying much.
- *`union_interval`'s reason policy.* The spec said "leftmost reason wins" but didn't pin down what happens when the new interval extends past the leftmost overlapping range. I took the simple rule: if any overlapping range exists, the leftmost overlapping range's reason wins; else the new range's reason is empty. The spec for `RestoreRange` doesn't take a reason, so the inserted-from-nothing case has no source for one anyway.
- *Migration's reason-attach algorithm with multiple cuts.* When a second cut is processed, the "preceding range" lookup may not match exactly (the preceding range's `end` might not equal `cut.start` because the range has already been clipped by an earlier cut). I let the algorithm fall through to "following" in that case, which yields a defensible result (see `test_migration_two_adjacent_cuts_handled_correctly` — the second cut's reason ends up on the range *after* the second cut, not on the truncated range before it). A different policy would be defensible too; this one is consistent and tested.
- *Whether to make `union_interval` accept a `reason` parameter for the new interval.* The spec's signature didn't include one and the migration uses `subtract_interval` plus a separate reason-attach step, so I kept `union_interval`'s signature minimal.

**5. Tests that were hard to migrate from v1 to v2.**
- `test_resolve_two_cuts_with_sub_merge_gap_keep_range_absorbed` — was implicitly asserting that v1's `MergeAdjacentCuts` ran *before* inversion, which produced 2 keep-ranges. v2's pipeline never has that pre-merge step (cuts don't exist; ranges are already canonical), so the same input produces 3 keep-ranges with different padding outcomes. The test now documents the new behavior, but it's the clearest case where a v1 test was asserting *shape*, not *behavior*.
- `test_remove_cut_*` (deleted) — assumed cuts were a list you could index into. There is no analogous behavior in v2; `RestoreRange` is the closest equivalent and it operates on intervals, not indices. Phase 5's editor view should plan around interval semantics, not list-position semantics.
- `test_merge_*` (deleted) — `MergeAdjacentCuts` is gone. The render-time merge-gap behavior is tested via `_merge_close_keep_ranges` directly, not as an EditCommand round-trip.

**6. `MergeAdjacentRanges` decision: dropped.**
v1's `MergeAdjacentCuts` had two consumers: the render pipeline (pre-merge before inversion) and any future user-visible "clean up close cuts" workflow. v2 ranges are canonicalized at construction (every helper produces sorted, non-overlapping output), so the editor will never have "two ranges 0.05s apart" to merge. The render-time merge-gap behavior moved to `core.render._merge_close_keep_ranges` as a private helper. If Phase 5 wants user-visible "merge close ranges" it should be a new command built on `union_interval`, not a resurrection of the old class.

**7. Cut-at-timestamp-0.0 edge case.**
Confirmed test exists: `test_migration_cut_at_start_attaches_reason_to_following_range` in `tests/test_document.py`. A v1 cut with `start=0.0, end=2.0` has no preceding range to attach its reason to (the original full-source range gets truncated to `(2.0, 10.0)` by the subtract). The migration falls through to the immediately-following range and attaches the reason there.
