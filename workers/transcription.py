"""Framework-agnostic transcription worker.

Previously the body of :meth:`ui.app.App._run_transcription`. Lifted out
in Phase 5a so the new PySide6 UI can drive the same backend without
duplicating cache-lookup, model-download, transcribe, and write-output
logic.

Design notes:

* The worker's only output is a stream of :class:`workers.events.WorkerEvent`
  values delivered through an injected ``on_event`` callback. The tkinter
  app pushes them onto a :class:`queue.Queue`; the Qt app emits them via
  a signal. The worker doesn't know or care.

* :class:`Transcriber`, :func:`download_model`, and :func:`is_downloaded`
  are imported at module top-level so tests can ``monkeypatch.setattr``
  them on this module — the same patching shape the previous tests used
  on ``ui.app``.

* Cancellation is a :class:`threading.Event`. The worker checks it
  between phases (after cache-miss decision, after model download,
  after transcribe). The wrapped :class:`Transcriber` is also told to
  ``cancel`` so its segment loop drops out promptly.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from core import exporters
from core.cache import cache_key
from core.document import Document, UnsupportedSchemaError, build_document
from core.model_loader import download_model
from core.models import is_downloaded
from core.settings import Settings
from core.transcriber import Transcriber
from workers.events import (
    DoneEvent,
    ErrorEvent,
    ProgressEvent,
    SegmentEvent,
    WorkerEvent,
)

EventCallback = Callable[[WorkerEvent], None]


@dataclass
class _CachedInfo:
    """Stand-in for ``faster_whisper`` ``TranscriptionInfo`` on cache hits.

    The UI only reads ``.language`` and ``.duration`` off the info object,
    so a small dataclass is enough to avoid plumbing optionality through
    the result-rendering code path.
    """

    language: str | None
    duration: float


def resolve_output_dir(settings: Settings, media_path: Path) -> Path:
    """Mirror the writer's choice of output directory."""
    if settings.output_dir:
        return Path(settings.output_dir)
    return media_path.parent


def candidate_cache_path(settings: Settings, media_path: Path) -> Path:
    """The unsuffixed path where a cached Document JSON would live.

    The post-transcription writer appends numbered suffixes on collision,
    but cache lookup only considers the unsuffixed path. A numbered file
    like ``sample_1.transcribe.json`` came from a different prior run
    (different mtime/size) and isn't a valid cache hit.
    """
    return resolve_output_dir(settings, media_path) / (
        media_path.stem + ".transcribe.json"
    )


def try_load_cached_document(
    settings: Settings, media_path: Path
) -> Document | None:
    """Return the cached Document iff it exists and ``source_hash`` matches.

    Any failure (missing file, JSON parse error, schema mismatch, missing
    or non-matching ``source_hash``) returns ``None`` and the caller
    falls through to a fresh transcription.
    """
    candidate = candidate_cache_path(settings, media_path)
    if not candidate.is_file():
        return None
    try:
        key = cache_key(media_path)
    except FileNotFoundError:
        return None
    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
        doc = Document.from_json(data)
    except (OSError, json.JSONDecodeError, UnsupportedSchemaError, KeyError, ValueError):
        return None
    if doc.source_hash != key:
        return None
    return doc


class TranscriptionWorker:
    """Run a transcription synchronously; emit events through a callback.

    Threading is the caller's responsibility: typically a UI spins a
    daemon :class:`threading.Thread` whose target is :meth:`run`. The
    worker holds a reference to the live :class:`Transcriber` so
    :meth:`cancel` can ask the model to stop mid-segment.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        media_path: Path,
        model_name: str,
        language: str | None,
        formats: list[str],
        on_event: EventCallback,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.settings = settings
        self.media_path = media_path
        self.model_name = model_name
        self.language = language
        self.formats = list(formats)
        self.on_event = on_event
        self.cancel_event = cancel_event or threading.Event()
        self._transcriber: Transcriber | None = None

    @property
    def transcriber(self) -> Transcriber | None:
        """Expose the live transcriber so the UI can cancel it directly."""
        return self._transcriber

    def cancel(self) -> None:
        """Mark the run cancelled; ask the live transcriber to stop too."""
        self.cancel_event.set()
        if self._transcriber is not None:
            self._transcriber.cancel()

    def run(self) -> None:
        """Execute the cache-or-transcribe flow. Safe to call once per instance."""
        start = time.monotonic()
        try:
            cached = try_load_cached_document(self.settings, self.media_path)
            if cached is not None:
                self._emit_cache_hit_done(cached, start)
                return

            if not is_downloaded(self.model_name):
                def on_dl(fraction: float, label: str) -> None:
                    if self.cancel_event.is_set():
                        return
                    self.on_event(ProgressEvent(fraction=fraction, label=label))

                download_model(self.model_name, on_progress=on_dl)
                if self.cancel_event.is_set():
                    return

            self._transcriber = Transcriber(
                self.model_name,
                device=self.settings.compute_device,
                compute_type=self.settings.compute_type,
            )

            def on_segment(text: str) -> None:
                if self.cancel_event.is_set():
                    return
                self.on_event(SegmentEvent(text=text))

            def on_progress(fraction: float) -> None:
                if self.cancel_event.is_set():
                    return
                self.on_event(
                    ProgressEvent(fraction=fraction, label="Transcribing…")
                )

            segments, info = self._transcriber.transcribe(
                self.media_path,
                language=self.language,
                on_segment=on_segment,
                on_progress=on_progress,
            )

            if self.cancel_event.is_set():
                return

            output_dir = resolve_output_dir(self.settings, self.media_path)
            output_dir.mkdir(parents=True, exist_ok=True)
            output_base = output_dir / self.media_path.name
            try:
                source_hash = cache_key(self.media_path)
            except FileNotFoundError:
                source_hash = None
            document = build_document(
                media_path=self.media_path,
                duration=float(getattr(info, "duration", 0.0) or 0.0),
                language=getattr(info, "language", None),
                segments=segments,
                model_name=self.model_name,
                source_hash=source_hash,
            )
            files = exporters.write_outputs(
                output_base, segments, self.formats, document=document
            )
            elapsed = time.monotonic() - start
            self.on_event(
                DoneEvent(
                    segments=segments,
                    info=info,
                    output_files=files,
                    elapsed=elapsed,
                )
            )
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            if not self.cancel_event.is_set():
                self.on_event(ErrorEvent(message=str(exc)))

    def _emit_cache_hit_done(self, cached: Document, start: float) -> None:
        """Fast path: write derivative outputs from cached segments, skip inference.

        ``txt``/``srt``/``vtt`` are re-rendered because the user clicked
        Transcribe and expects current files. The ``json`` format is
        special-cased — we don't re-write the cache file (that would
        produce a numbered-suffix duplicate sitting next to the original);
        the existing cache path is reported back as the json output so
        the result card can link to it.
        """
        self.on_event(
            ProgressEvent(fraction=1.0, label="Loaded cached transcript")
        )
        derivative_formats = [f for f in self.formats if f != "json"]
        output_dir = resolve_output_dir(self.settings, self.media_path)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_base = output_dir / self.media_path.name
        files = (
            exporters.write_outputs(output_base, cached.segments, derivative_formats)
            if derivative_formats
            else {}
        )
        if "json" in self.formats:
            files["json"] = candidate_cache_path(self.settings, self.media_path)
        elapsed = time.monotonic() - start
        primary_source = next(iter(cached.sources.values()))
        info = _CachedInfo(language=cached.language, duration=primary_source.duration)
        self.on_event(
            DoneEvent(
                segments=list(cached.segments),
                info=info,
                output_files=files,
                elapsed=elapsed,
            )
        )
