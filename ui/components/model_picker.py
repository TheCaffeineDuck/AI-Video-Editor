"""Radio-button row for picking a Whisper model size."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

import customtkinter as ctk

from core.models import MODEL_NAMES, MODEL_REGISTRY, is_downloaded
from ui import theme


class ModelPicker(ctk.CTkFrame):
    def __init__(
        self,
        parent: tk.Misc,
        *,
        initial: str = "base",
        on_change: Callable[[str], None] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(parent, fg_color="transparent", **kwargs)
        if initial not in MODEL_REGISTRY:
            raise ValueError(f"unknown model {initial!r}")
        self._on_change = on_change or (lambda _name: None)
        self._var = tk.StringVar(value=initial)
        self._radios: dict[str, ctk.CTkRadioButton] = {}
        self._build()

    def _build(self) -> None:
        label = ctk.CTkLabel(self, text="Model:", font=theme.body_font())
        label.pack(side="left", padx=(0, 8))

        for name in MODEL_NAMES:
            info = MODEL_REGISTRY[name]
            badge = " ✓" if is_downloaded(name) else ""
            tooltip = f"{info.size_mb} MB · {info.speed_label}"
            if name == "large-v3":
                tooltip += " · tight on 16 GB RAM"
            label_text = f"{name}{badge}"
            radio = ctk.CTkRadioButton(
                self,
                text=label_text,
                variable=self._var,
                value=name,
                command=self._fire_change,
            )
            radio.pack(side="left", padx=6)
            # No native tooltip in customtkinter; expose via _tooltip_text for now.
            radio._tooltip_text = tooltip  # noqa: SLF001
            self._radios[name] = radio

    def _fire_change(self) -> None:
        self._on_change(self.value)

    @property
    def value(self) -> str:
        return self._var.get()

    def set(self, name: str) -> None:
        if name not in MODEL_REGISTRY:
            raise ValueError(f"unknown model {name!r}")
        self._var.set(name)
        self._fire_change()

    def refresh_badges(self) -> None:
        """Re-check download status and update labels (after a fresh download)."""
        for name, radio in self._radios.items():
            badge = " ✓" if is_downloaded(name) else ""
            radio.configure(text=f"{name}{badge}")
