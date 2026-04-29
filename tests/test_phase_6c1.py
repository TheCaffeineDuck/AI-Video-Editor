"""Phase 6c-1 — Highlight artifact + reframe / caption render path.

Coverage:

- :class:`core.highlight.Highlight` validation + JSON round-trip.
- Sidecar persistence (``write_highlight`` / ``read_highlight`` /
  ``list_highlights_for_document`` / ``mark_rendered``).
- Reframe math (``compute_speaker_locked_crop`` / ``compute_center_crop``).
- Face detection on a synthetic image (slow, requires mediapipe).
- Caption SRT generation (word grouping, relative timestamps).
- ``render_highlight`` end-to-end on the synthetic fixture (slow).
- Stale-hash guard (now keyed on source ``cache_key`` per Phase 6c
  Section A — intra-doc edits don't invalidate, file replacement does).
- v1 schema refusal (the old ``parent_document_state_hash`` shape).
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.cache import cache_key
from core.document import Document, MediaSource, Range, Segment, UnsupportedSchemaError, Word
from core.highlight import (
    HIGHLIGHT_SCHEMA_VERSION,
    Highlight,
    StaleHighlightError,
    highlights_dir_for_document,
    list_highlights_for_document,
    mark_rendered,
    read_highlight,
    rendered_output_path_for,
    write_highlight,
)
from core.highlight_render import (
    OUTPUT_HEIGHT,
    OUTPUT_WIDTH,
    FaceDetectionError,
    build_caption_srt,
    compute_center_crop,
    compute_speaker_locked_crop,
    detect_speaker_bbox,
    render_highlight,
    words_in_span,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FFMPEG = REPO_ROOT / "resources" / "bin" / "ffmpeg-mac"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _doc(media: Path, duration: float = 30.0) -> Document:
    src = MediaSource(id="src0", path=media, duration=duration)
    # Build evenly-spaced words across the whole duration so render-time
    # word-boundary snapping has anchors near the highlight span.
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
        created_at=datetime(2026, 4, 29, 10, 0, 0, tzinfo=UTC),
        model_name="tiny",
    )


def _make_highlight(
    parent_path: Path,
    parent_doc: Document,  # noqa: ARG001 — kept for caller compat; hash now keyed on source file
    media: Path,
    *,
    start: float = 5.0,
    end: float = 35.0,
    reframe_mode: str = "speaker_locked",
    captions_enabled: bool = False,
) -> Highlight:
    return Highlight(
        highlight_id="20260429T100000-deadbeef",
        created_at=datetime(2026, 4, 29, 10, 0, 0, tzinfo=UTC),
        parent_document_path=parent_path,
        parent_source_hash=cache_key(media),
        span_source_path=media,
        span_source_start=start,
        span_source_end=end,
        reason="highlight reel",
        reframe_mode=reframe_mode,  # type: ignore[arg-type]
        captions_enabled=captions_enabled,
    )


# ---------------------------------------------------------------------------
# Dataclass + JSON round-trip
# ---------------------------------------------------------------------------


def test_highlight_round_trips_through_json(tmp_path):
    media = tmp_path / "movie.mp4"
    media.touch()
    parent_path = tmp_path / "movie.transcribe.json"
    doc = _doc(media)
    h = _make_highlight(parent_path, doc, media)
    payload = h.to_json()
    text = json.dumps(payload, indent=2)
    re_read = Highlight.from_json(json.loads(text))
    assert re_read == h
    assert payload["schema_version"] == HIGHLIGHT_SCHEMA_VERSION


def test_highlight_round_trip_preserves_full_precision(tmp_path):
    media = tmp_path / "movie.mp4"
    media.touch()
    parent_path = tmp_path / "movie.transcribe.json"
    h = Highlight(
        highlight_id="abc",
        created_at=datetime(2026, 4, 29, 10, 0, 0, tzinfo=UTC),
        parent_document_path=parent_path,
        parent_source_hash=cache_key(media),
        span_source_path=media,
        span_source_start=12.345_678_9,
        span_source_end=42.987_654_321,
        reason="highlight: emotional peak",
        reframe_mode="center",
        captions_enabled=True,
    )
    re_read = Highlight.from_json(json.loads(json.dumps(h.to_json())))
    assert re_read.span_source_start == 12.345_678_9
    assert re_read.span_source_end == 42.987_654_321


def test_highlight_validates_reason(tmp_path):
    media = tmp_path / "movie.mp4"
    media.touch()
    parent = tmp_path / "movie.transcribe.json"
    with pytest.raises(ValueError, match="not a valid rationale"):
        Highlight(
            highlight_id="abc",
            created_at=datetime(2026, 4, 29, tzinfo=UTC),
            parent_document_path=parent,
            parent_source_hash=cache_key(media),
            span_source_path=media,
            span_source_start=0.0,
            span_source_end=10.0,
            reason="x",  # too short, not a category
        )


def test_highlight_rejects_zero_or_inverted_span(tmp_path):
    media = tmp_path / "movie.mp4"
    media.touch()
    parent = tmp_path / "movie.transcribe.json"
    with pytest.raises(ValueError, match="positive duration"):
        Highlight(
            highlight_id="abc",
            created_at=datetime(2026, 4, 29, tzinfo=UTC),
            parent_document_path=parent,
            parent_source_hash=cache_key(media),
            span_source_path=media,
            span_source_start=10.0,
            span_source_end=10.0,
            reason="highlight reel",
        )


def test_highlight_rejects_unknown_reframe_mode(tmp_path):
    media = tmp_path / "movie.mp4"
    media.touch()
    parent = tmp_path / "movie.transcribe.json"
    with pytest.raises(ValueError, match="reframe_mode"):
        Highlight(
            highlight_id="abc",
            created_at=datetime(2026, 4, 29, tzinfo=UTC),
            parent_document_path=parent,
            parent_source_hash=cache_key(media),
            span_source_path=media,
            span_source_start=0.0,
            span_source_end=10.0,
            reason="highlight reel",
            reframe_mode="dynamic_track",  # type: ignore[arg-type]
        )


def test_highlight_from_json_rejects_unknown_schema_version(tmp_path):
    payload = {
        "schema_version": 99,
        "highlight_id": "abc",
        "created_at": "2026-04-29T10:00:00+00:00",
        "parent_document_path": str(tmp_path / "p.json"),
        "parent_source_hash": "deadbeef",
        "span_source_path": str(tmp_path / "m.mp4"),
        "span_source_start": 0.0,
        "span_source_end": 10.0,
        "reason": "highlight reel",
        "reframe_mode": "center",
        "captions_enabled": False,
        "rendered_output_path": None,
    }
    with pytest.raises(UnsupportedSchemaError, match="schema_version"):
        Highlight.from_json(payload)


def test_highlight_from_json_rejects_v1_schema(tmp_path):
    """Phase 6c Section A — v1 highlights stored ``parent_document_state_hash``,
    which incorrectly invalidated source-time spans on intra-doc edits.
    The reader must refuse v1 with a remediation message; the user
    re-proposes against the current source to get a v2 record."""
    v1_payload = {
        "schema_version": 1,
        "highlight_id": "abc",
        "created_at": "2026-04-29T10:00:00+00:00",
        "parent_document_path": str(tmp_path / "p.json"),
        "parent_document_state_hash": "deadbeef",
        "span_source_path": str(tmp_path / "m.mp4"),
        "span_source_start": 0.0,
        "span_source_end": 10.0,
        "reason": "highlight reel",
        "reframe_mode": "center",
        "captions_enabled": False,
        "rendered_output_path": None,
    }
    with pytest.raises(UnsupportedSchemaError, match="v1"):
        Highlight.from_json(v1_payload)


# ---------------------------------------------------------------------------
# Sidecar persistence
# ---------------------------------------------------------------------------


def test_highlights_dir_layout(tmp_path):
    parent = tmp_path / "movie.transcribe.json"
    d = highlights_dir_for_document(parent)
    assert d.name == "movie.transcribe.json.highlights"


def test_write_read_list_round_trip(tmp_path):
    media = tmp_path / "movie.mp4"
    media.touch()
    parent_path = tmp_path / "movie.transcribe.json"
    doc = _doc(media)
    h = _make_highlight(parent_path, doc, media)
    materialized, path = write_highlight(parent_path, h)
    assert path.exists()
    assert materialized.highlight_id == h.highlight_id
    again = read_highlight(parent_path, h.highlight_id)
    assert again == materialized
    listing = list_highlights_for_document(parent_path)
    assert listing == [materialized]


def test_list_returns_empty_when_dir_missing(tmp_path):
    parent_path = tmp_path / "movie.transcribe.json"
    assert list_highlights_for_document(parent_path) == []


def test_mark_rendered_writes_back_path(tmp_path):
    media = tmp_path / "movie.mp4"
    media.touch()
    parent_path = tmp_path / "movie.transcribe.json"
    doc = _doc(media)
    h = _make_highlight(parent_path, doc, media)
    write_highlight(parent_path, h)
    out = rendered_output_path_for(highlights_dir_for_document(parent_path), h.highlight_id)
    out.touch()
    updated = mark_rendered(parent_path, h, out)
    assert updated.rendered_output_path == out
    re_read = read_highlight(parent_path, h.highlight_id)
    assert re_read.rendered_output_path == out


def test_write_assigns_id_when_blank(tmp_path):
    media = tmp_path / "movie.mp4"
    media.touch()
    parent_path = tmp_path / "movie.transcribe.json"
    h = Highlight(
        highlight_id="",  # caller leaves blank
        created_at=datetime(2026, 4, 29, 10, 0, 0, tzinfo=UTC),
        parent_document_path=parent_path,
        parent_source_hash=cache_key(media),
        span_source_path=media,
        span_source_start=0.0,
        span_source_end=10.0,
        reason="highlight reel",
    )
    materialized, _ = write_highlight(parent_path, h)
    assert materialized.highlight_id != ""
    assert materialized.highlight_id.count("-") == 1


# ---------------------------------------------------------------------------
# Reframe math
# ---------------------------------------------------------------------------


def test_center_crop_dimensions_for_16_9_source():
    crop_w, crop_h, crop_x, crop_y = compute_center_crop(1920, 1080)
    # Full source height; 9:16 aspect => 607 or 608.
    assert crop_h == 1080
    assert abs(crop_w - int(round(1080 * 9 / 16))) <= 1
    assert crop_x == (1920 - crop_w) // 2
    assert crop_y == 0


def test_speaker_locked_crop_centers_on_face():
    # 1920x1080 source; face at x=1200..1400, y=300..600 (face center 1300, 450)
    face = (1200.0, 300.0, 200.0, 300.0)
    crop_w, crop_h, crop_x, crop_y = compute_speaker_locked_crop(1920, 1080, face)
    assert crop_h == 1080
    # face center (1300) should sit near crop_w/2 of the crop
    face_cx = 1200.0 + 200.0 / 2.0
    assert abs((crop_x + crop_w / 2.0) - face_cx) <= 1.0


def test_speaker_locked_crop_clamps_to_source_bounds():
    # Face near right edge — crop must clamp so it doesn't slip off.
    face = (1850.0, 300.0, 60.0, 80.0)
    crop_w, crop_h, crop_x, crop_y = compute_speaker_locked_crop(1920, 1080, face)
    assert crop_x + crop_w <= 1920
    assert crop_x >= 0


def test_speaker_locked_crop_clamps_left_edge():
    face = (5.0, 300.0, 60.0, 80.0)
    crop_w, crop_h, crop_x, crop_y = compute_speaker_locked_crop(1920, 1080, face)
    assert crop_x == 0
    assert crop_y == 0


# ---------------------------------------------------------------------------
# Caption SRT generation
# ---------------------------------------------------------------------------


def _doc_with_words(media: Path, words: list[tuple[str, float, float]]) -> Document:
    src = MediaSource(id="src0", path=media, duration=60.0)
    seg = Segment(
        text=" ".join(w[0] for w in words),
        start=words[0][1] if words else 0.0,
        end=words[-1][2] if words else 0.0,
        words=tuple(Word(text=t, start=s, end=e) for t, s, e in words),
    )
    return Document(
        sources={"src0": src},
        segments=[seg],
        ranges=[Range(source_id="src0", start=0.0, end=60.0, reason="manual")],
        language="en",
        created_at=datetime(2026, 4, 29, tzinfo=UTC),
        model_name="tiny",
    )


def test_words_in_span_overlap_convention(tmp_path):
    media = tmp_path / "m.mp4"
    media.touch()
    doc = _doc_with_words(
        media,
        [
            ("a", 0.0, 1.0),
            ("b", 1.5, 2.5),
            ("c", 5.0, 6.0),
            ("d", 6.0, 7.0),
        ],
    )
    out = words_in_span(doc, 1.4, 6.0)
    assert [w[0] for w in out] == ["b", "c"]


def test_caption_srt_groups_on_pause(tmp_path):
    media = tmp_path / "m.mp4"
    media.touch()
    # 6 words; a 0.5s gap between word 3 and 4 should split into 2 lines.
    doc = _doc_with_words(
        media,
        [
            ("hello", 10.00, 10.30),
            ("there", 10.35, 10.70),
            ("friend", 10.75, 11.10),
            ("how", 11.80, 12.00),
            ("are", 12.05, 12.20),
            ("you", 12.25, 12.50),
        ],
    )
    srt = build_caption_srt(doc, span_start=10.0, span_end=13.0)
    # Expect 2 cues.
    cues = [b for b in srt.split("\n\n") if b.strip()]
    assert len(cues) == 2, srt
    # Timestamps are relative — first cue starts at 0.0, not 10.0.
    assert "00:00:00,000" in cues[0]
    # Second line text contains "how are you".
    assert "how are you" in cues[1]


def test_caption_srt_max_words_per_line(tmp_path):
    media = tmp_path / "m.mp4"
    media.touch()
    # 7 contiguous words (no pauses) — max 5 per line means 2 lines (5 + 2).
    words = [(f"w{i}", float(i) * 0.2, float(i) * 0.2 + 0.18) for i in range(7)]
    doc = _doc_with_words(media, words)
    srt = build_caption_srt(doc, span_start=0.0, span_end=2.0)
    cues = [b for b in srt.split("\n\n") if b.strip()]
    assert len(cues) == 2


def test_caption_srt_empty_when_no_words(tmp_path):
    media = tmp_path / "m.mp4"
    media.touch()
    doc = _doc_with_words(media, [("alone", 0.0, 0.5)])
    assert build_caption_srt(doc, span_start=10.0, span_end=12.0) == ""


# ---------------------------------------------------------------------------
# Stale-hash guard
# ---------------------------------------------------------------------------


def test_render_raises_on_stale_hash(tmp_path, monkeypatch):
    """Source-file replacement (path+mtime+size change) invalidates the
    highlight. Intra-doc edits do *not*, because source-time spans are
    coordinates in the source — they're stable across timeline mutations.
    """
    media = tmp_path / "m.mp4"
    media.touch()
    parent_path = tmp_path / "doc.transcribe.json"
    doc = _doc(media)
    # Mint a highlight with a deliberately wrong source hash to simulate
    # the source file having been replaced since authoring time.
    h = Highlight(
        highlight_id="20260429T100000-deadbeef",
        created_at=datetime(2026, 4, 29, 10, 0, 0, tzinfo=UTC),
        parent_document_path=parent_path,
        parent_source_hash="not-the-real-hash",
        span_source_path=media,
        span_source_start=5.0,
        span_source_end=10.0,
        reason="highlight reel",
    )
    with pytest.raises(StaleHighlightError, match="parent_source_hash"):
        render_highlight(h, doc)


def test_render_does_not_invalidate_on_intra_doc_edit(tmp_path):
    """Phase 6c Section A — the Highlight stale-guard hashes the SOURCE
    FILE (cache_key), not the Document state. So an unrelated cut to
    the parent doc must NOT make the highlight stale; only file
    replacement at the OS layer does. We verify by minting a fresh
    highlight against the live source hash, then *modifying the doc*,
    and confirming the stale-guard would still pass.
    """
    media = tmp_path / "m.mp4"
    media.touch()
    parent_path = tmp_path / "doc.transcribe.json"
    doc = _doc(media)
    h = _make_highlight(parent_path, doc, media, start=5.0, end=10.0)
    # Drift the doc (simulates an unrelated cut on the parent timeline).
    drifted = Document(
        sources=doc.sources,
        segments=doc.segments,
        ranges=[Range(source_id="src0", start=0.0, end=15.0, reason="trim")],
        language=doc.language,
        created_at=doc.created_at,
        model_name=doc.model_name,
    )
    # The stale-guard reads ``cache_key(span_source_path)`` and compares
    # against ``parent_source_hash``. Both equal because the source file
    # is unchanged. ``render_highlight`` would raise StaleHighlightError
    # at the guard — assert it does not (it'll fail downstream because
    # ffmpeg can't read the empty media file, but past the guard).
    assert cache_key(h.span_source_path) == h.parent_source_hash
    # Render past the guard fails on ffmpeg input; that's not what we're
    # testing. The contract here is "does not raise StaleHighlightError"
    # — any other exception is fine.
    with pytest.raises(Exception) as exc_info:  # noqa: PT011 — see comment
        render_highlight(h, drifted)
    assert not isinstance(exc_info.value, StaleHighlightError)


# ---------------------------------------------------------------------------
# Slow integration tests: real face detection, real render
# ---------------------------------------------------------------------------


def _ffprobe() -> Path:
    for cand in (
        Path("/opt/homebrew/bin/ffprobe"),
        Path("/usr/local/bin/ffprobe"),
        REPO_ROOT / "resources" / "bin" / "ffprobe-mac",
    ):
        if cand.is_file():
            return cand
    pytest.skip("ffprobe not available")


def _probe_dimensions(path: Path) -> tuple[int, int]:
    out = subprocess.run(
        [
            str(_ffprobe()),
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


def _probe_duration(path: Path) -> float:
    out = subprocess.run(
        [
            str(_ffprobe()),
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


@pytest.fixture(scope="module")
def face_image(tmp_path_factory) -> Path:
    """Render a synthetic 1920×1080 frame with a clear high-contrast face shape.

    MediaPipe's face detection is robust enough that a stylized face
    (skin-tone ellipse + eye/mouth circles) usually triggers it. If
    not, the test that depends on this fixture will skip, surfacing as
    a soft signal rather than a hard failure.
    """
    out = tmp_path_factory.mktemp("face") / "face.png"
    if not FFMPEG.exists():
        pytest.skip("ffmpeg not available")
    cmd = [
        str(FFMPEG), "-y", "-loglevel", "error",
        "-f", "lavfi",
        "-i",
        # Skin-tone background, head ellipse-ish via geq, eyes and mouth.
        "color=c=0x202020:size=1920x1080:duration=1,"
        "drawbox=x=1200:y=240:w=400:h=520:color=0xc89070:t=fill,"
        "drawbox=x=1290:y=400:w=40:h=30:color=black:t=fill,"
        "drawbox=x=1470:y=400:w=40:h=30:color=black:t=fill,"
        "drawbox=x=1330:y=600:w=160:h=24:color=0x401010:t=fill",
        "-frames:v", "1",
        str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return out


@pytest.mark.slow
def test_face_detection_finds_synthetic_face(face_image):
    try:
        x, y, w, h = detect_speaker_bbox(face_image)
    except FaceDetectionError:
        pytest.skip("MediaPipe did not detect the synthetic face shape")
    # Synthetic face is at x≈1200..1600, y≈240..760
    cx = x + w / 2.0
    cy = y + h / 2.0
    assert 1100 < cx < 1700, f"face center x={cx}"
    assert 200 < cy < 900, f"face center y={cy}"


@pytest.mark.slow
def test_render_highlight_center_mode(synthetic_video, tmp_path):
    media = synthetic_video
    parent_path = tmp_path / "synth.transcribe.json"
    doc = _doc(media, duration=_probe_duration(media))
    h = _make_highlight(
        parent_path, doc, media, start=2.0, end=8.0, reframe_mode="center"
    )
    write_highlight(parent_path, h)

    metadata = render_highlight(h, doc)
    out = metadata.output_path
    assert out.exists()
    assert metadata.face_detection_used == "center"
    w, hgt = _probe_dimensions(out)
    assert (w, hgt) == (OUTPUT_WIDTH, OUTPUT_HEIGHT)
    dur = _probe_duration(out)
    # Span is 6s; word-boundary outward-snap + 100ms pad on each side
    # widens it by up to ~1.5s on this synthetic fixture.
    assert 5.5 < dur < 8.0, f"unexpected duration {dur}"


@pytest.mark.slow
def test_render_highlight_speaker_locked_falls_back_when_no_face(
    synthetic_video, tmp_path, caplog
):
    """Synthetic ffmpeg testsrc has no face — speaker_locked must fall back to center
    silently and still produce a 1080×1920 file."""
    media = synthetic_video
    parent_path = tmp_path / "synth.transcribe.json"
    doc = _doc(media, duration=_probe_duration(media))
    h = _make_highlight(
        parent_path, doc, media, start=2.0, end=6.0,
        reframe_mode="speaker_locked",
    )
    write_highlight(parent_path, h)
    with caplog.at_level("WARNING"):
        metadata = render_highlight(h, doc)
    out = metadata.output_path
    assert out.exists()
    assert metadata.face_detection_used == "speaker_locked_fallback_to_center"
    w, hgt = _probe_dimensions(out)
    assert (w, hgt) == (OUTPUT_WIDTH, OUTPUT_HEIGHT)
    # Log carries the fallback note.
    assert any("speaker-lock fallback" in rec.message for rec in caplog.records)


@pytest.mark.slow
def test_render_highlight_with_captions(synthetic_video, tmp_path):
    media = synthetic_video
    parent_path = tmp_path / "synth.transcribe.json"
    doc = _doc(media, duration=_probe_duration(media))
    # Override segments so words land inside the highlight span.
    doc_with_words = Document(
        sources=doc.sources,
        segments=[
            Segment(
                text="this is a captioned highlight test",
                start=2.0, end=7.0,
                words=tuple(
                    Word(text=t, start=2.0 + i * 0.6, end=2.0 + i * 0.6 + 0.5)
                    for i, t in enumerate(
                        ["this", "is", "a", "captioned", "highlight", "test"]
                    )
                ),
            )
        ],
        ranges=doc.ranges,
        language=doc.language,
        created_at=doc.created_at,
        model_name=doc.model_name,
    )
    h = _make_highlight(
        parent_path, doc_with_words, media,
        start=2.0, end=7.5,
        reframe_mode="center",
        captions_enabled=True,
    )
    write_highlight(parent_path, h)
    metadata = render_highlight(h, doc_with_words)
    out = metadata.output_path
    assert out.exists()
    w, hgt = _probe_dimensions(out)
    assert (w, hgt) == (OUTPUT_WIDTH, OUTPUT_HEIGHT)
    # Sidecar should now point to the rendered file.
    re_read = read_highlight(parent_path, h.highlight_id)
    assert re_read.rendered_output_path == out
