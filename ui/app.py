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
from core.model_loader import download_model
from core.models import is_downloaded
from core.settings import Settings, load_settings
from core.transcriber import Transcriber
from ui import theme
from ui.components.drop_zone import DropZone
from ui.components.language_picker import LanguagePicker
from ui.components.model_picker import ModelPicker
from ui.components.output_formats import OutputFormatPicker
from ui.components.progress_card import ProgressCard
from ui.components.result_card import ResultCard
from ui.components.settings_panel import open_settings
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

    def __init__(
        self,
        root: tk.Tk | None = None,
        *,
        settings: Settings | None = None,
    ) -> None:
        theme.apply_theme()
        self.root = root or _make_root()
        self.root.title(theme.WINDOW_TITLE)
        self.root.geometry(f"{theme.WINDOW_DEFAULT_SIZE[0]}x{theme.WINDOW_DEFAULT_SIZE[1]}")
        self.root.minsize(*theme.WINDOW_MIN_SIZE)

        self.settings = settings or load_settings()
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
        # Top bar with gear icon.
        topbar = ctk.CTkFrame(self.root, fg_color="transparent")
        topbar.pack(fill="x", padx=16, pady=(12, 0))
        ctk.CTkButton(
            topbar,
            text="⚙",
            width=32,
            fg_color="transparent",
            hover_color="#E5E7EB",
            text_color=theme.MUTED,
            command=self._open_settings,
        ).pack(side="right")

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
        controls.pack(fill="x", padx=8, pady=(8, 0))
        self.model_picker = ModelPicker(controls, initial=self.settings.default_model)
        self.model_picker.pack(side="left", anchor="w")

        controls2 = ctk.CTkFrame(self._idle_frame, fg_color="transparent")
        controls2.pack(fill="x", padx=8, pady=(4, 0))
        self.language_picker = LanguagePicker(
            controls2, initial_code=self.settings.default_language
        )
        self.language_picker.pack(side="left", anchor="w")

        controls3 = ctk.CTkFrame(self._idle_frame, fg_color="transparent")
        controls3.pack(fill="x", padx=8, pady=(4, 8))
        self.output_picker = OutputFormatPicker(
            controls3,
            initial=self.settings.output_formats,
            on_change=self._handle_output_format_change,
        )
        self.output_picker.pack(side="left", anchor="w")

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
                self._refresh_transcribe_button(text="Transcribe")
            elif state == AppState.ERROR and self.state.media_path:
                self._show_loaded_preview(self.state.media_path)
                self._refresh_transcribe_button(text="Retry")
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

    def _refresh_transcribe_button(self, *, text: str) -> None:
        if self.output_picker.has_selection:
            self.transcribe_btn.configure(text=text, state="normal")
        else:
            self.transcribe_btn.configure(
                text=f"{text} (pick an output format)", state="disabled"
            )

    def _handle_output_format_change(self, _formats: list[str]) -> None:
        # Re-render so Transcribe button enable/disable updates immediately.
        if self.state.state in (AppState.FILE_LOADED, AppState.ERROR):
            self._refresh_transcribe_button(
                text="Retry" if self.state.state == AppState.ERROR else "Transcribe"
            )

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
        # Surface as a transient banner without a persistent ERROR state.
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
        if not self.output_picker.has_selection:
            self._handle_invalid_file("Select at least one output format.")
            return
        self.state.start_transcribing()
        self.progress_card.reset()
        self.progress_card.set_label("Preparing…")
        self._cancel_requested.clear()

        media_path = self.state.media_path
        model_name = self.model_picker.value
        language = self.language_picker.selected_code
        formats = list(self.output_picker.formats)
        assert media_path is not None
        self._worker = threading.Thread(
            target=self._run_transcription,
            args=(media_path, model_name, language, formats),
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

    def _open_settings(self) -> None:
        open_settings(self.root, current=self.settings, on_save=self._apply_settings)

    def _apply_settings(self, new: Settings) -> None:
        self.settings = new
        # Live-apply the affected widgets where it makes sense.
        try:
            self.model_picker.set(new.default_model)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.language_picker.set_code(new.default_language)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.output_picker.set_formats(new.output_formats)
        except Exception:  # noqa: BLE001
            pass

    # ----- worker thread -----

    def _run_transcription(
        self,
        media_path: Path,
        model_name: str,
        language: str | None,
        formats: list[str],
    ) -> None:
        start = time.monotonic()
        try:
            # First-run: download model with real progress.
            if not is_downloaded(model_name):
                def on_dl(fraction: float, label: str) -> None:
                    if self._cancel_requested.is_set():
                        return
                    self.event_queue.put(ProgressEvent(fraction=fraction, label=label))
                download_model(model_name, on_progress=on_dl)
                if self._cancel_requested.is_set():
                    return

            self._transcriber = Transcriber(
                model_name,
                device=self.settings.compute_device,
                compute_type=self.settings.compute_type,
            )

            def on_segment(text: str) -> None:
                if self._cancel_requested.is_set():
                    return
                self.event_queue.put(SegmentEvent(text=text))

            def on_progress(fraction: float) -> None:
                if self._cancel_requested.is_set():
                    return
                self.event_queue.put(ProgressEvent(fraction=fraction, label="Transcribing…"))

            segments, info = self._transcriber.transcribe(
                media_path, language=language, on_segment=on_segment, on_progress=on_progress
            )

            if self._cancel_requested.is_set():
                return

            output_dir = (
                Path(self.settings.output_dir)
                if self.settings.output_dir
                else media_path.parent
            )
            output_dir.mkdir(parents=True, exist_ok=True)
            output_base = output_dir / media_path.name
            files = exporters.write_outputs(output_base, segments, formats)
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
                self.progress_card.set_progress(
                    self.state.progress,
                    label=self.state.progress_label or "Transcribing…",
                )
                for line in self.state.streaming_text[-3:]:
                    self.progress_card.append_stream(line)
                self.state.streaming_text = []
        return n

    # ----- entry -----

    def run(self) -> None:
        self.root.mainloop()
