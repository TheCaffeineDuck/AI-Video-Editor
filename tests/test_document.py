"""Tests for core.document — Word, Segment, Document, CutMark, JSON I/O."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from pathlib import Path

import pytest

from core.document import (
    CutMark,
    Document,
    Segment,
    UnsupportedSchemaError,
    Word,
)


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
    # words must be a tuple, not a list — frozen requires hashable contents.
    assert isinstance(s.words, tuple)


def test_segment_is_hashable():
    """Frozen segments with tuple words can live in sets / dict keys."""
    s = Segment(text="a", start=0.0, end=1.0, words=(Word("a", 0.0, 1.0),))
    assert hash(s) == hash(s)  # doesn't raise
    assert s in {s}


# ---------------------------------------------------------------------------
# CutMark
# ---------------------------------------------------------------------------


def test_cutmark_is_frozen():
    c = CutMark(start=1.0, end=2.0, reason="filler")
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.reason = "manual"  # type: ignore[misc]


def test_cutmark_default_reason_empty_string():
    c = CutMark(start=0.0, end=1.0)
    assert c.reason == ""


# ---------------------------------------------------------------------------
# Document — basic construction
# ---------------------------------------------------------------------------


def _example_doc(**overrides) -> Document:
    """Helper: a minimal but realistic Document for round-trip tests."""
    defaults = dict(
        media_path=Path("/tmp/example.wav"),
        duration=4.5,
        language="en",
        segments=[
            Segment(
                text="Hello",
                start=0.0,
                end=1.5,
                words=(
                    Word("Hello", 0.0, 1.5, 0.95),
                ),
            ),
            Segment(text="world", start=1.5, end=3.0, words=()),
        ],
        cuts=[
            CutMark(start=2.0, end=2.5, reason="filler"),
        ],
        created_at=datetime(2026, 4, 26, 10, 0, 0),
        model_name="tiny",
    )
    defaults.update(overrides)
    return Document(**defaults)


def test_document_schema_version_constant():
    assert Document.SCHEMA_VERSION == 1


# ---------------------------------------------------------------------------
# Document.to_json / from_json — round-trip
# ---------------------------------------------------------------------------


def test_document_to_json_emits_schema_version():
    doc = _example_doc()
    data = doc.to_json()
    assert data["schema_version"] == 1


def test_document_to_json_is_json_serializable():
    """to_json output must survive json.dumps without custom encoders."""
    data = _example_doc().to_json()
    blob = json.dumps(data)
    parsed = json.loads(blob)
    assert parsed["schema_version"] == 1
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


def test_document_round_trip_preserves_cuts():
    doc = _example_doc()
    restored = Document.from_json(doc.to_json())
    assert restored.cuts == doc.cuts


def test_document_round_trip_with_no_cuts_or_words():
    doc = Document(
        media_path=Path("/tmp/x.mp4"),
        duration=10.0,
        language=None,
        segments=[Segment(text="hi", start=0.0, end=1.0)],
        created_at=datetime(2026, 4, 26),
        model_name="base",
    )
    restored = Document.from_json(doc.to_json())
    assert restored == doc


# ---------------------------------------------------------------------------
# Document.from_json — schema version error paths (mandatory)
# ---------------------------------------------------------------------------


def _minimal_payload(**override) -> dict:
    """Valid payload skeleton; tests override `schema_version` to misbehave."""
    payload = {
        "schema_version": 1,
        "media_path": "/tmp/x.wav",
        "duration": 1.0,
        "language": "en",
        "model_name": "tiny",
        "created_at": "2026-04-26T10:00:00",
        "segments": [],
        "cuts": [],
    }
    payload.update(override)
    return payload


def test_from_json_missing_schema_version_raises():
    payload = _minimal_payload()
    del payload["schema_version"]
    with pytest.raises(UnsupportedSchemaError) as excinfo:
        Document.from_json(payload)
    msg = str(excinfo.value)
    assert "schema_version" in msg
    assert "1" in msg  # mentions the supported version


def test_from_json_null_schema_version_raises():
    payload = _minimal_payload(schema_version=None)
    with pytest.raises(UnsupportedSchemaError) as excinfo:
        Document.from_json(payload)
    msg = str(excinfo.value)
    assert "null" in msg.lower()
    assert "1" in msg


def test_from_json_unknown_schema_version_raises():
    payload = _minimal_payload(schema_version=999)
    with pytest.raises(UnsupportedSchemaError) as excinfo:
        Document.from_json(payload)
    msg = str(excinfo.value)
    assert "999" in msg
    assert "1" in msg


def test_unsupported_schema_error_is_value_error_subclass():
    """Subclassing ValueError lets generic except blocks catch it cleanly."""
    assert issubclass(UnsupportedSchemaError, ValueError)
