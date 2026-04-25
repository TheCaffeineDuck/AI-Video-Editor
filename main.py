"""Entry point for Whisper Transcriber.

Run from the repo root:

    .venv/bin/python main.py
"""

from __future__ import annotations

from ui.app import App


def main() -> int:
    App().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
