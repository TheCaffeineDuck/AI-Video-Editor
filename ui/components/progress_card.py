"""Progress card shown during transcription. Cancel button + streaming preview."""

from __future__ import annotations

import time
import tkinter as tk
from collections.abc import Callable

import customtkinter as ctk

from ui import theme


class ProgressCard(ctk.CTkFrame):
    def __init__(
        self,
        parent: tk.Misc,
        *,
        on_cancel: Callable[[], None],
        **kwargs,
    ) -> None:
        super().__init__(parent, corner_radius=12, **kwargs)
        self._on_cancel = on_cancel
        self._start_time: float | None = None
        self._last_progress: float = 0.0
        self._build()

    def _build(self) -> None:
        self._label = ctk.CTkLabel(
            self, text="Preparing…", font=theme.heading_font()
        )
        self._label.pack(pady=(16, 8))

        self._bar = ctk.CTkProgressBar(self, width=500)
        self._bar.set(0.0)
        self._bar.pack(padx=24)

        self._timing = ctk.CTkLabel(
            self, text="", font=theme.small_font(), text_color=theme.MUTED
        )
        self._timing.pack(pady=(8, 0))

        self._stream = ctk.CTkTextbox(self, height=80, wrap="word")
        self._stream.configure(state="disabled")
        self._stream.pack(fill="x", padx=24, pady=(12, 8))

        self._cancel_btn = ctk.CTkButton(
            self,
            text="Cancel",
            fg_color="transparent",
            border_width=1,
            text_color=theme.DANGER,
            border_color=theme.DANGER,
            hover_color="#FEE2E2",
            command=self._handle_cancel,
        )
        self._cancel_btn.pack(pady=(4, 16))

    # ----- public API -----

    def reset(self) -> None:
        self._start_time = time.monotonic()
        self._last_progress = 0.0
        self._label.configure(text="Preparing…")
        self._bar.set(0.0)
        self._timing.configure(text="")
        self._set_stream_text("")

    def set_label(self, text: str) -> None:
        self._label.configure(text=text)

    def set_progress(self, fraction: float, label: str | None = None) -> None:
        fraction = max(0.0, min(1.0, fraction))
        self._last_progress = fraction
        self._bar.set(fraction)
        if label:
            self._label.configure(text=label)
        self._update_timing()

    def append_stream(self, text: str, *, max_lines: int = 3) -> None:
        existing = self._read_stream_text().splitlines()
        existing.append(text.strip())
        existing = [line for line in existing if line]
        existing = existing[-max_lines:]
        self._set_stream_text("\n".join(existing))

    # ----- internals -----

    def _handle_cancel(self) -> None:
        self._cancel_btn.configure(state="disabled", text="Cancelling…")
        self._on_cancel()

    def _update_timing(self) -> None:
        if self._start_time is None:
            return
        elapsed = time.monotonic() - self._start_time
        if self._last_progress > 0.01:
            total = elapsed / self._last_progress
            remaining = max(0.0, total - elapsed)
            self._timing.configure(
                text=f"Elapsed {elapsed:.0f}s   ·   ~{remaining:.0f}s remaining"
            )
        else:
            self._timing.configure(text=f"Elapsed {elapsed:.0f}s")

    def _set_stream_text(self, text: str) -> None:
        self._stream.configure(state="normal")
        self._stream.delete("1.0", "end")
        if text:
            self._stream.insert("1.0", text)
        self._stream.configure(state="disabled")

    def _read_stream_text(self) -> str:
        self._stream.configure(state="normal")
        text = self._stream.get("1.0", "end-1c")
        self._stream.configure(state="disabled")
        return text
