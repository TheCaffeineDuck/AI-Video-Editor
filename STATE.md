# Transcribe — Project State Report

**Date:** 2026-04-27
**Branch:** main
**Commit:** Phase 5a — Qt scaffold + port transcribe flow
**Status:** All 413 tests passing (374 fast tkinter + 11 slow + 28 new Qt). Lint clean for changed files.

---

## 1. Phase 5a in two paragraphs

Phase 5a stands up a second UI alongside the existing customtkinter app:
a PySide6 window that does what `python main.py` does today (drop a
media file, pick model/language/output formats, transcribe, see the
result paths) and looks more native on macOS. The transcription worker
that was a method on `ui.app.App` is now its own framework-agnostic
class in `workers/transcription.py`; both UIs construct one, hand it a
callback that puts events on a `queue.Queue`, and run it on a daemon
thread. The customtkinter app keeps working unchanged from the user's
perspective; the Qt app launches via `python main_qt.py`.

Settings are shared on disk between the two apps — same
`~/Library/Application Support/WhisperTranscriber/settings.json` — and
five new editor-related fields (`layout`, `default_pad_lead`,
`default_pad_trail`, `default_audio_fade_ms`, `autosave_interval_s`)
landed now even though most aren't read until 5b/5c/5f. Per the Phase-4e
non-migration rule, missing keys in older settings files fall through
to the documented defaults; nothing on disk is auto-rewritten.

---

## 2. Project structure

```
.
├── core/
│   ├── __init__.py
│   ├── audio.py
│   ├── cache.py
│   ├── document.py
│   ├── editing.py
│   ├── exporters.py
│   ├── languages.py
│   ├── model_loader.py
│   ├── models.py
│   ├── render.py
│   ├── settings.py            # +5 Phase-5 editor fields (5a)
│   ├── timeline.py
│   └── transcriber.py
├── workers/                   # NEW (5a)
│   ├── __init__.py
│   ├── events.py              # WorkerEvent dataclasses (lifted from ui/state.py)
│   └── transcription.py       # TranscriptionWorker + cache helpers
├── ui/                        # legacy customtkinter UI (still runnable)
│   ├── app.py                 # delegates worker work to TranscriptionWorker
│   ├── components/
│   │   ├── drop_zone.py
│   │   ├── language_picker.py
│   │   ├── model_picker.py
│   │   ├── output_formats.py
│   │   ├── progress_card.py
│   │   ├── result_card.py
│   │   └── settings_panel.py
│   ├── state.py               # re-exports WorkerEvents from workers.events
│   └── theme.py
├── ui_qt/                     # NEW (5a) PySide6 UI
│   ├── __init__.py
│   ├── app.py                 # MainWindow + run() entry
│   ├── components/
│   │   ├── drop_zone.py
│   │   ├── language_picker.py
│   │   ├── model_picker.py
│   │   ├── output_formats.py
│   │   ├── progress_card.py
│   │   ├── result_card.py
│   │   └── settings_panel.py
│   └── style.py               # palette + small QSS snippets
├── docs/
│   └── PRODUCTION_RULES.md
├── tests/
│   ├── conftest.py
│   ├── fixtures/ ...
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
│   ├── test_ui.py             # cache-hit tests now patch workers.transcription
│   └── test_ui_qt.py          # NEW (5a) — 28 pytest-qt tests
├── scripts/
│   ├── cli_test.py
│   └── word_probe.py
├── resources/
│   ├── bin/ffmpeg-mac
│   ├── fonts/
│   └── icons/
├── main.py                    # tkinter entry (unchanged)
├── main_qt.py                 # NEW (5a) — Qt entry
├── pyproject.toml             # +per-file ruff ignore for ui_qt N802 (Qt overrides)
├── requirements.txt           # +PySide6>=6.6
├── requirements-dev.txt       # +pytest-qt==4.4.0
├── CLAUDE.md
├── STATE.md                   # this file
└── whisper_transcriber_spec.md
```

---

## 3. Dependencies

### requirements.txt

```
customtkinter==5.2.2
tkinterdnd2==0.4.2
faster-whisper==1.2.1
huggingface-hub==0.24.0
smartcut==1.7
PySide6>=6.6                  # NEW (5a)
```

### requirements-dev.txt

```
-r requirements.txt
pytest==8.3.0
pytest-cov==5.0.0
pytest-qt==4.4.0              # NEW (5a)
ruff==0.6.0
```

### Resolved in venv (additions only)

```
PySide6==6.11.0
PySide6-Addons==6.11.0
PySide6-Essentials==6.11.0
shiboken6==6.11.0
pytest-qt==4.4.0
```

`pip check` exits 0 after install. The PySide6 wheel for
`cp310-abi3-macosx_13_0_universal2` installs cleanly on Apple Silicon
M4 — no source build, no Qt SDK, no Xcode requirement.

### Python / platform — unchanged

- Python 3.11.15 (`>=3.11,<3.12`)
- Apple Silicon M4, 16 GB
- CPU-only inference (`compute_type="int8"`, `device="auto"`)

---

## 4. Code inventory

### core/ (settings.py grew +20 LOC; everything else unchanged)

| File | Lines | What's new in 5a |
|------|------:|------------------|
| `core/settings.py` | 125 | +5 editor-preference fields; `LAYOUT_CHOICES` constant; `from_dict` normalizes unknown layout values |

### workers/ (NEW; 282 LOC)

| File | Lines | Purpose |
|------|------:|---------|
| `workers/transcription.py` | 224 | `TranscriptionWorker` class + `try_load_cached_document` / `candidate_cache_path` / `resolve_output_dir` helpers + `_CachedInfo` adapter |
| `workers/events.py` | 47 | `WorkerEvent` / `SegmentEvent` / `ProgressEvent` / `DoneEvent` / `ErrorEvent` / `CancelledEvent` (lifted from `ui/state.py`) |
| `workers/__init__.py` | 11 | docstring explaining the package's role |

### ui/ (app.py shrank by ~150 LOC; state.py lost the event dataclasses)

| File | Lines | What's new in 5a |
|------|------:|------------------|
| `ui/app.py` | 392 | `_run_transcription` is now a 12-line wrapper around `TranscriptionWorker`; cache-hit helper methods are 1-liners delegating to `workers.transcription`; deleted `_CachedInfo`, `_emit_cache_hit_done`, `_resolve_output_dir`, the in-line cache-load body, and the in-line transcribe body |
| `ui/state.py` | 217 | event dataclasses moved to `workers/events.py`, re-exported here for backward compat; explicit `__all__`; `AppStateMachine` unchanged |

### ui_qt/ (NEW; 760 LOC)

| File | Lines | Purpose |
|------|------:|---------|
| `ui_qt/app.py` | 296 | `MainWindow(QMainWindow)` + module-level `run()` |
| `ui_qt/components/drop_zone.py` | 142 | native Qt drag-and-drop frame with click-to-browse fallback |
| `ui_qt/components/settings_panel.py` | 165 | `SettingsDialog(QDialog)` with Transcription tab; emits `settings_saved(Settings)`; writes the same on-disk file |
| `ui_qt/components/progress_card.py` | 109 | `QProgressBar`-backed card with cancel signal |
| `ui_qt/components/result_card.py` | 105 | transcript preview + Open Folder / Copy / New Transcription |
| `ui_qt/components/language_picker.py` | 60 | editable `QComboBox` with case-insensitive substring completion |
| `ui_qt/components/model_picker.py` | 65 | `QComboBox` with downloaded ✓ badge + tooltip per item |
| `ui_qt/components/output_formats.py` | 78 | `QCheckBox` row + nudge label about the JSON format |
| `ui_qt/style.py` | 60 | `ACCENT`/`MUTED`/etc. constants + small QSS helpers |
| `ui_qt/__init__.py` + `components/__init__.py` | 14 | package docstrings |

### tests/ (test_ui_qt.py NEW; test_ui.py monkeypatches updated)

| File | Lines | What's new in 5a |
|------|------:|------------------|
| `tests/test_ui_qt.py` | 263 | NEW — 28 pytest-qt tests (component construction smoke tests, drop-event simulation via QDropEvent, MainWindow wiring + queue-pump tests, shared-settings round-trip test) |
| `tests/test_ui.py` | 449 | three cache-hit tests now `monkeypatch.setattr(worker_module, ...)` instead of `app_module` (workers.transcription owns the imports now) |

### Test count

| Phase | Total | Fast | Slow |
|-------|------:|-----:|-----:|
| End of Phase 4f-3 | 385 | 374 | 11 |
| **End of Phase 5a** | **413** | **402** | **11** |

`pytest -q` runs all 413 green in ~13 s on this M4 with the model
cached. The five `RuntimeWarning: Failed to disconnect ... timeout()`
lines are an internal pytest-qt issue around `qtbot.waitSignal`'s
timeout cleanup — not from project code, not test failures.

---

## 5. Git history (post-5a)

```
phase 5a: qt scaffold + port transcribe flow              (this commit)
phase 4f-3 (3/3): production rules + STATE.md final
phase 4f-3 (2/3): schema v2 multi-clip-ready document with v1 migration
phase 4f-3 (1/3): timeline helpers + Range/MediaSource types
phase 4f-2: document json cache via source_hash
docs: tighten audio-passthru rule to reflect single-pass re-encode reality
phase 4f-1: pad_lead/pad_trail + audio fades + render-time boundary snap
phase 4f-0: utc default-factory fix + three doc additions
…
```

---

## 6. Public APIs added or reshaped in Phase 5a

```python
# workers.events  (lifted out of ui.state; re-exported there for backward compat)
@dataclass class WorkerEvent: ...
@dataclass class SegmentEvent(WorkerEvent): text: str
@dataclass class ProgressEvent(WorkerEvent):
    fraction: float; label: str = "Transcribing…"
@dataclass class DoneEvent(WorkerEvent):
    segments: list; info: Any; output_files: dict[str, Path]; elapsed: float
@dataclass class ErrorEvent(WorkerEvent): message: str
@dataclass class CancelledEvent(WorkerEvent): ...

# workers.transcription
EventCallback = Callable[[WorkerEvent], None]

def resolve_output_dir(settings: Settings, media_path: Path) -> Path: ...
def candidate_cache_path(settings: Settings, media_path: Path) -> Path: ...
def try_load_cached_document(settings: Settings, media_path: Path) -> Document | None: ...

class TranscriptionWorker:
    def __init__(
        self, *,
        settings: Settings, media_path: Path,
        model_name: str, language: str | None, formats: list[str],
        on_event: EventCallback,
        cancel_event: threading.Event | None = None,
    ) -> None: ...
    def run(self) -> None: ...        # synchronous; UI spawns a daemon thread
    def cancel(self) -> None: ...     # safe from any thread
    @property
    def transcriber(self) -> Transcriber | None: ...

# core.settings — additions
DEFAULT_LAYOUT = "video_top"; LAYOUT_CHOICES = ("video_top", "video_left")
DEFAULT_PAD_LEAD = 0.10; DEFAULT_PAD_TRAIL = 0.10
DEFAULT_AUDIO_FADE_MS = 30; DEFAULT_AUTOSAVE_INTERVAL_S = 0

@dataclass class Settings:
    # … unchanged fields plus:
    layout: str = DEFAULT_LAYOUT
    default_pad_lead: float = DEFAULT_PAD_LEAD
    default_pad_trail: float = DEFAULT_PAD_TRAIL
    default_audio_fade_ms: int = DEFAULT_AUDIO_FADE_MS
    autosave_interval_s: int = DEFAULT_AUTOSAVE_INTERVAL_S

# ui_qt.app
class MainWindow(QMainWindow):
    state: AppStateMachine
    settings: Settings
    drop_zone: DropZone; model_picker: ModelPicker; …
    def pump_once(self) -> int: ...   # public so tests don't need event-loop spinning

def run() -> int: ...                  # convenience entry; main_qt.py calls this
```

The previous controller-private surface (`App._try_load_cached_document`,
`App._candidate_cache_path`, `App._run_transcription`) is preserved as
1-line wrappers so the existing `tests/test_ui.py` cache-hit tests
keep passing without touching the controller-call shape.

---

## 7. Document JSON format — unchanged from 4f-3

(See git history at `STATE.md@4f-3` if a reminder of the exact shape
is needed; nothing in 5a touched persistence.)

---

## 8. What's solid

1. **Worker is framework-agnostic and tested as such.** The same
   `TranscriptionWorker` powers both UIs. Cache lookup, model download,
   transcribe, and output writing are one code path that the tkinter
   side already exercises in production and the Qt side now exercises
   in tests.
2. **Settings are shared on disk between the two apps.** A user who
   ran the customtkinter app yesterday opens the Qt app today and sees
   their model / language / output formats / output dir intact, and
   vice versa. Verified by `test_settings_dialog_save_writes_shared_file`.
3. **New Settings fields follow the non-migration rule.** A pre-5a
   `settings.json` opened by the new `from_dict` path picks up
   defaults for the five missing keys without rewriting on disk.
   Confirmed by re-running the full `test_settings.py` suite (no test
   needed an update; lenient `from_dict` already covered the case).
4. **Native Qt drag-and-drop works without third-party shims.** The
   tkinter side needed `tkinterdnd2`; the Qt side uses Qt's built-in
   `dragEnterEvent` / `dropEvent` directly. Tested via a synthesized
   `QDropEvent` with a `file://` URL.
5. **Pump shape transports cleanly to QTimer.** The same
   `pump_queue(queue, machine)` helper drives both UIs — a Tk
   `root.after(100, …)` loop in one, a `QTimer.timeout` connection in
   the other. No queue/state-machine code was duplicated.
6. **All 385 prior tests still pass.** No behavioral regressions from
   the worker extraction.

---

## 9. What's fragile or worth knowing (Phase 5a additions)

1. **QMediaPlayer is imported but not exercised yet.** The 5a smoke
   test confirms `from PySide6.QtMultimedia import QMediaPlayer`
   succeeds, but no playback path is wired. Whether QMediaPlayer
   handles our actual `.mp4` / `.mov` / `.mkv` corpus stays an open
   question until 5b. Decision 9 keeps the option to swap in
   `python-vlc` if codec issues surface.
2. **Pump cadence is identical between the two apps (100 ms).** If 5b
   adds a high-frequency video-frame timer on top of the pump, we may
   want to split: keep the slow worker-event pump at 100 ms, run the
   playback timer at a faster rate. Not a problem for 5a.
3. **`_emit` access in MainWindow.** `MainWindow._handle_transcribe_click`
   calls `self.state._emit()` to force a re-render after manually
   resetting the state from `ERROR` to `FILE_LOADED`. Same trick the
   tkinter `App` uses; both reach into the same private. If the state
   machine grows transition-side-effects, both UIs need to stay in
   sync — easy to miss.
4. **The Qt language picker doesn't reproduce the modal listbox UX.**
   `QComboBox.setEditable(True)` + the built-in completer give a
   reasonable substring-search experience without a separate dialog.
   That's a real divergence from the tkinter UI; it's appropriate
   (Qt has the better widget) but worth flagging.
5. **The new Settings fields are stored, never read by the active UI
   yet.** They're plumbed end-to-end (defaults, `from_dict`,
   `to_dict`) and the Qt SettingsDialog round-trips them through
   `_save`. Reading them lands in 5b–5f.
6. **`ui_qt` has a per-file `N802` lint exception.** Qt overrides
   (`dropEvent`, `mousePressEvent`, `closeEvent`, …) follow Qt's
   camelCase API; the project's `ruff` config now ignores N802 under
   `ui_qt/`. Pre-existing lint debt in `tests/test_render.py` /
   `test_document.py` / `test_editing.py` is unrelated and untouched.
7. **macOS-specific Qt behavior not yet exercised.** Native menu bar
   (Cmd-, Cmd-W, Cmd-Q wiring), dark mode appearance, high-DPI
   rendering at 2× — none of those are validated yet. Phase 5f's
   "polish" sub-phase explicitly owns them.

---

## 10. Definition-of-done checklist (5a)

- [x] `python main_qt.py` launches a window with drop zone, model /
      language / output controls, Transcribe button, and a Settings
      dialog.
- [x] `python main.py` (the customtkinter app) still works identically
      — same 19 UI tests pass, same flows.
- [x] Worker refactored into `workers/transcription.py`; both UIs
      delegate to it.
- [x] Settings shared between the two apps (`WHISPER_SETTINGS_DIR`-
      aware shared loader).
- [x] Five new editor-preference fields in `Settings` with documented
      defaults and lenient `from_dict`.
- [x] 28 new pytest-qt tests pass.
- [x] All 385 prior tests still pass (413 total).
- [x] `STATE.md` overwritten in place (this file).
- [x] Commit message: `phase 5a: qt scaffold + port transcribe flow`.

---

## 11. What Phase 5b inherits

- A working PySide6 `MainWindow` with the IDLE / TRANSCRIBING /
  COMPLETE flow already wired through the existing
  `AppStateMachine`. Adding an `EDITING` state and a fourth
  `QStackedWidget` page is the obvious next move.
- A worker that the editor view can keep calling unchanged — it
  already returns a `Document` (via the cache-hit path) or rebuilds
  one (via the inference path).
- Settings fields for editor layout (`layout`,
  `default_pad_lead`/`default_pad_trail`/`default_audio_fade_ms`,
  `autosave_interval_s`) ready to read from the editor's render
  invocation.
- A pytest-qt test scaffold (`tests/test_ui_qt.py` + the `qtbot`
  fixture) ready to extend with editor-pane tests.
- A `ui_qt/components/` directory laid out with one widget per file —
  same shape as `ui/components/` so the next sub-phase can keep
  adding files without reorganising.

---

## 12. Phase 5a final report (per spec request)

**1. Worker refactor — needed and shape.**

Yes, the refactor was needed. The transcription work was a method on
`ui.app.App` (plain Python — `queue.Queue` + dataclass events — but
coupled in code location to the customtkinter UI's Tk root). The
extracted shape is `workers.transcription.TranscriptionWorker`, a
class instantiated by either UI with:

- the immutable `Settings` and per-run inputs (`media_path`,
  `model_name`, `language`, `formats`),
- an `on_event: Callable[[WorkerEvent], None]` callback (tkinter
  passes `self.event_queue.put`; Qt passes the same — both UIs
  happen to use a `queue.Queue` for cross-thread delivery, but the
  worker doesn't depend on that),
- an optional shared `threading.Event` for cooperative cancel.

`run()` is synchronous; the UI spawns a daemon `Thread(target=worker.run)`.
`cancel()` flips the event and tells the live `Transcriber` to stop.
Cache-helper functions (`try_load_cached_document`, `candidate_cache_path`,
`resolve_output_dir`) are module-level so neither UI needs to subclass
or own them. The `WorkerEvent` dataclasses moved to
`workers/events.py` so `ui_qt` doesn't have to import from `ui.state`;
`ui.state` re-exports them for backward compat.

`tests/test_ui.py`'s three cache-hit tests now patch
`workers.transcription.{Transcriber, download_model, is_downloaded}`
instead of the old `ui.app.{...}` location. Same assertions, same
behavioural coverage; one-import-line change per test.

**2. QMediaPlayer in 5a.**

Imported only as a smoke test (`from PySide6.QtMultimedia import
QMediaPlayer; print('ok')` returns ok). No playback path wired; the
editor pane lands in 5b and that's where playback either works or
forces the python-vlc fallback per Decision 9. The Settings dialog
and the IDLE / TRANSCRIBING / COMPLETE flow have no use for it.

**3. Qt + macOS observations.**

- **PySide6 install is painless on M4.** The 6.11.0 wheel
  (`cp310-abi3-macosx_13_0_universal2`, ~1 GB across PySide6 +
  Essentials + Addons + shiboken6) installs in ~50 s and exposes
  QtMultimedia without a separate dependency.
- **Native drag-and-drop works out of the box.** No `tkinterdnd2`
  shim, no DnDWrapper plumbing — `setAcceptDrops(True)` plus the
  three handlers, done.
- **`QComboBox`'s built-in completer is enough for the language
  picker** with `MatchContains` + case-insensitive — the modal
  search dialog the tkinter side needed isn't necessary here.
- **Dark mode inherited automatically.** No code changed; the Qt
  app respects macOS appearance because we don't override the
  palette except for two colour accents (drop-zone border, banner
  background). The accent button stays blue in both modes by
  design.
- **`QMainWindow.setStatusBar(QStatusBar(self))`** is a no-op
  visually for now but reserves space for the modified-indicator
  dot Phase 5f wants.
- **Native menubar (Cmd-, Cmd-W, Cmd-Q) is NOT yet wired.** The
  Settings button on the toolbar opens the dialog; standard menu
  shortcuts ride along Phase 5f.
- **Pixel-perfect parity with the customtkinter look is explicitly
  out of scope.** The Qt UI is meant to feel native, not identical;
  spacing/padding sizing differs intentionally.

**4. Settings file format — backward-compat surprises.**

None. The lenient `Settings.from_dict` (4e) already tolerated
unknown keys and missing keys; the new `layout` /
`default_pad_lead` / `default_pad_trail` / `default_audio_fade_ms` /
`autosave_interval_s` slot in via the existing fall-back-to-default
loop. The one normalization added (unknown `layout` value falls back
to `"video_top"` rather than propagating a typo) is a defensive
guard against future hand-edits, not a migration. No existing test
needed updating; `tests/test_settings.py` continues to pass
unchanged.

`Settings.to_dict` (via `dataclasses.asdict`) writes the new fields
out on every save, so a user who opens the Qt Settings dialog and
clicks Save now has a `settings.json` containing all 11 fields. A
user who never touches Settings retains their pre-5a six-field file
indefinitely; the loader fills in the missing five from defaults at
load time.
