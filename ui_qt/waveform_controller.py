"""Glue between :class:`WaveformStrip`, :class:`DocumentSession`, and the player.

Owns the lifecycle the strip itself shouldn't care about:

- Lazy peak generation: synchronous fast-path on cache hit, off-thread
  on miss (with a "Generating waveform…" placeholder while it runs).
- Subscribing to ``DocumentSession.document_changed`` and forwarding
  the new ranges into ``WaveformStrip.set_ranges``.
- Receiving player ``positionChanged`` ticks and forwarding to
  ``WaveformStrip.set_position``.

EditorPane wires this once and forgets about it — the controller takes
care of the lazy-cache-or-generate decision and the strip-state pumping
on every doc/playback event.

Threading: peak generation runs on a :class:`QThread` rather than the
``threading.Thread`` we use for transcription, so the worker can emit
Qt signals back to the UI thread without re-entrancy concerns. The
worker is destroyed on completion; ``shutdown`` kills the in-flight
ffmpeg subprocess and ``wait``s for the thread so EditorPane.release
can tear down cleanly without leaving a QThread alive past widget
destruction (Qt aborts the process when that happens).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QThread, Signal

from core.document import Document
from ui_qt.document_session import DocumentSession
from ui_qt.waveform import WaveformStrip
from workers.waveform import (
    generate_peaks,
    load_peaks_cache,
    save_peaks_cache,
)

_LOG = logging.getLogger(__name__)


class _PeakWorker(QObject):
    """Off-UI peak generator. Lives on a :class:`QThread`."""

    finished = Signal(np.ndarray, float)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, source_path: Path, duration_s: float) -> None:
        super().__init__()
        self._source_path = source_path
        self._duration_s = duration_s
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            peaks = generate_peaks(self._source_path, cancel_event=self._cancel)
        except Exception as exc:  # noqa: BLE001 (surface as a clean signal)
            from workers.waveform import PeaksCancelledError

            if isinstance(exc, PeaksCancelledError) or self._cancel.is_set():
                self.cancelled.emit()
                return
            _LOG.warning("peak generation failed for %s: %s", self._source_path, exc)
            self.failed.emit(str(exc))
            return
        self.finished.emit(peaks, self._duration_s)


class WaveformController(QObject):
    """Orchestrates :class:`WaveformStrip` against a :class:`DocumentSession`.

    Construction wires the session's ``document_changed`` signal so the
    strip's keep-ranges follow doc edits. ``bind_player`` is a separate
    call so EditorPane can do it after the strip + viewport exist.

    Construction also kicks off the lazy peak load (cache hit path) or
    generation (cache miss path). The strip flips into "loading" state
    for the cache-miss case and back to peaks once the worker finishes.
    """

    def __init__(
        self,
        strip: WaveformStrip,
        session: DocumentSession,
        *,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._strip = strip
        self._session = session
        self._thread: QThread | None = None
        self._worker: _PeakWorker | None = None

        self._session.document_changed.connect(self._on_document_changed)

        # Initial state from the document we were handed.
        doc = session.document
        self._sync_ranges(doc)
        self._begin_peak_load(doc)

    # ----- public surface -----

    def bind_player(self, viewport) -> None:  # type: ignore[no-untyped-def]
        """Subscribe to the viewport's ``position_changed`` signal."""
        viewport.position_changed.connect(self._strip.set_position)

    def shutdown(self) -> None:
        """Cancel any in-flight peak generation and wait for the thread.

        Called from :meth:`EditorPane.release` so the QThread is fully
        joined before its widget tree is torn down. Letting the thread
        outlive the QObject tree is what triggers Qt's "QThread destroyed
        while still running" abort.
        """
        worker = self._worker
        thread = self._thread
        if worker is not None:
            worker.cancel()
        if thread is not None and thread.isRunning():
            thread.quit()
            # Bound the wait — generation against sample.wav finishes
            # in well under a second; for the 23 GB podcast cancellation
            # propagates within one chunk read. 5 s is generous.
            thread.wait(5_000)
        self._thread = None
        self._worker = None

    # ----- session reactions -----

    def _on_document_changed(self, doc: Document) -> None:
        self._sync_ranges(doc)

    def _sync_ranges(self, doc: Document) -> None:
        primary = self._primary_source(doc)
        duration = float(primary.duration) if primary is not None else 0.0
        # ranges is a list of Range objects; the strip handles the
        # complementing/dimming itself.
        self._strip.set_ranges(doc.ranges, duration)

    # ----- peak generation -----

    def _begin_peak_load(self, doc: Document) -> None:
        primary = self._primary_source(doc)
        if primary is None or not Path(primary.path).is_file():
            self._strip.set_loading(False)
            return
        cached = load_peaks_cache(Path(primary.path))
        if cached is not None:
            self._strip.set_peaks(cached.peaks, cached.duration_s or float(primary.duration))
            return
        # Cache miss → generate on a worker thread.
        self._strip.set_loading(True)
        self._launch_worker(Path(primary.path), float(primary.duration))

    def _launch_worker(self, source_path: Path, duration_s: float) -> None:
        thread = QThread(self)
        worker = _PeakWorker(source_path, duration_s)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(
            lambda peaks, dur: self._on_worker_finished(source_path, peaks, dur)
        )
        worker.failed.connect(self._on_worker_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        self._worker = worker
        thread.start()

    def _on_worker_finished(
        self,
        source_path: Path,
        peaks: np.ndarray,
        duration_s: float,
    ) -> None:
        try:
            save_peaks_cache(source_path, peaks, duration_s)
        except (OSError, FileNotFoundError) as exc:
            # Saving the cache is best-effort — peaks still work in-memory.
            _LOG.warning("could not save peaks cache for %s: %s", source_path, exc)
        self._strip.set_peaks(peaks, duration_s)
        self._thread = None
        self._worker = None

    def _on_worker_failed(self, message: str) -> None:
        _LOG.warning("waveform worker failed: %s", message)
        self._strip.set_loading(False)
        self._thread = None
        self._worker = None

    # ----- helpers -----

    @staticmethod
    def _primary_source(doc: Document):  # type: ignore[no-untyped-def]
        if not doc.sources:
            return None
        return next(iter(doc.sources.values()))
