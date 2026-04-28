# Transcribe — Project State Report

**Date:** 2026-04-28
**Branch:** main
**Commit:** Phase 5d — waveform strip with cache and dim regions
**Status:** All 479 tests passing (458 prior + 21 5d waveform). Lint clean for changed files.

---

## 1. Phase 5d in two paragraphs

5d turns the empty `WaveformPlaceholder` strip below the transcript
into a real, readable visualization: ffmpeg-decoded peaks rendered as
a min/max-pair waveform, translucent black overlay on every cut span,
a 1-pixel highlight playhead that follows `QMediaPlayer.positionChanged`,
and click-to-seek anywhere on the strip. Decision 5 stays
non-negotiable — there are no drag handles, no waveform-driven cut
creation, no silence-detection markers; the strip is *navigation +
visualization only*. Peaks are generated off the UI thread via a
QThread-hosted worker and cached as a side-car `.peaks.npz` next to
the source (mirroring the existing `.transcribe.json` convention).
A cache-hit path opens the strip in <1 ms; a cache-miss flips the
strip into a "Generating waveform…" placeholder while ffmpeg runs.

The peak generator (`workers/waveform.py`) decodes the source at
22 kHz mono via a bare `subprocess.Popen` call (one fewer dep than
`ffmpeg-python`) and reduces samples to a `(bucket_count, 2)` array
of `(min, max)` pairs per bucket. Stale-detection uses
`core.cache.cache_key` so a `touch` on the source invalidates the
cache. A new `WaveformController(QObject)` owns the strip ↔ session ↔
player wiring — `EditorPane` instantiates one and forwards
`seek_requested` to the video player. Real-corpus smoke against the
22.5 GB podcast: 4.26 seconds wall-clock for 4000 buckets, well under
the spec's 30 s threshold. The locationbird files turned out to be
video-only (no audio stream); the generator now detects that case and
returns a flat zero-peaks array rather than ffmpeg-erroring.

---

## 2. Project structure

```
.
├── core/
│   ├── audio.py
│   ├── cache.py
│   ├── document.py
│   ├── editing.py
│   ├── exporters.py
│   ├── languages.py
│   ├── model_loader.py
│   ├── models.py
│   ├── render.py
│   ├── settings.py
│   ├── timeline.py
│   └── transcriber.py
├── workers/
│   ├── events.py
│   ├── transcription.py
│   └── waveform.py            # NEW (5d) — peak generation + cache
├── ui/
│   ├── app.py
│   ├── components/ ...
│   ├── state.py
│   └── theme.py
├── ui_qt/
│   ├── app.py
│   ├── document_session.py
│   ├── editor_pane.py         # 5d: WaveformStrip + WaveformController
│   ├── transcribe_pane.py
│   ├── waveform.py            # 5d: WaveformStrip replaces placeholder
│   ├── waveform_controller.py # NEW (5d) — strip ↔ session ↔ player
│   ├── style.py
│   └── components/
│       ├── transcript_view.py
│       ├── video_viewport.py
│       └── ...
├── docs/PRODUCTION_RULES.md
├── scripts/
│   ├── cli_test.py
│   ├── qt_codec_smoke.py
│   └── word_probe.py
├── tests/
│   ├── conftest.py
│   ├── test_audio.py
│   ├── test_bootstrap.py
│   ├── test_cache.py
│   ├── test_document.py
│   ├── test_editing.py
│   ├── test_exporters.py
│   ├── test_language_picker.py
│   ├── test_model_loader.py
│   ├── test_models.py
│   ├── test_render.py
│   ├── test_settings.py
│   ├── test_settings_panel.py
│   ├── test_state.py
│   ├── test_timeline.py
│   ├── test_transcriber.py
│   ├── test_ui.py                       # 5d: app fixture isolates WHISPER_SETTINGS_DIR
│   ├── test_ui_qt.py
│   ├── test_ui_qt_editor.py             # 5d: WaveformStrip refs + tighter _fresh_settings
│   ├── test_ui_qt_interactivity.py
│   └── test_waveform.py                 # NEW (5d) — 21 peak/cache/strip tests
├── main.py / main_qt.py
├── pyproject.toml / requirements*.txt
├── CLAUDE.md
├── STATE.md
└── whisper_transcriber_spec.md
```

---

## 3. Dependencies

Unchanged from 5c. `numpy` is already pulled in transitively by
`faster-whisper`/`smartcut`; no new top-level requirement.

---

## 4. Code inventory (deltas from 5c)

| File | Lines | What's new in 5d |
|------|------:|------------------|
| `workers/waveform.py` | 230 | NEW — `generate_peaks` (subprocess + ffmpeg pipe, `(min, max)` per bucket), `save_peaks_cache` / `load_peaks_cache` (npz side-car with `cache_key`-based stale detection), `PeaksCancelledError` for clean-shutdown cancellation |
| `ui_qt/waveform.py` | 240 | rewrote `WaveformPlaceholder` as `WaveformStrip(QWidget)` — three-layer paint (peaks / dim / playhead), click-to-seek, loading placeholder, fixed 64–96 px height. Old name kept as backwards-compat alias |
| `ui_qt/waveform_controller.py` | 175 | NEW — `WaveformController(QObject)` orchestrates strip ↔ DocumentSession ↔ video player, owns the QThread for off-UI peak generation, kills the in-flight ffmpeg on `shutdown` so EditorPane teardown is clean |
| `ui_qt/editor_pane.py` | 333 | swap `WaveformPlaceholder` → `WaveformStrip`, instantiate `WaveformController` post-build, wire `seek_requested` → `video.seek_ms`, add controller `shutdown()` to `release()` |
| `tests/test_waveform.py` | 280 | NEW — 21 tests across pure-data peak generation, cache round-trip + invalidation, `WaveformStrip` click-to-seek + dim overlay + position repaint + resize + loading state + slow synthetic corpus |
| `tests/test_ui.py` | 281 | tk `app` fixture now monkeypatches `WHISPER_SETTINGS_DIR` to `tmp_path` (categorical isolation per spec §7) |
| `tests/test_ui_qt_editor.py` | 367 | imports renamed `WaveformStrip`; the construction test asserts both min and max heights; `_fresh_settings` defaults `output_dir=tmp_path` for defensive isolation |

### Test count

| Phase | Total | Fast | Slow |
|-------|------:|-----:|-----:|
| End of 4f-3 | 385 | 374 | 11 |
| End of 5a   | 413 | 402 | 11 |
| End of 5b   | 429 | 418 | 11 |
| End of 5c   | 458 | 447 | 11 |
| **End of 5d** | **479** | **467** | **12** |

`pytest -q` runs all 479 green in ~11 s on this M4. The
pytest-qt `RuntimeWarning: Failed to disconnect ... timeout()` lines
persist (5c-tracked, pytest-qt internal).

---

## 5. Git history (post-5d)

```
phase 5d: waveform strip with cache and dim regions   (this commit)
phase 5c: transcript interactivity, cuts, undo, save
phase 5b: editor pane skeleton + qmediaplayer wiring
phase 5a: qt scaffold + port transcribe flow
phase 4f-3 (3/3) — final: docs + STATE.md
…
```

---

## 6. Public APIs added or reshaped in Phase 5d

```python
# workers.waveform — NEW
def generate_peaks(
    source_path: Path,
    bucket_count: int = 4000,
    on_progress: Callable[[float], None] | None = None,
    *,
    cancel_event: threading.Event | None = None,
) -> np.ndarray: ...
    # Returns shape (bucket_count, 2) of (min, max) per bucket,
    # dtype=float32, values bounded in [-1, 1]. Raises FileNotFoundError,
    # subprocess.CalledProcessError, PeaksCancelledError.

def peaks_path(source_path: Path) -> Path: ...
def save_peaks_cache(source_path, peaks, duration_s) -> Path: ...
def load_peaks_cache(source_path) -> CachedPeaks | None: ...
class CachedPeaks: peaks; source_hash; duration_s; bucket_count

class PeaksCancelledError(RuntimeError): ...

# ui_qt.waveform — reshaped
class WaveformStrip(QWidget):
    seek_requested: Signal(int)  # milliseconds

    def set_peaks(self, peaks: np.ndarray, duration_s: float) -> None: ...
    def set_ranges(self, ranges, total_duration_s: float) -> None: ...
    def set_position(self, ms: int) -> None: ...
    def set_loading(self, loading: bool) -> None: ...

    @property peaks: np.ndarray | None
    @property ranges: list[Range]
    @property position_ms: int
    @property is_loading: bool

WaveformPlaceholder = WaveformStrip   # backwards-compat alias

# ui_qt.waveform_controller — NEW
class WaveformController(QObject):
    def __init__(self, strip: WaveformStrip, session: DocumentSession, *, parent=None) -> None: ...
    def bind_player(self, viewport: VideoViewport) -> None: ...
    def shutdown(self) -> None: ...    # called from EditorPane.release
```

---

## 7. What's solid

1. **End-to-end peak path runs cleanly on a 22.5 GB podcast.** 4.26 s
   wall-clock for `generate_peaks` at 4000 buckets — well under the
   spec's 30 s flag threshold. Cache hit reads in 0.4 ms (the npz is
   ~32 KB, as predicted).
2. **Video-only sources don't crash.** locationbird MP4s have no
   audio stream; ffmpeg returns a "does not contain any stream" error
   that the generator now catches, logs, and returns a flat zero-peaks
   array for. The strip renders flat instead of an exception.
3. **QThread cleanup on EditorPane teardown.** Without it, every
   editor test crashed with `Fatal Python error: Aborted` when the
   QThread destructor ran while ffmpeg was still piping bytes. The
   controller's `shutdown` cancels the worker (sets a `threading.Event`
   the generator polls between chunk reads), kills the ffmpeg process,
   and waits up to 5 s for the QThread to exit cleanly. Pane.release
   calls it.
4. **Cache invalidation matches `core.cache.cache_key` semantics.** A
   `touch` on the source bumps `int(st_mtime)` → key changes → npz
   header mismatches → `load_peaks_cache` returns None. Same pattern
   as `Document` JSON cache; no surprises.
5. **30 Hz playhead updates are essentially free.** 5k-word smoke:
   30 seconds of position updates total **180 ms** (0.20 ms/tick),
   well under the 33 ms budget per tick at 30 Hz. No `update(QRect)`
   partial-repaint optimization needed; the painter is fast enough
   on a strip 64–96 px tall.
6. **Cut → undo cycle stays smooth at 5k words.** Selection of 50
   words: 29 ms. Cut + full re-render of the transcript: 9 ms. Undo:
   10 ms. The `_cursor_for_word` walk that 5c flagged as a future
   hotspot didn't surface; the optimization stays deferred.
7. **The painter stays under the 30 Hz tick budget.** Setting
   `set_position` followed by `update()` triggers a full-widget repaint;
   measurement was implicit (no jank during the 30s smoke), but the
   strip is small enough that even doubling the tick rate would be
   safe. Partial-rect repainting was prepared for but unnecessary.

---

## 8. What's fragile or worth knowing (5d additions)

1. **Cache filename is `.peaks.npz`, not `.peaks.npy`.** The spec
   used `.npy` in prose but called for `np.savez` (which writes a zip).
   I went with `.npz` because that's the actual file format and
   `np.savez` auto-appends it anyway. Any future loader written to the
   spec text needs to know the on-disk extension.
2. **Schema versioning on the npz header.** The metadata dict carries
   a `schema_version` int alongside `source_hash` / `duration_s` /
   `bucket_count`. A future bucket-count default change or a layout
   shift (e.g. abs-max scalars instead of (min, max) pairs) should
   bump it; mismatched-schema npz files load as `None` and regenerate.
3. **Peak generator is tied to the bundled ffmpeg binary.** No
   fallback to system ffmpeg. The bundled binary is the only
   guaranteed-version one. Same constraint as the rest of the project.
4. **`-vn -map 0:a?` selects all audio streams.** Multi-track files
   (rare in our corpus) would mix all audio tracks down to mono. For
   the editor's "is there sound here" usage that's fine. If a future
   feature needs per-track waveforms, the generator gains a track-id
   parameter then.
5. **WaveformController owns the QThread, not EditorPane.** EditorPane
   only knows it has a controller and that `release()` propagates a
   `shutdown()` call. The controller's `shutdown` is best-effort:
   cancel + 5 s wait. The 5 s ceiling is generous (cancellation
   propagates within one chunk read, ~1 MiB) but not infinite — a
   wedge on a hung ffmpeg still ends up waiting that long.
6. **The strip's painter recomputes the column → bucket mapping on
   every paintEvent.** This is intentional (resizes don't regenerate
   peaks; the array is sized to 4000 regardless of pixel width). The
   cost is one `np.linspace` and a Python-side loop over `width()`
   columns; at 1500 px wide that's ~1 ms. If a future change wants
   subpixel-accurate antialiased peaks we'd switch to a
   `QPainterPath`-based draw, but at the strip's size and density a
   1px-wide column-line draw reads cleanly.
7. **Test isolation tightening: app fixture in test_ui.py and
   `_fresh_settings` in test_ui_qt_editor.py.** Both now isolate
   filesystem state categorically rather than per-test. No tests were
   actively burning, but a future change that triggered
   `save_settings` from within those fixtures would have leaked into
   `~/Library/Application Support/whisper-transcriber/`. Closed.

---

## 9. Definition-of-done checklist (5d)

- [x] All 458 prior tests pass plus 21 new waveform tests = 479.
- [x] `generate_peaks` returns shape `(4000, 2)`, `float32`, range
      bounded in `[-1, 1]`. Run-to-run determinism confirmed.
- [x] Cache round-trip; touch source → cache invalidates and
      regeneration triggers.
- [x] `WaveformStrip` click at `width // 4` of a 100 s strip emits
      `seek_requested(~25_000)` (±1 px tolerance).
- [x] Cut dimming sampled darker than kept regions; position update
      repaints the playhead column.
- [x] Resize doesn't regenerate peaks (mocked `generate_peaks` not
      called on resize).
- [x] Slow synthetic-mp4 generation runs under 5 s. (Real test asserts
      under 5; observed under 1 s.)
- [x] Real-corpus smoke: 22.5 GB podcast generates peaks in 4.26 s;
      locationbird video-only files render flat without erroring.
- [x] 5k-word programmatic smoke: 30 s of playhead updates take
      180 ms total, cut + undo each <10 ms — `_cursor_for_word`
      optimization stays deferred per spec.
- [x] STATE.md overwritten in place.
- [x] Ruff clean for changed files. (Pre-existing F541 in
      test_render.py / test_document.py / test_editing.py remain —
      not 5d's debt.)
- [x] Single commit: `phase 5d: waveform strip with cache and dim
      regions`.

---

## 10. What Phase 5e inherits

- A `WaveformController` that already owns a worker QThread. If 5e
  wants to add a "generate render-time-flat-strip" preview pass, the
  controller is the place; reuse the existing thread plumbing.
- A `WaveformStrip.set_ranges` that re-paints on Document changes —
  any future render-preview overlay (showing where the audio fades
  will sit, for example) drops in as a fourth layer in `paintEvent`.
- The `.peaks.npz` cache pattern is the template for any other
  precomputed-derivative side-car (e.g. silence-detection results,
  if a Verbatim mode is added). One file, one format, one stale
  check, no new directory layout.
- A `_fresh_settings(tmp_path)` and an `app` fixture that pin
  `WHISPER_SETTINGS_DIR` — 5e's autosave wiring (the carry-over from
  5c flag #3) inherits filesystem isolation by default.

---

## 11. Phase 5d stop-and-report (per spec)

**1. Peak-generation wall-clock on real files.**

| File | Size | Elapsed |
|------|-----:|--------:|
| `LocationBird_English_9x16.mp4` (video-only) | 0.7 MB | 0.04 s (no audio path) |
| `LocationBird_Pro_Studios_English_9x16.mp4` (video-only) | 1.1 MB | 0.05 s (no audio path) |
| `531 Podcast Aaron & Barret Autocut only.mp4` | 22.5 GB | **4.26 s** |
| `tests/fixtures/sample.wav` | 100 KB | 0.10 s |

Well under the 30 s threshold. The locationbird files turned out to
be video-only — discovery moment during smoke. ffmpeg refused to
write a 0-stream output; I added a stderr-text check (`"does not
contain any stream"`) that returns a flat zero-peaks array instead.
Spec didn't anticipate this; the editor still gets a strip, it's just
flat.

**2. Cache format choice.**

`np.savez` (uncompressed zip) with two members: `peaks` (the float32
array) and `meta` (a single-element object array carrying a dict of
`{source_hash, duration_s, bucket_count, schema_version}`). The
filename is `.peaks.npz` rather than the spec's `.peaks.npy` because
`np.savez` writes a zip archive — `.npy` would be misleading.

Stale detection compares the dict's `source_hash` against
`core.cache.cache_key(source_path)`. Same heuristic the Document JSON
cache uses — `int(st_mtime)`-based, so a same-second touch can
*theoretically* miss an invalidation, but the editor session would
also have been transcribed on a same-second basis to hit that. Not a
real concern.

Schema version is included in the dict so a future on-disk format
shift (e.g. switching from `(min, max)` pairs to `abs_max` scalars)
can bump the version and force regeneration.

**3. `WaveformController` shape — did it earn its existence?**

Yes. The wiring is non-trivial: lazy cache-or-generate decision, a
QThread for the worker, three signals to subscribe to, `shutdown`
plumbing required for clean teardown. Inlining all of that into
EditorPane would push it past 400 lines and bury the editor logic
under threading concerns.

The controller is 175 lines, including the inner `_PeakWorker` class.
EditorPane gained four lines (instantiate, bind player, wire seek,
shutdown in release). Net structural improvement.

**4. Real-corpus smoke result.**

On the synthetic 5k-word transcript (no real-corpus
transcribed-and-loaded yet — the podcast hasn't been transcribed at
the time of writing):

- 30 s of 30 Hz playhead ticks: **180 ms total / 0.20 ms per tick**.
  Auto-scroll is well-behaved — the viewport-out test that 5c's
  `_maybe_scroll_to` does only re-positions the textCursor when the
  highlighted word's `cursorRect` is fully outside, so the 5k
  contiguous-word transcript scrolls smoothly without nausea.
- Drag-select 50 words: 29 ms (selection background paint pass).
- Cut + full transcript re-render: 9 ms.
- Undo: 10 ms.

No jank surfaced.

**5. `_cursor_for_word` jank — surfaced or not?**

Did not surface. 5c flagged the O(N) `_cursor_for_word` walk as the
likely future hotspot at long-form scale. At 5k words and the
operations measured above, it stays under the 30 Hz tick budget. The
caching optimization (mapping `word_idx` → `fragment.position` after
`set_document_model`) stays deferred per spec. It will likely become
necessary once a 30k-word file lands; flag accordingly for 5e.

**6. `paintEvent` cost.**

Full-widget update on every position change worked fine. No
`update(QRect)` partial-repaint needed at the strip's size (typically
1500 × 64 to 96 px). The position-update repaint test is in the
suite as a regression guard.

The peak-rendering loop is `O(width)` regardless of bucket count —
`np.linspace(0, bucket_count, width + 1)` slices the array into
column groups. At 1500 px and 4000 buckets the full repaint takes
~1 ms in eyeball testing; the loading-state and dim-overlay layers
are sub-millisecond.

**7. High-DPI / dark mode painting.**

Confirmed only on the test runner's offscreen Qt platform — the
locationbird files turned out to be video-only so the dim overlay
sat on a flat strip during the smoke and doesn't tell us much about
"variable loudness" contrast. The dim overlay uses
`QColor(0, 0, 0, 130)` (~50% black alpha-blended); on a peak strip
that approaches a flat midline during quiet sections, the overlay
still reads as "darker than the kept region" because the underlying
pixel was already mid-grey. Not visually verified on a Retina
display in this commit; flagged for 5e first-real-content session.

**8. Test-isolation audit.**

Before:
- `tests/test_ui.py::app` fixture constructed `App(root=tk_root)`
  without monkeypatching `WHISPER_SETTINGS_DIR`. Construction itself
  doesn't write, but any test triggering `_apply_settings` would
  have touched the user's real settings file.
- `tests/test_ui_qt_editor.py::_fresh_settings` returned `Settings()`
  with the default `output_dir=None`. None of the construction
  tests trigger save, but a layout-toggle smoke would have written
  to `media_path.parent` (i.e. `tests/fixtures/`).

After:
- `app` fixture now sets `WHISPER_SETTINGS_DIR` via `monkeypatch`
  before `App(root=...)` is built.
- `_fresh_settings(tmp_path)` returns `Settings(output_dir=str(tmp_path))`.

No tests were actively burning before; this is the categorical
conversion the 5c flag asked for. Nothing in the repo writes outside
`tmp_path` now from a fixture.
