"""Tests for ``core.document`` — Word, Segment, MediaSource, Range, Document, JSON I/O.

Phase 4f-3 reshaped the live Document model to schema v2: ``sources`` /
``ranges`` instead of v1's ``media_path`` / ``duration`` / ``cuts``.
The on-disk v1 format still loads via the migration in
:meth:`Document.from_json`, exercised below.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.document import (
    CutMark,
    Document,
    MediaSource,
    Range,
    Segment,
    UnsupportedSchemaError,
    Word,
    build_document,
)

# ---------------------------------------------------------------------------
# Word / Segment
# ---------------------------------------------------------------------------


def test_word_is_frozen():
    w = Word(text="hello", start=0.0, end=0.5, probability=0.9)
    assert w.text == "hello"
    assert w.start == 0.0
    assert w.end == 0.5
    assert w.probability == 0.9
    with pytest.raises(dataclasses.FrozenInstanceError):
        w.text = "bye"  # type: ignore[misc]


def test_word_probability_optional():
    w = Word(text="x", start=0.0, end=0.1)
    assert w.probability is None


def test_segment_is_frozen():
    s = Segment(text="hi", start=0.0, end=1.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.text = "bye"  # type: ignore[misc]


def test_segment_words_default_empty():
    s = Segment(text="hi", start=0.0, end=1.0)
    assert s.words == ()


def test_segment_with_words():
    words = (
        Word(text="hi", start=0.0, end=0.4, probability=0.95),
        Word(text="there", start=0.4, end=0.9, probability=0.92),
    )
    s = Segment(text="hi there", start=0.0, end=0.9, words=words)
    assert s.words == words
    assert isinstance(s.words, tuple)


def test_segment_is_hashable():
    s = Segment(text="a", start=0.0, end=1.0, words=(Word("a", 0.0, 1.0),))
    assert hash(s) == hash(s)
    assert s in {s}


# ---------------------------------------------------------------------------
# MediaSource / Range / CutMark
# ---------------------------------------------------------------------------


def test_media_source_is_frozen():
    src = MediaSource(id="src0", path=Path("/tmp/x.wav"), duration=5.0, hash="abc")
    with pytest.raises(dataclasses.FrozenInstanceError):
        src.path = Path("/tmp/y.wav")  # type: ignore[misc]


def test_media_source_default_hash_empty():
    src = MediaSource(id="src0", path=Path("/tmp/x.wav"), duration=5.0)
    assert src.hash == ""


def test_range_is_frozen():
    r = Range(source_id="src0", start=1.0, end=2.0, reason="filler")
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.start = 0.0  # type: ignore[misc]


def test_range_default_reason_empty():
    r = Range(source_id="src0", start=1.0, end=2.0)
    assert r.reason == ""


def test_cutmark_still_constructible_for_migration():
    """CutMark is retired from live use but kept as the v1 migration intermediate."""
    c = CutMark(start=1.0, end=2.0, reason="filler")
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.reason = "manual"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Document — basic v2 construction
# ---------------------------------------------------------------------------


def _example_doc(**overrides) -> Document:
    """Helper: a minimal v2 Document for round-trip tests."""
    src = MediaSource(
        id="src0", path=Path("/tmp/example.wav"), duration=4.5, hash="hashabc"
    )
    defaults = dict(
        sources={"src0": src},
        segments=[
            Segment(
                text="Hello",
                start=0.0,
                end=1.5,
                words=(Word("Hello", 0.0, 1.5, 0.95),),
            ),
            Segment(text="world", start=1.5, end=3.0, words=()),
        ],
        ranges=[
            Range(source_id="src0", start=0.0, end=2.0),
            Range(source_id="src0", start=2.5, end=4.5, reason="filler"),
        ],
        language="en",
        created_at=datetime(2026, 4, 26, 10, 0, 0, tzinfo=UTC),
        model_name="tiny",
    )
    defaults.update(overrides)
    return Document(**defaults)


def test_document_schema_version_constant():
    # Phase 6a bumped to v3 (Clip/Timeline shape on disk; in-memory
    # ``ranges`` field still drives state).
    assert Document.SCHEMA_VERSION == 3


# ---------------------------------------------------------------------------
# Document.to_json / from_json — v3 round-trip
# ---------------------------------------------------------------------------


def test_document_to_json_emits_schema_version_3():
    data = _example_doc().to_json()
    assert data["schema_version"] == 3
    assert "sources" in data
    assert "main_timeline" in data
    assert "clips" in data["main_timeline"]
    assert "ranges" not in data  # v2 key gone
    assert "cuts" not in data
    assert "media_path" not in data
    assert "duration" not in data


def test_document_to_json_is_json_serializable():
    data = _example_doc().to_json()
    parsed = json.loads(json.dumps(data))
    assert parsed["schema_version"] == 3
    assert parsed["language"] == "en"


def test_document_round_trip_preserves_content():
    doc = _example_doc()
    restored = Document.from_json(json.loads(json.dumps(doc.to_json())))
    assert restored == doc


def test_document_round_trip_preserves_words():
    doc = _example_doc()
    restored = Document.from_json(doc.to_json())
    assert restored.segments[0].words == doc.segments[0].words
    assert restored.segments[1].words == ()


def test_document_round_trip_preserves_ranges():
    doc = _example_doc()
    restored = Document.from_json(doc.to_json())
    assert restored.ranges == doc.ranges


def test_document_round_trip_preserves_sources():
    doc = _example_doc()
    restored = Document.from_json(doc.to_json())
    assert restored.sources == doc.sources


def test_document_round_trip_with_no_words_or_ranges():
    src = MediaSource(id="src0", path=Path("/tmp/x.mp4"), duration=10.0)
    doc = Document(
        sources={"src0": src},
        segments=[Segment(text="hi", start=0.0, end=1.0)],
        ranges=[],
        language=None,
        created_at=datetime(2026, 4, 26, tzinfo=UTC),
        model_name="base",
    )
    restored = Document.from_json(doc.to_json())
    assert restored == doc


# ---------------------------------------------------------------------------
# Document.from_json — schema version error paths
# ---------------------------------------------------------------------------


def _v2_payload(**override) -> dict:
    """Valid v2 payload skeleton."""
    payload = {
        "schema_version": 2,
        "sources": {
            "src0": {
                "id": "src0",
                "path": "/tmp/x.wav",
                "duration": 1.0,
                "hash": "",
            }
        },
        "language": "en",
        "model_name": "tiny",
        "created_at": "2026-04-26T10:00:00+00:00",
        "segments": [],
        "ranges": [],
    }
    payload.update(override)
    return payload


def test_from_json_missing_schema_version_raises():
    payload = _v2_payload()
    del payload["schema_version"]
    with pytest.raises(UnsupportedSchemaError) as excinfo:
        Document.from_json(payload)
    msg = str(excinfo.value)
    assert "schema_version" in msg
    assert "3" in msg  # v3-aware build's expected schema


def test_from_json_null_schema_version_raises():
    payload = _v2_payload(schema_version=None)
    with pytest.raises(UnsupportedSchemaError) as excinfo:
        Document.from_json(payload)
    msg = str(excinfo.value)
    assert "null" in msg.lower()


def test_from_json_unknown_schema_version_raises():
    payload = _v2_payload(schema_version=999)
    with pytest.raises(UnsupportedSchemaError) as excinfo:
        Document.from_json(payload)
    assert "999" in str(excinfo.value)


def test_unsupported_schema_error_is_value_error_subclass():
    assert issubclass(UnsupportedSchemaError, ValueError)


# ---------------------------------------------------------------------------
# v1 → v2 migration
# ---------------------------------------------------------------------------


def _v1_payload(*, cuts: list[dict] | None = None, **override) -> dict:
    """Build a v1 JSON payload (the pre-4f-3 format) for migration tests."""
    payload = {
        "schema_version": 1,
        "media_path": "/tmp/example.wav",
        "duration": 10.0,
        "language": "en",
        "model_name": "tiny",
        "created_at": "2026-04-26T10:00:00+00:00",
        "segments": [],
        "cuts": cuts if cuts is not None else [],
    }
    payload.update(override)
    return payload


def test_migration_empty_cuts_yields_single_full_range():
    doc = Document.from_json(_v1_payload())
    # v1 → v2 → v3 chain; the in-memory build version is v3.
    assert doc.SCHEMA_VERSION == 3
    assert list(doc.sources.keys()) == ["src0"]
    assert doc.sources["src0"].path == Path("/tmp/example.wav")
    assert doc.sources["src0"].duration == 10.0
    assert doc.ranges == [Range(source_id="src0", start=0.0, end=10.0)]


def test_migration_single_middle_cut_yields_two_ranges():
    doc = Document.from_json(
        _v1_payload(cuts=[{"start": 4.0, "end": 6.0, "reason": "filler"}])
    )
    # Two ranges; the cut's reason attaches to the preceding range (0..4).
    assert doc.ranges == [
        Range(source_id="src0", start=0.0, end=4.0, reason="filler"),
        Range(source_id="src0", start=6.0, end=10.0),
    ]


def test_migration_cut_at_start_attaches_reason_to_following_range():
    """A cut with start == 0.0 has no preceding range; the reason must
    fall through to the immediately following range. This is the
    timestamp-0.0 edge case the spec flags as needing an explicit test."""
    doc = Document.from_json(
        _v1_payload(cuts=[{"start": 0.0, "end": 2.0, "reason": "intro-trim"}])
    )
    assert doc.ranges == [
        Range(source_id="src0", start=2.0, end=10.0, reason="intro-trim"),
    ]


def test_migration_cut_at_end_attaches_reason_to_preceding_range():
    doc = Document.from_json(
        _v1_payload(cuts=[{"start": 8.0, "end": 10.0, "reason": "outro"}])
    )
    assert doc.ranges == [
        Range(source_id="src0", start=0.0, end=8.0, reason="outro"),
    ]


def test_migration_two_adjacent_cuts_handled_correctly():
    """Two cuts that produce a merged removal: (3,4) and (4,5) wipe (3,5)."""
    doc = Document.from_json(
        _v1_payload(
            cuts=[
                {"start": 3.0, "end": 4.0, "reason": "first"},
                {"start": 4.0, "end": 5.0, "reason": "second"},
            ]
        )
    )
    # After cut 1: ranges = [(0, 3, "first"), (4, 10)]. Subtracting (4, 5)
    # truncates the second range to (5, 10). For cut 2 the only adjacent
    # range is (5, 10) (start == cut.end == 5) — preceding lookup fails
    # because no surviving range ends exactly at 4. So "second" attaches
    # to the following range.
    assert doc.ranges == [
        Range(source_id="src0", start=0.0, end=3.0, reason="first"),
        Range(source_id="src0", start=5.0, end=10.0, reason="second"),
    ]


def test_migration_full_duration_cut_drops_reason_with_warning(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="core.document"):
        doc = Document.from_json(
            _v1_payload(cuts=[{"start": 0.0, "end": 10.0, "reason": "wholly-removed"}])
        )
    assert doc.ranges == []
    # The reason is dropped; warning surfaces it for the operator.
    assert any("wholly-removed" in rec.getMessage() for rec in caplog.records)


def test_migration_cuts_arrive_unsorted_still_correct():
    """Cuts in reverse order should produce the same ranges as forward."""
    forward = Document.from_json(
        _v1_payload(
            cuts=[
                {"start": 1.0, "end": 2.0, "reason": "a"},
                {"start": 5.0, "end": 6.0, "reason": "b"},
            ]
        )
    )
    reversed_doc = Document.from_json(
        _v1_payload(
            cuts=[
                {"start": 5.0, "end": 6.0, "reason": "b"},
                {"start": 1.0, "end": 2.0, "reason": "a"},
            ]
        )
    )
    assert forward.ranges == reversed_doc.ranges


def test_migration_pre_4f2_v1_with_no_source_hash_loads_with_empty_hash():
    payload = _v1_payload()
    # Pre-4f-2 v1 files have no source_hash field at all.
    assert "source_hash" not in payload
    doc = Document.from_json(payload)
    assert doc.sources["src0"].hash == ""
    assert doc.source_hash is None


def test_migration_v1_with_source_hash_propagates_to_source_and_doc():
    payload = _v1_payload(source_hash="x" * 64)
    doc = Document.from_json(payload)
    assert doc.sources["src0"].hash == "x" * 64
    assert doc.source_hash == "x" * 64


def test_migration_does_not_mutate_disk_format_assumption():
    """The Document object returned is v2 in memory; ``to_json()`` will
    emit v2 — but the migration itself doesn't touch the input dict."""
    payload = _v1_payload(cuts=[{"start": 1.0, "end": 2.0}])
    snapshot = json.dumps(payload, sort_keys=True)
    Document.from_json(payload)
    # Migration must not mutate the caller's dict.
    assert json.dumps(payload, sort_keys=True) == snapshot


def test_migration_segments_carry_through_unchanged():
    payload = _v1_payload(
        segments=[
            {
                "text": "hi",
                "start": 0.0,
                "end": 1.0,
                "words": [{"text": "hi", "start": 0.0, "end": 1.0, "probability": 0.9}],
            }
        ]
    )
    doc = Document.from_json(payload)
    assert len(doc.segments) == 1
    assert doc.segments[0].text == "hi"
    assert doc.segments[0].words[0].text == "hi"


# ---------------------------------------------------------------------------
# build_document
# ---------------------------------------------------------------------------


def test_build_document_populates_all_fields():
    segments = [
        Segment(
            text=" hi",
            start=0.0,
            end=1.0,
            words=(Word(" hi", 0.0, 1.0, 0.92),),
        ),
    ]
    before = datetime.now(UTC)
    doc = build_document(
        media_path=Path("/tmp/sample.wav"),
        duration=6.10,
        language="en",
        segments=segments,
        model_name="tiny",
    )
    after = datetime.now(UTC)

    src = doc.sources["src0"]
    assert src.path == Path("/tmp/sample.wav")
    assert src.duration == 6.10
    assert src.hash == ""
    assert doc.language == "en"
    assert list(doc.segments) == segments
    assert doc.model_name == "tiny"
    # Initial timeline is one full-duration keep-range.
    assert doc.ranges == [Range(source_id="src0", start=0.0, end=6.10)]
    assert before <= doc.created_at <= after
    assert doc.created_at.tzinfo is not None
    assert doc.created_at.utcoffset().total_seconds() == 0.0


def test_build_document_serializes_with_schema_version():
    doc = build_document(
        media_path=Path("/tmp/x.wav"),
        duration=1.0,
        language=None,
        segments=[Segment("x", 0.0, 1.0)],
        model_name="base",
    )
    data = doc.to_json()
    assert data["schema_version"] == 3
    parsed = json.loads(json.dumps(data))
    assert parsed["schema_version"] == 3
    restored = Document.from_json(parsed)
    assert restored.created_at.utcoffset().total_seconds() == 0.0


def test_build_document_accepts_iterable_segments():
    def seg_iter():
        yield Segment("a", 0.0, 1.0)
        yield Segment("b", 1.0, 2.0)

    doc = build_document(
        media_path=Path("/tmp/x.wav"),
        duration=2.0,
        language=None,
        segments=seg_iter(),
        model_name="tiny",
    )
    assert [s.text for s in doc.segments] == ["a", "b"]


def test_build_document_default_initial_range_serialized():
    """The serialized form has a single full-duration keep-range as a v3 clip."""
    doc = build_document(
        media_path=Path("/tmp/x.wav"),
        duration=5.0,
        language=None,
        segments=[Segment("x", 0.0, 5.0)],
        model_name="tiny",
    )
    payload = doc.to_json()
    assert payload["main_timeline"]["clips"] == [
        {
            "source_id": "src0",
            "source_path": "/tmp/x.wav",
            "source_start": 0.0,
            "source_end": 5.0,
            "reason": "",
        }
    ]


def test_build_document_propagates_source_hash():
    h = "a" * 64
    doc = build_document(
        media_path=Path("/tmp/x.wav"),
        duration=1.0,
        language=None,
        segments=[Segment("x", 0.0, 1.0)],
        model_name="tiny",
        source_hash=h,
    )
    assert doc.source_hash == h
    assert doc.sources["src0"].hash == h


def test_document_source_hash_defaults_to_none():
    src = MediaSource(id="src0", path=Path("/tmp/x.wav"), duration=1.0)
    doc = Document(
        sources={"src0": src},
        segments=[Segment("x", 0.0, 1.0)],
        ranges=[Range(source_id="src0", start=0.0, end=1.0)],
        language=None,
    )
    assert doc.source_hash is None
    assert "source_hash" not in doc.to_json()


def test_document_with_source_hash_round_trips():
    h = "a" * 64
    src = MediaSource(id="src0", path=Path("/tmp/x.wav"), duration=1.0, hash=h)
    doc = Document(
        sources={"src0": src},
        segments=[Segment("x", 0.0, 1.0)],
        ranges=[Range(source_id="src0", start=0.0, end=1.0)],
        language="en",
        created_at=datetime(2026, 4, 27, tzinfo=UTC),
        model_name="tiny",
        source_hash=h,
    )
    payload = doc.to_json()
    assert payload["source_hash"] == h
    restored = Document.from_json(payload)
    assert restored.source_hash == h


def test_document_direct_construction_created_at_is_utc():
    """Constructing Document(...) directly (not via build_document) yields a
    tz-aware UTC ``created_at`` — guards against a regression where the
    default factory drops back to naive local time."""
    src = MediaSource(id="src0", path=Path("/tmp/x.wav"), duration=1.0)
    doc = Document(
        sources={"src0": src},
        segments=[Segment("x", 0.0, 1.0)],
        ranges=[Range(source_id="src0", start=0.0, end=1.0)],
        language=None,
    )
    assert doc.created_at.tzinfo is not None
    assert doc.created_at.utcoffset() == timedelta(0)
