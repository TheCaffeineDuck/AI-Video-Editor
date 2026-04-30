# Transcribe — AI Video Editor

A desktop app for offline whisper-based transcription, evolving toward
a Descript-style editor with an MCP surface so an LLM (Claude Desktop)
can drive the pipeline end-to-end.

The editing model is word-level: faster-whisper gives you per-word
timestamps; the editor lets a human (or Claude) cut, restore, rearrange,
and now author 9:16 highlight clips with face-locked reframing and
optional burned-in captions. Cuts are frame-accurate via smartcut;
audio joins get a 30 ms fade so segment boundaries don't click.

## Features

- **Word-accurate cutting / restoring.** Cut boundaries snap to word
  edges. Edits are stored as a keep-range timeline on a canonical
  `Document` JSON sidecar, with undo/redo and lossless schema
  migration from v1 → v3.1.
- **Frame-accurate render.** Smartcut copies aligned GOPs and
  re-encodes only at boundaries. Empty-cut renders are a byte-for-byte
  copy of the source.
- **Highlight clips.** Author 9:16 vertical clips on any source-time
  span; speaker-locked face detection (OpenCV Haar) crops around the
  speaker, optional ASS-styled captions are burned in.
- **MCP server.** 20 tools exposing the full pipeline (transcribe,
  read, cut, restore, render, propose-and-apply rearrange moves,
  propose-and-apply highlights). Drives end-to-end from Claude
  Desktop.
- **Two GUIs.** A customtkinter front-end for the legacy editor and
  a PySide6 GUI with proposal-review and highlight panels.

## Install

```bash
git clone https://github.com/TheCaffeineDuck/AI-Video-Editor.git
cd AI-Video-Editor
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

ffmpeg + ffprobe must be available — the project bundles macOS arm64
binaries under `resources/bin/`. On other platforms install via your
package manager (`brew install ffmpeg`, `apt-get install ffmpeg`,
etc.).

## Run

```bash
.venv/bin/python main_qt.py     # PySide6 editor with highlight panel
.venv/bin/python main.py        # legacy customtkinter transcriber
.venv/bin/python main_mcp.py    # MCP stdio server (point Claude Desktop at this)
```

For Claude Desktop integration see `mcp_server/README.md`.

## Smoke checklists

End-to-end manual checklists for Claude Desktop live under `docs/`:

- `docs/PHASE_6A_SMOKE.md` — base MCP / cut / render / get_timeline.
- `docs/PHASE_6B_SMOKE.md` — propose / apply rearrange moves (Path A).
- `docs/PHASE_6B3_SMOKE.md` — GUI proposal review with reasoned reject (Path B).
- `docs/PHASE_6C_SMOKE.md` — propose / render highlights, GUI inspection.

## Contributors

`STATE.md` is the canonical development log — the per-phase delta of
what shipped, what's solid, what's fragile, and what's deliberately
deferred. Read that before touching production code.

`docs/PRODUCTION_RULES.md` codifies non-obvious decisions (cutting,
rendering, caching, persistence) — change the rule first, in a
commit, before changing any code that breaks the rule.

## License

MIT. See [`LICENSE`](LICENSE).
