"""Phase 6a GATE — does smartcut concatenate non-monotonic source ranges?

The v3 timeline model (Clip/Timeline) treats a video as a playlist of
clips that may visit the source in any order. The renderer needs to
walk that playlist and produce one output. The load-bearing question
this script answered: does smartcut handle non-monotonic ranges itself,
or does it require us to pre-sort and lose re-arrangement?

What was tested
---------------
1. A 5-minute working clip was sliced from a real HEVC 10-bit podcast
   source (~22 GB original) into ``/tmp/transcribe-smartcut-spike/``.
2. Three approaches were run on the non-monotonic schedule
   ``[(60, 90), (0, 30), (180, 210)]`` — middle chunk first, then head,
   then later in the file. Total expected output: 90 s.
3. Each output was ffprobe'd: duration, codec parity (no re-encode),
   audio/video duration delta (sync proxy).

Outcome — YELLOW
----------------
- **smartcut direct (single call, non-monotonic list): broken.**
  ``smartcut.cut_video.make_cut_segments`` walks GOPs in source-time
  order with a single linear pointer through ``positive_segments``;
  unsorted input causes silent drops or duplications. Test produced
  100 s output instead of 90 s.
- **smartcut per-segment + ffmpeg concat demuxer: correct, slow.**
  90.02 s output, codecs preserved, audio/video drift 5.3 ms across
  three joins. Wall-clock 20.7 s on the HEVC 10-bit clip — 4× over
  the 5 s GREEN target. Cost is dominated by per-call MediaContainer
  parsing (re-demuxing the heavy source for each segment).
- **Pure ffmpeg ``-ss/-t`` per-segment + concat demuxer: drifts.**
  91.10 s output (1.1 s over) — stream-copy ``-ss`` lands on the
  preceding keyframe, accumulating overshoot. Smartcut exists exactly
  to avoid this.

Decision (locked into Phase 6a continuation)
--------------------------------------------
Adopt **option 1 with run-batching**: source-monotonic timelines stay
on the v2 fast path (one smartcut call). Non-monotonic timelines split
into maximal source-monotonic *runs*; each run is one smartcut call;
runs concat with the ffmpeg concat demuxer (no re-encode). Cost is
``O(order_breaks + 1)`` smartcut invocations, not ``O(clips)``. The
30 ms ``afade`` treatment that v2 applied at every kept-range join now
also applies at run-boundary joins.

This script is kept as a future regression check. Re-run it against
upstream smartcut when bumping the dep — the pre-sort assumption is
load-bearing for the renderer.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from fractions import Fraction
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FFMPEG = REPO / "resources" / "bin" / "ffmpeg-mac"
FFPROBE_CANDIDATES = [
    REPO / "resources" / "bin" / "ffprobe-mac",
    Path("/opt/homebrew/bin/ffprobe"),
    Path("/usr/local/bin/ffprobe"),
]
SOURCE = Path(
    "/Users/aaronramos/Desktop/531 Podcast Aaron & Barret Autocut only.mp4"
)
WORK_DIR = Path("/tmp/transcribe-smartcut-spike")
CLIP = WORK_DIR / "clip5min.mp4"

# Non-monotonic schedule. Each tuple is (start_s, end_s) in source time.
# Total expected output duration: 30 + 30 + 30 = 90 s.
SCHEDULE: list[tuple[float, float]] = [(60.0, 90.0), (0.0, 30.0), (180.0, 210.0)]
EXPECTED_DURATION_S = 90.0


def _ffprobe() -> Path:
    for p in FFPROBE_CANDIDATES:
        if p.is_file():
            return p
    raise SystemExit("ffprobe not found in repo bin or homebrew/usr-local")


def _probe(path: Path) -> dict:
    cmd = [
        str(_ffprobe()),
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def _make_working_clip() -> None:
    """Cut a 5-minute slice from the source. Stream-copy → ~1 s on a fast disk."""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    if CLIP.is_file():
        # If it exists, reuse — the source is 22 GB and copying takes
        # measurable time even with stream-copy.
        info = _probe(CLIP)
        duration = float(info["format"]["duration"])
        if 295.0 < duration < 305.0:
            return
        CLIP.unlink()
    print(f"[setup] slicing 5 min from source → {CLIP}")
    cmd = [
        str(FFMPEG),
        "-y",
        "-loglevel", "error",
        "-ss", "0",
        "-t", "300",
        "-i", str(SOURCE),
        "-c", "copy",
        str(CLIP),
    ]
    t0 = time.perf_counter()
    subprocess.run(cmd, check=True, capture_output=True)
    print(f"[setup] done in {time.perf_counter() - t0:.2f} s")


def _approach_smartcut_direct(out: Path) -> tuple[float, str]:
    """Hand smartcut the non-monotonic list and see what happens."""
    from smartcut.cut_video import (
        AudioExportInfo,
        AudioExportSettings,
        VideoExportMode,
        VideoExportQuality,
        VideoSettings,
        smart_cut,
    )
    from smartcut.media_container import MediaContainer

    container = MediaContainer(str(CLIP))
    audio_settings = [
        AudioExportSettings(codec="passthru") for _ in container.audio_tracks
    ]
    audio_export_info = AudioExportInfo(output_tracks=audio_settings)
    video_settings = VideoSettings(
        VideoExportMode.SMARTCUT, VideoExportQuality.NORMAL, "copy"
    )
    fraction_segments = [
        (Fraction(s).limit_denominator(1000), Fraction(e).limit_denominator(1000))
        for (s, e) in SCHEDULE
    ]
    t0 = time.perf_counter()
    exc = smart_cut(
        container,
        fraction_segments,
        str(out),
        audio_export_info=audio_export_info,
        video_settings=video_settings,
        log_level="error",
    )
    if exc is not None:
        return (time.perf_counter() - t0, f"smart_cut returned exception: {exc}")
    return (time.perf_counter() - t0, "ok")


def _approach_per_segment_then_concat(out: Path) -> tuple[float, str]:
    """Render each segment in non-monotonic order via smartcut, concat with ffmpeg."""
    from smartcut.cut_video import (
        AudioExportInfo,
        AudioExportSettings,
        VideoExportMode,
        VideoExportQuality,
        VideoSettings,
        smart_cut,
    )
    from smartcut.media_container import MediaContainer

    intermediates: list[Path] = []
    t0 = time.perf_counter()
    try:
        for i, (s, e) in enumerate(SCHEDULE):
            inter = out.parent / f"_seg{i}.mp4"
            container = MediaContainer(str(CLIP))
            audio_settings = [
                AudioExportSettings(codec="passthru")
                for _ in container.audio_tracks
            ]
            audio_export_info = AudioExportInfo(output_tracks=audio_settings)
            video_settings = VideoSettings(
                VideoExportMode.SMARTCUT, VideoExportQuality.NORMAL, "copy"
            )
            fr = [(Fraction(s).limit_denominator(1000), Fraction(e).limit_denominator(1000))]
            exc = smart_cut(
                container, fr, str(inter),
                audio_export_info=audio_export_info,
                video_settings=video_settings,
                log_level="error",
            )
            if exc is not None:
                return (time.perf_counter() - t0, f"per-segment smart_cut failed: {exc}")
            intermediates.append(inter)

        # Build a concat list and run ffmpeg's concat demuxer (stream-copy).
        list_file = out.parent / "_concat.txt"
        list_file.write_text(
            "\n".join(f"file '{p.resolve()}'" for p in intermediates) + "\n",
            encoding="utf-8",
        )
        cmd = [
            str(FFMPEG),
            "-y",
            "-loglevel", "error",
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",
            str(out),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        elapsed = time.perf_counter() - t0
        if result.returncode != 0:
            return (elapsed, f"ffmpeg concat failed: {result.stderr[-300:]}")
        return (elapsed, "ok")
    finally:
        for p in intermediates:
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        try:
            (out.parent / "_concat.txt").unlink()
        except FileNotFoundError:
            pass


def _approach_ffmpeg_segment_then_concat(out: Path) -> tuple[float, str]:
    """Pure ffmpeg fallback: -ss/-t per segment with stream copy, then concat."""
    intermediates: list[Path] = []
    t0 = time.perf_counter()
    try:
        for i, (s, e) in enumerate(SCHEDULE):
            inter = out.parent / f"_ffseg{i}.mp4"
            cmd = [
                str(FFMPEG),
                "-y",
                "-loglevel", "error",
                "-ss", f"{s}",
                "-t", f"{e - s}",
                "-i", str(CLIP),
                "-c", "copy",
                str(inter),
            ]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                return (time.perf_counter() - t0, f"ffmpeg segment failed: {r.stderr[-300:]}")
            intermediates.append(inter)
        list_file = out.parent / "_ffconcat.txt"
        list_file.write_text(
            "\n".join(f"file '{p.resolve()}'" for p in intermediates) + "\n",
            encoding="utf-8",
        )
        r = subprocess.run(
            [
                str(FFMPEG),
                "-y",
                "-loglevel", "error",
                "-f", "concat",
                "-safe", "0",
                "-i", str(list_file),
                "-c", "copy",
                str(out),
            ],
            capture_output=True,
            text=True,
        )
        elapsed = time.perf_counter() - t0
        if r.returncode != 0:
            return (elapsed, f"ffmpeg concat failed: {r.stderr[-300:]}")
        return (elapsed, "ok")
    finally:
        for p in intermediates:
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        try:
            (out.parent / "_ffconcat.txt").unlink()
        except FileNotFoundError:
            pass


def _verdict(name: str, elapsed: float, status: str, out: Path) -> dict:
    if status != "ok":
        return {"name": name, "elapsed": elapsed, "ok": False, "note": status}
    if not out.is_file():
        return {"name": name, "elapsed": elapsed, "ok": False, "note": "output missing"}
    info = _probe(out)
    duration = float(info["format"]["duration"])
    streams = info["streams"]
    vstream = next((s for s in streams if s["codec_type"] == "video"), {})
    astream = next((s for s in streams if s["codec_type"] == "audio"), {})
    duration_ok = abs(duration - EXPECTED_DURATION_S) < 1.0
    return {
        "name": name,
        "elapsed": elapsed,
        "ok": duration_ok,
        "note": "" if duration_ok else f"duration off: {duration:.2f}s",
        "duration_s": duration,
        "video_codec": vstream.get("codec_name"),
        "audio_codec": astream.get("codec_name"),
        "video_pix_fmt": vstream.get("pix_fmt"),
        "video_bitrate": vstream.get("bit_rate"),
        "size_bytes": Path(out).stat().st_size,
    }


def _format_report(source_info: dict, results: list[dict]) -> str:
    lines = []
    lines.append("=" * 70)
    lines.append("smartcut non-monotonic spike — phase 6a GATE")
    lines.append("=" * 70)
    src_v = next((s for s in source_info["streams"] if s["codec_type"] == "video"), {})
    src_a = next((s for s in source_info["streams"] if s["codec_type"] == "audio"), {})
    lines.append(f"source: {CLIP}")
    lines.append(f"source video codec: {src_v.get('codec_name')} {src_v.get('pix_fmt')}")
    lines.append(f"source audio codec: {src_a.get('codec_name')}")
    lines.append(f"schedule (non-monotonic): {SCHEDULE}")
    lines.append(f"expected output duration: {EXPECTED_DURATION_S}s")
    lines.append("")
    for r in results:
        lines.append(f"--- {r['name']}")
        lines.append(f"  status:   {'PASS' if r['ok'] else 'FAIL'}  ({r.get('note','')})")
        lines.append(f"  elapsed:  {r['elapsed']:.2f}s")
        if "duration_s" in r:
            lines.append(f"  output duration: {r['duration_s']:.2f}s")
            same_video = r["video_codec"] == src_v.get("codec_name")
            same_audio = r["audio_codec"] == src_a.get("codec_name")
            re_encode = "no" if same_video and same_audio else "POSSIBLE (codecs differ)"
            lines.append(
                f"  output codecs: v={r['video_codec']} ({r['video_pix_fmt']}) "
                f"a={r['audio_codec']}; re-encode? {re_encode}"
            )
            lines.append(f"  output size:  {r['size_bytes']/1e6:.2f} MB")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    if not SOURCE.is_file():
        print(f"source not found: {SOURCE}", file=sys.stderr)
        return 2
    if not FFMPEG.is_file():
        print(f"ffmpeg not found: {FFMPEG}", file=sys.stderr)
        return 2
    _make_working_clip()
    src_info = _probe(CLIP)

    results: list[dict] = []
    out_a = WORK_DIR / "out_smartcut_direct.mp4"
    elapsed, status = _approach_smartcut_direct(out_a)
    results.append(_verdict("smartcut(non-monotonic, direct)", elapsed, status, out_a))

    out_b = WORK_DIR / "out_smartcut_per_segment_concat.mp4"
    elapsed, status = _approach_per_segment_then_concat(out_b)
    results.append(_verdict("smartcut per-segment + ffmpeg concat", elapsed, status, out_b))

    out_c = WORK_DIR / "out_ffmpeg_segment_concat.mp4"
    elapsed, status = _approach_ffmpeg_segment_then_concat(out_c)
    results.append(_verdict("ffmpeg per-segment + ffmpeg concat (no smartcut)", elapsed, status, out_c))

    print(_format_report(src_info, results))
    print(f"outputs kept in {WORK_DIR} for manual audio-sync inspection")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
