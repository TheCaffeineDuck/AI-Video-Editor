"""Tests for core.document — the canonical Segment / Word types."""

from __future__ import annotations

import dataclasses

import pytest

from core.document import Segment, Word


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
