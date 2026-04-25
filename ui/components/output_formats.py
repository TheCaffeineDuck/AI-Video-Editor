"""Checkbox row for selecting output formats. Spec §4.2 default: txt + srt."""

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
)


class OutputFormatPicker(ctk.CTkFrame):
    def __init__(
        self,
        parent: tk.Misc,
        *,
        initial: Iterable[str] = ("txt", "srt"),
        on_change: Callable[[list[str]], None] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(parent, fg_color="transparent", **kwargs)
        self._on_change = on_change or (lambda _f: None)
        self._vars: dict[str, tk.BooleanVar] = {}
        initial_set = set(initial)
        self._build(initial_set)

    def _build(self, initial: set[str]) -> None:
        label = ctk.CTkLabel(self, text="Output:", font=theme.body_font())
        label.pack(side="left", padx=(0, 8))
        for fmt, display in ALL_FORMATS:
            var = tk.BooleanVar(value=fmt in initial)
            cb = ctk.CTkCheckBox(
                self,
                text=display,
                variable=var,
                command=self._fire,
            )
            cb.pack(side="left", padx=4)
            self._vars[fmt] = var

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
