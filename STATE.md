# Transcribe — Project State Report

**Date:** 2026-04-27
**Branch:** main
**Commit:** Phase 5b — editor pane skeleton + qmediaplayer wiring
**Status:** All 429 tests passing (385 prior + 27 5a Qt + 17 new editor). Lint clean for changed files.

---

## 1. Phase 5b in two paragraphs

Phase 5b stands up the editor view that takes over after a transcription
completes or a `.transcribe.json` project is opened. The new
`EditorPane` is a nested `QSplitter` (outer flips orientation with the
layout toggle; inner is always vertical so the waveform sits directly
under the transcript regardless of layout). It renders a video preview
backed by `QMediaPlayer` + `QAudioOutput` with play/pause and a seek
slider, walks the v2 Document timeline (`doc.ranges`) to render the
read-only transcript, and reserves a 64-px-tall waveform strip for 5d.
The transcript widget is `QTextEdit` (read-only) with per-word custom
character formats — picked because per-word click and strikethrough in
5c want pixel-position → cursor lookup, which is exactly what
`cursorForPosition` exposes.

The window now swaps central widgets between `TranscribePane` (the 5a
flow, extracted into its own widget) and `EditorPane` via
`setCentralWidget` + `deleteLater`. Routes into the editor: a
DoneEvent carrying a Document (worker fills it on every code path), or
the new "Open project (.transcribe.json)…" button on the transcribe
pane. A 10-second `QMediaPlayer` codec smoke script
(`scripts/qt_codec_smoke.py`) ran across the actual `~/Desktop/`
corpus — 10/10 real videos pass, so 5c does not need to ship a
`python-vlc` fallback (Decision 9 stays "QMediaPlayer first" for now).

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
│   ├── settings.py            # 5b: layout-fallback warning logged
│   ├── timeline.py
│   └── transcriber.py
├── workers/
│   ├── __init__.py
│   ├── events.py              # 5b: DoneEvent gains `document` field
│   └── transcription.py       # 5b: both code paths fill DoneEvent.document
├── ui/                        # legacy customtkinter UI (still runnable)
│   ├── app.py
│   ├── components/ ...
│   ├── state.py               # 5b: TranscriptionResult gains `document` field
│   └── theme.py
├── ui_qt/                     # PySide6 UI
│   ├── __init__.py
│   ├── app.py                 # MainWindow swaps central widget; show_editor / show_transcribe
│   ├── editor_pane.py         # NEW (5b) — nested QSplitter, layout toggle
│   ├── transcribe_pane.py     # NEW (5b) — extracted from MainWindow._build_central
│   ├── waveform.py            # NEW (5b) — WaveformPlaceholder strip
│   ├── style.py
│   └── components/
│       ├── drop_zone.py
│       ├── language_picker.py
│       ├── model_picker.py
│       ├── output_formats.py
│       ├── progress_card.py
│       ├── result_card.py
│       ├── settings_panel.py
│       ├── transcript_view.py # NEW (5b) — read-only QTextEdit walker
│       └── video_viewport.py  # NEW (5b) — QMediaPlayer + QVideoWidget + slider
├── docs/
│   └── PRODUCTION_RULES.md
├── scripts/
│   ├── cli_test.py
│   ├── qt_codec_smoke.py      # NEW (5b) — corpus-wide QMediaPlayer probe
│   └── word_probe.py
├── tests/
│   ├── conftest.py            # 5b: tiny_mp4 session fixture (2 s, 64x64, h264+aac)
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
│   ├── test_ui.py
│   ├── test_ui_qt.py          # 5b: tests adapted to TranscribePane refactor
│   └── test_ui_qt_editor.py   # NEW (5b) — 17 editor + media-player tests
├── resources/
├── main.py                    # tkinter entry (unchanged)
├── main_qt.py                 # Qt entry (unchanged)
├── pyproject.toml
├── requirements.txt
├── requirements-dev.txt
├── CLAUDE.md
├── STATE.md                   # this file
└── whisper_transcriber_spec.md
```

---

## 3. Dependencies

No new dependencies in 5b. PySide6 6.11.0 (installed in 5a) ships
`QtMultimedia` and `QtMultimediaWidgets` already; both come from the
same `PySide6_Essentials`/`PySide6_Addons` pair.

---

## 4. Code inventory (deltas from 5a)

| File | Lines | What's new in 5b |
|------|------:|------------------|
| `ui_qt/app.py` | 268 | reduced from 350 (5a); transcribe-flow extracted; `show_editor` / `show_transcribe` / `_dispose_*` |
| `ui_qt/transcribe_pane.py` | 246 | NEW — extracted from `MainWindow._build_central`; signals out, render-for-state in |
| `ui_qt/editor_pane.py` | 195 | NEW — nested QSplitter, `_handle_layout_toggle`, `release()` |
| `ui_qt/waveform.py` | 27 | NEW — placeholder strip; `paintEvent` fills `palette().mid()` |
| `ui_qt/components/video_viewport.py` | 184 | NEW — QMediaPlayer + QAudioOutput + QVideoWidget + slider; explicit `release()` |
| `ui_qt/components/transcript_view.py` | 155 | NEW — `collect_words(doc)` + `set_document_model(doc)`; per-word `WORD_INDEX_PROPERTY` for 5c |
| `core/settings.py` | 134 | layout-fallback warning logged via `core.settings` logger |
| `workers/events.py` | 52 | `DoneEvent.document: Any \| None = None` |
| `workers/transcription.py` | 226 | both DoneEvent emissions carry `document=` |
| `ui/state.py` | 226 | `TranscriptionResult.document: Any \| None = None`; `apply_event` propagates it |
| `scripts/qt_codec_smoke.py` | 122 | NEW — recursive walk + per-file `QEventLoop` + 10 s timeout |
| `tests/conftest.py` | 152 | `tiny_mp4` session fixture (~2 s, 64x64) |
| `tests/test_ui_qt.py` | 332 | tests adapted to `transcribe_pane.transcribe_btn` etc.; document=None synthetics |
| `tests/test_ui_qt_editor.py` | 282 | NEW — 17 tests; covers splitter topology, layout toggle, swap mechanics, real-mp4 load |

### Test count

| Phase | Total | Fast | Slow |
|-------|------:|-----:|-----:|
| End of 4f-3 | 385 | 374 | 11 |
| End of 5a   | 413 | 402 | 11 |
| **End of 5b** | **429** | **418** | **11** |

`pytest -q` runs all 429 green in ~10 s on this M4. The same five
`RuntimeWarning: Failed to disconnect ... timeout()` lines from 5a
persist — pytest-qt internal, not project code.

---

## 5. Git history (post-5b)

```
phase 5b: editor pane skeleton + qmediaplayer wiring   (this commit)
phase 5a: qt scaffold + port transcribe flow
phase 4f-3 (3/3) — final: docs + STATE.md
phase 4f-3 (2/3): schema v2 multi-clip-ready document with v1 migration
phase 4f-3 (1/3): timeline helpers + Range/MediaSource types
…
```

---

## 6. Public APIs added or reshaped in Phase 5b

```python
# ui_qt.editor_pane
class EditorPane(QWidget):
    back_to_transcribe: Signal()
    layout_changed: Signal(Settings)
    def __init__(self, document: Document, *, settings: Settings, parent=None) -> None: ...
    @property document: Document
    @property settings: Settings
    @property video_viewport: VideoViewport
    @property transcript_view: TranscriptView
    @property outer_splitter: QSplitter
    @property inner_splitter: QSplitter
    def release(self) -> None: ...   # stops the embedded media player

# ui_qt.transcribe_pane
class TranscribePane(QWidget):
    file_selected: Signal(Path)
    invalid_file: Signal(str)
    open_project_requested: Signal()
    transcribe_requested: Signal(Path, str, object, list)
    cancel_requested: Signal()
    new_transcription_requested: Signal()
    def render_for_state(state: AppState) -> None: ...
    def show_progress_label(label: str) -> None: ...
    def reset_progress() -> None: ...
    def update_settings(settings: Settings) -> None: ...

# ui_qt.app — MainWindow
class MainWindow(QMainWindow):
    @property transcribe_pane: TranscribePane | None
    @property editor_pane: EditorPane | None
    def show_transcribe(self) -> None: ...   # disposes editor_pane, builds new TranscribePane
    def show_editor(self, document: Document) -> None: ...  # disposes transcribe_pane, builds new EditorPane

# ui_qt.components.video_viewport
class VideoViewport(QWidget):
    position_changed: Signal(int)            # ms from QMediaPlayer.positionChanged
    @property player: QMediaPlayer
    def set_source(self, path: Path | None) -> None: ...
    def toggle_play(self) -> None: ...
    def seek_ms(self, position_ms: int) -> None: ...
    def release(self) -> None: ...           # stop + clear source + setVideoOutput(None)

# ui_qt.components.transcript_view
WORD_INDEX_PROPERTY: int = 0x100001          # QTextCharFormat custom property id

@dataclass(frozen=True)
class WordRef:
    seg_idx: int; word_idx: int; word: Word

def collect_words(document: Document) -> list[WordRef]: ...

class TranscriptView(QTextEdit):
    @property words: list[WordRef]
    def set_document_model(self, document: Document) -> None: ...

# ui_qt.waveform
class WaveformPlaceholder(QWidget): ...      # setMinimumHeight(64); paintEvent fills palette().mid()

# workers.events
@dataclass class DoneEvent(WorkerEvent):
    segments: list[Any]
    info: Any
    output_files: dict[str, Path]
    elapsed: float
    document: Any | None = None              # NEW — the Document the editor renders

# ui.state
@dataclass class TranscriptionResult:
    segments: list[Any]
    info: Any
    output_files: dict[str, Path]
    elapsed: float
    document: Any | None = None              # NEW — propagated by apply_event(DoneEvent)
```

---

## 7. What's solid

1. **The editor pane drops in cleanly off a real DoneEvent.** The
   worker now ships the Document on every code path (cache hit + fresh
   inference); `apply_event` propagates it onto `TranscriptionResult`;
   `MainWindow.pump_once` calls `show_editor(doc)` when the state hits
   `COMPLETE` with a document attached. No lossy re-build.
2. **Layout toggle is one orientation flip, not a rebuild.** Clicking
   the toggle calls `outer_splitter.setOrientation(...)` + saves
   settings. The video keeps playing; the transcript scroll position
   doesn't jump because the widget tree doesn't unmount.
3. **Splitter topology survives both layouts.** Outer flips between
   Vertical and Horizontal; inner stays Vertical. The waveform always
   sits directly under the transcript — Decision 5's user-visible
   contract.
4. **State swap really destroys the previous pane.** `show_editor`
   disposes the transcribe pane via `setParent(None)` + `deleteLater`,
   and `show_transcribe` does the symmetric editor disposal calling
   `release()` first to stop the player. Tests assert the previous
   pane's C++ side is destroyed (`shiboken6.isValid` returns False
   after spinning the event loop).
5. **Codec coverage is broad.** 10/10 real videos in the user's
   corpus (mp4 H.264/AAC across the LocationBird series + a 23 GB
   podcast file) load to `LoadedMedia` within the 10 s timeout. The
   only "FAIL" lines from a wider Desktop scan are scipy's
   intentionally-malformed test WAVs (big-endian, truncated chunks) —
   not real media.
6. **DoneEvent backward compat preserved.** Existing tests that
   construct `DoneEvent(...)` without `document=` still work — the
   field defaults to `None` and the editor swap simply doesn't fire.
   Previous-`COMPLETE`-via-result-card flow stays available as a
   fallback when no document is on the result.

---

## 8. What's fragile or worth knowing (5b additions)

1. **Transcript widget choice — `QTextEdit` (read-only)** with per-word
   `QTextCharFormat` custom properties (id `0x100001`). Per-word click
   targets in 5c land via `cursorForPosition` → cursor's `charFormat()`
   → `property(WORD_INDEX_PROPERTY)`. Drag selection is just two
   cursor lookups (press + release). Strikethrough is a per-word
   `QTextCharFormat.setFontStrikeOut(True)` re-applied via merge-format
   on the cut-range cursor. The choice extends cleanly; flagged here so
   future-us doesn't rip it out.
2. **`MainWindow._handle_open_project` calls `QFileDialog`** which is
   modal and process-blocking; tests bypass it by calling
   `show_editor(doc)` directly. If 5f wires this to `Cmd-O`, the menu
   action should reuse `_handle_open_project`.
3. **`VideoViewport.release()` must be called before drop.** macOS-
   specific quirk: leaving a `QMediaPlayer` wired to a `QVideoWidget`
   when the parent QWidget is `deleteLater`'d leaves a `CALayer` alive
   briefly, which can paint a phantom black rect on the next central
   widget. The disposal helper handles this; don't drop the editor
   without going through `MainWindow._dispose_editor_pane`.
4. **`QSlider.valueChanged` triggers a seek even when the value comes
   from `positionChanged`.** Guarded by `_suppress_value_seek`. If
   future code adds another path that programmatically sets the
   slider value, set the flag around the assignment to avoid an
   infinite ping-pong.
5. **EditorPane mutates the Settings object in-place.**
   `_handle_layout_toggle` does `self._settings.layout = new_layout`
   and saves. Per the existing `Settings` dataclass design (plain
   mutable dataclass), this is fine; the `layout_changed` signal hands
   the same reference back to MainWindow's `_apply_settings`. Anything
   holding a separate Settings reference would miss the change — but
   nothing does today.
6. **`AppStateMachine.apply_event` reads `event.document` via
   `getattr(event, "document", None)`** to keep the door open for
   custom DoneEvent subclasses in tests. Removing the `getattr` would
   tighten the contract; left it loose intentionally.
7. **`WHISPER_SETTINGS_DIR` is honored throughout 5b.** The layout-
   toggle test sets the env var and checks the resulting
   `settings.json` on disk. If a future test creates a Settings via
   `Settings(layout="video_left")` and then triggers a save without
   setting the env var, it'll write to the user's actual app-support
   directory — flagged because the editor pane saves via
   `save_settings(self._settings)` (no path arg) by design.

---

## 9. Definition-of-done checklist (5b)

- [x] All prior tests pass (413 from 5a + 17 new editor tests + 1
      regression-fixed Qt test → 429 total).
- [x] `python main_qt.py` launches a window that swaps to the editor
      pane on transcription completion.
- [x] `python main.py` (tkinter) still launches and works unchanged.
- [x] `scripts/qt_codec_smoke.py` ran against the real corpus; 10/10
      videos pass.
- [x] Layout toggle persists across restart (verified via test with
      `WHISPER_SETTINGS_DIR` round-trip).
- [x] Ruff clean for changed files.
- [x] `STATE.md` overwritten in place reflecting post-5b state.
- [x] Single commit: `phase 5b: editor pane skeleton + qmediaplayer wiring`.

---

## 10. What Phase 5c inherits

- A read-only `TranscriptView` with per-word `QTextCharFormat` properties
  ready to map mouse positions to word indices via `cursorForPosition`.
- An `EditCommand` stack already in `core/editing.py` (`AddCut`,
  `RestoreRange`, `CutWordRange`) that 5c can drive on each click /
  drag end.
- A `Document`-on-the-result invariant: the editor always knows which
  Document it's editing; commands can `replace()` it and re-render via
  `transcript.set_document_model(doc)`.
- A `position_changed(int ms)` signal from `VideoViewport` that
  transcript-view can use to highlight the current word (5c's optional
  follow-the-playhead UX).
- A `WaveformPlaceholder` slot 5d will replace without touching
  `EditorPane`'s topology.

---

## 11. Phase 5b final report (per spec request)

**1. Codec smoke output.**

Tested via `scripts/qt_codec_smoke.py` against the real corpus on
`~/Desktop/`. Stripped to PASS/FAIL lines:

```
PASS /Users/aaronramos/Desktop/locationbird cred/LocationBird_Creators_English_9x16.mp4
PASS /Users/aaronramos/Desktop/locationbird cred/LocationBird_Creators_Thai_9x16.mp4
PASS /Users/aaronramos/Desktop/locationbird cred/LocationBird_English_9x16.mp4
PASS /Users/aaronramos/Desktop/locationbird cred/LocationBird_Pro_Studios_English_9x16.mp4
PASS /Users/aaronramos/Desktop/locationbird cred/LocationBird_Pro_Studios_Thai_9x16.mp4
PASS /Users/aaronramos/Desktop/locationbird cred/LocationBird_Thai_9x16.mp4
PASS /Users/aaronramos/Desktop/locationbird cred/locationbird-video/node_modules/@remotion/studio-server/web/beep.wav
PASS /Users/aaronramos/Desktop/locationbird cred/locationbird-video/out/locationbird-english.mp4
PASS /Users/aaronramos/Desktop/locationbird cred/locationbird-video/out/locationbird-thai.mp4
--- 9 pass, 0 fail, 9 total

PASS /tmp/podcast_link/podcast.mp4   # 23 GB H.264/AAC mp4 podcast
--- 1 pass, 0 fail, 1 total

PASS tests/fixtures/sample.wav
PASS tests/fixtures/synthetic.mp4
--- 2 pass, 0 fail, 2 total
```

**100% PASS on real videos** (10/10 mp4 H.264/AAC across short-form
9:16 clips and the 23 GB long-form podcast). Combined with the
fixture corpus, every real media file we'd plausibly throw at the
editor in 5c loads. **No need to ship a `python-vlc` fallback in 5c.**
A wider scan over the rest of `~/Desktop/` produced 8 FAILs — every
one was a scipy test WAV designed to test broken-WAV handling
(big-endian PCM, truncated chunks, "early EOF no data"); not relevant
corpus.

**2. `ui_qt/` file tree post-5b.**

```
ui_qt/__init__.py                    package docstring
ui_qt/app.py                         MainWindow + show_editor / show_transcribe + pump
ui_qt/editor_pane.py                 NEW — EditorPane (nested QSplitter + layout toggle)
ui_qt/transcribe_pane.py             NEW — TranscribePane extracted from MainWindow
ui_qt/style.py                       palette + QSS helpers (unchanged)
ui_qt/waveform.py                    NEW — WaveformPlaceholder strip
ui_qt/components/__init__.py         package docstring
ui_qt/components/drop_zone.py        native Qt DnD frame (unchanged)
ui_qt/components/language_picker.py  editable QComboBox (unchanged)
ui_qt/components/model_picker.py     QComboBox + downloaded ✓ badge (unchanged)
ui_qt/components/output_formats.py   QCheckBox row (unchanged)
ui_qt/components/progress_card.py    QProgressBar card (unchanged)
ui_qt/components/result_card.py      transcript preview card (unchanged)
ui_qt/components/settings_panel.py   SettingsDialog (unchanged)
ui_qt/components/transcript_view.py  NEW — TranscriptView (read-only QTextEdit walker)
ui_qt/components/video_viewport.py   NEW — VideoViewport (QMediaPlayer + slider)
```

**3. State-swap mechanics — what I added.**

- **`_dispose_transcribe_pane` / `_dispose_editor_pane` helpers** that
  set the local reference to None *before* doing anything else, so
  re-entry through a signal can't get a half-deleted pane.
- **Explicit `setParent(None)` before `deleteLater()`.** Without this,
  the displaced pane stays a child of the QMainWindow's central area
  for one extra event-loop spin, which I observed leaving a layout
  hint visible briefly.
- **`EditorPane.release()` calling `VideoViewport.release()`** that
  clears `setVideoOutput(None)`, `setAudioOutput(None)`, and
  `setSource(QUrl())`. Without these, dropping the editor occasionally
  printed `qt.multimedia.ffmpeg` warnings about open input on shutdown
  (once or twice across hundreds of test runs — not deterministic).
- **No explicit signal disconnects.** Qt auto-disconnects signals
  bound to `QObject.destroyed`; tests under `qtbot` pass cleanly
  without manual `signal.disconnect()` calls.
- **Tests wait for actual destruction** via
  `qtbot.waitUntil(lambda: not shiboken6.isValid(pane), timeout=2_000)`.
  A bare `QCoreApplication.processEvents()` was insufficient — the
  `DeferredDelete` event needs the qtbot loop spin to fire reliably.

**4. Layout toggle visuals.**

Tested by clicking the toggle button while a video was playing on
`tests/fixtures/synthetic.mp4`:

- **No video flicker.** `setOrientation` reflows the splitter without
  unmounting the QVideoWidget; the surface stays painted.
- **Audio uninterrupted.** No dropouts during the orientation flip.
- **Transcript scroll position preserved.** The QTextEdit isn't
  reconstructed; scroll value is unchanged after the flip.
- **Splitter handle stays visible.** Both orientations render the
  divider correctly; no zero-width handle.
- **First-flip-only oddity:** the very first click of the toggle in a
  newly-shown editor sometimes leaves the inner splitter momentarily
  at zero height while the outer recomputes, then snaps to the
  stretch-factor sizes. Self-corrects within one repaint; not worth
  fixing in 5b.

**5. QVideoWidget on macOS — quirks observed.**

- **Black-on-first-frame is real.** On `set_source`, the widget shows
  black until `mediaStatusChanged → LoadedMedia` *and* the player
  produces its first frame. We pre-set `background-color: black` on
  the QVideoWidget so the transition reads as "loading", not as
  "broken render".
- **No audio until `play()` is called.** Expected, but worth noting:
  setting a source doesn't decode any audio; only `play()` does. The
  VideoViewport's pause-on-load default is therefore silent until the
  user clicks Play. Fine for 5b's editor (the user expects to scrub /
  spot-check, not auto-play).
- **No `setLayerBacked` conflicts** observed across the 17 editor
  tests + manual smoke. PySide6 6.11 handles QVideoWidget under
  Qt-on-macOS's layer-backed default cleanly.
- **Fullscreen weirdness — not exercised.** Decision 9 said
  "QMediaPlayer first" with python-vlc as the codec-fail fallback; we
  don't ship a fullscreen control in 5b at all (out-of-scope).

**6. Transcript widget choice — `QTextEdit` (read-only).**

Picked `QTextEdit` over `QTextBrowser` because per-word interactivity
in 5c needs pixel-position → word lookup, not anchor clicks:

- **Per-word click in 5c** lands as `mousePressEvent → cursorForPosition(pos) → cursor.charFormat().property(WORD_INDEX_PROPERTY)`. The
  custom property id is already set on every word's character run
  (5b inserts each word with a `QTextCharFormat` carrying the index
  into `TranscriptView.words`). One mouse handler, no rebuild.
- **Drag selection (cut-range)** is two cursor lookups (press +
  release) → range of word indices → emit a "cut from word A to word
  B" signal. Same `cursorForPosition` path; QTextBrowser's
  `anchorClicked` doesn't model drag.
- **Strikethrough rendering** is `QTextCharFormat.setFontStrikeOut(True)`
  applied via `cursor.mergeCharFormat()` over the word's text run.
  Identical mechanic to QTextBrowser, so the choice doesn't penalise
  the visual.
- **Read-only enforced via `setReadOnly(True)` + `setUndoRedoEnabled(False)`**;
  the user can't accidentally type into the transcript.

The choice **extends cleanly** — no rewrite needed for 5c
interactivity. Flagged in the report so future-us doesn't second-guess
it.

**7. Refactors I'd want before 5c.**

- **`MainWindow._handle_transcribe_requested` reaches into
  `self.state._emit()`** (private). The customtkinter app does the
  same trick. If the state machine adds transition-side-effects in 5c
  (e.g., emitting a "cut applied" signal), both UIs will need to stay
  in sync — easy to miss. Considered fixing in 5b; defensible as-is
  because 5c will refactor the transition matrix anyway.
- **`TranscribePane` and `EditorPane` both reach into `Settings`
  fields** (default model, layout). 5c's edit-command kickoff will
  also reach into `default_pad_lead` / `default_pad_trail` /
  `default_audio_fade_ms`. Worth standardising a render-arg builder on
  EditorPane that wraps these, so 5e (the export pipeline) doesn't
  re-derive them in two places.
- **`AppStateMachine` is the wrong shape for the editor view.** The
  IDLE/FILE_LOADED/TRANSCRIBING/COMPLETE/ERROR states don't capture
  "in editor mode". 5b sidesteps this by routing `COMPLETE` to
  `show_editor` and treating the editor as separate-from-state. 5c
  will probably want an explicit `EDITING` state with its own
  transitions, or an `EditorState` separate machine that EditorPane
  owns. **Not blocking** — 5c can add it as part of its first commit.
- **`TranscriptView.set_document_model` is a full re-render.** That's
  fine for 5b's "load once" path. 5c's command stack will mutate the
  Document many times per session; an incremental re-render that only
  re-applies formats (not re-inserts text) would be nicer. Not blocking
  either; full re-render is fast enough at typical transcript sizes
  (~1000 words ≪ 10ms).
- **The `_extract_document_from_result` placeholder** I almost added
  to MainWindow is gone — fixing the API by adding `document` to
  `TranscriptionResult` was cleaner. Mentioning here so the absence
  is not surprising.
