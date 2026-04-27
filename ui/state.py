"""Pure-logic state machine + worker-event types for the UI.

Kept Tk-free so tests can exercise transitions, validation, and queue pumping
without instantiating widgets. The widget layer in ``ui.app`` owns an
:class:`AppStateMachine` and reacts to its ``on_change`` notifications.

Worker-event dataclasses moved to :mod:`workers.events` in Phase 5a so the
PySide6 UI can import them without depending on this (tkinter-flavored)
module. They're re-exported here for backward compatibility.
"""

from __future__ import annotations

import queue
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from workers.events import (
    CancelledEvent,
    DoneEvent,
    ErrorEvent,
    ProgressEvent,
    SegmentEvent,
    WorkerEvent,
)

__all__ = [
    "AppState",
    "AppStateMachine",
    "CancelledEvent",
    "DoneEvent",
    "ErrorEvent",
    "InvalidTransitionError",
    "ProgressEvent",
    "SegmentEvent",
    "SUPPORTED_EXTENSIONS",
    "TranscriptionResult",
    "WorkerEvent",
    "is_supported_media",
    "pump_queue",
]


class AppState(str, Enum):
    IDLE = "idle"
    FILE_LOADED = "file_loaded"
    TRANSCRIBING = "transcribing"
    COMPLETE = "complete"
    ERROR = "error"


# Mapping of which transitions are legal. We model this explicitly so
# misbehaving callers fail loudly in tests.
_LEGAL_TRANSITIONS: dict[AppState, set[AppState]] = {
    AppState.IDLE: {AppState.FILE_LOADED},
    AppState.FILE_LOADED: {AppState.TRANSCRIBING, AppState.IDLE, AppState.FILE_LOADED},
    AppState.TRANSCRIBING: {AppState.COMPLETE, AppState.ERROR, AppState.IDLE},
    AppState.COMPLETE: {AppState.IDLE, AppState.FILE_LOADED},
    AppState.ERROR: {AppState.IDLE, AppState.FILE_LOADED},
}


class InvalidTransitionError(RuntimeError):
    pass


# Files the drop zone accepts. Lower-case extensions only; we match
# case-insensitively in :func:`is_supported_media`.
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {".mp4", ".mov", ".mkv", ".mp3", ".wav", ".m4a"}
)


def is_supported_media(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


@dataclass
class TranscriptionResult:
    segments: list[Any]
    info: Any
    output_files: dict[str, Path]
    elapsed: float
    # Phase 5b — the canonical Document carried through from the
    # worker. Optional for tests that synthesize a TranscriptionResult
    # without one; the worker always provides it.
    document: Any | None = None


@dataclass
class AppStateMachine:
    """Tiny FSM driving the UI. All mutators validate transitions."""

    state: AppState = AppState.IDLE
    media_path: Path | None = None
    progress: float = 0.0
    progress_label: str = ""
    streaming_text: list[str] = field(default_factory=list)
    error_message: str | None = None
    result: TranscriptionResult | None = None
    _listeners: list[Callable[[AppState], None]] = field(default_factory=list)

    # ----- listeners -----

    def on_change(self, listener: Callable[[AppState], None]) -> None:
        self._listeners.append(listener)

    def _emit(self) -> None:
        for cb in self._listeners:
            cb(self.state)

    def transition_to(self, new_state: AppState) -> None:
        """Force a validated transition into ``new_state`` and notify listeners.

        Public counterpart to the internal call sites (``load_file``,
        ``start_transcribing``, …). Use this from UI code instead of
        ``state.state = X; state._emit()``: the assignment-then-emit
        pattern bypasses the legal-transitions check, and re-using
        ``transition_to`` keeps the state-machine the single owner of
        validation. Phase 5c added this so both UIs could come back from
        ERROR cleanly without poking at the internals.
        """
        self._go(new_state)

    def _go(self, new_state: AppState) -> None:
        if new_state == self.state:
            return
        if new_state not in _LEGAL_TRANSITIONS[self.state]:
            raise InvalidTransitionError(
                f"illegal transition {self.state.value} → {new_state.value}"
            )
        self.state = new_state
        self._emit()

    # ----- user actions -----

    def load_file(self, path: Path) -> None:
        if self.state not in (AppState.IDLE, AppState.FILE_LOADED, AppState.COMPLETE,
                              AppState.ERROR):
            raise InvalidTransitionError(f"cannot load file from {self.state.value}")
        if not is_supported_media(path):
            raise ValueError(f"unsupported file extension: {path.suffix}")
        self.media_path = path
        self.error_message = None
        self.result = None
        self.progress = 0.0
        self.streaming_text = []
        # Normalize to FILE_LOADED regardless of which non-transcribing state we came from.
        if self.state in (AppState.COMPLETE, AppState.ERROR):
            self.state = AppState.IDLE
            self._emit()
        self._go(AppState.FILE_LOADED)

    def start_transcribing(self) -> None:
        if self.state != AppState.FILE_LOADED:
            raise InvalidTransitionError(f"cannot start transcribing from {self.state.value}")
        if self.media_path is None:
            raise InvalidTransitionError("no media loaded")
        self.progress = 0.0
        self.streaming_text = []
        self._go(AppState.TRANSCRIBING)

    def cancel(self) -> None:
        """User-initiated cancel during transcribing → return to idle."""
        if self.state != AppState.TRANSCRIBING:
            raise InvalidTransitionError(f"cannot cancel from {self.state.value}")
        self.media_path = None
        self.progress = 0.0
        self.streaming_text = []
        self._go(AppState.IDLE)

    def reset(self) -> None:
        """User pressed New Transcription / Retry. Returns to idle."""
        self.media_path = None
        self.progress = 0.0
        self.progress_label = ""
        self.streaming_text = []
        self.error_message = None
        self.result = None
        if self.state == AppState.IDLE:
            self._emit()
            return
        # Force-jump regardless of source state. We allow this because
        # COMPLETE/ERROR/FILE_LOADED → IDLE is always a valid user reset.
        self.state = AppState.IDLE
        self._emit()

    # ----- worker → state -----

    def apply_event(self, event: WorkerEvent) -> None:
        """Apply a worker event. Unknown event types raise TypeError."""
        if isinstance(event, SegmentEvent):
            if self.state == AppState.TRANSCRIBING:
                self.streaming_text.append(event.text)
        elif isinstance(event, ProgressEvent):
            if self.state == AppState.TRANSCRIBING:
                self.progress = max(0.0, min(1.0, event.fraction))
                self.progress_label = event.label
        elif isinstance(event, DoneEvent):
            if self.state != AppState.TRANSCRIBING:
                # Stale event after cancel — drop it.
                return
            self.result = TranscriptionResult(
                segments=event.segments,
                info=event.info,
                output_files=event.output_files,
                elapsed=event.elapsed,
                document=getattr(event, "document", None),
            )
            self.progress = 1.0
            self._go(AppState.COMPLETE)
        elif isinstance(event, ErrorEvent):
            if self.state == AppState.IDLE:
                return
            self.error_message = event.message
            self.state = AppState.ERROR  # error reachable from anywhere active
            self._emit()
        elif isinstance(event, CancelledEvent):
            # Worker confirmed cancel — UI may have already moved to IDLE.
            return
        else:
            raise TypeError(f"unknown worker event: {type(event).__name__}")


# ---------------------------------------------------------------------------
# Queue plumbing
# ---------------------------------------------------------------------------


def pump_queue(q: queue.Queue, machine: AppStateMachine) -> int:
    """Drain ``q`` into ``machine``. Returns the number of events applied."""
    n = 0
    while True:
        try:
            event = q.get_nowait()
        except queue.Empty:
            return n
        machine.apply_event(event)
        n += 1
