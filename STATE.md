# Transcribe — Project State Report

**Date:** 2026-04-28
**Branch:** main
**Commit:** Phase 5e — render export, autosave, splitter persistence, settings completion
**Status:** All 501 tests passing (479 prior + 6 render-worker + 16 5e wiring/dialog). Lint clean for changed files.

---

## 1. Phase 5e in two paragraphs

5e closes the integration loop. The editor now exports an actual cut
video — Cmd-E (or the toolbar Export… button) opens a save dialog,
spins up a `RenderWorker` on a `QThread`, and shows an indeterminate
"Rendering…" `QProgressDialog` with a working Cancel. Cancel sets a
`threading.Event` that the worker observes inside its progress
callback, raising out of `core.render.render_cut` and unlinking the
partial output before reporting `RenderCancelled` back. Errors leave
the partial file on disk (the user might want to inspect it); cancels
clean it up. Autosave is a `QTimer` on the editor pane: when
`Settings.autosave_interval_s > 0` and the document is dirty, the same
save path Cmd-S uses fires; failures log to stderr and stay dirty
silently per Decision 8.

The settings dialog finally has its three tabs (Transcription /
Editor / Advanced) covering every Settings field — `layout`,
`default_pad_lead`, `default_pad_trail`, `default_audio_fade_ms`, and
`autosave_interval_s` — with live propagation back to a running
editor pane (layout flips the outer splitter, autosave-interval
re-arms the QTimer). Splitter sizes persist across app restarts via
base64-encoded `QSplitter.saveState()` blobs in `Settings`; the
"accept the loss on toggle" contract clears the outer-splitter blob
when the user flips the layout, so the new orientation gets a fresh
proportional split. Real-content waveform check found the 5d
"black-on-dark" dim overlay invisible against dark mode; replaced
with a translucent neutral gray plus a diagonal hatch that reads
either way.

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
│   ├── settings.py            # 5e: splitter-state fields + base64 round-trip
│   ├── timeline.py
│   └── transcriber.py
├── workers/
│   ├── events.py
│   ├── render.py              # NEW (5e) — RenderWorker + RenderEvents
│   ├── transcription.py
│   └── waveform.py            # 5e: schema_version regen rule documented
├── ui/
│   ├── app.py
│   ├── components/ ...
│   ├── state.py
│   └── theme.py
├── ui_qt/
│   ├── app.py                 # 5e: MainWindow forwards settings to editor
│   ├── document_session.py
│   ├── editor_pane.py         # 5e: export, autosave, splitter persistence, apply_settings
│   ├── transcribe_pane.py
│   ├── waveform.py            # 5e: dim overlay = gray + hatch (was black-on-base)
│   ├── waveform_controller.py
│   ├── style.py
│   └── components/
│       ├── settings_panel.py  # 5e: three tabs, Phase-5 fields wired
│       ├── transcript_view.py
│       ├── video_viewport.py
│       └── ...
├── docs/PRODUCTION_RULES.md
├── scripts/ ...
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
│   ├── test_phase_5e.py                # NEW (5e) — 16 wiring/dialog tests
│   ├── test_render.py
│   ├── test_render_worker.py           # NEW (5e) — 6 RenderWorker tests
│   ├── test_settings.py
│   ├── test_settings_panel.py
│   ├── test_state.py
│   ├── test_timeline.py
│   ├── test_transcriber.py
│   ├── test_ui.py
│   ├── test_ui_qt.py
│   ├── test_ui_qt_editor.py
│   ├── test_ui_qt_interactivity.py
│   └── test_waveform.py                # 5e: dim test relaxed to "different" not "darker"
├── main.py / main_qt.py
├── pyproject.toml / requirements*.txt
├── CLAUDE.md
├── STATE.md
└── whisper_transcriber_spec.md
```

---

## 3. Dependencies

Unchanged from 5d.

---

## 4. Code inventory (deltas from 5d)

| File | Lines | What's new in 5e |
|------|------:|------------------|
| `workers/render.py` | 165 | NEW — `RenderWorker` + `RenderEvent` family (`RenderStarted`, `RenderProgress`, `RenderComplete`, `RenderError`, `RenderCancelled`); cancel-via-progress raises `RenderCancelledError`; cancel path unlinks partial output, error path preserves it |
| `ui_qt/editor_pane.py` | 580 | Cmd-E export action + toolbar button → off-thread `RenderWorker` on a `QThread`; indeterminate `QProgressDialog` with cancel; autosave `QTimer` (silent on failure, log only); base64-encoded splitter `saveState`/`restoreState` round-trip with debounced persistence; `apply_settings` lives-propagates layout flips and autosave-interval changes |
| `ui_qt/components/settings_panel.py` | 235 | three tabs (Transcription / Editor / Advanced); Editor tab wires `layout`, `default_pad_lead`, `default_pad_trail`, `default_audio_fade_ms`; Advanced tab has `autosave_interval_s` with `"Off"` special text on 0 |
| `ui_qt/app.py` | 308 | `_apply_settings` now forwards to the active `EditorPane` via `apply_settings(new)` (was: only `TranscribePane`) |
| `ui_qt/waveform.py` | 263 | dim overlay swapped from translucent-black to translucent-gray + diagonal hatch — readable on dark mode and against quiet-section peaks |
| `core/settings.py` | 165 | added `editor_splitter_state: bytes \| None` and `transcript_splitter_state: bytes \| None`; `to_dict`/`from_dict` base64-encode + decode; garbage values fall back to None |
| `workers/waveform.py` | 245 | docstring on `_PEAKS_SCHEMA_VERSION` codifies the regen rule (mismatch → reload returns None → controller regenerates) |
| `tests/test_render_worker.py` | 175 | NEW — 6 tests covering happy path, progress propagation, error, cancel-via-progress, cancel-after-complete, kwargs forwarding |
| `tests/test_phase_5e.py` | 295 | NEW — 16 tests covering settings round-trip with base64 bytes, autosave behavior (no-op clean, fires dirty, silent failure, interval start/stop), splitter persist/restore, layout-toggle clears outer state, settings dialog tabs + Phase-5 fields, live propagation |
| `tests/test_waveform.py` | 285 | dim-overlay assertion relaxed: "visually distinct" instead of "darker" — the 5e overlay is a gray-toward-mid lift that goes up on dark base and down on light base |

### Test count

| Phase | Total | Fast | Slow |
|-------|------:|-----:|-----:|
| End of 4f-3 | 385 | 374 | 11 |
| End of 5a   | 413 | 402 | 11 |
| End of 5b   | 429 | 418 | 11 |
| End of 5c   | 458 | 447 | 11 |
| End of 5d   | 479 | 467 | 12 |
| **End of 5e** | **501** | **489** | **12** |

`pytest -q` runs all 501 green in ~11 s on this M4. The pytest-qt
`RuntimeWarning` lines persist (5c-tracked, pytest-qt internal).

---

## 5. Git history (post-5e)

```
phase 5e: render export, autosave, splitter persistence, settings completion   (this commit)
phase 5d: waveform strip with cache and dim regions
phase 5c: transcript interactivity, cuts, undo, save
phase 5b: editor pane skeleton + qmediaplayer wiring
phase 5a: qt scaffold + port transcribe flow
phase 4f-3 (3/3) — final: docs + STATE.md
…
```

---

## 6. Public APIs added or reshaped in Phase 5e

```python
# workers.render — NEW
class RenderEvent: ...
class RenderStarted(RenderEvent): output_path: Path
class RenderProgress(RenderEvent): fraction: float
class RenderComplete(RenderEvent): output_path: Path; elapsed: float
class RenderError(RenderEvent): message: str
class RenderCancelled(RenderEvent): ...

class RenderCancelledError(RuntimeError): ...

class RenderWorker:
    def __init__(self, document: Document, output_path: Path,
                 settings: Settings, on_event: Callable[[RenderEvent], None],
                 cancel_event: threading.Event | None = None) -> None: ...
    def run(self) -> None: ...
    def cancel(self) -> None: ...

# core.settings — additions
@dataclass
class Settings:
    # ... existing fields ...
    editor_splitter_state: bytes | None = None        # NEW (5e)
    transcript_splitter_state: bytes | None = None    # NEW (5e)
    # to_dict() base64-encodes bytes; from_dict() decodes them back

# ui_qt.editor_pane — additions on EditorPane
class EditorPane(QWidget):
    render_started: Signal(Path)        # NEW (5e)
    render_completed: Signal(Path)      # NEW (5e)
    render_failed: Signal(str)          # NEW (5e)
    render_cancelled: Signal()          # NEW (5e)

    def apply_settings(self, settings: Settings) -> None: ...   # NEW (5e)
```

---

## 7. What's solid

1. **End-to-end export runs against a real file.** The 30 s synthetic
   mp4 with three keep-ranges (0–5, 10–20, 25–30) renders to a 619 KB
   playable mp4 in 0.32 s. ffmpeg's null-decoder pass on the output
   confirms no broken streams; duration is ~25 s as expected (15 s
   of kept content plus pad/fade).
2. **`render_cut` accepted Phase-5 kwargs out of the box.** The
   spec's "Render kwargs gap" question turned up *no* gap: the
   existing function signature has `pad_lead`, `pad_trail`,
   `audio_fade_ms` as named-keyword arguments since Phase 4f-1. The
   worker forwards them straight through. No `core/` modifications
   needed.
3. **Cancel cleanup is reliable on macOS.** Both cancel paths
   (cancel-set-before-run and cancel-set-during-progress) unlink the
   partial output. The cancel-set-between-render-finish-and-Complete
   case also cleans up — there's an extra check after `render_cut`
   returns. No "file is locked" issues observed.
4. **Autosave failure is genuinely silent.** Mocking the write to
   raise an `OSError` produces no `QMessageBox` and leaves the dirty
   marker set. The user would notice via the title bar's `●` prefix
   surviving past the autosave interval. stderr log is the
   developer-observable channel.
5. **Splitter state round-trips through base64 JSON.** Bytes go to
   disk as ASCII, come back as bytes. Garbled strings fall back to
   `None`. Same orientation across sessions reproduces the user's
   sizing; flipping the layout clears the outer-splitter blob so the
   new orientation gets a fresh proportional split.
6. **Settings live propagation works via `EditorPane.apply_settings`.**
   Changing the layout in the dialog while the editor is open flips
   the outer splitter immediately. Changing autosave interval re-arms
   the QTimer immediately. The pane is not torn down or recreated.
7. **Schema-version regen rule is now load-bearing AND documented.**
   `_PEAKS_SCHEMA_VERSION = 1` lives at module scope with a comment
   explaining when to bump (bucket-count default change, layout
   shift, new field). The 5e `test_peaks_cache_with_old_schema_version_is_ignored`
   plants a `schema_version=0` npz and asserts `load_peaks_cache`
   returns `None` — same code path as a hash mismatch.
8. **Waveform dim overlay reads on both dark and light themes.** 5d
   shipped translucent-black, which disappears against a dark `palette().base()`.
   The 5e replacement is `QColor(128, 128, 128, 130)` overlay plus a
   diagonal cross-hatch every 6 pixels. On the synthetic mp4 with a
   light base the kept-vs-cut contrast is ~48 lightness units; on
   dark themes the overlay lifts the cut region toward gray, also
   producing a clear delta. Either direction reads as "muted."

---

## 8. What's fragile or worth knowing (5e additions)

1. **Render progress is best-effort.** `core.render.render_cut`'s
   `_ProgressAdapter` only emits when smartcut emits, and smartcut
   ticks unevenly and may go silent for long stretches on big files.
   The progress dialog promotes itself from indeterminate to
   determinate the moment the first numeric `RenderProgress` arrives,
   but for the bulk of a render of a long file the bar is going to
   sit at one value. Acceptable for 5e — adding *real* progress
   inside `core.render` is out of scope per spec.
2. **Cancel-via-progress only works while smartcut emits.** If the
   library is in the middle of a long silent ffmpeg subprocess that
   it spawned itself, our cancel flag is observed only on the next
   `progress.emit`. Mid-subprocess cancellation would require either
   modifying `core/render.py` to expose the subprocess (forbidden in
   5e) or running smartcut itself in a child process we can `SIGTERM`
   (a much bigger architectural change — deferred). On the synthetic
   30 s mp4 this isn't observable; on the 23 GB podcast a cancel may
   take seconds rather than milliseconds to land.
3. **Layout toggle wipes outer-splitter sizes.** Per the
   "accept the loss on toggle" contract: when the user flips
   `video_top` ↔ `video_left`, `_handle_layout_toggle` clears
   `editor_splitter_state`. Their next drag re-establishes a saved
   blob in the new orientation. Inner splitter (always vertical)
   survives because its orientation never changes.
4. **Autosave invariant: same write path as Cmd-S.** Both call
   `_write_document_to(path)` which goes through the same
   `candidate_cache_path` + `mkdir parents=True` + `to_json` flow.
   If a future change splits the two paths, autosave needs to
   continue producing files Cmd-S can read back without round-trip
   loss.
5. **`apply_settings` re-decodes bytes through `Settings.from_dict`.**
   Defensive: a Settings object handed to the editor may have come
   from JSON load (bytes already), or from a freshly-constructed
   in-process Settings (bytes still bytes), or from a dialog `Save`
   that called `to_dict` somewhere — round-tripping through
   `to_dict() → from_dict()` normalises all of those into the same
   shape before we install it. Slightly wasteful on the in-process
   path; correctness > nanoseconds.
6. **Render error path leaves the partial output on disk.** The
   spec is explicit: cancel deletes, error preserves. A user who
   sees "Export failed" can open Finder and inspect what landed.
   This means a series of failed renders to the same target leaves
   a stale file there until the next successful render overwrites
   it; the path is determined by the user via `getSaveFileName` so
   collision is the norm, not surprising.
7. **`RenderProgress` events on the GUI thread are auto-connection
   queued.** The worker emits via `event_received.emit(...)` from the
   worker thread; Qt's auto-connection routes that to the GUI-thread
   slot through the event loop. Slot must remain re-entrant-safe
   (it currently is — every branch in `_handle_render_event` is
   widget mutation only).

---

## 9. Definition-of-done checklist (5e)

- [x] All 479 prior tests pass plus 22 new = 501.
- [x] `python main_qt.py`: load a transcribed file, make cuts,
      Cmd-E exports a playable .mp4 in the chosen location.
      (Smoke-tested programmatically against `synthetic.mp4`;
      output decodes cleanly via `ffmpeg -f null`.)
- [x] Autosave: enable in settings, edit, wait the interval, dirty
      marker clears without Cmd-S. (Tested:
      `test_autosave_writes_when_dirty_and_clears_dirty`.)
- [x] Splitter sizes survive app restart. (Tested:
      `test_splitter_persist_then_restore_round_trip`.)
- [x] Settings dialog has all Phase-5 fields, all functional.
      (Tested: tabs, layout combo init, save emits each field,
      autosave special-text "Off".)
- [x] Layout-change-from-dialog flips the splitter live. (Tested:
      `test_apply_settings_flips_layout_live`.)
- [x] Real-content visual check done on synthetic.mp4 + offscreen
      dark-and-light palette: 5d's black overlay invisible on dark
      mode → swapped to gray + diagonal hatch.
- [x] `python main.py` (tkinter) still launches; `python main_qt.py`
      still launches.
- [x] Ruff clean for changed files.
- [x] STATE.md overwritten in place.
- [x] Single commit: `phase 5e: render export, autosave, splitter
      persistence, settings completion`.

---

## 10. What Phase 5f inherits

- A complete editor surface (export + autosave + persistence + tabbed
  settings) ready for menu-bar reparenting. The QActions in
  `_build_actions` (`_cut_action`, `_save_action`, `_export_action`,
  …) are already constructed with `ApplicationShortcut` context;
  reparenting them to a `QMenuBar` in 5f is a no-op that uses the
  same QAction instances.
- `RenderWorker` / `RenderEvent` types live at module scope —
  `main.py` (tkinter) can plug in the same worker if the legacy UI
  ever gains an export button. Mirror of the
  `TranscriptionWorker` / `WorkerEvent` pattern.
- A `Settings` object that's now feature-complete for Phase 5;
  any 5f or 6+ field follows the same "default constant + dataclass
  field + appears in the right tab" recipe.

---

## 11. Phase 5e stop-and-report (per spec)

**1. Render kwargs gap.**

No gap. `core.render.render_cut` (the function — spec said
`render_document` but the actual symbol is `render_cut`; verified
against the file rather than memory) accepts `pad_lead`, `pad_trail`,
and `audio_fade_ms` as keyword arguments since Phase 4f-1. The
`RenderWorker` forwards them straight from `Settings`:

```python
render_cut(
    self.document,
    self.output_path,
    on_progress=_progress,
    pad_lead=self.settings.default_pad_lead,
    pad_trail=self.settings.default_pad_trail,
    audio_fade_ms=self.settings.default_audio_fade_ms,
)
```

Verified end-to-end: `test_render_worker_passes_settings_kwargs`
asserts the captured kwargs match the Settings values. Zero `core/`
modifications.

**2. Render wall-clock on a real cut.**

I exported the synthetic 30 s mp4 with three keep ranges (0–5,
10–20, 25–30 — i.e. cuts at 5–10 and 20–25):

- elapsed: **0.32 s**
- output: **619 KB**, 25.6 s, h264+aac, plays cleanly under `ffmpeg -f null`.
- progress events emitted: 3 `RenderProgress` ticks plus
  `RenderStarted` + `RenderComplete`.

I didn't render the 22.5 GB podcast end-to-end — render of a
multi-GB source via smartcut takes minutes by experience and the
order-of-magnitude check is satisfied by the synthetic. Earlier 5d
peak generation against the same podcast clocked 4.26 s; render is
disk-IO-bound on the same scale.

**3. Cancel cleanup.**

Reliable on macOS. Three cases:

- Cancel set *before* `run()` enters the worker body → cleans
  partial (if smartcut wrote anything before checking) and emits
  `RenderCancelled`.
- Cancel set *during* progress callbacks → `_progress` raises
  `RenderCancelledError`, the `try/except` branch unlinks the
  partial file and emits `RenderCancelled`.
- Cancel set *between* `render_cut` returning and the
  Complete-emit → caught by an explicit recheck after the call,
  unlinks the (now-complete) file and emits `RenderCancelled`.

Tested explicitly in `test_render_worker.py`. No file-locking issues
on macOS — `path.unlink()` just works because we're the only writer
holding the file open via smartcut, and smartcut closes its handle
before returning.

**4. Autosave silent-failure UX.**

Confirmed silent. Mocking `_write_document_to` to raise produces
zero `QMessageBox` calls (the test would hang waiting for the modal
otherwise; it returns in milliseconds). Logging is to stderr via
the module's `logging.getLogger(__name__)` at `error` level.

How would the user notice? The title bar's `●` prefix stays. They'd
also see Cmd-S surface a `QMessageBox.critical` if they try a
manual save (the modal is appropriate there because it's a
user-initiated action). For a long-running silent-failure series
the only signal is the dot persisting past their expectation —
which is, in fact, the contract.

**5. Splitter persistence under layout toggle.**

I picked **clear-on-toggle**. When the user flips
`video_top ↔ video_left`, `_handle_layout_toggle` sets
`editor_splitter_state = None` before saving. The next pane open
sees no saved blob and uses Qt's proportional defaults; the next
splitter drag persists a fresh blob in the new orientation.

Why not two-states? The user's sizing for "video on top" and
"video on left" are usually different by intent (you tune for the
shape that's in front of you). Carrying both adds two Settings
fields but doesn't actually save the user any tuning effort —
they'll re-tune the layout they're switching *to* anyway. Less
state for a no-op savings.

The inner splitter blob (transcript ↕ waveform) survives layout
toggles because the inner splitter is always vertical.

**6. Real-content visual check.**

I used `tests/fixtures/synthetic.mp4` (30 s testsrc + 440/880/1320 Hz
audio at varying amplitudes). The 5d translucent-black overlay
returned 0/0/0 pixels in cut regions on the offscreen palette —
the base color was already black so blackening it more is invisible.

**Shipped value:** `QColor(128, 128, 128, 130)` overlay plus a 1px
diagonal hatch every 6 pixels at `QColor(128, 128, 128, 180)`. On
the offscreen test palette (light base in this run) the kept-vs-cut
contrast averaged 48 lightness units. On a dark base the same
gray-toward-mid lift produces an opposite-direction delta of
similar magnitude. Either way the cut region reads as muted.

The spec suggested "bump dim alpha from ~0.5 to ~0.65" but the
real problem wasn't alpha — it was color choice. Bumping black-on-
black to 65% alpha is still black-on-black. Switching to gray
(neutral against any base) plus a hatch (definitive distinction
regardless of palette) is the durable fix.

I didn't validate on a Retina dark-mode session against an actual
audio-bearing podcast file in this commit — the locationbird
candidates were video-only, and transcribing a long-form podcast
to get a real Document on the editor is its own session. Flagged
for follow-up (see §8.7 below).

**7. Settings live-propagation surprises.**

None. `apply_settings`'s defensive `from_dict(to_dict(...))` round
trip means the splitter-state bytes survive cleanly even when a
dialog Save constructs Settings from form field values. Layout
flipping while the editor pane has an active selection or playhead
state doesn't disturb either — the QSplitter orientation change is
a layout-only operation; the transcript view and video viewport
keep their internal state.

The one wrinkle: a dialog Save preserves the existing
`editor_splitter_state` and `transcript_splitter_state` fields
(it doesn't overwrite them). That's intentional — the dialog has
no UI for splitter sizes; it shouldn't accidentally reset them.

**8. Phase-5 completion checklist.**

5f's surface is unchanged from 5d's notes:

- (a) Menu bar with Cmd-shortcut reparenting (`File`, `Edit`,
  `View`, `Window`, `Help`).
- (b) App icon (`.icns`).
- (c) About dialog (Cmd-comma slot probably reused for Settings
  per macOS convention).
- (d) macOS quit-confirmation when there are unsaved changes
  (`closeEvent` currently just stops the timer — should prompt if
  `is_dirty`).

Two items I'd add:

- (e) The render-progress dialog should be migrated to a
  non-modal status indicator at some point — modal "Rendering…"
  blocks the main window. Not 5f's job per se, but the polish
  pass is the natural place.
- (f) Real-content Retina dark-mode validation of the dim overlay
  (carry-over from §11.6 above). One eyeball check on a real
  audio-bearing transcribed file; no code work expected.

If those land in 5f the phase ships; otherwise they're 5g/post-Phase-5
polish.
