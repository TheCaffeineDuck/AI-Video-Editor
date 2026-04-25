"""Result card: scrollable transcript + Open folder / Copy / New transcription."""

from __future__ import annotations

import platform
import subprocess
import tkinter as tk
from collections.abc import Callable
from pathlib import Path

import customtkinter as ctk

from ui import theme


class ResultCard(ctk.CTkFrame):
    def __init__(
        self,
        parent: tk.Misc,
        *,
        on_new_transcription: Callable[[], None],
        **kwargs,
    ) -> None:
        super().__init__(parent, corner_radius=12, **kwargs)
        self._on_new = on_new_transcription
        self._output_dir: Path | None = None
        self._transcript_text: str = ""
        self._build()

    def _build(self) -> None:
        header = ctk.CTkLabel(self, text="Transcript", font=theme.heading_font())
        header.pack(pady=(16, 4))

        self._meta = ctk.CTkLabel(
            self, text="", font=theme.small_font(), text_color=theme.MUTED
        )
        self._meta.pack(pady=(0, 8))

        self._textbox = ctk.CTkTextbox(self, wrap="word")
        self._textbox.pack(fill="both", expand=True, padx=16)
        self._textbox.configure(state="disabled")

        button_row = ctk.CTkFrame(self, fg_color="transparent")
        button_row.pack(pady=12)
        self._open_btn = ctk.CTkButton(
            button_row, text="Open folder", command=self._open_folder
        )
        self._open_btn.pack(side="left", padx=4)
        self._copy_btn = ctk.CTkButton(button_row, text="Copy all", command=self._copy_all)
        self._copy_btn.pack(side="left", padx=4)
        self._new_btn = ctk.CTkButton(
            button_row,
            text="New transcription",
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            command=self._on_new,
        )
        self._new_btn.pack(side="left", padx=4)

    # ----- public API -----

    def show_result(
        self,
        *,
        transcript: str,
        output_files: dict[str, Path],
        language: str,
        elapsed_seconds: float,
    ) -> None:
        self._transcript_text = transcript
        self._textbox.configure(state="normal")
        self._textbox.delete("1.0", "end")
        self._textbox.insert("1.0", transcript)
        self._textbox.configure(state="disabled")

        word_count = len(transcript.split())
        first_path = next(iter(output_files.values()))
        self._output_dir = first_path.parent
        formats = ", ".join(sorted(output_files.keys()))
        self._meta.configure(
            text=(
                f"{first_path.parent}    ·    {word_count} words    ·    "
                f"language: {language}    ·    {elapsed_seconds:.1f}s    ·    {formats}"
            )
        )

    # ----- button handlers -----

    def _open_folder(self) -> None:
        if self._output_dir is None or not self._output_dir.exists():
            return
        system = platform.system()
        if system == "Darwin":
            subprocess.run(["open", str(self._output_dir)], check=False)
        elif system == "Windows":  # pragma: no cover - mac dev box
            subprocess.run(["explorer", str(self._output_dir)], check=False)
        else:  # pragma: no cover
            subprocess.run(["xdg-open", str(self._output_dir)], check=False)

    def _copy_all(self) -> None:
        if not self._transcript_text:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(self._transcript_text)
        except tk.TclError:
            pass
