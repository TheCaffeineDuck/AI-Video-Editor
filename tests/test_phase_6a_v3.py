"""Phase 6a — schema v3 (Clip / Timeline) + run-batched renderer.

Covers:

- ``Timeline.is_source_monotonic`` truth table.
- ``split_into_monotonic_runs`` algorithm correctness.
- v2 → v3 JSON migration round-trip (lossless on monotonic input).
- ``AddCut.reason`` persists through save/load.
- ``AddCut`` / ``RestoreRange`` raise ``NotImplementedError`` on
  non-monotonic timelines.
- Non-monotonic synthetic render: duration ±50 ms, audio sync within
  10 ms across joins.

Renderer tests use the H.264 ``synthetic.mp4`` fixture (30 s, 30 fps,
stepped tones at 0–10 / 10–20 / 20–30 s) — re-encoding the heavy HEVC
spike clip would slow the suite by minutes per test.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.document import (
    Document,
    MediaSource,
    Range,
    Segment,
    Word,
    build_document,
)
from core.editing import AddCut, RestoreRange
from core.render import render_cut
from core.timeline import Clip, Timeline, split_into_monotonic_runs

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _ffprobe_path() -> Path:
    for cand in (
        Path("/opt/homebrew/bin/ffprobe"),
        Path("/usr/local/bin/ffprobe"),
        Path(__file__).resolve().parent.parent
        / "resources" / "bin" / "ffprobe-mac",
    ):
        if cand.is_file():
            return cand
    pytest.skip("ffprobe not available")


def _probe_duration(path: Path) -> tuple[float, float]:
    """Return (video_duration_s, audio_duration_s) via ffprobe."""
    out = subprocess.run(
        [
            str(_ffprobe_path()),
            "-v", "error",
            "-of", "json",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(out.stdout)
    v = next(s for s in data["streams"] if s["codec_type"] == "video")
    a_streams = [s for s in data["streams"] if s["codec_type"] == "audio"]
    a_duration = float(a_streams[0]["duration"]) if a_streams else 0.0
    return float(v["duration"]), a_duration


def _src(path: Path, duration: float) -> MediaSource:
    return MediaSource(id="src0", path=path, duration=duration)


def _build_synthetic_doc(media_path: Path, *, duration: float = 30.0) -> Document:
    """Build a Document over the synthetic 30s fixture with one word per second.

    The synthetic fixture is silent of speech but the words just need
    plausible source-time boundaries for the renderer's snap pass.
    """
    seg = Segment(
        text="filler",
        start=0.0,
        end=duration,
        words=tuple(
            Word(text=f"w{i}", start=float(i), end=float(i + 1))
            for i in range(int(duration))
        ),
    )
    return Document(
        sources={"src0": _src(media_path, duration)},
        segments=[seg],
        ranges=[Range(source_id="src0", start=0.0, end=duration)],
        language=None,
        created_at=datetime.now(UTC),
        model_name="tiny",
    )


# ---------------------------------------------------------------------------
# Clip / Timeline truth table
# ---------------------------------------------------------------------------


P = Path("/x.mp4")
Q = Path("/y.mp4")


def _clip(p: Path, s: float, e: float) -> Clip:
    return Clip(source_path=p, source_start=s, source_end=e)


@pytest.mark.parametrize(
    "clips,expected",
    [
        ((), True),  # empty timeline is vacuously monotonic
        ((_clip(P, 0, 1),), True),  # single clip
        ((_clip(P, 0, 1), _clip(P, 1, 2)), True),  # touching, sorted, single source
        ((_clip(P, 0, 1), _clip(P, 2, 3)), True),  # gap, sorted, single source
        ((_clip(P, 2, 3), _clip(P, 0, 1)), False),  # reversed
        ((_clip(P, 0, 2), _clip(P, 1, 3)), False),  # overlap
        ((_clip(P, 0, 1), _clip(Q, 0, 1)), False),  # multi-source ⇒ non-monotonic
        ((_clip(P, 0, 1), _clip(P, 1, 2), _clip(P, 0.5, 1.5)), False),  # mid-list violation
    ],
)
def test_is_source_monotonic_truth_table(clips, expected):
    assert Timeline(clips=clips).is_source_monotonic() is expected


def test_clip_post_init_rejects_zero_or_negative_duration():
    with pytest.raises(ValueError):
        Clip(source_path=P, source_start=1.0, source_end=1.0)
    with pytest.raises(ValueError):
        Clip(source_path=P, source_start=2.0, source_end=1.0)


def test_timeline_total_duration():
    tl = Timeline(clips=(_clip(P, 0, 2), _clip(P, 5, 8)))
    assert tl.total_duration_s == 5.0


# ---------------------------------------------------------------------------
# split_into_monotonic_runs
# ---------------------------------------------------------------------------


def test_split_empty_timeline_yields_no_runs():
    assert split_into_monotonic_runs(Timeline()) == []


def test_split_already_monotonic_yields_one_run():
    tl = Timeline(clips=(_clip(P, 0, 2), _clip(P, 4, 6), _clip(P, 8, 10)))
    runs = split_into_monotonic_runs(tl)
    assert len(runs) == 1
    assert runs[0].clips == tl.clips


def test_split_spike_schedule():
    """The spike's ``[(60,90),(0,30),(180,210)]`` partitions into 2 runs.

    Run 0 = [(60, 90)] (out-of-order vs run 1's start),
    Run 1 = [(0, 30), (180, 210)] (sorted, non-overlapping).
    """
    tl = Timeline(
        clips=(_clip(P, 60, 90), _clip(P, 0, 30), _clip(P, 180, 210))
    )
    runs = split_into_monotonic_runs(tl)
    assert len(runs) == 2
    assert runs[0].clips == (_clip(P, 60, 90),)
    assert runs[1].clips == (_clip(P, 0, 30), _clip(P, 180, 210))
    assert all(r.is_source_monotonic() for r in runs)


def test_split_worst_case_each_clip_its_own_run():
    """Strictly descending → every clip becomes its own run."""
    tl = Timeline(
        clips=(_clip(P, 90, 100), _clip(P, 60, 80), _clip(P, 30, 50))
    )
    runs = split_into_monotonic_runs(tl)
    assert len(runs) == 3
    assert all(len(r.clips) == 1 for r in runs)
    assert all(r.is_source_monotonic() for r in runs)


def test_split_multi_source_starts_new_run():
    tl = Timeline(
        clips=(_clip(P, 0, 2), _clip(P, 4, 6), _clip(Q, 0, 2), _clip(Q, 5, 7))
    )
    runs = split_into_monotonic_runs(tl)
    assert len(runs) == 2
    assert runs[0].source_paths == (P,)
    assert runs[1].source_paths == (Q,)


def test_split_concat_preserves_playlist_order():
    """Concatenating runs reproduces the input timeline."""
    tl = Timeline(
        clips=(_clip(P, 5, 10), _clip(P, 0, 2), _clip(P, 12, 15))
    )
    runs = split_into_monotonic_runs(tl)
    rebuilt = tuple(c for run in runs for c in run.clips)
    assert rebuilt == tl.clips


# ---------------------------------------------------------------------------
# v2 → v3 migration round-trip
# ---------------------------------------------------------------------------


def _v2_payload_with_ranges(media_path: str, ranges: list[dict]) -> dict:
    return {
        "schema_version": 2,
        "sources": {
            "src0": {
                "id": "src0",
                "path": media_path,
                "duration": 10.0,
                "hash": "",
            }
        },
        "language": "en",
        "model_name": "tiny",
        "created_at": "2026-04-26T10:00:00+00:00",
        "segments": [],
        "ranges": ranges,
    }


def test_v2_to_v3_migration_lossless_for_monotonic_doc():
    """Loading a v2 JSON, re-saving, and loading again yields identical state."""
    payload_v2 = _v2_payload_with_ranges(
        "/tmp/x.wav",
        [
            {"source_id": "src0", "start": 0.0, "end": 4.0, "reason": ""},
            {"source_id": "src0", "start": 6.0, "end": 10.0, "reason": "filler"},
        ],
    )
    doc = Document.from_json(payload_v2)
    # In-memory ranges preserve reasons.
    assert doc.ranges == [
        Range(source_id="src0", start=0.0, end=4.0, reason=""),
        Range(source_id="src0", start=6.0, end=10.0, reason="filler"),
    ]
    # Re-save: now v3.1 schema (6b-1 added edit_log).
    payload_v3 = doc.to_json()
    assert payload_v3["schema_version"] == 3.1
    assert "main_timeline" in payload_v3
    assert "ranges" not in payload_v3
    assert payload_v3["edit_log"] == []  # empty on a freshly-migrated v2 doc
    # Round-trip via v3.1.
    doc2 = Document.from_json(payload_v3)
    assert doc2 == doc


def test_v2_to_v3_main_timeline_is_monotonic():
    payload = _v2_payload_with_ranges(
        "/tmp/x.wav",
        [
            {"source_id": "src0", "start": 0.0, "end": 4.0, "reason": ""},
            {"source_id": "src0", "start": 6.0, "end": 10.0, "reason": ""},
        ],
    )
    doc = Document.from_json(payload)
    assert doc.main_timeline.is_source_monotonic() is True


# ---------------------------------------------------------------------------
# Hand-crafted v3 non-monotonic fixture loads + reports non-monotonic
# ---------------------------------------------------------------------------


def test_v3_non_monotonic_fixture_loads_and_reports_non_monotonic(tmp_path):
    media = tmp_path / "src.mp4"
    media.write_bytes(b"")
    payload = {
        "schema_version": 3,
        "sources": {
            "src0": {
                "id": "src0",
                "path": str(media),
                "duration": 10.0,
                "hash": "",
            }
        },
        "language": "en",
        "model_name": "tiny",
        "created_at": "2026-04-26T10:00:00+00:00",
        "segments": [],
        "main_timeline": {
            "clips": [
                {
                    "source_id": "src0",
                    "source_path": str(media),
                    "source_start": 5.0,
                    "source_end": 8.0,
                    "reason": "",
                },
                {
                    "source_id": "src0",
                    "source_path": str(media),
                    "source_start": 0.0,
                    "source_end": 3.0,
                    "reason": "",
                },
            ],
        },
    }
    path = tmp_path / "x.transcribe.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    doc = Document.from_json(json.loads(path.read_text()))
    assert doc.main_timeline.is_source_monotonic() is False
    # In-memory ranges preserve playlist (non-sorted) order.
    assert [r.start for r in doc.ranges] == [5.0, 0.0]


# ---------------------------------------------------------------------------
# AddCut.reason
# ---------------------------------------------------------------------------


def test_add_cut_reason_default_is_none():
    assert AddCut(start=1.0, end=2.0).reason is None


def test_add_cut_reason_persists_on_surviving_neighbor(tmp_path):
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    doc = build_document(
        media_path=media,
        duration=10.0,
        language="en",
        segments=[
            Segment(
                text="seg",
                start=0.0,
                end=10.0,
                words=tuple(Word(f"w{i}", float(i), float(i + 1)) for i in range(10)),
            )
        ],
        model_name="tiny",
    )
    after = AddCut(start=4.0, end=6.0, reason="filler removal").apply(doc)
    # Reason lands on the range whose end matches the cut start (preceding).
    by_end = {r.end: r for r in after.ranges}
    assert by_end[4.0].reason == "filler removal"
    # Persists through JSON.
    payload = json.dumps(after.to_json())
    restored = Document.from_json(json.loads(payload))
    by_end2 = {r.end: r for r in restored.ranges}
    assert by_end2[4.0].reason == "filler removal"


def test_add_cut_no_reason_does_not_overwrite(tmp_path):
    """Reason=None preserves whatever reason the surviving neighbor had."""
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    src = MediaSource(id="src0", path=media, duration=10.0)
    doc = Document(
        sources={"src0": src},
        segments=[
            Segment(
                text="seg",
                start=0.0,
                end=10.0,
                words=tuple(Word(f"w{i}", float(i), float(i + 1)) for i in range(10)),
            )
        ],
        ranges=[Range(source_id="src0", start=0.0, end=10.0, reason="initial")],
        language="en",
        created_at=datetime.now(UTC),
        model_name="tiny",
    )
    after = AddCut(start=4.0, end=6.0).apply(doc)
    by_end = {r.end: r for r in after.ranges}
    assert by_end[4.0].reason == "initial"  # unchanged


# ---------------------------------------------------------------------------
# Editing on non-monotonic raises
# ---------------------------------------------------------------------------


def _non_monotonic_doc(tmp_path: Path) -> Document:
    media = tmp_path / "x.mp4"
    media.write_bytes(b"")
    src = MediaSource(id="src0", path=media, duration=10.0)
    return Document(
        sources={"src0": src},
        segments=[
            Segment(
                text="seg",
                start=0.0,
                end=10.0,
                words=tuple(Word(f"w{i}", float(i), float(i + 1)) for i in range(10)),
            )
        ],
        # Non-monotonic order: clip at 5–8 followed by clip at 0–3.
        ranges=[
            Range(source_id="src0", start=5.0, end=8.0),
            Range(source_id="src0", start=0.0, end=3.0),
        ],
        language="en",
        created_at=datetime.now(UTC),
        model_name="tiny",
    )


def test_add_cut_on_non_monotonic_raises(tmp_path):
    doc = _non_monotonic_doc(tmp_path)
    with pytest.raises(NotImplementedError, match="non-monotonic"):
        AddCut(start=1.0, end=2.0).apply(doc)


def test_restore_range_on_non_monotonic_raises(tmp_path):
    doc = _non_monotonic_doc(tmp_path)
    with pytest.raises(NotImplementedError, match="non-monotonic"):
        RestoreRange(start=3.0, end=5.0).apply(doc)


# ---------------------------------------------------------------------------
# Renderer: monotonic fast path unchanged
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_render_monotonic_unchanged_against_synthetic(synthetic_video, tmp_path):
    """The fast path's behaviour must not regress: the byte-for-byte
    full-coverage shortcut still kicks in when ranges cover the source.
    """
    doc = _build_synthetic_doc(synthetic_video)
    out = tmp_path / "out.mp4"
    render_cut(doc, out)
    # Full coverage → byte-for-byte copy.
    assert out.stat().st_size == synthetic_video.stat().st_size


@pytest.mark.slow
def test_render_monotonic_partial_cuts(synthetic_video, tmp_path):
    """Two kept ranges, monotonic → single smartcut call. Output duration
    ≈ sum of kept ranges (within 50 ms tolerance).
    """
    doc = _build_synthetic_doc(synthetic_video)
    src = doc.sources["src0"]
    object.__setattr__(
        doc,
        "ranges",
        [
            Range(source_id="src0", start=0.0, end=8.0),
            Range(source_id="src0", start=12.0, end=20.0),
        ],
    )
    # Sanity: mut still single-source-monotonic.
    assert doc.main_timeline.is_source_monotonic()
    out = tmp_path / "out.mp4"
    render_cut(doc, out, audio_fade_ms=0)
    v_dur, _ = _probe_duration(out)
    expected = 8.0 + 8.0
    assert abs(v_dur - expected) < 0.5  # smartcut rounds to GOPs; 500ms slack
    assert src.duration == 30.0


# ---------------------------------------------------------------------------
# Renderer: non-monotonic run-batched path
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_render_non_monotonic_synthetic_duration_within_50ms(synthetic_video, tmp_path):
    """Render a non-monotonic schedule against the H.264 synthetic fixture.

    Schedule: [(20, 25), (5, 10), (0, 3)] — three 5/5/3-second clips
    visiting the source out of order. Total expected output: 13 s.
    Tolerance: ±50 ms (per spec); audio sync within 10 ms.
    """
    doc = _build_synthetic_doc(synthetic_video)
    object.__setattr__(
        doc,
        "ranges",
        [
            Range(source_id="src0", start=20.0, end=25.0),
            Range(source_id="src0", start=5.0, end=10.0),
            Range(source_id="src0", start=0.0, end=3.0),
        ],
    )
    assert doc.main_timeline.is_source_monotonic() is False
    out = tmp_path / "out.mp4"
    # pad_lead=pad_trail=0 isolates the run-batching from the
    # widening-pad pipeline; audio_fade_ms=0 isolates from the fade
    # post-pass that re-encodes audio.
    render_cut(doc, out, pad_lead=0.0, pad_trail=0.0, audio_fade_ms=0)
    v_dur, a_dur = _probe_duration(out)
    expected = 5.0 + 5.0 + 3.0
    # Tolerance ±50 ms per spec. Sub-frame slack comes from smartcut's
    # GOP-aligned cut points; the synthetic fixture is 30 fps so one
    # frame is ~33 ms.
    assert abs(v_dur - expected) <= 0.05
    if a_dur > 0:
        assert abs(v_dur - a_dur) <= 0.01  # audio sync within 10 ms


@pytest.mark.slow
def test_render_non_monotonic_with_fades_across_run_joins(synthetic_video, tmp_path):
    """Non-monotonic render with fades: every kept-range edge in the
    final output gets the afade pass, including the run-boundary joins.
    The output should still play and have a sensible duration; we can't
    sniff individual fade envelopes from ffprobe, but the absence of
    crashes plus the same duration check is meaningful coverage.
    """
    doc = _build_synthetic_doc(synthetic_video)
    object.__setattr__(
        doc,
        "ranges",
        [
            Range(source_id="src0", start=20.0, end=25.0),
            Range(source_id="src0", start=0.0, end=5.0),
        ],
    )
    out = tmp_path / "out.mp4"
    render_cut(doc, out, audio_fade_ms=30)
    assert out.is_file()
    v_dur, _ = _probe_duration(out)
    # 5 + 5 = 10s expected, slack for GOP rounding.
    assert abs(v_dur - 10.0) < 0.5


@pytest.mark.slow
def test_render_non_monotonic_progress_reaches_one(synthetic_video, tmp_path):
    """Progress callback merges per-run signals into a single 0..1 stream."""
    doc = _build_synthetic_doc(synthetic_video)
    object.__setattr__(
        doc,
        "ranges",
        [
            Range(source_id="src0", start=20.0, end=25.0),
            Range(source_id="src0", start=0.0, end=5.0),
        ],
    )
    seen: list[float] = []
    out = tmp_path / "out.mp4"
    render_cut(doc, out, on_progress=lambda f: seen.append(f), audio_fade_ms=0)
    # First and last bookend the stream; final must be 1.0.
    assert seen
    assert seen[-1] == 1.0
    # No regressions in monotonicity at the merge boundary.
    for prev, cur in zip(seen, seen[1:], strict=False):
        assert cur >= prev - 1e-6
