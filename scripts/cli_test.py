"""End-to-end Phase 1 smoke test: load tiny model, transcribe sample fixture,
write all 3 output formats next to it.

Run from the repo root with the dev venv:

    .venv/bin/python scripts/cli_test.py

Optional argument: a media path (defaults to tests/fixtures/sample.wav).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Make sibling packages importable when running this script directly.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import audio, exporters, transcriber  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 1 CLI smoke test")
    parser.add_argument(
        "media",
        nargs="?",
        default=str(REPO_ROOT / "tests" / "fixtures" / "sample.wav"),
        help="Path to a media file (default: tests/fixtures/sample.wav)",
    )
    parser.add_argument("--model", default="tiny", help="Model size (default: tiny)")
    parser.add_argument("--language", default=None, help="Force a language code (default: auto)")
    args = parser.parse_args()

    media = Path(args.media).resolve()
    if not media.is_file():
        print(f"ERROR: media file not found: {media}", file=sys.stderr)
        return 1

    duration = audio.get_duration(media)
    print(f"Media:    {media.name}  ({duration:.2f}s)")
    print(f"Model:    {args.model}  (compute_type={transcriber.DEFAULT_COMPUTE_TYPE})")
    print(f"Language: {args.language or 'auto'}")
    print()

    print("Loading model …")
    t0 = time.time()
    tx = transcriber.Transcriber(args.model)
    print(f"  loaded in {time.time() - t0:.1f}s")

    def on_segment(text: str) -> None:
        print(f"  · {text.strip()}")

    def on_progress(p: float) -> None:
        print(f"  [{int(p * 100):3d}%]", end="\r", flush=True)

    print("Transcribing …")
    t0 = time.time()
    segments, info = tx.transcribe(
        media, language=args.language, on_segment=on_segment, on_progress=on_progress
    )
    elapsed = time.time() - t0
    print()
    print(f"  detected language: {info.language}")
    print(f"  segments:          {len(segments)}")
    print(f"  elapsed:           {elapsed:.1f}s")

    print("\nWriting outputs …")
    written = exporters.write_outputs(media, segments, ["txt", "srt", "vtt"])
    for fmt, path in written.items():
        print(f"  {fmt}: {path}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
