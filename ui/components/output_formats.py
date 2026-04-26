"""Checkbox row for selecting output formats.

Phase 4e adds ``"json"`` (the Document/editable-project artifact) to
the default-on set, alongside the existing txt/srt. The nudge label
reminds existing users why they'd want to opt in.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Iterable

import customtkinter as ctk

from ui import theme

# Order matters for display.
ALL_FORMATS: tuple[tuple[str, str], ...] = (
    ("txt", "Text (.txt)"),
    ("srt", "Subtitles (.srt)"),
    ("vtt", "WebVTT (.vtt)"),
    ("json", "Editable project (.transcribe.json)"),
)


class OutputFormatPicker(ctk.CTkFrame):
    def __init__(
        self,
        parent: tk.Misc,
        *,
        initial: Iterable[str] = ("txt", "srt", "json"),
        on_change: Callable[[list[str]], None] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(parent, fg_color="transparent", **kwargs)
        self._on_change = on_change or (lambda _f: None)
        self._vars: dict[str, tk.BooleanVar] = {}
        initial_set = set(initial)
        self._build(initial_set)

    def _build(self, initial: set[str]) -> None:
        # Top row: label + checkboxes.
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x")
        label = ctk.CTkLabel(row, text="Output:", font=theme.body_font())
        label.pack(side="left", padx=(0, 8))
        for fmt, display in ALL_FORMATS:
            var = tk.BooleanVar(value=fmt in initial)
            cb = ctk.CTkCheckBox(
                row,
                text=display,
                variable=var,
                command=self._fire,
            )
            cb.pack(side="left", padx=4)
            self._vars[fmt] = var

        # Subtle nudge — explains why users would want JSON on. Always
        # visible (not just when JSON is off) to avoid layout shifts.
        ctk.CTkLabel(
            self,
            text="JSON is required for the upcoming editor view.",
            font=theme.small_font(),
            text_color=theme.MUTED,
            anchor="w",
        ).pack(fill="x", padx=(56, 0), pady=(2, 0))

    def _fire(self) -> None:
        self._on_change(self.formats)

    @property
    def formats(self) -> list[str]:
        return [fmt for fmt, var in self._vars.items() if var.get()]

    @property
    def has_selection(self) -> bool:
        return any(v.get() for v in self._vars.values())

    def set_formats(self, formats: Iterable[str]) -> None:
        chosen = set(formats)
        for fmt, var in self._vars.items():
            var.set(fmt in chosen)
        self._fire()
