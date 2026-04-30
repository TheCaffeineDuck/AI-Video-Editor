"""Phase 7 — synced multi-cam highlights.

Coverage:

- :class:`core.highlight.SubSpan` validation + JSON round-trip.
- v2 → v3 highlight migration (single-span legacy file lifts cleanly).
- Schema-version v3 fields: ``sub_spans``, ``parent_source_hashes``,
  ``sync_group_id``.
- :class:`core.highlight.HighlightRenderResult` v1 → v2 migration on
  read.
- :func:`core.highlight.reassign_fragment_source` swaps a fragment's
  source and re-hashes correctly.
- :mod:`core.sync` — :func:`estimate_offset` against a known shifted
  signal, :class:`SyncGroup` round-trip,
  :func:`set_manual_offset`, freshness validation.
- MCP propose_highlights with sub_spans + sync_group_id + invalid
  group reference; sync-group tool surface
  (create / list / read / set_offset).
- Slow integration: synthetic 3-camera fixture (each camera carries a
  shifted slice of a known audio master), auto-sync, propose with
  per-fragment camera picks, render with mixed-source fragments.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import struct
import subprocess
import wave
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from core.cache import cache_key
from core.document import Document, MediaSource, Range, Segment, UnsupportedSchemaError, Word
from core.highlight import (
    HIGHLIGHT_SCHEMA_VERSION,
    Highlight,
    HighlightRenderResult,
    SubSpan,
    list_highlights_for_document,
    new_render_result_id,
    read_highlight,
    reassign_fragment_source,
    write_highlight,
)
from core.sync import (
    CONFIDENCE_GOOD,
    StaleSyncGroupError,
    SyncEstimationError,
    SyncGroup,
    SyncSource,
    build_sync_group,
    estimate_offset,
    list_sync_groups_for_document,
    read_sync_group,
    set_manual_offset,
    sync_dir_for_document,
    validate_sync_group_freshness,
    write_sync_group,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FFMPEG = REPO_ROOT / "resources" / "bin" / "ffmpeg-mac"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_pcm_wav(
    path: Path,
    samples: np.ndarray,
    *,
    rate: int = 16000,
) -> None:
    """Write a 16-bit mono PCM wav to ``path`` from a float ``[-1, 1]`` array."""
    clipped = np.clip(samples, -1.0, 1.0)
    int16 = (clipped * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(int16.tobytes())


def _doc(media: Path, duration: float = 30.0) -> Document:
    src = MediaSource(id="src0", path=media, duration=duration)
    n_words = max(2, int(duration))
    seg = Segment(
        text=" ".join(f"w{i}" for i in range(n_words)),
        start=0.0,
        end=duration,
        words=tuple(
            Word(text=f"w{i}", start=float(i), end=float(i) + 0.9)
            for i in range(n_words)
        ),
    )
    return Document(
        sources={"src0": src},
        segments=[seg],
        ranges=[Range(source_id="src0", start=0.0, end=duration, reason="manual")],
        language="en",
        created_at=datetime(2026, 4, 30, 10, 0, 0, tzinfo=UTC),
        model_name="tiny",
    )


def _write_doc(doc: Document, path: Path) -> Path:
    path.write_text(json.dumps(doc.to_json(), indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# SubSpan basics
# ---------------------------------------------------------------------------


def test_sub_span_round_trips_through_json(tmp_path):
    media = tmp_path / "m.mp4"
    media.touch()
    s = SubSpan(source_path=media, source_start=2.0, source_end=8.0, reason="wide for laughter")
    re_read = SubSpan.from_json(json.loads(json.dumps(s.to_json())))
    assert re_read == s


def test_sub_span_rejects_zero_or_inverted_duration(tmp_path):
    media = tmp_path / "m.mp4"
    with pytest.raises(ValueError, match="positive duration"):
        SubSpan(source_path=media, source_start=5.0, source_end=5.0)


# ---------------------------------------------------------------------------
# Highlight v3 round-trip + v2 migration
# ---------------------------------------------------------------------------


def test_highlight_v3_round_trip(tmp_path):
    media = tmp_path / "m.mp4"
    media.touch()
    parent = tmp_path / "doc.transcribe.json"
    h = Highlight(
        highlight_id="20260430T100000-deadbeef",
        created_at=datetime(2026, 4, 30, tzinfo=UTC),
        parent_document_path=parent,
        parent_source_hashes={str(media): cache_key(media)},
        sub_spans=(
            SubSpan(source_path=media, source_start=2.0, source_end=8.0),
        ),
        reason="highlight reel",
    )
    payload = h.to_json()
    assert payload["schema_version"] == HIGHLIGHT_SCHEMA_VERSION
    assert payload["sync_group_id"] is None
    re_read = Highlight.from_json(json.loads(json.dumps(payload)))
    assert re_read == h


def test_highlight_v3_multi_fragment(tmp_path):
    cam_a = tmp_path / "a.mp4"
    cam_b = tmp_path / "b.mp4"
    cam_a.touch()
    cam_b.touch()
    parent = tmp_path / "doc.transcribe.json"
    h = Highlight(
        highlight_id="hl1",
        created_at=datetime(2026, 4, 30, tzinfo=UTC),
        parent_document_path=parent,
        parent_source_hashes={
            str(cam_a): cache_key(cam_a),
            str(cam_b): cache_key(cam_b),
        },
        sub_spans=(
            SubSpan(source_path=cam_a, source_start=0.0, source_end=4.0, reason="wide"),
            SubSpan(source_path=cam_b, source_start=2.5, source_end=7.0, reason="closeup"),
        ),
        reason="highlight reel",
        sync_group_id="sg1",
    )
    re_read = Highlight.from_json(json.loads(json.dumps(h.to_json())))
    assert re_read == h
    assert len(re_read.sub_spans) == 2
    assert re_read.unique_source_paths == (cam_a, cam_b)


def test_highlight_rejects_empty_sub_spans(tmp_path):
    parent = tmp_path / "p.json"
    with pytest.raises(ValueError, match="at least one sub_span"):
        Highlight(
            highlight_id="x",
            created_at=datetime(2026, 4, 30, tzinfo=UTC),
            parent_document_path=parent,
            parent_source_hashes={},
            sub_spans=(),
            reason="highlight reel",
        )


def test_highlight_rejects_missing_source_hash(tmp_path):
    media = tmp_path / "m.mp4"
    parent = tmp_path / "p.json"
    with pytest.raises(ValueError, match="parent_source_hashes is missing"):
        Highlight(
            highlight_id="x",
            created_at=datetime(2026, 4, 30, tzinfo=UTC),
            parent_document_path=parent,
            parent_source_hashes={},
            sub_spans=(SubSpan(source_path=media, source_start=0.0, source_end=5.0),),
            reason="highlight reel",
        )


def test_highlight_v2_payload_migrates_to_v3(tmp_path):
    """A v2-shaped JSON file should load as a single-fragment v3 in memory."""
    media = tmp_path / "m.mp4"
    parent = tmp_path / "p.json"
    v2_payload = {
        "schema_version": 2,
        "highlight_id": "v2-id",
        "created_at": "2026-04-29T10:00:00+00:00",
        "parent_document_path": str(parent),
        "parent_source_hash": "abc123",
        "span_source_path": str(media),
        "span_source_start": 5.0,
        "span_source_end": 12.0,
        "reason": "highlight reel",
        "reframe_mode": "speaker_locked",
        "captions_enabled": False,
        "rendered_output_path": None,
    }
    h = Highlight.from_json(v2_payload)
    assert h.schema_version == HIGHLIGHT_SCHEMA_VERSION
    assert len(h.sub_spans) == 1
    assert h.sub_spans[0].source_path == media
    assert h.sub_spans[0].source_start == 5.0
    assert h.sub_spans[0].source_end == 12.0
    assert h.parent_source_hashes == {str(media): "abc123"}
    assert h.sync_group_id is None
    # Single-source compat properties still work.
    assert h.parent_source_hash == "abc123"
    assert h.span_source_start == 5.0


def test_highlight_v1_still_raises(tmp_path):
    parent = tmp_path / "p.json"
    media = tmp_path / "m.mp4"
    v1_payload = {
        "schema_version": 1,
        "highlight_id": "v1",
        "created_at": "2026-04-29T10:00:00+00:00",
        "parent_document_path": str(parent),
        "parent_document_state_hash": "deadbeef",
        "span_source_path": str(media),
        "span_source_start": 0.0,
        "span_source_end": 10.0,
        "reason": "highlight reel",
        "reframe_mode": "center",
        "captions_enabled": False,
        "rendered_output_path": None,
    }
    with pytest.raises(UnsupportedSchemaError, match="v1"):
        Highlight.from_json(v1_payload)


def test_compat_property_raises_on_multi_fragment(tmp_path):
    cam_a = tmp_path / "a.mp4"
    cam_b = tmp_path / "b.mp4"
    cam_a.touch()
    cam_b.touch()
    parent = tmp_path / "p.json"
    h = Highlight(
        highlight_id="hl1",
        created_at=datetime(2026, 4, 30, tzinfo=UTC),
        parent_document_path=parent,
        parent_source_hashes={
            str(cam_a): cache_key(cam_a),
            str(cam_b): cache_key(cam_b),
        },
        sub_spans=(
            SubSpan(source_path=cam_a, source_start=0.0, source_end=4.0),
            SubSpan(source_path=cam_b, source_start=2.5, source_end=7.0),
        ),
        reason="highlight reel",
    )
    with pytest.raises(ValueError, match="single-fragment"):
        _ = h.span_source_path
    with pytest.raises(ValueError, match="single-source"):
        _ = h.parent_source_hash


def test_reassign_fragment_source_swaps_path_and_clears_render(tmp_path):
    cam_a = tmp_path / "a.mp4"
    cam_b = tmp_path / "b.mp4"
    cam_a.write_bytes(b"a")
    cam_b.write_bytes(b"b")
    parent = tmp_path / "doc.transcribe.json"
    h = Highlight(
        highlight_id="hl1",
        created_at=datetime(2026, 4, 30, tzinfo=UTC),
        parent_document_path=parent,
        parent_source_hashes={str(cam_a): cache_key(cam_a)},
        sub_spans=(SubSpan(source_path=cam_a, source_start=0.0, source_end=4.0),),
        reason="highlight reel",
        rendered_output_path=tmp_path / "previous.mp4",
    )
    write_highlight(parent, h)
    updated = reassign_fragment_source(
        parent, h, fragment_index=0, new_source_path=cam_b, new_source_hash=cache_key(cam_b)
    )
    assert updated.sub_spans[0].source_path == cam_b
    assert str(cam_b) in updated.parent_source_hashes
    assert str(cam_a) not in updated.parent_source_hashes
    assert updated.rendered_output_path is None
    re_read = read_highlight(parent, h.highlight_id)
    assert re_read.sub_spans[0].source_path == cam_b


# ---------------------------------------------------------------------------
# HighlightRenderResult v1 → v2 migration
# ---------------------------------------------------------------------------


def test_render_result_v1_payload_migrates(tmp_path):
    parent = tmp_path / "doc.transcribe.json"
    parent.parent.mkdir(parents=True, exist_ok=True)
    (parent.parent / "doc.transcribe.json.highlights").mkdir(parents=True, exist_ok=True)
    v1_payload = {
        "schema_version": 1,
        "render_result_id": "rr-v1",
        "highlight_id": "hl-1",
        "created_at": "2026-04-29T10:00:00+00:00",
        "output_path": str(tmp_path / "out.mp4"),
        "parent_source_hash": "legacy_hash",
        "face_detection_used": "speaker_locked",
        "crop_box": {"x": 100, "y": 200, "w": 1080, "h": 1920},
        "wall_clock_s": 3.5,
    }
    rr = HighlightRenderResult.from_json(v1_payload)
    assert rr.parent_source_hashes == {"primary": "legacy_hash"}
    assert rr.crop_boxes_by_source == {"primary": (100, 200, 1080, 1920)}
    assert rr.sync_group_id is None
    assert rr.crop_box == (100, 200, 1080, 1920)


def test_render_result_v2_round_trip(tmp_path):
    rr = HighlightRenderResult(
        render_result_id=new_render_result_id(),
        highlight_id="hl-1",
        created_at=datetime(2026, 4, 30, tzinfo=UTC),
        output_path=tmp_path / "out.mp4",
        parent_source_hashes={"/path/cam_a.mp4": "h1", "/path/cam_b.mp4": "h2"},
        face_detection_used="speaker_locked_fallback_to_center",
        crop_box=(0, 0, 540, 1920),
        crop_boxes_by_source={
            "/path/cam_a.mp4": (0, 0, 540, 1920),
            "/path/cam_b.mp4": (100, 0, 540, 1920),
        },
        sync_group_id="sg-1",
        wall_clock_s=12.34,
    )
    re_read = HighlightRenderResult.from_json(json.loads(json.dumps(rr.to_json())))
    assert re_read == rr


# ---------------------------------------------------------------------------
# Sync — cross-correlation of synthetic shifted signals
# ---------------------------------------------------------------------------


def _make_fake_master_wav(path: Path, *, duration_s: float = 8.0, rate: int = 16000) -> None:
    """Synthetic master: a band-limited noise signal with two amplitude bursts.

    Bursts at 1.5s and 4.0s give cross-correlation prominent
    spikes to lock onto. Pure white noise also works but the bursts
    make the peak more readable for low-confidence checks.
    """
    rng = np.random.default_rng(seed=42)
    n = int(duration_s * rate)
    base = rng.standard_normal(n).astype(np.float32) * 0.05
    # Place a short, high-amplitude burst near 25 % through. We avoid
    # placing extra bursts past the available duration, so the helper
    # works on short fixtures (used by the silent-input regression
    # test, which only renders 4 s of master).
    burst_center = int(0.25 * n)
    burst_len = min(800, max(0, n - burst_center))
    if burst_len > 0:
        burst = rng.standard_normal(burst_len).astype(np.float32) * 0.5
        base[burst_center : burst_center + burst_len] += burst
    _write_pcm_wav(path, base, rate=rate)


def _shift_wav(in_path: Path, out_path: Path, *, shift_s: float, rate: int = 16000) -> None:
    """Write a copy of ``in_path`` shifted by ``shift_s`` seconds.

    Positive shift = the output is *delayed* relative to the input
    (i.e., the output represents a camera that started recording
    later than the master). The first shift_s seconds of the output
    are silence; the tail is truncated to keep the same length.
    """
    with wave.open(str(in_path), "rb") as w:
        n = w.getnframes()
        raw = w.readframes(n)
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32767.0
    shift_n = int(shift_s * rate)
    if shift_n >= 0:
        pad = np.zeros(shift_n, dtype=np.float32)
        out = np.concatenate([pad, samples[: len(samples) - shift_n]])
    else:
        # Negative shift: output is *ahead* of input; chop head, pad tail.
        absn = -shift_n
        out = np.concatenate(
            [samples[absn:], np.zeros(absn, dtype=np.float32)]
        )
    _write_pcm_wav(out_path, out, rate=rate)


def test_estimate_offset_recovers_known_positive_shift(tmp_path):
    """Camera audio delayed by 0.4 s relative to master.

    Convention check: the camera plays the same content 0.4 s later
    than the master, so master started 0.4 s *after* the camera —
    offset (= master − camera) is **−0.4** in this codebase's sign
    convention.
    """
    master = tmp_path / "master.wav"
    _make_fake_master_wav(master, duration_s=8.0)
    cam = tmp_path / "cam.wav"
    _shift_wav(master, cam, shift_s=0.4)
    est = estimate_offset(cam, master, max_lag_s=2.0, search_window_s=8.0, workdir=tmp_path)
    assert abs(est.offset_s - (-0.4)) < 0.01, f"offset_s={est.offset_s}"
    assert est.confidence > CONFIDENCE_GOOD, f"confidence={est.confidence}"


def test_estimate_offset_recovers_known_negative_shift(tmp_path):
    """Camera audio ahead of master by 0.3 s → master is 'ahead' on the
    same content, so offset_s ≈ +0.3."""
    master = tmp_path / "master.wav"
    _make_fake_master_wav(master, duration_s=8.0)
    cam = tmp_path / "cam.wav"
    _shift_wav(master, cam, shift_s=-0.3)
    est = estimate_offset(cam, master, max_lag_s=2.0, search_window_s=8.0, workdir=tmp_path)
    assert abs(est.offset_s - 0.3) < 0.01, f"offset_s={est.offset_s}"


def test_estimate_offset_silent_input_raises(tmp_path):
    master = tmp_path / "master.wav"
    _make_fake_master_wav(master, duration_s=4.0)
    cam = tmp_path / "cam.wav"
    _write_pcm_wav(cam, np.zeros(int(4.0 * 16000), dtype=np.float32))
    with pytest.raises(SyncEstimationError, match="silent"):
        estimate_offset(cam, master, max_lag_s=1.0, search_window_s=4.0, workdir=tmp_path)


def test_estimate_offset_too_short_raises(tmp_path):
    master = tmp_path / "master.wav"
    _write_pcm_wav(master, np.random.default_rng(0).standard_normal(int(0.3 * 16000)).astype(np.float32))
    cam = tmp_path / "cam.wav"
    _write_pcm_wav(cam, np.random.default_rng(1).standard_normal(int(0.3 * 16000)).astype(np.float32))
    with pytest.raises(SyncEstimationError, match="too short"):
        estimate_offset(cam, master, max_lag_s=1.0, search_window_s=4.0, workdir=tmp_path)


# ---------------------------------------------------------------------------
# SyncGroup persistence
# ---------------------------------------------------------------------------


def test_sync_group_round_trips_through_json(tmp_path):
    cam = tmp_path / "cam.mp4"
    cam.write_bytes(b"x")
    master = tmp_path / "master.wav"
    master.write_bytes(b"y")
    group = SyncGroup(
        sync_group_id="sg1",
        audio_master_path=master,
        audio_master_hash=cache_key(master),
        cameras={
            str(cam): SyncSource(
                source_path=cam,
                source_hash=cache_key(cam),
                offset_s=0.45,
                manual_override=False,
                confidence=12.3,
            )
        },
        created_at=datetime(2026, 4, 30, tzinfo=UTC),
        estimated_at=datetime(2026, 4, 30, 1, 0, 0, tzinfo=UTC),
        description="ep42",
    )
    re_read = SyncGroup.from_json(json.loads(json.dumps(group.to_json())))
    assert re_read == group


def test_sync_group_persistence_round_trip(tmp_path):
    cam = tmp_path / "cam.mp4"
    cam.write_bytes(b"x")
    master = tmp_path / "master.wav"
    master.write_bytes(b"y")
    parent = tmp_path / "doc.transcribe.json"
    group = SyncGroup(
        sync_group_id="",
        audio_master_path=master,
        audio_master_hash=cache_key(master),
        cameras={
            str(cam): SyncSource(
                source_path=cam,
                source_hash=cache_key(cam),
                offset_s=0.0,
            )
        },
        created_at=datetime(2026, 4, 30, tzinfo=UTC),
    )
    materialized, path = write_sync_group(parent, group)
    assert path.exists()
    assert materialized.sync_group_id != ""
    re_read = read_sync_group(parent, materialized.sync_group_id)
    assert re_read == materialized
    listing = list_sync_groups_for_document(parent)
    assert listing == [materialized]


def test_sync_dir_layout(tmp_path):
    parent = tmp_path / "doc.transcribe.json"
    d = sync_dir_for_document(parent)
    assert d.name == "doc.transcribe.json.sync"


def test_set_manual_offset_marks_override(tmp_path):
    cam = tmp_path / "cam.mp4"
    cam.write_bytes(b"x")
    master = tmp_path / "master.wav"
    master.write_bytes(b"y")
    group = SyncGroup(
        sync_group_id="sg1",
        audio_master_path=master,
        audio_master_hash=cache_key(master),
        cameras={
            str(cam): SyncSource(
                source_path=cam,
                source_hash=cache_key(cam),
                offset_s=0.45,
                manual_override=False,
                confidence=12.3,
            )
        },
        created_at=datetime(2026, 4, 30, tzinfo=UTC),
    )
    updated = set_manual_offset(group, cam, 1.25)
    cam_entry = updated.cameras[str(cam)]
    assert cam_entry.offset_s == 1.25
    assert cam_entry.manual_override is True
    assert cam_entry.confidence is None


def test_validate_sync_group_freshness_detects_drift(tmp_path):
    cam = tmp_path / "cam.mp4"
    cam.write_bytes(b"x")
    master = tmp_path / "master.wav"
    master.write_bytes(b"y")
    group = SyncGroup(
        sync_group_id="sg1",
        audio_master_path=master,
        audio_master_hash="not-the-real-hash",
        cameras={
            str(cam): SyncSource(
                source_path=cam,
                source_hash=cache_key(cam),
                offset_s=0.0,
            )
        },
        created_at=datetime(2026, 4, 30, tzinfo=UTC),
    )
    with pytest.raises(StaleSyncGroupError, match="audio master"):
        validate_sync_group_freshness(group)


# ---------------------------------------------------------------------------
# Renderer — multi-fragment same-source path (no sync group)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_render_multi_fragment_same_source(synthetic_video, tmp_path):
    """Two fragments from one source render to a single 1080×1920 mp4."""
    from core.highlight_render import render_highlight

    media = tmp_path / "src.mp4"
    shutil.copy2(synthetic_video, media)
    parent = tmp_path / "src.transcribe.json"
    duration = _probe_duration(media)
    doc = _doc(media, duration=duration)
    _write_doc(doc, parent)

    h = Highlight(
        highlight_id="multi-frag-same",
        created_at=datetime(2026, 4, 30, tzinfo=UTC),
        parent_document_path=parent,
        parent_source_hashes={str(media): cache_key(media)},
        sub_spans=(
            SubSpan(source_path=media, source_start=2.0, source_end=5.0),
            SubSpan(source_path=media, source_start=10.0, source_end=14.0),
        ),
        reason="highlight reel",
        reframe_mode="center",
    )
    write_highlight(parent, h)
    metadata = render_highlight(h, doc)
    assert metadata.output_path.exists()
    w, hh = _probe_dimensions(metadata.output_path)
    assert (w, hh) == (1080, 1920)
    # Output ≈ sum of fragment durations (3s + 4s = 7s) plus the
    # word-boundary outward-snap + per-side 100 ms pad on each
    # fragment. On the synthetic fixture (whose word grid is 1 s
    # apart) snap can widen each fragment by up to ~1 s in either
    # direction, so the 7 s nominal lands in [6.5, 10] s in practice.
    out_dur = _probe_duration(metadata.output_path)
    assert 6.5 < out_dur < 10.0, f"unexpected duration {out_dur}"


# ---------------------------------------------------------------------------
# Multi-cam fixture + end-to-end sync-group render
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def multicam_project(tmp_path_factory) -> dict:
    """Synthetic 3-camera project with a separate audio master.

    Layout:

    * Audio master at ``master.m4a``: 30 s of band-limited noise +
      two amplitude bursts. This is the "podcast mic" audio.
    * Three cameras (``cam_a.mp4``, ``cam_b.mp4``, ``cam_c.mp4``)
      each 30 s, 320×240, with a colored solid-fill background that
      the test can identify by sampling a frame, and an audio track
      derived from the master with a known offset:

      - cam_a: master audio shifted by +0.0 s (i.e., camera A was
        rolling in lock-step with the master — offset_s should be 0).
      - cam_b: master audio shifted by +0.5 s (camera B rolled
        late — offset_s should be ~0.5).
      - cam_c: master audio shifted by -0.3 s (camera C rolled
        early — offset_s should be ~-0.3).

    All ffmpeg-driven; the bin must exist or the fixture skips.
    """
    if not FFMPEG.exists():
        pytest.skip(f"ffmpeg not found at {FFMPEG}")
    out_dir = tmp_path_factory.mktemp("multicam")

    # 1) Render the audio master as a wav with two distinguishable bursts.
    master_wav = out_dir / "master.wav"
    _make_fake_master_wav(master_wav, duration_s=30.0)
    master = out_dir / "master.m4a"
    subprocess.run(
        [
            str(FFMPEG), "-y", "-loglevel", "error",
            "-i", str(master_wav),
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            str(master),
        ],
        check=True, capture_output=True,
    )

    # 2) Per-camera shifted audio + a colored video track.
    cam_specs = [
        ("cam_a.mp4", 0.0, "red"),
        ("cam_b.mp4", 0.5, "green"),
        ("cam_c.mp4", -0.3, "blue"),
    ]
    cam_paths: list[Path] = []
    for fname, shift_s, color in cam_specs:
        cam_audio_wav = out_dir / f".{fname}.wav"
        _shift_wav(master_wav, cam_audio_wav, shift_s=shift_s)
        cam_audio_aac = out_dir / f".{fname}.aac"
        subprocess.run(
            [
                str(FFMPEG), "-y", "-loglevel", "error",
                "-i", str(cam_audio_wav),
                "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
                str(cam_audio_aac),
            ],
            check=True, capture_output=True,
        )
        cam_path = out_dir / fname
        subprocess.run(
            [
                str(FFMPEG), "-y", "-loglevel", "error",
                "-f", "lavfi", "-i",
                f"color=c={color}:size=320x240:duration=30:rate=15",
                "-i", str(cam_audio_aac),
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
                "-c:a", "copy",
                "-shortest",
                str(cam_path),
            ],
            check=True, capture_output=True,
        )
        cam_paths.append(cam_path)
        cam_audio_wav.unlink()
        cam_audio_aac.unlink()
    master_wav.unlink()

    return {
        "audio_master": master,
        "cameras": cam_paths,
        # Sign convention: offset_s = master_time − camera_time at
        # coincidence. cam_a is in lock-step (shift 0 → offset 0).
        # cam_b's audio is delayed 0.5 s → master is 0.5 s "behind"
        # cam at the same content → offset = -0.5. cam_c's audio is
        # 0.3 s ahead of master → master is 0.3 s "ahead" → offset
        # = +0.3.
        "expected_offsets": {
            str(cam_paths[0]): 0.0,
            str(cam_paths[1]): -0.5,
            str(cam_paths[2]): 0.3,
        },
        "out_dir": out_dir,
    }


def _probe_duration(path: Path) -> float:
    """ffprobe-based duration."""
    for cand in (
        Path("/opt/homebrew/bin/ffprobe"),
        Path("/usr/local/bin/ffprobe"),
        REPO_ROOT / "resources" / "bin" / "ffprobe-mac",
    ):
        if cand.is_file():
            ffprobe = cand
            break
    else:
        pytest.skip("ffprobe not available")
    out = subprocess.run(
        [
            str(ffprobe),
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def _probe_dimensions(path: Path) -> tuple[int, int]:
    for cand in (
        Path("/opt/homebrew/bin/ffprobe"),
        Path("/usr/local/bin/ffprobe"),
        REPO_ROOT / "resources" / "bin" / "ffprobe-mac",
    ):
        if cand.is_file():
            ffprobe = cand
            break
    else:
        pytest.skip("ffprobe not available")
    out = subprocess.run(
        [
            str(ffprobe),
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=s=x:p=0",
            str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    w, h = out.stdout.strip().split("x")
    return int(w), int(h)


@pytest.mark.slow
def test_multicam_auto_sync_recovers_known_offsets(multicam_project, tmp_path):
    """build_sync_group recovers the synthetic shifts within tolerance."""
    parent = tmp_path / "multicam.transcribe.json"
    parent.parent.mkdir(parents=True, exist_ok=True)
    group = build_sync_group(
        parent,
        multicam_project["audio_master"],
        multicam_project["cameras"],
        description="multicam test",
        max_lag_s=2.0,
        search_window_s=15.0,
        workdir=multicam_project["out_dir"],
    )
    expected = multicam_project["expected_offsets"]
    for cam_path, cam in group.cameras.items():
        recovered = cam.offset_s
        target = expected[cam_path]
        # 25 ms tolerance covers AAC encode jitter + sub-sample lag picking.
        assert abs(recovered - target) < 0.025, (
            f"camera {cam_path}: expected ≈ {target}, got {recovered}"
        )


@pytest.mark.slow
def test_multicam_end_to_end_render(multicam_project, tmp_path):
    """Auto-sync → propose multi-fragment highlight → render."""
    from core.highlight_render import render_highlight

    parent = tmp_path / "multicam.transcribe.json"
    # Build a simple parent doc whose source is the audio master (the
    # transcript that captioning would consult); the highlight renderer
    # ignores ``ranges`` for sync-group highlights, so the doc just
    # needs to exist with a sane shape.
    master = multicam_project["audio_master"]
    duration = _probe_duration(master)
    src = MediaSource(id="src0", path=master, duration=duration)
    parent_doc = Document(
        sources={"src0": src},
        segments=[
            Segment(
                text="hello world",
                start=0.0, end=10.0,
                words=(
                    Word(text="hello", start=0.5, end=1.0),
                    Word(text="world", start=1.5, end=2.0),
                ),
            )
        ],
        ranges=[Range(source_id="src0", start=0.0, end=duration)],
        language="en",
        created_at=datetime(2026, 4, 30, tzinfo=UTC),
    )
    _write_doc(parent_doc, parent)

    # 1) Auto-sync.
    group = build_sync_group(
        parent,
        multicam_project["audio_master"],
        multicam_project["cameras"],
        max_lag_s=2.0,
        search_window_s=15.0,
        workdir=multicam_project["out_dir"],
    )
    materialized, _ = write_sync_group(parent, group)

    # 2) Propose a multi-fragment highlight: alternate cameras
    # every couple of seconds. Use camera-time intervals; the
    # renderer translates to master time via the group's offsets.
    cams = multicam_project["cameras"]
    h = Highlight(
        highlight_id="mc-hl-1",
        created_at=datetime(2026, 4, 30, tzinfo=UTC),
        parent_document_path=parent,
        parent_source_hashes={
            str(c): cache_key(c) for c in cams
        },
        sub_spans=(
            SubSpan(source_path=cams[0], source_start=2.0, source_end=4.0, reason="wide intro"),
            SubSpan(source_path=cams[1], source_start=4.5, source_end=6.5, reason="closeup beat"),
            SubSpan(source_path=cams[2], source_start=7.0, source_end=9.0, reason="alt angle"),
        ),
        reason="highlight reel",
        sync_group_id=materialized.sync_group_id,
        reframe_mode="center",  # solid-color fixture has no face
    )
    write_highlight(parent, h)

    # 3) Render.
    metadata = render_highlight(h, parent_doc)
    assert metadata.output_path.exists()
    assert metadata.sync_group_id == materialized.sync_group_id
    assert len(metadata.crop_boxes_by_source) == 3
    w, hh = _probe_dimensions(metadata.output_path)
    assert (w, hh) == (1080, 1920)
    out_dur = _probe_duration(metadata.output_path)
    # 3 fragments × 2 s each = 6 s. Allow ±0.3 s for encode tolerance.
    assert 5.5 < out_dur < 6.6, f"unexpected duration {out_dur}"


# ---------------------------------------------------------------------------
# MCP — propose with sub_spans + sync_group + sync tools
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _doc_pair_for_mcp(tmp_path: Path) -> tuple[Path, Path]:
    media = tmp_path / "src.mp4"
    media.write_bytes(b"")
    doc = _doc(media, duration=30.0)
    doc_path = tmp_path / "src.transcribe.json"
    _write_doc(doc, doc_path)
    return doc_path, media


def test_propose_highlights_accepts_sub_spans(tmp_path):
    from mcp_server.schemas import (
        HighlightSpec,
        ProposeHighlightsRequest,
        SubSpanSpec,
    )
    from mcp_server.tools.highlights import propose_highlights

    doc_path, media = _doc_pair_for_mcp(tmp_path)
    spec = HighlightSpec(
        sub_spans=[
            SubSpanSpec(
                source_path=str(media),
                source_start_s=2.0,
                source_end_s=8.0,
                reason="opening beat",
            ),
        ],
        reason="highlight reel",
    )
    result = _run(
        propose_highlights(
            ProposeHighlightsRequest(
                json_path=str(doc_path),
                highlights=[spec],
            )
        )
    )
    assert len(result.highlights) == 1
    h = list_highlights_for_document(doc_path)[0]
    assert len(h.sub_spans) == 1
    assert h.sub_spans[0].reason == "opening beat"


def test_propose_highlights_accepts_legacy_single_span(tmp_path):
    from mcp_server.schemas import HighlightSpec, ProposeHighlightsRequest
    from mcp_server.tools.highlights import propose_highlights

    doc_path, media = _doc_pair_for_mcp(tmp_path)
    spec = HighlightSpec(
        source_path=str(media),
        source_start_s=2.0,
        source_end_s=8.0,
        reason="highlight reel",
    )
    result = _run(
        propose_highlights(
            ProposeHighlightsRequest(
                json_path=str(doc_path),
                highlights=[spec],
            )
        )
    )
    assert len(result.highlights) == 1
    h = list_highlights_for_document(doc_path)[0]
    assert len(h.sub_spans) == 1
    assert h.sub_spans[0].source_start == 2.0


def test_propose_highlights_rejects_mixed_forms(tmp_path):
    from mcp.shared.exceptions import McpError

    from mcp_server.schemas import (
        HighlightSpec,
        ProposeHighlightsRequest,
        SubSpanSpec,
    )
    from mcp_server.tools.highlights import propose_highlights

    doc_path, media = _doc_pair_for_mcp(tmp_path)
    spec = HighlightSpec(
        source_path=str(media),
        source_start_s=2.0,
        source_end_s=8.0,
        sub_spans=[
            SubSpanSpec(source_path=str(media), source_start_s=4.0, source_end_s=6.0)
        ],
        reason="highlight reel",
    )
    with pytest.raises(McpError) as exc_info:
        _run(
            propose_highlights(
                ProposeHighlightsRequest(
                    json_path=str(doc_path),
                    highlights=[spec],
                )
            )
        )
    assert "INVALID_HIGHLIGHT" in exc_info.value.error.message


def test_propose_highlights_rejects_unknown_sync_group(tmp_path):
    from mcp.shared.exceptions import McpError

    from mcp_server.schemas import (
        HighlightSpec,
        ProposeHighlightsRequest,
        SubSpanSpec,
    )
    from mcp_server.tools.highlights import propose_highlights

    doc_path, media = _doc_pair_for_mcp(tmp_path)
    spec = HighlightSpec(
        sub_spans=[
            SubSpanSpec(source_path=str(media), source_start_s=2.0, source_end_s=8.0)
        ],
        reason="highlight reel",
        sync_group_id="nonexistent-sync-group",
    )
    with pytest.raises(McpError) as exc_info:
        _run(
            propose_highlights(
                ProposeHighlightsRequest(
                    json_path=str(doc_path),
                    highlights=[spec],
                )
            )
        )
    assert "SYNC_GROUP_NOT_FOUND" in exc_info.value.error.message


@pytest.mark.slow
def test_create_sync_group_mcp_tool(multicam_project, tmp_path):
    from mcp_server.schemas import (
        CreateSyncGroupRequest,
        ListSyncGroupsRequest,
        ReadSyncGroupRequest,
        SetSyncOffsetRequest,
    )
    from mcp_server.tools.sync import (
        create_sync_group,
        list_sync_groups,
        read_sync_group,
        set_sync_offset,
    )

    parent = tmp_path / "doc.transcribe.json"
    master = multicam_project["audio_master"]
    src = MediaSource(id="src0", path=master, duration=30.0)
    doc = Document(
        sources={"src0": src},
        segments=[],
        ranges=[Range(source_id="src0", start=0.0, end=30.0)],
        language="en",
        created_at=datetime(2026, 4, 30, tzinfo=UTC),
    )
    _write_doc(doc, parent)

    result = _run(
        create_sync_group(
            CreateSyncGroupRequest(
                json_path=str(parent),
                audio_master_path=str(master),
                camera_paths=[str(p) for p in multicam_project["cameras"]],
                description="multicam smoke",
                max_lag_s=2.0,
                search_window_s=15.0,
            )
        )
    )
    assert result.sync_group_id != ""
    assert len(result.cameras) == 3

    # list
    listing = _run(list_sync_groups(ListSyncGroupsRequest(json_path=str(parent))))
    assert len(listing.sync_groups) == 1

    # read
    group_out = _run(
        read_sync_group(
            ReadSyncGroupRequest(json_path=str(parent), sync_group_id=result.sync_group_id)
        )
    )
    assert group_out.sync_group_id == result.sync_group_id

    # set_offset
    cam0_path = str(multicam_project["cameras"][0])
    updated = _run(
        set_sync_offset(
            SetSyncOffsetRequest(
                json_path=str(parent),
                sync_group_id=result.sync_group_id,
                camera_path=cam0_path,
                offset_s=1.234,
            )
        )
    )
    assert updated.camera.manual_override is True
    assert updated.camera.offset_s == 1.234


def test_set_sync_offset_rejects_unknown_camera(tmp_path):
    from mcp.shared.exceptions import McpError

    from core.sync import SyncGroup, SyncSource, write_sync_group
    from mcp_server.schemas import SetSyncOffsetRequest
    from mcp_server.tools.sync import set_sync_offset

    parent = tmp_path / "doc.transcribe.json"
    src = tmp_path / "x.mp4"
    src.write_bytes(b"x")
    doc = _doc(src)
    _write_doc(doc, parent)
    cam = tmp_path / "registered_cam.mp4"
    cam.write_bytes(b"c")
    master = tmp_path / "master.wav"
    master.write_bytes(b"m")
    group = SyncGroup(
        sync_group_id="sg1",
        audio_master_path=master,
        audio_master_hash=cache_key(master),
        cameras={
            str(cam): SyncSource(
                source_path=cam,
                source_hash=cache_key(cam),
                offset_s=0.0,
            )
        },
        created_at=datetime(2026, 4, 30, tzinfo=UTC),
    )
    write_sync_group(parent, group)
    bad_cam = tmp_path / "ghost_cam.mp4"
    bad_cam.write_bytes(b"g")
    with pytest.raises(McpError) as exc_info:
        _run(
            set_sync_offset(
                SetSyncOffsetRequest(
                    json_path=str(parent),
                    sync_group_id="sg1",
                    camera_path=str(bad_cam),
                    offset_s=0.5,
                )
            )
        )
    assert "INVALID_SYNC_GROUP" in exc_info.value.error.message


# Suppress unused-import lint signal — these are imported for the
# tests above but a few are only used inside multicam_project.
_ = (struct,)  # noqa
