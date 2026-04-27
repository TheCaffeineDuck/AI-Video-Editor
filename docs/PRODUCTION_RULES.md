# Production Rules — Transcribe

This document codifies non-obvious decisions about how this project cuts, renders, caches, and persists. It is loaded into every Claude Code session via `[CLAUDE.md](http://CLAUDE.md)` so that future changes don't silently violate constraints chosen for reasons.

Each rule has:

- **Rule** — the normative statement
- **Why** — rationale (delete this and the rule looks arbitrary)
- **Status** — `PASS` (implemented), `GAP` (scheduled for Phase 4f), or `FUTURE` (later phase, captured here so the architecture leaves room)
- **Where** — file or function reference

If a rule looks wrong while you're modifying code that touches it, change the rule first (in this doc, in a commit) before changing the code.

---

## Cutting and rendering

### Per-segment extract → concat, never single-pass filtergraph

**Rule.** When rendering a cut version of a video, extract each kept range as an independent intermediate, then concatenate. Never build a single ffmpeg filtergraph that does the whole edit in one pass.

**Why.** A 100-segment filtergraph using `aselect`/`vselect`/`concat` chains is a debugging nightmare: a failure at segment 47 surfaces as a generic ffmpeg error with no clue which segment caused it. Per-segment intermediates mean each cut is isolated — a single bad timestamp affects exactly one file, not the whole render. This is also how smartcut works: per keep-range, copying when frame boundaries align, re-encoding only at the boundaries.

**Status.** `PASS` — `core/[render.py](http://render.py):render_cut` delegates to smartcut, which is per-segment by construction.

**Where.** `core/[render.py](http://render.py)`

### Never cut inside a word

**Rule.** Cut boundaries always sit on word boundaries — never mid-word.

**Why.** Cutting mid-word produces an audible glitch that no fade can rescue, and there's no use case where it's the right thing to do. A user who selects part of a word in the editor means "cut the whole word" — not "cut a fraction of the audio." Two layers of enforcement matter: high-level callers using `CutWordRange` are blocked at construction time, but the renderer also accepts low-level `AddCut(start, end)` operations whose boundaries may land mid-word; snapping at render ingest catches those without making `AddCut` itself rejecting (filler-removal heuristics that operate on time-only intervals stay simple).

**Status.** `PASS` — enforced at TWO points. (1) At construction in `CutWordRange`: building one whose boundaries don't match a word's start/end raises `ValueError`. (2) At render ingest: `_resolve_keep_ranges` calls `_snap_cuts_to_word_boundaries` on `doc.cuts` before inversion, snapping each cut's start/end to the nearest word boundary across all segments. Cuts that don't overlap any word (e.g. pure-silence cuts between segments) pass through unchanged. Tie-break: snap outward, erring on cutting more.

**Where.** `core/[editing.py](http://editing.py):CutWordRange` (construction-time check); `core/[render.py](http://render.py):_snap_cuts_to_word_boundaries` (render-ingest snap, Phase 4f-1).

### Pad direction expands keep-ranges

**Rule.** The `pad` parameter to `render_cut` widens kept ranges, which equivalently shrinks cut ranges. A `pad=0.10` cut leaves 100ms of the cut content on each side intact.

**Why.** Whisper's word boundaries are slightly conservative — it tends to call a word ended a few tens of ms before the actual acoustic decay. If pad shrunk keep-ranges, every cut would clip the trailing consonants of words on either side. Expanding kept ranges guarantees the surrounding words are intact, at the cost of leaving 100ms of cut content. For removing fillers and silences that's the right trade. The reverse semantic ("widen the cut to be safe") clips word audio and is wrong.

**Status.** `PASS` — implemented and tested in Phase 4d-1.

**Where.** `core/[render.py](http://render.py):render_cut`

### Asymmetric pad: lead and trail are independent parameters

**Rule.** `render_cut` exposes `pad_lead` (before each kept range) and `pad_trail` (after) as separate parameters. Both default to 0.10. Per-project overrides allowed.

**Why.** Leading and trailing time around a cut serve different purposes. Leading is "breath before the next word" — too much and pacing drags. Trailing is "decay of the previous word + breath" — too little and consonants clip, too much and the dead air signals "this was edited." Symmetric `pad=0.10` is a fine starting point but the right defaults for emotional weight are typically asymmetric (less lead, more trail). Surfacing them separately is what makes that tunable.

**Status.** `PASS` — Phase 4f-1 split `pad` into `pad_lead` and `pad_trail`, both defaulting to 0.10. The legacy `pad` keyword is retained as a deprecated parameter that emits `DeprecationWarning` and applies symmetrically.

**Where.** `core/[render.py](http://render.py):render_cut`.

### 30ms audio fades at every segment boundary

**Rule.** Every cut boundary in the rendered output applies a 30ms fade on the audio track — fade-out before the cut, fade-in after. Configurable via `audio_fade_ms`, default 30. Values above 50ms are discouraged (viewers hear the dissolve).

**Why.** Hard cuts at arbitrary samples produce a discontinuity in the waveform — a click or pop, depending on amplitude at the cut point. 30ms is below the threshold of conscious perception of "fade" but above the threshold needed to avoid the discontinuity. Smartcut's frame-accurate cutting solves the visual side; the audio still needs this even on per-frame-aligned cuts because audio samples don't align to frame boundaries.

**Status.** `PASS` — Phase 4f-1 added `audio_fade_ms` (default 30). Investigation confirmed smartcut has no native fade option (`AudioExportSettings` only exposes codec/channels/bitrate/sample_rate/denoise; no `fade` references anywhere in the package). Implementation: after smartcut writes the cut output, a second ffmpeg pass with `-c:v copy -af "afade=t=out:...:enable='between(t,...)',afade=t=in:...:enable='between(t,...)'"` applies the fades only at internal segment joins (not the file's outer boundaries). The `enable=` gating is mandatory: a bare `afade=t=out` silences everything past `st+d`, so an unguarded chain of fade-in/out pairs silences the entire track. Values >50 emit a logger warning.

**Where.** `core/[render.py](http://render.py):_apply_audio_fades` and `core/[render.py](http://render.py):render_cut`.

### Subtitles render last in the filter chain

**Rule.** When subtitle burn-in lands (Phase 6), the subtitle filter is applied to the concatenated output, not to each segment intermediate. Filter chain order: per-segment cut → concat → audio fades at joins → subtitle burn-in → final encode.

**Why.** Burning subtitles per-segment produces stutters at boundaries (the subtitle filter's internal state resets at each segment). Burning after concat means subtitle timestamps are output-timeline timestamps, not source-timeline (see "Output-timeline SRT" below). Baking the order into `core/[render.py](http://render.py)`'s structure now means Phase 6 doesn't need to refactor.

**Status.** `FUTURE` (Phase 6). Captured now so `core/[render.py](http://render.py)` is structured to accommodate it. No code yet.

**Where.** `core/[render.py](http://render.py)` structure; Phase 6 burn-in TBD.

### Output-timeline SRT (deferred)

**Rule.** When subtitle burn-in lands, the SRT used for burn-in carries output-timeline timestamps — reflecting position in the rendered output, not the source. The source-timeline SRT (the one we ship today) remains the editing artifact.

**Why.** Burning a source-timeline SRT into a cut output produces subtitles at the wrong times because cuts have changed the timeline. Two SRTs serve two purposes: source-timeline for editing/reference, output-timeline for burn-in.

**Status.** `FUTURE` (Phase 6). Do not write the second renderer until Phase 6 burn-in actually has a caller — code without a caller rots.

**Where.** TBD.

### Empty-cuts render is a byte-for-byte copy

**Rule.** When `doc.cuts` is empty, `render_cut` does a `shutil.copy2` of the source rather than routing through smartcut.

**Why.** The invariant "no edit ⇒ no transcoding" matters because (a) lossless preservation is the user's expectation when they hit Render on an unedited project, and (b) smartcut may re-mux or container-fixup in ways that surprise downstream tooling. Future "consistency" refactors that route everything through smartcut would silently break this.

**Status.** `PASS` — `core/[render.py](http://render.py):172-176` (the `if not doc.cuts:` shortcut).

**Where.** `core/[render.py](http://render.py):render_cut`

### Audio is passthru except at fade boundaries

**Rule.** smartcut is invoked with `audio_settings=AudioExportSettings(codec="passthru")`. The only audio post-process applied is the 30ms fade at cut boundaries (Phase 4f-1).

**Why.** Two stages, two contracts. Stage 1 — smartcut's concat — is genuinely passthru and lossless: every frame from the kept ranges is stream-copied. Stage 2 — the fade post-process — is a full audio re-encode, because ffmpeg's `afade` filter graph operates on the whole stream, not just the fade windows; samples far from any cut still pass through the encoder once. The load-bearing invariant is therefore "at most one generation of audio re-encode per render," not "lossless throughout." Within that budget, fades are the only added processing. Re-encoding audio for any other reason — bitrate normalization, format conversion, loudness — is out of scope; each additional re-encode is another generation-loss step and a future drift point.

**Status.** `PASS` — `core/[render.py](http://render.py):197-200`. (Will remain PASS after 4f-1 because fades are the only added processing.)

**Where.** `core/[render.py](http://render.py):render_cut`

### Cut timestamps quantize to 1ms

**Rule.** `Fraction.limit_denominator(1000)` constrains every timestamp handed to smartcut to 1ms precision.

**Why.** Whisper's word timestamps are 20-50ms-grain in practice; sub-ms precision is noise. Smartcut's bookkeeping benefits from rational denominators ≤1000 for frame-rate math. A future change that drops `limit_denominator` (e.g., to "preserve precision") would expose smartcut to fractions whose denominator equals the audio sample rate — a known footgun in the smartcut codebase.

**Status.** `PASS` — `core/[render.py](http://render.py):_to_fraction_seconds` (line 49).

**Where.** `core/[render.py](http://render.py):_to_fraction_seconds`

### Smartcut's `emit()` is non-monotonic; wrap it

**Rule.** Smartcut's progress callbacks emit non-uniform increments and can briefly exceed the announced total. Any progress signal piped to the UI must be clamped to `[0, 1]` and made monotonic by an adapter, never trusted raw.

**Why.** A progress bar that flickers from 0.95 → 1.02 → 0.99 → 1.0 looks broken and undermines user trust. Fix it in one place: wrap the sloppy upstream signal into a clean downstream contract at the boundary. The pattern (adapter at the boundary) generalizes — apply it whenever an upstream library emits messy signals.

**Status.** `PASS` — `_ProgressAdapter` in `core/[render.py](http://render.py)` clamps and monotonizes. A `finalize()` call ensures the bar reaches 1.0 even if smartcut stops emitting before completion.

**Where.** `core/[render.py](http://render.py):_ProgressAdapter`

---

## Transcripts and caching

### Document JSON is the cache; do not add a separate cache file

**Rule.** When a media file is transcribed, the resulting `Document` JSON is the cache. On a subsequent transcribe request for the same file, if a Document JSON exists with a matching `source_hash`, load it instead of re-running inference. Do not add a separate cache database, cache directory, or cache key store.

**Why.** Whisper inference is non-deterministic: re-transcribing the same file produces slightly different word timestamps each run. Variance is small but non-zero — enough to break edit reproducibility across sessions. If a user opens a project on Tuesday whose timestamps were set on Monday, every cut they made Monday sits in a slightly different place — sometimes mid-word, sometimes off by a beat. Caching the Document means timestamps are immutable for the life of the project, which is what an editor needs. The Document JSON is already on disk as the editable artifact; doubling it as the cache eliminates a class of synchronization bugs.

**Status.** `PASS` — Phase 4f-2 added the cache. `Document` carries an optional `source_hash`; `App._try_load_cached_document` is called before model download and before transcribe; on hit, the cached Document drives the result and `Transcriber` is never constructed. Derivative outputs (txt/srt/vtt) are still re-rendered from cached segments because the user clicked Transcribe and expects current files; the JSON sidecar itself is not rewritten on hit (no numbered-suffix duplicate). On miss with an existing-but-stale sidecar, the new transcription writes a numbered-suffix file rather than overwriting — the user's old artifact stays put.

**Where.** `core/[cache.py](http://cache.py):cache_key`; `core/[document.py](http://document.py):Document.source_hash`; `ui/[app.py](http://app.py):App._try_load_cached_document` and `App._emit_cache_hit_done`.

### Cache key: sha256 of path + mtime + size

**Rule.** The cache key for a media file is `sha256(absolute_path_bytes || mtime_int || size_int).hexdigest()`. Stored as `source_hash` on the Document.

**Why.** A full content hash is too slow for large media files. Path+mtime+size is the standard "is this the same file" heuristic — wrong only if a user replaces the file in-place with the exact same byte count and mtime, an edge case where requiring an explicit re-transcribe is acceptable. Including the absolute path means renaming or moving the file invalidates the cache, which is correct: a moved file is a different project context.

**Status.** `PASS` — Phase 4f-2.

**Where.** `core/[cache.py](http://cache.py):cache_key`.

---

## Project layout

### Output isolation: `<source_dir>/edit/` for new projects

**Rule.** New projects write all derived artifacts (Document JSON, eventual master SRT, smartcut intermediates, preview frames) into a `<source_dir>/edit/` subdirectory rather than alongside the source media. Existing sidecar-file projects (Document JSON next to the source) continue to work — backward-compat fallback, not migrated.

**Why.** A user with five source videos in a folder ends up with twenty-plus derived files clogging the same folder if outputs sit alongside sources. Isolating outputs in a subdirectory keeps the source folder readable and makes "delete all my edits, keep the source" trivial. Backward compat for existing layouts follows the same principle as the Phase 4e settings non-migration: don't surprise users with a working setup.

Subdirectory layout:

- `project.json` — the Document JSON (canonical artifact)
- `clips/` — smartcut intermediates per cut operation
- `verify/` — preview frames generated during editing
- `master.srt` — burn-in SRT (Phase 6, when burn-in lands)

Do **not** add `transcripts/<n>.json` — Document JSON is the cache.

**Status.** `FUTURE` (Phase 5+) — current code writes sidecar-style.

**Where.** `ui/[app.py](http://app.py)` output path resolution; to be extended in Phase 5+.

---

## Schema and versioning

### Schema version is mandatory; unknown versions raise

**Rule.** Every persisted Document JSON has a `schema_version` integer field. `Document.from_json` raises `UnsupportedSchemaError` (a `ValueError` subclass) on missing, null, or unrecognized version. Never silently coerce.

**Why.** A future contributor who modifies the Document shape without bumping the version creates a bug class where old files load with wrong assumptions and fail downstream confusingly ("why is `cuts` a string?"). A loud failure at parse time forces the discipline of versioning every breaking change. Migrations are written as needed; silent coercion is never the answer.

**Status.** `PASS` — `core/[document.py](http://document.py):Document.from_json` raises on missing/null/unknown.

**Where.** `core/[document.py](http://document.py):Document.from_json`, `core/[document.py](http://document.py):UnsupportedSchemaError`

### Migrations are written, not skipped

**Rule.** When the Document shape changes in a breaking way, bump `schema_version` and write an explicit migration in `from_json` that converts the old shape to the new one. The migration runs automatically on load.

**Why.** Users with existing projects shouldn't be told "delete and re-transcribe" because we changed the schema. Writing the migration is the cost of breaking the schema; it's paid by the engineer making the change, not the user.

**Status.** `PASS` — Phase 4f-3's v1→v2 migration is the canonical example. v1 sidecars (the flat `media_path` / `duration` / `cuts` shape) load on demand via `_migrate_v1_to_v2`, which derives v2 ranges by subtracting each cut from a full-source keep-range and attaches each cut's reason to the surviving range immediately preceding (or, for cuts at timestamp 0, immediately following) the cut.

**Where.** `core/[document.py](http://document.py):Document.from_json` and `core/[document.py](http://document.py):_migrate_v1_to_v2`.

### Migration on read, write-through on next save

**Rule.** Schema migration happens in `Document.from_json` when the file is loaded. The migrated Document is returned in memory. The on-disk file is **not** rewritten as a side effect of loading. The next `to_json` / save will emit the new schema; until then the file stays as-is.

**Why.** On-load migration in `from_json` keeps the load path simple — there's no separate migration tool, no command users have to run, no question about whether their files have been "upgraded." Write-through on save means the migration is one-way and clean once a file is touched. The opposite policy — aggressive auto-migrate-on-load that rewrites the file immediately — would surprise users with version-controlled project files (a `git diff` showing a bunch of schema-change churn the user didn't intend), and would also break the "open the same file from two builds" workflow since older builds would suddenly be unable to read newer files. Lazy write-through is the user-friendly default.

**Status.** `PASS` — Phase 4f-3.

**Where.** `core/[document.py](http://document.py):_migrate_v1_to_v2` (loads but doesn't write); the next `to_json` call elsewhere in the app emits v2.

### Range model: timeline is the ordered keep-list, cuts are derived

**Rule.** The v2 Document's canonical timeline data is `ranges: list[Range]` — what to KEEP, in playback order. Cuts are not stored; they're whatever's complementary to the ranges over a source's duration. Edit commands operate on ranges (subtract or union an interval) and emit a new Documents with a new ranges list.

**Why.** v1's cuts model was operationally convenient for filler-removal — "I want to remove these spans from the original" — but doesn't scale to multi-source compositing. Storing keep-ranges as the canonical data lets us add sources, reorder ranges, and have multiple non-contiguous spans of the same source on the timeline — none of which a flat cuts list could express. It also collapses two abstractions (source media + edit decisions) into one (a timeline of `(source_id, start, end)` tuples) that Phase 5's editor view can work with directly. The cost — every v1 file needs a one-time migration, every edit command needs to be re-thought as range arithmetic — was paid in 4f-3.

**Status.** `PASS` — Phase 4f-3.

**Where.** `core/[document.py](http://document.py):Range`, `core/[document.py](http://document.py):Document.ranges`, `core/[timeline.py](http://timeline.py)`.

---

## Settings and migration

### Settings non-migration: existing users keep their settings

**Rule.** When a default value in `Settings` changes, existing users on disk keep whatever they had. Only fresh installs (no `settings.json` on disk) get the new default.

**Why.** A user who explicitly saved `output_formats = ["txt", "srt"]` shouldn't have JSON silently appear in their output folder on the next app launch because we changed the default. The right behavior: respect their choice, communicate the new default in UI copy where it matters ("JSON is required for the upcoming editor view"), let them opt in. The Phase 4e default-format change ("json" added to defaults) is the template.

**Status.** `PASS` — Phase 4e `output_formats` default change followed this rule.

**Where.** `core/[settings.py](http://settings.py)` defaults; `ui/components/output_[formats.py](http://formats.py)` for the nudge copy.

---

## Rejected rules

These rules appear in upstream production-rules documents (notably `browser-use/video-use`'s, which informed but did not dictate ours). Each was considered and rejected. Recording the rationale here prevents re-litigating later.

### REJECTED — WhisperX as a hard requirement

**Why rejected.** faster-whisper's native word timestamps + our 100ms pad are good enough for editing-grade cuts. Phase 4a probe data confirmed cross-attention-derived word boundaries are clean enough. WhisperX adds a 600MB model, a second inference pass, and a pyannote-audio dependency for diarization we don't need yet. Cost (install size, runtime, packaging complexity) exceeds benefit (sub-frame timestamp precision) for our use case.

**Reconsider when.** Sub-frame precision becomes a user-visible problem (which it isn't yet — Phase 4d-1 verified pad semantics handle the slack), or diarization becomes a feature we want.

### REJECTED — CrisperWhisper as the default ASR model

**Why rejected.** Research-grade model with a non-PyPI install path (`pip install git+https://github.com/nyrahealth/transformers.git@crisper_whisper`). Installing a custom transformers fork breaks dependency hygiene for everyone using the app to make first-pass cuts on a long-form podcast. CrisperWhisper's main strength — disfluency/filler detection — is genuinely useful, but for a specific Verbatim mode, not as the default.

**Reconsider when.** Phase 6+ Verbatim mode toggle. Off by default, opt-in for users who specifically want filler-by-filler transcripts.

### REJECTED — Separate `edl.json` from `project.json`

**Why rejected.** Splitting the edit decision list into a second file (the upstream doc's pattern) creates synchronization bugs the moment any tool updates one without the other. Our unified `Document` JSON holds segments, words, cuts, and (in v2) ranges in one place — one file, one truth. Cost of consolidation is negligible; benefit is no class of "the EDL says cut at 12.3s but the project says 12.5s, who wins" bugs.

### REJECTED — `takes_[packed.md](http://packed.md)` export format

**Why rejected.** A markdown export of "all the takes packed together for an LLM to pick the best one" is useful when an LLM is doing the editing. Our workflow is GUI-driven: a human picks takes by clicking. The export adds maintenance cost without serving the workflow we built.

**Reconsider when.** An LLM-driven editing mode exists. Phase 6+, if at all.

---

## Phase 4f gap summary

All Phase 4f gaps are closed as of `phase 4f-3 (3/3)`:

- **4f-0** — UTC default-factory fix; three new PASS rules (empty-cuts byte-for-byte copy; audio is passthru except at fade boundaries; cut timestamps quantize to 1ms).
- **4f-1** — `pad_lead` / `pad_trail` split + 30ms `audio_fade_ms` parameter + render-time word-boundary snap. Asymmetric pad and audio-fade rules flipped GAP→PASS.
- **4f-2** — Document JSON cache via `source_hash`. "Document JSON is the cache" and "Cache key" rules flipped GAP→PASS.
- **4f-3** — `schema_version: 2` migration (multi-clip-ready Document). "Migrations are written, not skipped" flipped FUTURE→PASS; two new PASS rules (migration-on-read-write-through-on-save; range-model-is-canonical).
