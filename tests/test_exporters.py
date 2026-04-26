"""Tests for core.exporters."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.document import Segment, Word
from core.exporters import (
    SimpleSegment,
    format_srt_timestamp,
    format_vtt_timestamp,
    render_srt,
    render_txt,
    render_vtt,
    resolve_output_path,
    write_outputs,
)


def seg(start: float, end: float, text: str) -> SimpleSegment:
    return SimpleSegment(start=start, end=end, text=text)


# ---------------------------------------------------------------------------
# Timestamp formatting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.0, "00:00:00,000"),
        (0.001, "00:00:00,001"),
        (1.5, "00:00:01,500"),
        (61.123, "00:01:01,123"),
        (3600.0, "01:00:00,000"),
        (3661.999, "01:01:01,999"),
    ],
)
def test_srt_timestamp(seconds, expected):
    assert format_srt_timestamp(seconds) == expected


def test_srt_timestamp_clamps_negative_to_zero():
    assert format_srt_timestamp(-1.0) == "00:00:00,000"


def test_vtt_timestamp_uses_period_separator():
    assert format_vtt_timestamp(61.5) == "00:01:01.500"


def test_vtt_timestamp_rounds_milliseconds():
    # 0.0006 rounds up to 1ms
    assert format_vtt_timestamp(0.0006) == "00:00:00.001"


# ---------------------------------------------------------------------------
# render_txt
# ---------------------------------------------------------------------------


def test_render_txt_joins_with_spaces_and_strips():
    segments = [seg(0.0, 1.0, "  Hello  "), seg(1.0, 2.0, "world")]
    assert render_txt(segments) == "Hello world\n"


def test_render_txt_empty_returns_empty_string():
    assert render_txt([]) == ""


def test_render_txt_skips_blank_segments():
    segments = [seg(0, 1, "Hello"), seg(1, 2, "   "), seg(2, 3, "world")]
    assert render_txt(segments) == "Hello world\n"


def test_render_txt_preserves_internal_quotes_and_newlines():
    segments = [seg(0, 1, 'She said "hi"'), seg(1, 2, "line1\nline2")]
    out = render_txt(segments)
    assert 'She said "hi"' in out
    assert "line1\nline2" in out


# ---------------------------------------------------------------------------
# render_srt
# ---------------------------------------------------------------------------


def test_render_srt_format():
    segments = [seg(0.0, 1.5, "Hello"), seg(1.5, 3.0, "world")]
    out = render_srt(segments)
    expected = (
        "1\n00:00:00,000 --> 00:00:01,500\nHello\n"
        "\n"
        "2\n00:00:01,500 --> 00:00:03,000\nworld\n"
    )
    assert out == expected


def test_render_srt_indices_are_sequential():
    segments = [seg(i, i + 1, f"line {i}") for i in range(5)]
    out = render_srt(segments)
    for i in range(1, 6):
        assert f"\n{i}\n" in "\n" + out or out.startswith(f"{i}\n")


def test_render_srt_empty_returns_empty_string():
    assert render_srt([]) == ""


def test_render_srt_single_segment():
    out = render_srt([seg(0.0, 1.0, "solo")])
    assert out == "1\n00:00:00,000 --> 00:00:01,000\nsolo\n"


def test_render_srt_blank_text_kept_as_space():
    """Empty cue text would break some SRT parsers; we substitute a space."""
    out = render_srt([seg(0.0, 1.0, "")])
    assert out == "1\n00:00:00,000 --> 00:00:01,000\n \n"


def test_render_srt_preserves_quotes_and_newlines_in_text():
    out = render_srt([seg(0, 1, 'a "quote"\nwith newline')])
    assert 'a "quote"' in out
    assert "with newline" in out


# ---------------------------------------------------------------------------
# render_vtt
# ---------------------------------------------------------------------------


def test_render_vtt_header_and_format():
    segments = [seg(0.0, 1.5, "Hello"), seg(1.5, 3.0, "world")]
    out = render_vtt(segments)
    assert out.startswith("WEBVTT\n\n")
    assert "00:00:00.000 --> 00:00:01.500" in out
    assert "00:00:01.500 --> 00:00:03.000" in out


def test_render_vtt_empty_still_has_header():
    assert render_vtt([]) == "WEBVTT\n"


def test_render_vtt_single_segment():
    out = render_vtt([seg(0.0, 1.0, "solo")])
    assert out == "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nsolo\n"


# ---------------------------------------------------------------------------
# resolve_output_path / collisions
# ---------------------------------------------------------------------------


def test_resolve_output_path_returns_target_when_free(tmp_path: Path):
    src = tmp_path / "lecture.mp4"
    out = resolve_output_path(src, "txt")
    assert out == tmp_path / "lecture.txt"


def test_resolve_output_path_appends_suffix_on_collision(tmp_path: Path):
    src = tmp_path / "lecture.mp4"
    (tmp_path / "lecture.txt").write_text("existing")
    out = resolve_output_path(src, "txt")
    assert out == tmp_path / "lecture_1.txt"


def test_resolve_output_path_walks_to_next_free(tmp_path: Path):
    src = tmp_path / "lecture.mp4"
    (tmp_path / "lecture.txt").write_text("a")
    (tmp_path / "lecture_1.txt").write_text("b")
    (tmp_path / "lecture_2.txt").write_text("c")
    out = resolve_output_path(src, "txt")
    assert out == tmp_path / "lecture_3.txt"


def test_resolve_output_path_rejects_dotted_suffix():
    with pytest.raises(ValueError):
        resolve_output_path(Path("/tmp/x.mp4"), ".txt")


# ---------------------------------------------------------------------------
# write_outputs
# ---------------------------------------------------------------------------


def test_write_outputs_writes_all_requested_formats(tmp_path: Path):
    src = tmp_path / "lecture.mp4"
    segments = [seg(0.0, 1.0, "Hello"), seg(1.0, 2.0, "world")]
    written = write_outputs(src, segments, ["txt", "srt", "vtt"])

    assert set(written) == {"txt", "srt", "vtt"}
    assert written["txt"].read_text() == "Hello world\n"
    assert written["srt"].read_text().startswith("1\n00:00:00,000")
    assert written["vtt"].read_text().startswith("WEBVTT\n")


def test_write_outputs_rejects_unknown_format(tmp_path: Path):
    src = tmp_path / "x.mp4"
    with pytest.raises(ValueError):
        write_outputs(src, [], ["docx"])


def test_write_outputs_preserves_existing_files_via_suffix(tmp_path: Path):
    src = tmp_path / "lecture.mp4"
    (tmp_path / "lecture.txt").write_text("PRIOR")
    written = write_outputs(src, [seg(0, 1, "new")], ["txt"])
    assert (tmp_path / "lecture.txt").read_text() == "PRIOR"
    assert written["txt"] == tmp_path / "lecture_1.txt"
    assert written["txt"].read_text() == "new\n"


# ---------------------------------------------------------------------------
# Phase 4a: Segment dataclass (with words) renders identically to SimpleSegment
# ---------------------------------------------------------------------------


def _doc_seg(start: float, end: float, text: str, words: tuple[Word, ...] = ()) -> Segment:
    return Segment(text=text, start=start, end=end, words=words)


def test_renderers_accept_core_document_segment():
    """The new Segment dataclass must render the same as SimpleSegment."""
    words = (
        Word(text="Hello", start=0.0, end=0.5, probability=0.9),
        Word(text="world", start=0.6, end=1.4, probability=0.8),
    )
    new = [
        _doc_seg(0.0, 1.5, "Hello", words=words),
        _doc_seg(1.5, 3.0, "world"),
    ]
    old = [seg(0.0, 1.5, "Hello"), seg(1.5, 3.0, "world")]
    assert render_txt(new) == render_txt(old)
    assert render_srt(new) == render_srt(old)
    assert render_vtt(new) == render_vtt(old)


def test_write_outputs_accepts_core_document_segment(tmp_path: Path):
    src = tmp_path / "lecture.mp4"
    segments = [
        _doc_seg(0.0, 1.0, "Hello", words=(Word("Hello", 0.0, 1.0, 0.9),)),
        _doc_seg(1.0, 2.0, "world"),
    ]
    written = write_outputs(src, segments, ["txt", "srt", "vtt"])
    assert written["txt"].read_text() == "Hello world\n"
    assert "00:00:00,000 --> 00:00:01,000" in written["srt"].read_text()
    assert written["vtt"].read_text().startswith("WEBVTT\n")
