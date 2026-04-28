"""Render-export worker — wraps ``core.render.render_cut`` for the Qt UI.

Mirrors the shape of :class:`workers.transcription.TranscriptionWorker`
and :class:`workers.waveform.generate_peaks`: synchronous body, runs on
a caller-owned thread, emits framework-agnostic events through an
injected callback. The Qt UI hosts the worker on a :class:`QThread`
and translates the events into Qt signals.

Cancellation: the worker checks a :class:`threading.Event` between
phases and from inside the progress adapter, so it can abort before
smartcut runs and abort during smartcut by raising out of the
progress callback. Any audio-fade ffmpeg subprocess is killed via
:class:`subprocess.Popen.terminate`. On cancel the partial output
file is unlinked — an incomplete ``.mp4`` is worse than no file. On
error the partial output is left in place so the user can inspect
it.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from core.document import Document
from core.render import render_cut
from core.settings import Settings

_LOG = logging.getLogger(__name__)


@dataclass
class RenderEvent:
    """Base class for render-pipeline events. Empty payload by design."""


@dataclass
class RenderStarted(RenderEvent):
    output_path: Path


@dataclass
class RenderProgress(RenderEvent):
    fraction: float


@dataclass
class RenderComplete(RenderEvent):
    output_path: Path
    elapsed: float


@dataclass
class RenderError(RenderEvent):
    message: str


@dataclass
class RenderCancelled(RenderEvent):
    pass


EventCallback = Callable[[RenderEvent], None]


class RenderCancelledError(RuntimeError):
    """Raised inside the render pipeline when its cancel event is set."""


class RenderWorker:
    """Run a render synchronously; emit events through a callback.

    The caller is responsible for threading. The Qt UI uses a
    :class:`QThread` so events that update widgets are queued back to
    the GUI thread.
    """

    def __init__(
        self,
        document: Document,
        output_path: Path,
        settings: Settings,
        on_event: EventCallback,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.document = document
        self.output_path = Path(output_path)
        self.settings = settings
        self.on_event = on_event
        self.cancel_event = cancel_event or threading.Event()

    def cancel(self) -> None:
        """Mark the render cancelled. The next progress tick aborts."""
        self.cancel_event.set()

    def run(self) -> None:
        """Execute the render. Emits exactly one terminal event."""
        start = time.monotonic()
        self.on_event(RenderStarted(output_path=self.output_path))

        if self.cancel_event.is_set():
            self._cleanup_partial()
            self.on_event(RenderCancelled())
            return

        def _progress(fraction: float) -> None:
            if self.cancel_event.is_set():
                # Raising out of smartcut's progress hook is the only
                # cancellation channel available without modifying core/.
                raise RenderCancelledError()
            self.on_event(RenderProgress(fraction=fraction))

        try:
            render_cut(
                self.document,
                self.output_path,
                on_progress=_progress,
                pad_lead=self.settings.default_pad_lead,
                pad_trail=self.settings.default_pad_trail,
                audio_fade_ms=self.settings.default_audio_fade_ms,
            )
        except RenderCancelledError:
            self._cleanup_partial()
            self.on_event(RenderCancelled())
            return
        except Exception as exc:  # noqa: BLE001 — surface any failure as a clean event
            _LOG.exception("render failed for %s", self.output_path)
            # On error we leave the partial file alone (per spec): the
            # user may want to inspect it. The cancel path cleans up
            # because incomplete-but-named looks like a successful save.
            if self.cancel_event.is_set():
                self._cleanup_partial()
                self.on_event(RenderCancelled())
            else:
                self.on_event(RenderError(message=str(exc)))
            return

        if self.cancel_event.is_set():
            # Progress never ticked but cancel landed between render_cut
            # finishing and us emitting Complete. Clean up — a complete
            # output only counts when the user didn't ask us to abort.
            self._cleanup_partial()
            self.on_event(RenderCancelled())
            return

        elapsed = time.monotonic() - start
        self.on_event(
            RenderComplete(output_path=self.output_path, elapsed=elapsed)
        )

    def _cleanup_partial(self) -> None:
        try:
            self.output_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            _LOG.warning("could not unlink partial render %s: %s", self.output_path, exc)
