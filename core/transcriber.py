"""Wrap faster-whisper as a cancellable, callback-driven transcription job."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

# We don't import faster_whisper at module top-level so that the rest of
# core/ stays cheap to import in tests that don't actually load a model.

DEFAULT_DEVICE = "auto"
DEFAULT_COMPUTE_TYPE = "int8"


SegmentCallback = Callable[[str], None]
ProgressCallback = Callable[[float], None]


class Transcriber:
    """Holds a loaded WhisperModel and runs transcription jobs on demand.

    The class is intentionally synchronous — the UI layer is responsible for
    pushing the call onto a worker thread. Cancellation is exposed via
    :meth:`cancel`, which the iteration loop checks between segments.
    """

    def __init__(
        self,
        model_name: str,
        device: str = DEFAULT_DEVICE,
        compute_type: str = DEFAULT_COMPUTE_TYPE,
    ) -> None:
        from faster_whisper import WhisperModel

        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self._cancelled = threading.Event()
        self.model = WhisperModel(model_name, device=device, compute_type=compute_type)

    def cancel(self) -> None:
        """Mark the current transcription as cancelled. Safe to call from any thread."""
        self._cancelled.set()

    def reset_cancel(self) -> None:
        self._cancelled.clear()

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def transcribe(
        self,
        audio_path: Path,
        language: str | None,
        on_segment: SegmentCallback,
        on_progress: ProgressCallback,
        *,
        beam_size: int = 5,
        vad_filter: bool = True,
    ) -> tuple[list[Any], Any]:
        """Run a transcription job and stream callbacks for each segment.

        Returns a ``(segments, info)`` tuple. ``segments`` is a list of the
        underlying faster_whisper Segment objects (whatever was consumed
        before cancellation, if any). ``info`` is the TranscriptionInfo
        returned by the model.
        """
        self.reset_cancel()
        segments_iter, info = self.model.transcribe(
            str(audio_path),
            language=language,
            beam_size=beam_size,
            vad_filter=vad_filter,
            word_timestamps=False,
        )
        total = float(getattr(info, "duration", 0.0) or 0.0)
        collected = self._consume_segments(segments_iter, total, on_segment, on_progress)
        return collected, info

    def _consume_segments(
        self,
        segments: Iterable[Any],
        total_duration: float,
        on_segment: SegmentCallback,
        on_progress: ProgressCallback,
    ) -> list[Any]:
        """Iterate segments, fire callbacks, honor cancellation between segments.

        Split out so tests can drive it with a fake generator rather than
        loading a real model.
        """
        collected: list[Any] = []
        for seg in segments:
            if self._cancelled.is_set():
                break
            collected.append(seg)
            on_segment(seg.text)
            if total_duration > 0:
                progress = max(0.0, min(1.0, seg.end / total_duration))
                on_progress(progress)
        return collected
