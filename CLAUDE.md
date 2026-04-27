# Transcribe

A customtkinter desktop app for whisper-based transcription, evolving toward a Descript-style editor. Phase 4 added word-level timestamps, a canonical `Document` model, SRT round-tripping, an undo/redo edit-command stack, and frame-accurate cutting via smartcut. See `STATE.md` for the current snapshot.

## Production rules

Read `docs/PRODUCTION_RULES.md` at the start of every session. It codifies non-obvious decisions about cutting, rendering, caching, and persistence that are easy to violate inadvertently. Each rule has a status (PASS / GAP / FUTURE) — pay particular attention to PASS rules, because regressing one is silent.
