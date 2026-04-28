# Transcribe — Project State Report

**Date:** 2026-04-28
**Branch:** main
**Commit:** Phase 5f — macos polish, menu bar, quit guard, render ux
**Status:** Phase 5 complete. All 529 tests passing (501 prior + 28 5f). Lint
clean for changed files.

---

## 1. Phase 5 in two paragraphs

Phase 5 ported the customtkinter app onto PySide6 and built a Descript-
style editor on top: 5a scaffolded the Qt UI; 5b laid down the editor
pane skeleton with `QSplitter` topology and a real `QMediaPlayer`; 5c
added word-level cuts, undo/redo, and Cmd-S persistence; 5d shipped a
waveform strip with cached peaks and a dim-overlay-and-hatch readout
that survives both dark and light palettes; 5e closed the integration
loop with off-thread render export, autosave, splitter persistence,
and a complete tabbed Settings dialog.

Phase 5f turned the Qt window into a Mac app. A native menu bar
(File / Edit / View / Window / Help) reparents the Cmd-shortcut
actions onto MainWindow, properly enabled/disabled with editor
presence; About / Settings / Quit roles route via
`QAction.MenuRole` so macOS surfaces them in the application menu.
A quit-when-dirty guard prompts on `closeEvent` with three branches
(Save / Discard / Cancel) and cancels the close if the save raises.
The render progress dialog migrated from modal `QProgressDialog` to a
non-modal status-bar strip — the user keeps editing during a render,
and `RenderWorker` snapshots the document at construction so mid-render
edits don't reach the rendered output. An autosave indicator on the
left of the status bar reports "Saved" / "Saving…" / "Unsaved changes" /
"Autosave failed". A geometric mark icon (committed as
`resources/icons/transcribe.icns`, regeneratable via
`scripts/make_icon.py`) hangs on the window via `setWindowIcon`. With
that, Phase 5 ships.

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
│   ├── render.py
│   ├── transcription.py
│   └── waveform.py
├── ui/                              # legacy customtkinter UI (still launches)
│   ├── app.py
│   ├── components/ ...
│   ├── state.py
│   └── theme.py
├── ui_qt/
│   ├── __init__.py                  # 5f: __version__ = "0.5.0"
│   ├── app.py                       # 5f: menu bar, quit guard, status bar, app icon
│   ├── document_session.py
│   ├── editor_pane.py               # 5f: external EditorActions, non-modal render
│   ├── transcribe_pane.py
│   ├── waveform.py
│   ├── waveform_controller.py
│   ├── style.py
│   └── components/
│       ├── about_dialog.py          # NEW (5f)
│       ├── settings_panel.py
│       ├── status_widgets.py        # NEW (5f) — RenderStatusWidget, AutosaveStatusLabel
│       ├── transcript_view.py
│       ├── video_viewport.py
│       └── ...
├── resources/
│   └── icons/
│       ├── transcribe.icns          # NEW (5f) — bundled icon
│       └── transcribe_1024.png      # NEW (5f) — source bitmap
├── scripts/
│   └── make_icon.py                 # NEW (5f) — sips + iconutil pipeline
├── docs/PRODUCTION_RULES.md
├── tests/
│   ├── conftest.py
│   ├── ... (all prior)
│   └── test_phase_5f.py             # NEW (5f) — 28 tests
├── main.py / main_qt.py
├── pyproject.toml / requirements*.txt
├── CLAUDE.md
├── STATE.md
└── whisper_transcriber_spec.md
```

---

## 3. Dependencies

Unchanged from 5e.

---

## 4. Code inventory (deltas from 5e)

| File | Lines | What's new in 5f |
|------|------:|------------------|
| `ui_qt/app.py` | 660 | `_build_actions` constructs an :class:`EditorActions` bundle owned by MainWindow plus role-bearing app-menu actions (About, Settings, Quit); `_build_menu_bar` populates File/Edit/View/Window/Help; `_setup_status_bar` adds :class:`AutosaveStatusLabel` on the left and :class:`RenderStatusWidget` on the right; `closeEvent` prompts on dirty editor with Save/Discard/Cancel and cancels close on save failure; `event` overrides `ApplicationActivate` for Dock-icon reopen; `_handle_render_*` slots replace EditorPane's modal dialog; `_force_close` flag bypasses the prompt during programmatic teardown |
| `ui_qt/editor_pane.py` | 800 | New :class:`EditorActions` dataclass holding the seven shared QActions; `__init__` accepts an optional `actions` kwarg (None → falls back to local instances for standalone use); `_wire_actions` connects via tracked `(signal, slot)` pairs so :meth:`release` can disconnect cleanly across pane swaps; render progress migrated to typed signals (`render_started`/`render_progress`/`render_completed`/`render_failed`/`render_cancelled`) plus public `cancel_render`/`is_rendering` accessors; modal QProgressDialog removed; backwards-compat `_save_action`/`_cut_action`/etc properties keep test reach intact |
| `ui_qt/__init__.py` | 14 | `__version__ = "0.5.0"` — single source of truth for the About dialog |
| `ui_qt/components/about_dialog.py` | 100 | NEW — modal "About Transcribe" with version, Python/PySide6/faster-whisper/ffmpeg runtime info; `about_text()` exposed for tests |
| `ui_qt/components/status_widgets.py` | 150 | NEW — :class:`RenderStatusWidget` (label + indeterminate→determinate `QProgressBar` + Cancel button; hidden when idle); :class:`AutosaveStatusLabel` (debounced state machine with four labels) |
| `scripts/make_icon.py` | 130 | NEW — paints a 1024×1024 geometric T-and-waveform PNG via QPainter, then runs `sips`/`iconutil` to emit the `.icns` |
| `resources/icons/transcribe.icns` | binary | NEW — committed icon bundle (16/32/64/128/256/512/1024 from one source) |
| `resources/icons/transcribe_1024.png` | binary | NEW — committed source PNG |
| `tests/test_phase_5f.py` | 530 | NEW — 28 tests across menu-bar reparenting, enabled-state mirroring, quit-when-dirty (3 branches + save-failure-cancels-close), About dialog, render-status non-modal flow + Export-disabled-during-render + snapshot isolation, autosave indicator state machine, action-connection-disconnect-on-release |

### Test count

| Phase | Total | Fast | Slow |
|-------|------:|-----:|-----:|
| End of 4f-3 | 385 | 374 | 11 |
| End of 5a   | 413 | 402 | 11 |
| End of 5b   | 429 | 418 | 11 |
| End of 5c   | 458 | 447 | 11 |
| End of 5d   | 479 | 467 | 12 |
| End of 5e   | 501 | 489 | 12 |
| **End of 5f** | **529** | **517** | **12** |

`pytest -q` runs all 529 green in ~10 s on this M4. The pytest-qt
`RuntimeWarning` lines persist (5c-tracked, pytest-qt internal).

---

## 5. Git history

```
phase 5f: macos polish, menu bar, quit guard, render ux  (this commit)
phase 5e: render export, autosave, splitter persistence, settings completion
phase 5d: waveform strip with cache and dim regions
phase 5c: transcript interactivity, cuts, undo, save
phase 5b: editor pane skeleton + qmediaplayer wiring
phase 5a: qt scaffold + port transcribe flow
phase 4f-3 (3/3) — final: docs + STATE.md
…
```

---

## 6. Public APIs added or reshaped in Phase 5f

```python
# ui_qt — new
__version__: str  # "0.5.0"

# ui_qt.editor_pane — additions
@dataclass
class EditorActions:
    save: QAction
    export_: QAction
    undo: QAction
    redo: QAction
    cut: QAction
    restore: QAction
    delete: QAction

class EditorPane(QWidget):
    # ... existing signals + new render_progress(float)
    render_progress: Signal(float)

    def __init__(
        self,
        document: Document,
        *,
        settings: Settings,
        actions: EditorActions | None = None,   # NEW (5f)
        parent: QWidget | None = None,
    ) -> None: ...

    @property
    def actions_bundle(self) -> EditorActions: ...   # NEW (5f)
    @property
    def is_rendering(self) -> bool: ...               # NEW (5f)
    def cancel_render(self) -> None: ...              # NEW (5f)

# ui_qt.app — additions on MainWindow
class MainWindow(QMainWindow):
    @property
    def editor_actions(self) -> EditorActions: ...   # NEW (5f)
    save_action: QAction         # alias via editor_actions.save
    export_action: QAction
    # ... other action properties via editor_actions
    # closeEvent now prompts on dirty editor; ``_force_close`` bypasses.

# ui_qt.components.status_widgets — NEW
class RenderStatusWidget(QWidget):
    cancel_clicked: Signal()
    def start(self, label: str = "Rendering…") -> None: ...
    def set_progress(self, fraction: float) -> None: ...
    def mark_cancelling(self) -> None: ...
    def finish(self) -> None: ...

class AutosaveStatusLabel(QLabel):
    def set_state(self, state: str) -> None: ...
    def clear_indicator(self) -> None: ...
AUTOSAVE_SAVED, AUTOSAVE_SAVING, AUTOSAVE_DIRTY, AUTOSAVE_FAILED: str

# ui_qt.components.about_dialog — NEW
class AboutDialog(QDialog): ...
def about_text() -> str: ...
```

---

## 7. What's solid

1. **Menu bar reparenting works without ambiguous-shortcut warnings.**
   Each editor QAction lives once on MainWindow's `EditorActions`
   bundle. EditorPane receives the bundle on construction and
   connects via `(signal, slot)` pairs tracked for explicit disconnect
   in `release()`. The "Save action on the File menu is the same
   QAction object as the one wired to Cmd-S in EditorPane" test
   assertion (`main.editor_actions.save is main.editor_pane._save_action`)
   passes verbatim.
2. **Quit-when-dirty has all four behaviours covered by tests.**
   Clean editor closes immediately. Dirty editor prompts. Save branch
   writes and closes. Discard branch closes without writing. Cancel
   branch keeps the window open. Save-fails-cancels-close: mocked
   write raise → "Save failed" critical → close ignored, editor still
   alive and dirty. The four-test surface is the contract.
3. **Render runs non-modally; user can edit during render.**
   `RenderWorker` constructor stores a reference to the Document at
   that moment. Mid-render `session.apply` produces a new Document
   object on the session — the worker's snapshot is the original. The
   `test_render_during_edit_uses_pre_render_snapshot` test confirms
   `captured["doc"] is snapshot_doc` after a cut applies during the
   worker run.
4. **Export action disables during render.** `_handle_render_started`
   sets `editor_actions.export_.setEnabled(False)`; the four terminal
   render events (complete/error/cancel) re-enable it. A second export
   shortcut press during a render hits a disabled action and is a no-op.
5. **App icon ships.** `resources/icons/transcribe.icns` is committed,
   regeneratable via `scripts/make_icon.py`. `QApplication.setWindowIcon`
   in `run()` and `MainWindow.setWindowIcon` in `__init__` give us the
   window-bar mark. Dock icon stays Python's launcher icon when running
   `python main_qt.py` (no `.app` bundle yet); flagged in §8.
6. **About dialog reports real runtime versions.** `about_text()`
   pulls live `PySide6`, `faster_whisper`, and `ffmpeg -version`
   strings each call — no stale cached strings.
7. **Autosave indicator transitions cleanly.** `AutosaveStatusLabel`
   coalesces rapid set_state churn through an 80 ms debounce. With
   autosave on, dirty → "Saving…" → on save complete → "Saved". With
   autosave off, dirty → "Unsaved changes" until manual save.
8. **Pane swap doesn't leak action handlers.** `release` disconnects
   each `(signal, slot)` pair we tracked at wire time. The
   `test_action_connections_disconnect_on_release` test cycles two
   `show_editor` calls and asserts the first pane's connection list
   is empty post-swap — no stale double-handlers waiting for
   DeferredDelete.

---

## 8. What's fragile or worth knowing (5f additions)

1. **App icon caveat: Dock icon needs bundling.**
   `setWindowIcon` only paints the icon in the title bar / window
   close-button area. The Dock icon comes from the executable's
   `Info.plist`; running `python main_qt.py` shows Python's launcher
   icon, not Transcribe's. Proper Dock icon requires `py2app` /
   `pyinstaller` bundling, which is post-Phase-5 packaging work.
2. **Render progress is still best-effort.** No change from 5e
   here — `core.render.render_cut`'s `_ProgressAdapter` only emits
   when smartcut emits, and smartcut goes silent during long ffmpeg
   stretches. The status-bar `RenderStatusWidget` promotes itself
   from indeterminate to a 0–100 bar on first numeric tick, but on
   long sources the bar still sits at one value for chunks. Genuine
   progress would require modifying `core/render.py`, deferred per
   the 5f spec's "no `core/` changes" rule.
3. **Cancel-during-render only takes effect on next progress tick.**
   `cancel_render()` sets the `threading.Event` synchronously, but
   the worker observes it through smartcut's progress callback. If
   smartcut is mid-ffmpeg, cancellation lands when the next progress
   tick fires — which on a 23 GB podcast may be seconds, not
   milliseconds.
4. **Quit guard relies on `_force_close` for programmatic teardown.**
   pytest-qt's `_close_widgets` (and any future programmatic close
   path) needs to set `_force_close = True` before calling close, or
   the dirty-editor prompt blocks forever waiting for input. The
   pytest fixture installs this via `qtbot.addWidget(win,
   before_close_func=...)`. The non-test path is naturally fine
   because the user dismisses the prompt themselves.
5. **Editor actions stay parented to MainWindow even when no editor.**
   The seven QActions in `EditorActions` are children of MainWindow
   via `QAction(text, self)`. They're disabled while no editor pane
   is open. EditorPane, when constructed, calls `self.addAction` on
   each so shortcuts fire while the pane is alive. On
   `_dispose_editor_pane`, the actions stay alive (just disabled);
   the addAction-association with the now-deleted pane is dropped by
   Qt automatically.
6. **`show_editor` now disposes the prior editor pane too.**
   Original 5e behaviour assumed editor was only entered from
   transcribe. 5f tests (`action_connections_disconnect_on_release`)
   exercise editor → editor swaps; `show_editor` calls
   `_dispose_editor_pane` first to prevent leaked panes. Real-world
   only path that hits this is "user opens a different project file
   while one's already open via Cmd-O" — the existing
   `_handle_open_project` now flows through this cleanly.
7. **Dim-overlay verification result.** Re-checked programmatically
   on varied-amplitude peaks (loud + quiet sections) against both
   dark (#242424) and light (#ffffff) palettes. Loud-cut-vs-loud-kept
   delta: 52 (dark) / 72 (light). Quiet-cut-vs-quiet-kept delta: 31
   (dark) / 43 (light). All four well above the 10-unit threshold
   the existing test enforces. No code change to the overlay itself
   was needed — 5e's gray-plus-hatch ships verified for 5f.

---

## 9. Phase 5f stop-and-report (per spec)

**1. Dim-overlay verification.**

Verified programmatically against varied-amplitude peaks in both dark
mode (base #242424, ink #DCDCDC) and light mode (base #FFFFFF, ink
#000000). Same-amplitude cut-vs-kept lightness deltas:

```
dark   loud    52    quiet 31
light  loud    72    quiet 43
```

All four pairs read distinct above the 10-unit perceptual threshold
the existing test enforces. The 5e gray-plus-hatch overlay is
durable. No code change. (I did not eyeball this on a Retina display
against an actual transcribed long-form podcast clip — the
synthetic fixture and the dark/light palette sweep are the
mechanical verification I could honestly do in this session.)

**2. Reparent vs. promote.**

Promoted action ownership to MainWindow. The seven editor QActions
(`save`, `export_`, `undo`, `redo`, `cut`, `restore`, `delete`)
live on a `EditorActions` dataclass owned by MainWindow and handed
to every EditorPane on construction. EditorPane's `_wire_actions`
connects each action to its handler and records the `(signal, slot)`
pair; `release` disconnects them so a pane-swap doesn't leak
double-handlers.

The "EditorPane is recreated per-document" path was real: on every
`show_editor` we destroy the old pane and create a new one. If the
QActions had been parented to EditorPane, they'd die with each
pane and the menu bar would point at deleted QAction instances on
swap. Promoting was the right call; lifetime story is "MainWindow
owns the actions, panes connect on construct and disconnect on
release, no zombies."

The standalone-EditorPane test path (`tests/test_phase_5e.py`'s
fixture) gets a local `EditorActions` from `_build_local_actions()`
when the constructor's `actions` arg is `None`. Same handler wiring
either way; `_save_action`/`_cut_action`/etc properties keep the
old test-reach intact.

**3. Quit-when-dirty edge cases.**

Covered: clean editor → instant close. Dirty + Save → write succeeds
→ close. Dirty + Save → write fails (OSError) → critical popup,
close cancelled, editor stays alive and dirty. Dirty + Discard →
close without write. Dirty + Cancel → close cancelled. All five
covered by tests.

Not covered:
- Cmd-Q from within a `QFileDialog` (the dialog's own modal swallows
  the Cmd-Q before MainWindow sees it; that's macOS HIG-correct).
- Force-quit via Activity Monitor or Cmd-Option-Esc (these are SIGKILL,
  the process never gets to run cleanup; that's an OS-level escape
  hatch the user has explicitly opted into).
- The `applicationShouldTerminate` callback Qt's QApplication
  doesn't expose for us to vet from the application-level Quit menu
  before the window-level closeEvent. macOS routes Cmd-Q through the
  application-menu Quit action (which our `_quit_action.triggered`
  connects to `self.close`), which then sends a closeEvent — so the
  guard fires there too, by construction.

**4. Render UX during edit.**

Snapshot isolation held. `RenderWorker.__init__` captures
`self.document = document` at construction. The session's `apply`
returns a fresh Document via `dataclasses.replace`, so the worker's
captured reference stays pointing at the original. The
`test_render_during_edit_uses_pre_render_snapshot` test starts a
worker, mutates the session via cut.trigger, runs the worker, and
asserts `captured["doc"] is snapshot_doc` — passes.

Visible weirdness during the in-flight edit window: undo past the
snapshot point would pop the worker's snapshot off the user's
visible undo stack, but doesn't affect the worker's captured ref.
The user could, in principle, undo all the way back, redo to a
totally different state, and the rendered output still reflects the
exact pre-render Document. That's correct snapshot semantics —
"render produces the version you asked it to render at the moment
you asked." But it can confuse a user who expects "the render
follows whatever I'm looking at." Worth a doc note in the user
manual when that exists; not a code issue.

**5. App icon path.**

Drafted a geometric mark — translucent blue rounded-square
background with a stylised T crossbar over six waveform-style
vertical bars. Painted via QPainter into a 1024×1024 PNG, then run
through `sips` to emit the iconutil-compatible iconset and finally
`iconutil -c icns` for the `.icns` bundle. Committed both the source
PNG (`resources/icons/transcribe_1024.png`, ~67 KB) and the bundle
(`resources/icons/transcribe.icns`, ~315 KB). `scripts/make_icon.py`
regenerates both end-to-end.

Nothing weird about the `.icns` generation. `sips`/`iconutil` are
both first-party macOS tools; the recipe is the standard one from
Apple's own docs. The icon is functional but not pretty —
re-skinning is a one-line change to `_render_source` in the script.

**6. Phase 5 retrospective.**

Three things I'd do differently if running 5a-5f again:

1. **Decide action ownership earlier.** 5c put QActions on
   EditorPane to satisfy the shortcut-fires-anywhere requirement.
   5f had to refactor that into a MainWindow-owned bundle when the
   menu bar landed. Both calls were locally correct, but the 5f
   refactor was bigger than it needed to be — `_action_connections`
   tracking, backwards-compat property aliases, the
   `actions=None`-fallback constructor branch. If 5c had built
   actions on a separate `EditorActions` namespace from the start
   (even when EditorPane was their only consumer), 5f's menu-bar
   reparenting would've been a 20-line change.

2. **Don't ship a render dialog you'll throw away in three commits.**
   5e's modal `QProgressDialog` was scaffolding that read like
   product, then got migrated to a status-bar widget in 5f. The
   modal worked but everyone who saw it knew it was wrong (modal
   "Rendering…" blocking the editor for minutes is not a thing). I
   should've pushed back on shipping it in 5e and gone straight to
   the status-bar pattern. The cost of "do it right the first time"
   was a half-day in 5e; the cost of "ship the modal, refactor in
   5f" was that half-day plus the 5f migration cost plus the
   cognitive load of two release-noted UX states.

3. **Test the macOS-specific paths during development, not at end.**
   The pytest-qt-quit-guard interaction (where `_close_widgets` runs
   before fixture finalizers, blocking on the dirty prompt) cost an
   hour of debugging at the very end of 5f. A 10-line spike with a
   real MainWindow and a real close call earlier would've surfaced
   it immediately. The general lesson: when adding code that runs
   in test teardown (`closeEvent`), write the test that exercises
   teardown the same day, not at the end of the phase.

**7. Phase 6 surface.**

With Phase 5 complete the four candidate next phases differ in
load-bearing-ness:

- **Render-time playback preview** (least). Lets the user scrub
  through a virtual timeline that reflects cuts, before clicking
  Export. Useful for confirming pacing of a long edit without burning
  CPU on a render. But our smartcut output is fast (sub-second on
  the 30-s synthetic) and Cmd-E followed by playing the result is
  a 5-second feedback loop already. Convenience > load-bearing.

- **I/O loop marks.** The "play this section on repeat" workflow
  for cleanup passes — flag a 2-second region as "loop until I'm
  satisfied," then keep iterating cuts within it. Decision 7
  reserved I/O for this use; nothing's bound yet. **Load-bearing
  for the cleanup workflow** because loop-listening is *the* way
  voice-edit pacing is verified. Cheap to ship — `QMediaPlayer`
  natively supports loop ranges via `setLoops` plus a position
  watchdog.

- **Multi-clip support.** 4f-3 made Document.ranges store
  `(source_id, start, end)` so the architecture is *supposed* to
  be multi-clip-ready. The render path operates per kept-range and
  smartcut concatenates, so two sources should already work
  end-to-end. **Verifying the claim** is the work — drop a second
  source onto an open project, confirm the timeline composes them,
  confirm render concatenates correctly across sources. Load-bearing
  because it's how a podcast with a host's main mic and an intro
  jingle gets edited. Probably medium effort: the data model is
  ready, the UI for "add another source" needs design, smartcut may
  or may not handle source-mismatch gracefully.

- **`.app` bundling.** Packaging via `py2app` or `pyinstaller`. Gets
  us a real Dock icon, double-clickable launch, and the app on
  someone else's Mac without `git clone + pip install`. Load-bearing
  for distribution but not for the editing workflow itself. Stable
  once it works; the iteration cost is high (a packaging change
  takes minutes per cycle).

If I had to pick one: **I/O loop marks**. It's the smallest
meaningful piece of editing functionality the app is missing for
the workflow it's targeting (long-form podcast cleanup), the
architecture is ready, and shipping it lets the app cross the
"functional but not yet ergonomic" threshold. Multi-clip is the
bigger payoff but a multi-week commitment; loop marks is days.

---

## 10. Definition-of-done checklist (5f)

- [x] Dim-overlay verification done; result reported (§9.1, §8.7).
- [x] Native macOS menu bar with all listed items, all functional,
      all correctly disabled when there's no editor.
- [x] Cmd-Q with dirty editor prompts; all three branches behave
      correctly; save-fails-cancel works.
- [x] App icon visible on the window. (Dock-icon caveat documented
      in §8.1.)
- [x] About dialog opens and shows real version info.
- [x] Render progress is non-modal; user can edit during render;
      output reflects pre-render state.
- [x] Autosave status visible in status bar when relevant.
- [x] All prior 501 tests pass plus 28 new = 529.
- [x] `python main_qt.py` launches; full flow exercised via test
      paths (open file, transcribe, edit, save, export).
- [x] `python main.py` (tkinter) still launches.
- [x] Ruff clean for changed files.
- [x] STATE.md overwritten — Phase 5 *complete*.
- [x] Single commit: `phase 5f: macos polish, menu bar, quit guard,
      render ux`.
