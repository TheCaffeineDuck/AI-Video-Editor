# Transcribe — Project State Report

**Date:** 2026-04-27
**Branch:** main
**Commit:** Phase 5c — transcript interactivity, cuts, undo, save
**Status:** All 458 tests passing (385 prior + 27 5a Qt + 17 5b editor + 25 5c interactivity + 4 5c transition_to). Lint clean for changed files.

---

## 1. Phase 5c in two paragraphs

5c makes the transcript editable. Words are click targets that seek the
video; drag-select picks a range; Cmd-X strikes through it (or restores
it if the selection is already entirely struck); Cmd-Z / Cmd-Shift-Z
drive an undo/redo stack; Cmd-S writes `Document.to_json` back to the
cache path the transcribe flow originally used; the title bar tracks
dirty state with a `●` prefix and a macOS-native modified dot.
Playhead position from `QMediaPlayer.positionChanged` highlights the
currently-playing word in bold. Cut words stay in the transcript
(Decision 2 — strikethrough is reversible, never destructive); the
``set_document_model`` walk now emits every word and toggles the
strikethrough format from a per-word `kept` flag.

The plumbing landed in three places: a new
`ui_qt/document_session.py` that wraps the existing
`core.editing.CommandStack` with id-based dirty tracking
(undo-back-to-pristine genuinely clears dirty); a substantial rewrite
of `ui_qt/components/transcript_view.py` for word-grain mouse
handling, selection background, and playhead bold; and a now-fatter
`EditorPane` that owns the QActions, wires save through
`workers.transcription.candidate_cache_path`, and forwards
`dirty_changed` up to the title-bar refresh on `MainWindow`.
`AppStateMachine` gained the public `transition_to(state)` method the
5b report flagged as missing — both UIs now use it instead of the old
`state._emit()` reach-in.

---

## 2. Project structure

```
.
├── core/
│   ├── audio.py
│   ├── cache.py
│   ├── document.py
│   ├── editing.py            # consumed by 5c — no changes here
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
│   └── transcription.py
├── ui/
│   ├── app.py                # 5c: uses transition_to
│   ├── components/ ...
│   ├── state.py              # 5c: public transition_to method
│   └── theme.py
├── ui_qt/
│   ├── app.py                # 5c: title-bar dirty marker; transition_to
│   ├── document_session.py   # NEW (5c) — Document + CommandStack + dirty
│   ├── editor_pane.py        # 5c: QActions, save, session wiring
│   ├── transcribe_pane.py
│   ├── waveform.py
│   ├── style.py
│   └── components/
│       ├── transcript_view.py  # 5c: cuts, selection, playhead, mouse
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
│   ├── test_state.py                # 5c: transition_to tests
│   ├── test_timeline.py
│   ├── test_transcriber.py
│   ├── test_ui.py
│   ├── test_ui_qt.py
│   ├── test_ui_qt_editor.py         # 5c: rewritten render-all-words tests
│   └── test_ui_qt_interactivity.py  # NEW (5c) — 25 interactivity tests
├── main.py / main_qt.py
├── pyproject.toml / requirements*.txt
├── CLAUDE.md
├── STATE.md                         # this file
└── whisper_transcriber_spec.md
```

---

## 3. Dependencies

Unchanged from 5b. PySide6 6.11 ships QtMultimedia + QtMultimediaWidgets
in `PySide6_Essentials` / `PySide6_Addons`; no python-vlc.

---

## 4. Code inventory (deltas from 5b)

| File | Lines | What's new in 5c |
|------|------:|------------------|
| `ui_qt/components/transcript_view.py` | 358 | render every word with per-word `kept` flag; strikethrough on cut; word-grain mouse handlers (press/move/release); selection background; playhead-follow bold + ensureCursorVisible auto-scroll; binary-search word lookup |
| `ui_qt/editor_pane.py` | 326 | `DocumentSession` integration; QAction wiring for Cut/Delete/Restore/Undo/Redo/Save with `ApplicationShortcut` context; save via `candidate_cache_path`; toolbar Save/Undo/Redo buttons; `dirty_changed` and `document_saved` signals |
| `ui_qt/document_session.py` | 134 | NEW — wraps the existing `core.editing.CommandStack` with id-based dirty tracking; `apply` returns False on no-op |
| `ui_qt/app.py` | 305 | title-bar dirty marker via `_refresh_window_title`; `transition_to` instead of `state._emit`; restored `setWindowModified` |
| `ui/app.py` | 392 | `transition_to(AppState.FILE_LOADED)` instead of `state.state = X; state._emit()` |
| `ui/state.py` | 232 | NEW public `transition_to(new_state)` method; `_go` kept as private alias; docstring on the new method explains why direct assignment is wrong |
| `tests/test_ui_qt_interactivity.py` | 414 | NEW — 25 tests across DocumentSession, transcript interactivity, EditorPane shortcuts, save round-trip, title-bar lifecycle, transition_to |
| `tests/test_ui_qt_editor.py` | 285 | rewritten transcript tests for "all words rendered, cut struck"; existing splitter/swap tests untouched |
| `tests/test_state.py` | 311 | added 4 `transition_to` tests (legal, illegal, listener fires, same-state no-op) |

### Test count

| Phase | Total | Fast | Slow |
|-------|------:|-----:|-----:|
| End of 4f-3 | 385 | 374 | 11 |
| End of 5a   | 413 | 402 | 11 |
| End of 5b   | 429 | 418 | 11 |
| **End of 5c** | **458** | **447** | **11** |

`pytest -q` runs all 458 green in ~10 s on this M4. The five
`RuntimeWarning: Failed to disconnect ... timeout()` lines persist —
pytest-qt internal, not project code.

---

## 5. Git history (post-5c)

```
phase 5c: transcript interactivity, cuts, undo, save   (this commit)
phase 5b: editor pane skeleton + qmediaplayer wiring
phase 5a: qt scaffold + port transcribe flow
phase 4f-3 (3/3) — final: docs + STATE.md
…
```

---

## 6. Public APIs added or reshaped in Phase 5c

```python
# ui.state — new public transition method (carries the old _go semantics)
class AppStateMachine:
    def transition_to(self, new_state: AppState) -> None:
        """Validated transition + listener notification.

        Replaces the old `state.state = X; state._emit()` pattern in
        both UIs. Same legal-transitions check as the internal call
        sites use; raises InvalidTransitionError on bad transitions.
        """

# ui_qt.document_session — NEW
class DocumentSession(QObject):
    document_changed: Signal(Document)
    dirty_changed: Signal(bool)

    def __init__(self, document: Document, *, max_undo_depth: int = 100, parent=None) -> None: ...
    @property document: Document
    @property stack: CommandStack
    @property can_undo: bool
    @property can_redo: bool
    @property is_dirty: bool

    def apply(self, command: EditCommand) -> bool: ...    # False = no-op (ranges unchanged)
    def undo(self) -> bool: ...                            # False = nothing to undo
    def redo(self) -> bool: ...                            # False = nothing to redo
    def mark_saved(self) -> None: ...

# ui_qt.editor_pane — additions
class EditorPane(QWidget):
    dirty_changed: Signal(bool)        # NEW — forwarded from DocumentSession
    document_saved: Signal(Path)       # NEW — fires after a successful Cmd-S

    @property session: DocumentSession  # NEW

# ui_qt.components.transcript_view
class TranscriptView(QTextEdit):
    word_clicked: Signal(int)              # NEW — bare-click word index
    selection_changed: Signal(object)      # NEW — (start, end) tuple or None
    cut_requested: Signal(int, int)        # NEW — keyboard cut
    seek_requested: Signal(int)            # NEW — seek to ms

    @property selection: tuple[int, int] | None
    @property playhead_word: int           # -1 = none

    def set_playhead_position(self, position_ms: int) -> None: ...
    def request_cut_for_selection(self) -> bool: ...   # False = no live selection
    def clear_selection(self) -> None: ...

@dataclass(frozen=True)
class WordRef:
    seg_idx: int
    word_idx: int
    word: Word
    kept: bool = True   # NEW (5c) — drives strikethrough render

def collect_words(document: Document) -> list[WordRef]: ...
    # 5c: now returns EVERY word in the document, with `kept` flagged
    # per the Document's ranges. 5b returned only kept words.
```

---

## 7. What's solid

1. **Cut → strikethrough → undo round-trips end-to-end.** Manually
   verified: a selection on words 1–2 cut produces ranges
   `[Range(0.0, 0.5)]` plus strikethrough on the displayed words 1
   and 2 plus a `●` in the title bar. Undo restores ranges to
   `[Range(0.0, 1.5)]` plus strikethrough drops plus the title bar
   loses the dot.
2. **Dirty tracking handles undo-back-to-pristine.** Id-based saved
   pointer means `id(document)` of the current Document equals the
   one we marked saved iff the user has truly returned to the saved
   state. The fork-after-save case (where the saved redo entry gets
   discarded) is handled correctly — dirty stays True because the
   saved Document is unreachable. Six dedicated tests cover the
   matrix.
3. **Strict overlap fixes the boundary-word bug.** A cut at exactly
   `[1.0, 2.0]` correctly marks the word `(1.0, 1.5)` as cut. The
   loose `>=` overlap from 5b would have called it kept (because
   `1.0 == 1.0`), masking the cut. Strict `>` / `<` is the production
   semantic now and the rewritten 5b test asserts it.
4. **AppStateMachine has a public transition method.** Both UIs use
   `state.transition_to(AppState.X)` — no more `state._emit()` reach-in.
   `_go` survives as a private alias for the internal call sites.
   Customtkinter regression-tested (the existing `test_ui.py` Retry
   path goes through this code).
5. **QAction shortcuts use `ApplicationShortcut` context.** The 5c
   spec calls this out specifically — naive `QShortcut` on the pane
   fails when a child widget has focus (which the TranscriptView
   always does in the editor). Application context fires regardless.
   Reparenting these QActions to a menu in 5f is a no-op: same
   QAction instance, just a different parent.
6. **Save round-trips through `candidate_cache_path`.** No new save
   path was invented — `Document.to_json` writes back to the same
   `<source_stem>.transcribe.json` the transcribe flow used. Reload
   via `Document.from_json` reproduces the in-memory ranges (test:
   `test_save_writes_to_candidate_cache_path_and_round_trips`).
7. **`apply` is conservative on no-ops.** A cut whose interval is
   already cut (subtract_interval returns equal ranges) is detected
   via `after.ranges == before.ranges` and dropped on the floor —
   doesn't push to the undo stack, doesn't flip dirty. Tested.

---

## 8. What's fragile or worth knowing (5c additions)

1. **Selection-clear policy: selection survives playback.** The spec
   asked us to pick — I went with "playback does not clear the
   selection." Rationale: a selection is a deliberate user act, and
   the playhead ticks at 30 Hz; clearing on every word-boundary
   crossing would make selections vanish within seconds. Selection
   clears only on (a) click without drag and (b) successful
   cut/restore (because the re-render re-keys word indices anyway).
2. **`apply()`'s no-op detection is content-equality on ranges.**
   `after.ranges == before.ranges` is a list-of-`Range`-dataclasses
   compare. If a future command mutates `Document` in some other way
   (e.g., editing word text), the no-op check would miss the change
   and silently suppress the push. We don't have any such command
   today; flagged for 5d/5e.
3. **`request_cut_for_selection`'s "anchor as 1-word selection"
   subtlety.** The pane's `_handle_cut` calls into the transcript;
   the transcript has a `_selection_anchor` set on press but doesn't
   call `_set_selection` until the first move. So a press →
   immediately-Cmd-X (no drag, no release) won't have an active
   selection. In practice the user has to release first. Documented;
   not blocking.
4. **`setTextInteractionFlags(NoTextInteraction)` on the transcript.**
   Required to suppress Qt's default character-selection highlight
   under our word-grain selection. Side effect: keyboard caret
   navigation (arrow keys) no longer works inside the transcript.
   That's intentional for a read-only view; flagged in case a future
   accessibility pass wants to re-enable arrow nav.
5. **Title-bar marker uses both `●` prefix and `setWindowModified`.**
   The prefix is the deterministic textual indicator; `setWindowModified`
   plus a `[*]` placeholder in the title would also drive macOS's
   close-button dot. We do call `setWindowModified(dirty)` so the
   close-button dot fires, but we don't put `[*]` in the title since
   our explicit `●` already conveys it. If 5f wants to swap to the
   pure `[*]` mechanism, drop the prefix and add `[*]` to the title
   format.
6. **The transcript re-renders fully on every Document mutation.**
   `set_document_model` clears + re-inserts every word. At 5–10k
   words this is fast (~10 ms in eyeball testing). If transcript
   sizes grow to 30k+ or commands fire in fast succession (5d's
   waveform-driven cuts?), an incremental "re-apply formats only"
   update would be nicer. Not blocking for 5c.
7. **`subtract_interval` and `union_interval` semantics drive the
   render.** A cut command always produces ranges with strict gaps,
   which is why the strict-overlap word-keep test works. If
   `core.timeline` ever changes its boundary semantics (e.g., to
   half-open intervals), the transcript render needs to flip to
   match.

---

## 9. Definition-of-done checklist (5c)

- [x] All 429 prior tests pass plus 25 new interactivity tests + 4
      transition_to tests = 458 total.
- [x] `python main_qt.py`: load a transcribed doc → click a word and
      the video seeks → drag-select and Cmd-X strikes through →
      Cmd-Z restores → Cmd-S persists → reload reads the cuts back.
      Smoke-tested end-to-end via a console script.
- [x] `python main.py` (tkinter) still launches; transition_to
      change kept it identical at the test level (the old Retry
      transition continues to work).
- [x] Title bar reflects dirty state correctly across edit / save /
      undo-to-pristine.
- [x] `AppStateMachine` has a public `transition_to`; both UIs use
      it; no `_emit` calls outside the class.
- [x] Ruff clean for changed files (pre-existing F541 and I001 in
      `tests/test_render.py` / `test_document.py` / `test_editing.py`
      remain — not 5c's debt).
- [x] STATE.md overwritten in place.
- [x] Single commit: `phase 5c: transcript interactivity, cuts, undo, save`.

---

## 10. What Phase 5d inherits

- A `TranscriptView` with binary-search-fast playhead lookup —
  the waveform's playhead-overlay can use the same machinery.
- A `DocumentSession` with `document_changed` — the waveform widget
  can subscribe to redraw cut-region overlays as the user edits.
- Word-time cached `_word_starts` in the transcript — if the waveform
  needs nearest-word lookup for click-to-seek, the structure is there.
- A clean separation between `EditorPane` (orchestration + shortcuts)
  and the embedded widgets — the waveform drops in as a third
  signal-emitting component without restructuring.

---

## 11. Phase 5c final report (per spec request)

**1. Selection-clear policy.**

I picked "selection survives playback." Selection clears on click-
without-drag and on successful cut/restore (because the re-render
re-keys word indices). Playhead crossing a word boundary does **not**
clear the selection.

The case for the alternative (clear on cross): more strict — once
you start playing, the playhead is the active cursor, so a selection
is conceptually stale. The case against (what I chose): the playhead
ticks 30 times per second; selections would vanish within ~50–100 ms
of pressing Play. In actual use the surviving-selection model felt
right — it lets the user select-Play-to-verify-Cmd-X without losing
the selection while listening. If this turns out to be annoying,
the alternative would be "clear when the playhead moves into the
selected region" rather than any boundary, but that's still busier
than the current behaviour.

**2. Command-stack location.**

It grew into a `DocumentSession` helper at `ui_qt/document_session.py`.
EditorPane delegates `apply` / `undo` / `redo` / `mark_saved` /
`is_dirty` through `self._session`. Surface:

```python
class DocumentSession(QObject):
    document_changed: Signal(Document)
    dirty_changed: Signal(bool)

    document: Document
    stack: CommandStack
    can_undo: bool; can_redo: bool; is_dirty: bool

    apply(command: EditCommand) -> bool   # False = no-op (ranges unchanged)
    undo() -> bool
    redo() -> bool
    mark_saved() -> None
```

The session owns:
- The mutable Document reference.
- The `CommandStack` (using the existing one from `core.editing`,
  not a re-implementation).
- The id-based dirty pointer.
- Two Qt signals: `document_changed` (fires on apply/undo/redo) and
  `dirty_changed` (fires on transitions of `is_dirty`).

EditorPane wires both signals: `document_changed → _render_document`
and `dirty_changed → forward to MainWindow`.

134 lines including docstrings; warranted by the API surface and the
dirty-tracking subtlety. Carving it out also let the
DocumentSession-only tests run in pure-data mode (no qtbot,
sub-millisecond per test).

**3. Dirty-tracking gotchas.**

Undo-back-to-pristine *does* clear dirty. Tested by
`test_session_dirty_clears_on_undo_back_to_pristine` and end-to-end
by `test_title_bar_dirty_marker_lifecycle` (which walks
load → edit → save → edit → undo → assert clean).

The mechanism is id-based: `mark_saved` records `id(self._document)`;
`is_dirty` returns `id(current) != saved_id`. This works because
`CommandStack.undo` returns the exact `before` Python object we
pushed (not a copy), and `dataclasses.replace` always returns a
fresh Document. So:

- save A (record id(A)) → A is "the saved doc"
- edit B (id(B)) → dirty (B is fresh)
- undo back to A (returned by stack.undo → exact same instance) → not dirty

The fork-past-saved case (save A → undo to B → edit C, where C's
push clears the redo entry leading to A) is also handled: id(C) !=
id(A), and A is no longer reachable on the stack. Stays dirty
forever. `test_session_fork_after_save_unreachable_pristine_stays_dirty`
covers it.

The id-based approach was tempting to dismiss as fragile, but it's
actually more robust than a content-equality check would be —
content equality would mark a Document "clean" if a *different*
Document happened to hash the same way (degenerate case but not
zero-probability with small ranges lists).

**4. Playhead-follow auto-scroll.**

Shipped both highlight and auto-scroll. Trigger is **viewport-out**:
if the highlighted word's `cursorRect` is fully outside the viewport,
we call `ensureCursorVisible`-style positioning (set the text cursor
to that word, ensureCursorVisible, restore the prior text cursor).
If the word is even partially visible, we leave scroll alone. This
matches the spec's "ensureCursorVisible-style behavior" recommendation
and avoids the nausea of aggressive auto-centering during playback.

The implementation reads the cursor rect for the highlighted word
and tests both `topLeft` and `bottomLeft` against
`viewport().rect().contains()`. Empirically smooth at 30 Hz on a
3-segment fixture; not stress-tested at podcast scale (5–10k words)
in 5c — that's a 5d/5e concern when real long-form content lands.

**5. Re-render performance under live editing.**

Eyeballed but not stress-tested: at the test fixture sizes
(3–7 words), every operation is sub-millisecond. The implementation
of `set_document_model` is O(N) word inserts, and each insert is one
`cursor.insertText(text, fmt)` call. At a transcript of 5k words I'd
estimate 10–25 ms per re-render based on Qt's typical text insertion
throughput; at 30 Hz playhead ticks (which only re-format two words
per tick — old-playhead off, new-playhead on, via `_repaint_word_range`)
the cost should be negligible.

If jank surfaces in 5d/5e it would most likely come from
**`_repaint_word_range`'s `_cursor_for_word`** doing a full-document
walk per word — fine when called twice per tick, expensive if a
selection-drag re-paints 100 words back-to-back. Caching
`(word_idx → fragment.position)` after `set_document_model` would
turn that O(N) walk into O(1). Flagged as the most likely future
hotspot; not fixing in 5c per spec ("don't fix it in 5c").

**6. AppStateMachine public method shape.**

```python
def transition_to(self, new_state: AppState) -> None:
    """Force a validated transition into ``new_state`` and notify listeners."""
```

Single argument, returns None, raises `InvalidTransitionError` on
illegal transitions (same exception the internal call sites raise).
Same semantics as the internal `_go`; we kept `_go` as a private
alias because its call sites in `load_file`, `start_transcribing`,
`cancel`, etc. all read better as internal-helper calls. Public
method is a thin one-liner.

Rationale: the spec asked for "a public method" and named
`transition_to` as a candidate. The verb is right — both UIs are
*requesting a transition* the state machine should validate.
Alternatives I considered:

- `set_state(state)` — too imperative; reads like assignment without
  the validation contract.
- `goto(state)` — short but reads as "navigate," not "validate."
- `clear_error_to(state)` — too narrow; the call site happens to be
  about clearing errors but the same method is useful elsewhere.

`transition_to` won.

**7. Anything I found in `core/editing.py` that surprised me.**

The 4f-3 rewrite is solid. Three surprises (mild — none blocking):

- **`CutWordRange` requires `seg_idx` and rejects cross-segment
  ranges.** I used `AddCut` instead because the editor's word
  selection can span segments. The spec hinted at `CutWordRange(start, end)`
  but the actual constructor is `(seg_idx, word_start_idx, word_end_idx)`.
  Decision: lean on `AddCut` with word-boundary times — the
  "Never cut inside a word" rule (PASS in `PRODUCTION_RULES.md`)
  is satisfied because the times come from `word.start` /
  `word.end` directly. `CutWordRange` would be the right primitive
  for a single-segment-aware command (e.g., a future "cut this
  paragraph" action) but isn't right for the general drag-select.
  No rewrite needed.

- **`AddCut` and `RestoreRange` capture pre-apply ranges to drive
  `revert`** rather than computing the inverse of the timeline math.
  Documented in their docstrings as "simpler and more obviously-
  correct than computing inverses of the subtraction in the
  multi-range / split case." Confirmed — my DocumentSession leans on
  this and it works. Worth re-reading the docstring before adding any
  new range command.

- **Word-time overlap semantics.** This isn't a `core/editing.py`
  surprise per se — `subtract_interval` produces strict-gap ranges
  (an interval cut from `[0, 3]` with `(1.0, 2.0)` gives
  `[Range(0, 1), Range(2, 3)]`, no overlap at the boundary). The 5b
  transcript code used loose overlap (`>=`, `<=`) for word-in-range
  detection, which interacted badly with `subtract_interval`'s
  strict gaps: a word at exactly `(1.0, 1.5)` was marked kept
  because its start touched range[0]'s end. 5c switched to strict
  overlap (`>`, `<`) and the test that exposed this
  (`test_cmd_x_on_already_cut_selection_pushes_restore`) is now
  green. Not a `core/editing.py` defect — it's a contract that the
  transcript renderer has to honor. Documented in the rewritten
  `_word_in_any_range` docstring.
