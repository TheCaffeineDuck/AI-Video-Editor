"""Phase 4a probe: print the first N word-level timestamps from sample.wav.

Used to eyeball whether faster-whisper's native word timing is accurate
enough for editor-style use, or whether Phase 4a-bis (WhisperX) is needed.

Run from the repo root with the dev venv:

    .venv/bin/python scripts/word_probe.py

Optional args: a media path and a count. Defaults: tests/fixtures/sample.wav, 10.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import transcriber  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 4a word-timestamp probe")
    parser.add_argument(
        "media",
        nargs="?",
        default=str(REPO_ROOT / "tests" / "fixtures" / "sample.wav"),
    )
    parser.add_argument("--model", default="tiny")
    parser.add_argument("--count", type=int, default=10)
    args = parser.parse_args()

    media = Path(args.media).resolve()
    if not media.is_file():
        print(f"ERROR: media not found: {media}", file=sys.stderr)
        return 1

    tx = transcriber.Transcriber(args.model)
    segments, info = tx.transcribe(
        media,
        language=None,
        on_segment=lambda _t: None,
        on_progress=lambda _p: None,
    )

    print(f"file:     {media.name}")
    print(f"model:    {args.model}")
    print(f"language: {info.language}  (p={info.language_probability:.2f})")
    print(f"segments: {len(segments)}")
    flat = [(seg_idx, w) for seg_idx, s in enumerate(segments) for w in s.words]
    print(f"words:    {len(flat)} total\n")

    print(f"first {args.count} words:")
    print(f"  {'#':>3}  {'seg':>3}  {'start':>7}  {'end':>7}  {'dur':>6}  {'p':>4}  text")
    print(f"  {'-'*3}  {'-'*3}  {'-'*7}  {'-'*7}  {'-'*6}  {'-'*4}  {'-'*20}")
    for i, (seg_idx, w) in enumerate(flat[: args.count], start=1):
        dur = w.end - w.start
        prob = f"{w.probability:.2f}" if w.probability is not None else "  - "
        print(f"  {i:>3}  {seg_idx:>3}  {w.start:>7.3f}  {w.end:>7.3f}  {dur:>6.3f}  {prob}  {w.text!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
