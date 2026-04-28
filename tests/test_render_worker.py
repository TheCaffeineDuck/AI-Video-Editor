"""Phase 5e — RenderWorker behavioural tests.

Pure-data tests against :class:`workers.render.RenderWorker`. The
worker is a plain Python object (no Qt dependency); we mock
``core.render.render_cut`` so the tests don't need a real video file
or a smartcut install.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.document import Document, MediaSource, Range, Segment, Word
from core.settings import Settings
from workers import render as render_mod
from workers.render import (
    RenderCancelled,
    RenderComplete,
    RenderError,
    RenderProgress,
    RenderStarted,
    RenderWorker,
)


def _make_doc(media_path: Path) -> Document:
    seg = Segment(
        text="hello world",
        start=0.0,
        end=1.0,
        words=(
            Word(text="hello", start=0.0, end=0.5),
            Word(text=" world", start=0.5, end=1.0),
        ),
    )
    return Document(
        sources={"src0": MediaSource(id="src0", path=media_path, duration=1.0)},
        segments=[seg],
        ranges=[Range(source_id="src0", start=0.0, end=1.0)],
        language="en",
        created_at=datetime.now(UTC),
        model_name="tiny",
        source_hash=None,
    )


def test_render_worker_emits_started_then_complete(monkeypatch, tmp_path):
    """Happy path: Started → Complete with the expected output path."""
    output = tmp_path / "edited.mp4"

    def _fake_render(doc, out, on_progress=None, **kwargs):
        # No progress emit; render_cut today doesn't always tick.
        out.write_bytes(b"\x00\x00")  # something resembling a file
        return out

    monkeypatch.setattr(render_mod, "render_cut", _fake_render)

    events = []
    worker = RenderWorker(
        document=_make_doc(tmp_path / "src.wav"),
        output_path=output,
        settings=Settings(),
        on_event=events.append,
    )
    worker.run()

    assert any(isinstance(e, RenderStarted) for e in events)
    assert any(isinstance(e, RenderComplete) for e in events)
    complete = [e for e in events if isinstance(e, RenderComplete)][0]
    assert complete.output_path == output
    assert complete.elapsed >= 0.0


def test_render_worker_emits_progress(monkeypatch, tmp_path):
    """A render that calls on_progress propagates RenderProgress events."""
    output = tmp_path / "edited.mp4"

    def _fake_render(doc, out, on_progress=None, **kwargs):
        if on_progress is not None:
            on_progress(0.25)
            on_progress(0.5)
            on_progress(1.0)
        out.write_bytes(b"")
        return out

    monkeypatch.setattr(render_mod, "render_cut", _fake_render)

    events = []
    worker = RenderWorker(
        document=_make_doc(tmp_path / "src.wav"),
        output_path=output,
        settings=Settings(),
        on_event=events.append,
    )
    worker.run()

    fractions = [e.fraction for e in events if isinstance(e, RenderProgress)]
    assert fractions == [0.25, 0.5, 1.0]


def test_render_worker_emits_error_on_raise(monkeypatch, tmp_path):
    """A render that raises non-cancel surfaces RenderError, leaves output alone."""
    output = tmp_path / "edited.mp4"

    def _fake_render(doc, out, **kwargs):
        # Simulate a partial write that smartcut sometimes leaves behind.
        out.write_bytes(b"partial")
        raise RuntimeError("ffmpeg blew up")

    monkeypatch.setattr(render_mod, "render_cut", _fake_render)

    events = []
    worker = RenderWorker(
        document=_make_doc(tmp_path / "src.wav"),
        output_path=output,
        settings=Settings(),
        on_event=events.append,
    )
    worker.run()

    error_events = [e for e in events if isinstance(e, RenderError)]
    assert len(error_events) == 1
    assert "ffmpeg blew up" in error_events[0].message
    # Per spec: error path leaves the partial file intact.
    assert output.is_file()


def test_render_worker_cancels_via_progress(monkeypatch, tmp_path):
    """Cancel during smartcut: cancel_event set in progress callback aborts."""
    output = tmp_path / "edited.mp4"

    def _fake_render(doc, out, on_progress=None, **kwargs):
        # Simulate smartcut writing a partial output and ticking progress.
        out.write_bytes(b"partial")
        if on_progress is not None:
            on_progress(0.10)  # the worker raises on this tick after we set cancel
            on_progress(0.50)
        return out

    monkeypatch.setattr(render_mod, "render_cut", _fake_render)

    cancel = threading.Event()
    cancel.set()  # already cancelled when run() begins

    events = []
    worker = RenderWorker(
        document=_make_doc(tmp_path / "src.wav"),
        output_path=output,
        settings=Settings(),
        on_event=events.append,
        cancel_event=cancel,
    )
    worker.run()

    cancel_events = [e for e in events if isinstance(e, RenderCancelled)]
    assert len(cancel_events) == 1
    # Per spec: cancel path cleans up the partial output.
    assert not output.exists()


def test_render_worker_cancel_after_complete_still_cleans_up(monkeypatch, tmp_path):
    """A cancel landing between render_cut return and Complete-emit cleans up."""
    output = tmp_path / "edited.mp4"

    cancel = threading.Event()

    def _fake_render(doc, out, on_progress=None, **kwargs):
        out.write_bytes(b"\x00")
        # The worker sets cancel after render_cut returns, before
        # the Complete event would have been emitted.
        cancel.set()
        return out

    monkeypatch.setattr(render_mod, "render_cut", _fake_render)

    events = []
    worker = RenderWorker(
        document=_make_doc(tmp_path / "src.wav"),
        output_path=output,
        settings=Settings(),
        on_event=events.append,
        cancel_event=cancel,
    )
    worker.run()

    assert any(isinstance(e, RenderCancelled) for e in events)
    assert not any(isinstance(e, RenderComplete) for e in events)
    assert not output.exists()


def test_render_worker_passes_settings_kwargs(monkeypatch, tmp_path):
    """Render is called with pad_lead/pad_trail/audio_fade_ms from Settings."""
    output = tmp_path / "edited.mp4"

    captured = {}

    def _fake_render(doc, out, **kwargs):
        captured.update(kwargs)
        out.write_bytes(b"")
        return out

    monkeypatch.setattr(render_mod, "render_cut", _fake_render)

    settings = Settings(
        default_pad_lead=0.20,
        default_pad_trail=0.15,
        default_audio_fade_ms=40,
    )
    worker = RenderWorker(
        document=_make_doc(tmp_path / "src.wav"),
        output_path=output,
        settings=settings,
        on_event=lambda _: None,
    )
    worker.run()

    assert captured["pad_lead"] == pytest.approx(0.20)
    assert captured["pad_trail"] == pytest.approx(0.15)
    assert captured["audio_fade_ms"] == 40
