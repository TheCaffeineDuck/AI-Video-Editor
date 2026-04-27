"""Worker → UI event dataclasses.

Plain dataclasses with no UI dependencies. The tkinter UI drains them
via a :mod:`queue.Queue` polled on a Tk timer; the Qt UI drains them
via a Qt signal connected through a thread-safe queue. Both call the
same worker; only the transport differs.

Originally defined in :mod:`ui.state`; lifted into this module in
Phase 5a so :mod:`ui_qt` can import them without depending on the
tkinter package. ``ui.state`` re-exports them for backward compat.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class WorkerEvent:
    pass


@dataclass
class SegmentEvent(WorkerEvent):
    text: str


@dataclass
class ProgressEvent(WorkerEvent):
    fraction: float
    label: str = "Transcribing…"


@dataclass
class DoneEvent(WorkerEvent):
    segments: list[Any]
    info: Any
    output_files: dict[str, Path]
    elapsed: float


@dataclass
class ErrorEvent(WorkerEvent):
    message: str


@dataclass
class CancelledEvent(WorkerEvent):
    pass
