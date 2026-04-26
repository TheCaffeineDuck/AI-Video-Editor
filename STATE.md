# Whisper Transcriber — Project State Report

**Date:** 2026-04-27
**Branch:** main
**Commit:** `dd186b2` (Phase 4 complete)
**Status:** All 318 tests passing (315 fast + 3 slow). Lint clean.

---

## 1. Phase 4 in one paragraph

Phase 4 added the data layer a Descript-style editor needs: word-level
timestamps, a canonical `Document` model, SRT round-tripping, a typed
edit-command stack with undo/redo, and frame-accurate cutting via
smartcut. The customtkinter app is unchanged behaviorally except for
one new checkbox ("Editable project (.transcribe.json)") and one new
file written next to each transcription. Phase 5 will replace the GUI
with PySide6 and build the editor view on top of these primitives.

---

## 2. Project structure

```
.
├── core/
│   ├── __init__.py
│   ├── audio.py            # ffmpeg path/duration/extract
│   ├── document.py         # Word, Segment, CutMark, Document, build_document, schema v1 JSON
│   ├── editing.py          # EditCommand, AddCut, RemoveCut, MergeAdjacentCuts, CutWordRange, CommandStack
│   ├── exporters.py        # render_txt/srt/vtt + parse_srt + write_outputs (now handles "json")
│   ├── languages.py        # 99-language registry
│   ├── model_loader.py     # HF download with progress
│   ├── models.py           # model registry, cache paths
│   ├── render.py           # render_cut via smartcut (NEW in 4d)
│   ├── settings.py         # JSON-on-disk preferences
│   └── transcriber.py      # faster-whisper wrapper, returns list[Segment] with words
├── ui/
│   ├── app.py              # main controller (one-line diff in 4e)
│   ├── components/
│   │   ├── drop_zone.py
│   │   ├── language_picker.py
│   │   ├── model_picker.py
│   │   ├── output_formats.py    # +json checkbox + nudge label
│   │   ├── progress_card.py
│   │   ├── result_card.py
│   │   └── settings_panel.py
│   ├── state.py
│   └── theme.py
├── tests/
│   ├── conftest.py             # +synthetic_video, probe_duration, is_playable
│   ├── fixtures/
│   │   ├── sample.wav
│   │   ├── srt/                # 8 SRT round-trip fixtures (added 4b)
│   │   └── synthetic.mp4       # gitignored, generated on first run (4d)
│   ├── test_audio.py
│   ├── test_bootstrap.py
│   ├── test_document.py        # NEW in 4a; expanded in 4b/4e
│   ├── test_editing.py         # NEW in 4c
│   ├── test_exporters.py       # +parse_srt, +json output, +collisions
│   ├── test_language_picker.py # +json toggle
│   ├── test_model_loader.py
│   ├── test_models.py
│   ├── test_render.py          # NEW in 4d
│   ├── test_settings.py        # +backward-compat
│   ├── test_settings_panel.py
│   ├── test_state.py
│   ├── test_transcriber.py     # +word-timestamps + e2e json
│   └── test_ui.py
├── scripts/
│   ├── cli_test.py
│   └── word_probe.py           # NEW in 4a — first-10-words debug probe
├── resources/
│   ├── bin/ffmpeg-mac
│   ├── fonts/
│   └── icons/
├── main.py
├── pyproject.toml
├── requirements.txt
├── requirements-dev.txt
├── .gitattributes              # NEW in 4b — keeps SRT fixtures' line endings intact
├── .gitignore                  # +tests/fixtures/synthetic.mp4
└── whisper_transcriber_spec.md
```

---

## 3. Dependencies

### requirements.txt (final, post-4d-0 bump)

```
customtkinter==5.2.2
tkinterdnd2==0.4.2
faster-whisper==1.2.1     # bumped from 1.0.3 — 1.1+ relaxed av<13 to av>=11
huggingface-hub==0.24.0
smartcut==1.7             # NEW in 4d
```

### Resolved in venv

```
av==16.0.1                # forced by smartcut, allowed by faster-whisper>=1.1.0
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

`pip check` exits 0 with no broken requirements.

### Python / platform

- Python 3.11.15 (constrained `>=3.11,<3.12` in pyproject.toml — has
  not changed)
- Apple Silicon M4, 16 GB
- CPU-only inference (`compute_type="int8"`, `device="auto"`)

---

## 4. Code inventory

### core/ (1,670 LOC, +870 from Phase 3)

| File | Lines | What's new in Phase 4 |
|------|------:|-----------------------|
| `core/transcriber.py` | 133 | `word_timestamps=True` default; returns `list[Segment]` (our type) |
| `core/exporters.py` | 261 | `parse_srt`, `write_outputs(document=...)`, `"json"` format, `ALL_FORMATS` |
| `core/document.py` | 221 | NEW. `Word`, `Segment`, `CutMark`, `Document` (frozen), schema-versioned JSON, `build_document` |
| `core/editing.py` | 313 | NEW. `EditCommand` Protocol, 4 commands, `CommandStack` |
| `core/render.py` | 225 | NEW. `render_cut` via smartcut, helpers, `_ProgressAdapter` |
| `core/settings.py` | 105 | `DEFAULT_OUTPUT_FORMATS = ("txt", "srt", "json")` |
| `core/audio.py` | 120 | unchanged |
| `core/languages.py` | 156 | unchanged |
| `core/model_loader.py` | 54 | unchanged |
| `core/models.py` | 82 | unchanged |

### ui/ (1,525 LOC, +0 net — only output_formats.py and app.py touched)

| File | Lines | What's new in Phase 4 |
|------|------:|-----------------------|
| `ui/components/output_formats.py` | 84 | +`json` checkbox, +nudge label |
| `ui/app.py` | 414 | +`build_document` call site, passes `document=` to write_outputs |
| (everything else) | | unchanged |

### tests/ (3,474 LOC, +1,400 from Phase 3)

| File | Lines | Phase 4 deltas |
|------|------:|---------------|
| `tests/conftest.py` | 122 | +synthetic_video fixture, +probe_duration, +is_playable |
| `tests/test_document.py` | 305 | NEW (expanded across 4a/4b/4e) |
| `tests/test_editing.py` | 477 | NEW |
| `tests/test_render.py` | 384 | NEW |
| `tests/test_exporters.py` | 519 | +parse_srt round-trips, +json output, +collisions |
| `tests/test_transcriber.py` | 291 | +word-timestamp tests, +e2e json |
| `tests/test_settings.py` | 135 | +backward-compat tests |
| `tests/test_language_picker.py` | 232 | +json toggle test |

### Test count

| Phase | Total | Fast | Slow |
|-------|------:|-----:|-----:|
| End of Phase 3 | 169 | 168 | 1 |
| End of Phase 4a | 181 | 179 | 2 |
| End of Phase 4b | 221 | 219 | 2 |
| End of Phase 4c | 266 | 264 | 2 |
| End of Phase 4d-0 | 266 | 264 | 2 |
| End of Phase 4d-1 | 302 | 294 | 8 |
| **End of Phase 4e** | **318** | **315** | **3** *(see note)* |

Note: 4e replaces some slow tests with fast equivalents and adds one
new slow e2e test. The "3 slow" figure is what the default `pytest -m
"not slow"` excludes — `pytest -q` (no marker filter) runs all 318
green in ~12 s on this M4 with the model already cached.

---

## 5. Git history

```
dd186b2 Phase 4e: write Document JSON next to .srt/.txt; checkbox to toggle
56ebca7 Phase 4d-1: render_cut via smartcut + synthetic video fixture
8be3ab6 Phase 4d-0: bump faster-whisper for av 16 compatibility
c2e4681 Phase 4c: edit commands + undo/redo CommandStack
22f8c3e Phase 4b: Document/CutMark model, JSON I/O with schema versioning, parse_srt
af64d38 Phase 4a: word-level timestamps + core.document boundary types
5fb19e3 Phase 3: settings, language picker, output formats, download progress
b1002f5 Phase 2: UI shell, state machine, queue pump, components, tests
99a7490 Phase 1: core engine (exporters, models, audio, transcriber) + tests
71d3cb7 Phase 0: bootstrap repo, deps, ffmpeg, fixture, smoke tests
```

Each Phase-4 sub-phase landed as its own commit per the spec.

---

## 6. Public APIs added in Phase 4

```python
# core.document
@dataclass(frozen=True)
class Word:
    text: str; start: float; end: float
    probability: float | None = None

@dataclass(frozen=True)
class Segment:
    text: str; start: float; end: float
    words: tuple[Word, ...] = ()

@dataclass(frozen=True)
class CutMark:
    start: float; end: float; reason: str = ""

@dataclass(frozen=True)
class Document:
    media_path: Path; duration: float; language: str | None
    segments: list[Segment]
    cuts: list[CutMark] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    model_name: str = ""
    SCHEMA_VERSION: ClassVar[int] = 1
    def to_json(self) -> dict[str, Any]: ...
    @classmethod
    def from_json(cls, data: dict) -> Document: ...

class UnsupportedSchemaError(ValueError): ...

def build_document(*, media_path, duration, language, segments, model_name) -> Document: ...

# core.exporters
def parse_srt(text: str) -> list[Segment]: ...
def write_outputs(source_path, segments, formats, *, document=None) -> dict[str, Path]: ...

# core.editing
class EditCommand(Protocol):
    description: str
    def apply(self, doc: Document) -> Document: ...
    def revert(self, doc: Document) -> Document: ...

class AddCut: start: float; end: float; reason: str = "manual"
class RemoveCut: index: int
class MergeAdjacentCuts: threshold_seconds: float
class CutWordRange: seg_idx: int; word_start_idx: int; word_end_idx: int; reason: str = "manual"

class CommandStack:
    def __init__(self, max_depth: int = 100): ...
    can_undo: bool; can_redo: bool; undo_depth: int; redo_depth: int
    def push(self, command, before, after) -> None: ...   # clears redo on fork
    def undo(self) -> Document | None: ...
    def redo(self) -> Document | None: ...
    def clear(self) -> None: ...

# core.render
def render_cut(
    doc: Document,
    output_path: Path,
    on_progress: Callable[[float], None] | None = None,
    *,
    pad: float = 0.10,
    merge_gap: float = 0.30,
) -> Path: ...
```

---

## 7. Document JSON format (schema v1)

Saved as `<source_stem>.transcribe.json` next to the source media.

```json
{
  "schema_version": 1,
  "media_path": "/path/to/sample.wav",
  "duration": 6.10,
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
  "cuts": []
}
```

`schema_version` is mandatory. `Document.from_json` raises
`UnsupportedSchemaError` on missing/null/unknown — no silent coercion.
`created_at` is always UTC ISO format.

---

## 8. What's solid

1. **Boundary types own the contract.** faster-whisper's objects no
   longer leak past `core/transcriber.py`. The rest of the system
   speaks `Segment` / `Word` / `Document` exclusively, which made
   adding the JSON, the editor commands, and the renderer all
   essentially mechanical.

2. **Frozen Document + replace-only commands.** Tests prove (a)
   reassigning `doc.cuts` raises, (b) `apply` always produces a new
   list object, (c) every command's `apply → revert` round-trips to an
   equal Document. The undo stack is correct on the classic fork case.

3. **SRT parser is genuinely lenient.** Eight ugly fixtures (BOM,
   CRLF, period decimals, missing index, extra blanks, no trailing
   newline, plus a hand-crafted "messy.srt" combining several) all
   normalize to byte-identical canonical output via parse → render.
   `.gitattributes` keeps the bytes from being mangled on checkout.

4. **render_cut works end-to-end.** Synthetic 30-second video fixture
   (testsrc + stepped 440/880/1320 Hz tones) gets cut by smartcut and
   the output is decode-clean and the right duration. Empty cuts copies
   bytes; full-duration cuts raise; padding clamps at file boundaries
   and post-pad overlap merges. Progress is monotonic and ends at 1.0.

5. **Backward-compat for settings is automatic.** Pre-Phase-4e users'
   `settings.json` saying `["txt", "srt"]` is preserved verbatim; only
   fresh installs (no settings.json) get JSON on by default. Tests
   verify both paths.

---

## 9. What's fragile or worth knowing

1. **`av==16.0.1` resolves cleanly but is *recent*.** smartcut forces
   it; `faster-whisper>=1.1.0` permits it. `pip check` is happy. We
   smoke-tested wav/mp3/m4a/mp4 decode at 4d-0-bis. But this is a
   minor library on a minor backend — if a future smartcut release
   pins av differently, or faster-whisper tightens its bound again,
   we'll have to revisit. Document JSON output is stable regardless.

2. **smartcut's progress API is quirky.** First `emit(N)` is the
   total, subsequent emits are non-uniform increments that can
   *exceed* the announced total. We bridge this with `_ProgressAdapter`
   (clamp to [0, 1], monotonic, finalize() forces 1.0 on success).
   Watch for this if smartcut ever adds e.g. `emit(value, total)`
   variants — our adapter would need updating.

3. **Synthetic video fixture is per-developer.** `tests/fixtures/synthetic.mp4`
   is gitignored and generated on first run via `ffmpeg-mac`. ~1 s.
   If `ffmpeg-mac` isn't on disk (e.g. someone deleted it from
   `resources/bin/`), the slow tests `pytest.skip` rather than fail.

4. **`MergeAdjacentCuts` and `RemoveCut` are stateful commands.**
   Their `revert` reads a captured value set during `apply`. Calling
   `revert` before `apply` raises `RuntimeError` (tested). Stack-driven
   undo/redo never hits that path because the stack always pairs
   apply with revert through the same instance.

5. **Pad direction was non-obvious.** `pad=0.10` *expands* keep-ranges
   (eats 0.1 s into adjacent cuts), it doesn't shrink them. So a
   `[12, 18]` cut with the default pad actually removes 5.8 s, not
   6 s. This is documented in `core/render.py` and
   `tests/test_render.py`; future-us should re-read those before
   tweaking defaults.

6. **No editor UI yet.** `Document` is the canonical artifact, but
   nothing reads it back into the running app. That's Phase 5. Until
   then, the JSON file is write-only from the app's perspective —
   it's there for power users who want to inspect or pre-process
   transcripts before the editor lands.

7. **Threading model unchanged from Phase 3.** Single worker thread,
   single transcriber, batch transcription is not supported. None of
   Phase 4 changed this — the editor work in Phase 5 may or may not
   warrant revisiting.

---

## 10. Definition-of-done checklist (from Phase 4 spec)

- [x] All 169 original tests still pass
- [x] New tests cover: word timestamps, parse_srt round-trip, all edit
  commands, command stack undo/redo, render_cut on a synthetic video
- [x] Transcribing `tests/fixtures/sample.wav` produces .srt/.txt/.vtt
  identical to before, plus a new `.transcribe.json` containing
  word-level data (verified by the slow e2e test)
- [x] `git log --oneline -7` shows distinct commits per sub-phase
  (4a, 4b, 4c, 4d-0, 4d-1, 4e)
- [x] STATE.md updated (this file, overwriting in place)

---

## 11. What Phase 5 inherits

- A canonical `Document` model with word-level timing.
- Parse + render symmetry on SRT.
- Full undo/redo command stack ready to drive an editor view.
- `render_cut(doc, ...)` ready to wire to a "Render cuts" button.
- `Document.from_json` ready to wire to an "Open project" path.
- A clean `core/` <-> `ui/` boundary — none of the Phase 4 additions
  reach into `ui/state.py` or any widget. Phase 5's PySide6 work can
  start fresh on the GUI side without unwinding anything.
