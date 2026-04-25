"""Main application window. Wires components, state machine, worker thread."""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path

import customtkinter as ctk

from core import audio, exporters
from core.transcriber import Transcriber
from ui import theme
from ui.components.drop_zone import DropZone
from ui.components.model_picker import ModelPicker
from ui.components.progress_card import ProgressCard
from ui.components.result_card import ResultCard
from ui.state import (
    AppState,
    AppStateMachine,
    DoneEvent,
    ErrorEvent,
    ProgressEvent,
    SegmentEvent,
    pump_queue,
)

PUMP_INTERVAL_MS = 100


def _make_root() -> tk.Tk:
    """Create the Tk root, preferring a TkinterDnD-enabled one when available."""
    try:
        from tkinterdnd2 import TkinterDnD
    except ImportError:
        return ctk.CTk()

    class _Root(ctk.CTk, TkinterDnD.DnDWrapper):
        def __init__(self):
            super().__init__()
            self.TkdndVersion = TkinterDnD._require(self)

    return _Root()


class App:
    """Top-level controller. Owns the root window, state, worker thread, and queue."""

    def __init__(self, root: tk.Tk | None = None) -> None:
        theme.apply_theme()
        self.root = root or _make_root()
        self.root.title(theme.WINDOW_TITLE)
        self.root.geometry(f"{theme.WINDOW_DEFAULT_SIZE[0]}x{theme.WINDOW_DEFAULT_SIZE[1]}")
        self.root.minsize(*theme.WINDOW_MIN_SIZE)

        self.state = AppStateMachine()
        self.event_queue: queue.Queue = queue.Queue()
        self._transcriber: Transcriber | None = None
        self._worker: threading.Thread | None = None
        self._cancel_requested = threading.Event()

        self._build()
        self.state.on_change(self._render_for_state)
        self._render_for_state(self.state.state)
        self._schedule_pump()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ----- layout -----

    def _build(self) -> None:
        self._main = ctk.CTkFrame(self.root, fg_color="transparent")
        self._main.pack(fill="both", expand=True, padx=16, pady=16)

        self._error_banner = ctk.CTkLabel(
            self._main,
            text="",
            fg_color=theme.DANGER,
            text_color="white",
            corner_radius=6,
            anchor="w",
            padx=12,
            font=theme.body_font(),
        )
        # Hidden until an error occurs.

        # ----- idle / file_loaded view -----
        self._idle_frame = ctk.CTkFrame(self._main, fg_color="transparent")
        self.drop_zone = DropZone(
            self._idle_frame,
            on_file_selected=self._handle_file_selected,
            on_invalid_file=self._handle_invalid_file,
        )
        self.drop_zone.pack(fill="both", expand=True, padx=8, pady=8)

        controls = ctk.CTkFrame(self._idle_frame, fg_color="transparent")
        controls.pack(fill="x", padx=8, pady=8)
        self.model_picker = ModelPicker(controls, initial="base")
        self.model_picker.pack(side="left")

        self.transcribe_btn = ctk.CTkButton(
            self._idle_frame,
            text="Transcribe",
            font=theme.heading_font(),
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            height=44,
            command=self._handle_transcribe_click,
        )
        self.transcribe_btn.pack(fill="x", padx=8, pady=8)

        # ----- transcribing view -----
        self.progress_card = ProgressCard(self._main, on_cancel=self._handle_cancel)

        # ----- complete view -----
        self.result_card = ResultCard(
            self._main, on_new_transcription=self._handle_new_transcription
        )

        # Now register DND on the drop zone (root must already be initialized).
        self.drop_zone.register_dnd()

    # ----- state-driven rendering -----

    def _render_for_state(self, state: AppState) -> None:
        for w in (self._idle_frame, self.progress_card, self.result_card):
            w.pack_forget()

        if self.state.error_message:
            self._error_banner.configure(text=f"⚠  {self.state.error_message}")
            self._error_banner.pack(fill="x", pady=(0, 8))
        else:
            self._error_banner.pack_forget()

        if state in (AppState.IDLE, AppState.FILE_LOADED, AppState.ERROR):
            self._idle_frame.pack(fill="both", expand=True)
            if state == AppState.FILE_LOADED and self.state.media_path:
                self._show_loaded_preview(self.state.media_path)
                self.transcribe_btn.configure(state="normal")
            elif state == AppState.ERROR and self.state.media_path:
                self._show_loaded_preview(self.state.media_path)
                self.transcribe_btn.configure(text="Retry", state="normal")
            else:
                self.drop_zone.show_idle()
                self.transcribe_btn.configure(text="Transcribe", state="disabled")
        elif state == AppState.TRANSCRIBING:
            self.progress_card.pack(fill="both", expand=True)
            self.progress_card.set_progress(self.state.progress)
            for line in self.state.streaming_text:
                self.progress_card.append_stream(line)
        elif state == AppState.COMPLETE:
            self._show_result()
            self.result_card.pack(fill="both", expand=True)

    def _show_loaded_preview(self, path: Path) -> None:
        try:
            duration = audio.get_duration(path)
        except Exception:
            duration = 0.0
        size = path.stat().st_size if path.is_file() else 0
        self.drop_zone.show_loaded(
            name=path.name, duration_seconds=duration, size_bytes=size
        )

    def _show_result(self) -> None:
        result = self.state.result
        if result is None:
            return
        transcript = " ".join(s.text.strip() for s in result.segments).strip()
        language = getattr(result.info, "language", "") or "?"
        self.result_card.show_result(
            transcript=transcript,
            output_files=result.output_files,
            language=language,
            elapsed_seconds=result.elapsed,
        )

    # ----- user actions -----

    def _handle_file_selected(self, path: Path) -> None:
        try:
            self.state.load_file(path)
        except ValueError as exc:
            self._handle_invalid_file(str(exc))

    def _handle_invalid_file(self, message: str) -> None:
        # We surface the error as a transient message via the error banner state.
        self.state.error_message = message
        self._render_for_state(self.state.state)
        self.root.after(2500, self._clear_transient_error)

    def _clear_transient_error(self) -> None:
        if self.state.state in (AppState.IDLE, AppState.FILE_LOADED):
            self.state.error_message = None
            self._render_for_state(self.state.state)

    def _handle_transcribe_click(self) -> None:
        if self.state.state == AppState.ERROR:
            # Retry: clear error, drop back to FILE_LOADED if a media_path exists.
            self.state.error_message = None
            if self.state.media_path:
                self.state.state = AppState.FILE_LOADED
                self.state._emit()
        if self.state.state != AppState.FILE_LOADED:
            return
        self.state.start_transcribing()
        self.progress_card.reset()
        self._cancel_requested.clear()

        media_path = self.state.media_path
        model_name = self.model_picker.value
        assert media_path is not None
        self._worker = threading.Thread(
            target=self._run_transcription,
            args=(media_path, model_name),
            daemon=True,
        )
        self._worker.start()

    def _handle_cancel(self) -> None:
        self._cancel_requested.set()
        if self._transcriber is not None:
            self._transcriber.cancel()
        # Snap UI to idle immediately; worker drains in background.
        if self.state.state == AppState.TRANSCRIBING:
            self.state.cancel()

    def _handle_new_transcription(self) -> None:
        self.state.reset()

    def _on_close(self) -> None:
        self._cancel_requested.set()
        if self._transcriber is not None:
            self._transcriber.cancel()
        self.root.destroy()

    # ----- worker thread -----

    def _run_transcription(self, media_path: Path, model_name: str) -> None:
        start = time.monotonic()
        try:
            self._transcriber = Transcriber(model_name)

            def on_segment(text: str) -> None:
                if self._cancel_requested.is_set():
                    return
                self.event_queue.put(SegmentEvent(text=text))

            def on_progress(fraction: float) -> None:
                if self._cancel_requested.is_set():
                    return
                self.event_queue.put(ProgressEvent(fraction=fraction))

            segments, info = self._transcriber.transcribe(
                media_path, language=None, on_segment=on_segment, on_progress=on_progress
            )

            if self._cancel_requested.is_set():
                return

            files = exporters.write_outputs(media_path, segments, ["txt", "srt"])
            elapsed = time.monotonic() - start
            self.event_queue.put(
                DoneEvent(segments=segments, info=info, output_files=files, elapsed=elapsed)
            )
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            if not self._cancel_requested.is_set():
                self.event_queue.put(ErrorEvent(message=str(exc)))

    # ----- queue pump (UI thread) -----

    def _schedule_pump(self) -> None:
        self.pump_once()
        self.root.after(PUMP_INTERVAL_MS, self._schedule_pump)

    def pump_once(self) -> int:
        """Drain pending events into the state machine. Public so tests can call it."""
        n = pump_queue(self.event_queue, self.state)
        if n > 0:
            self._render_for_state(self.state.state)
            if self.state.state == AppState.TRANSCRIBING:
                self.progress_card.set_progress(self.state.progress)
                for line in self.state.streaming_text[-3:]:
                    self.progress_card.append_stream(line)
                self.state.streaming_text = []
        return n

    # ----- entry -----

    def run(self) -> None:
        self.root.mainloop()
