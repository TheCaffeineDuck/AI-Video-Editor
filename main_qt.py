"""Entry point for the PySide6 Whisper Transcriber.

Run from the repo root:

    .venv/bin/python main_qt.py

The customtkinter entry point at ``main.py`` continues to work in
parallel through Phase 5d. ``main_qt.py`` is renamed to ``main.py`` in
Phase 5e once the Qt app reaches feature parity for the editor flow.
"""

from __future__ import annotations

from ui_qt.app import run


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
