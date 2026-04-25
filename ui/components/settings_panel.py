"""Settings modal (spec §4.9): defaults, device/precision, cache management."""

from __future__ import annotations

import platform
import shutil
import subprocess
import tkinter as tk
from collections.abc import Callable
from tkinter import filedialog

import customtkinter as ctk

from core.models import MODEL_NAMES, cache_root
from core.settings import Settings, save_settings
from ui import theme

VERSION = "0.1.0"
DEVICES = ("auto", "cpu", "cuda")
COMPUTE_TYPES = ("auto", "int8", "float16", "float32")


class SettingsPanel(ctk.CTkToplevel):
    """Modal settings sheet. Returns the new ``Settings`` via ``on_save``."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        current: Settings,
        on_save: Callable[[Settings], None],
    ) -> None:
        super().__init__(parent)
        self.title("Settings")
        self.geometry("520x540")
        self.transient(parent)
        self.grab_set()
        self._on_save = on_save
        self._current = current

        self._model_var = tk.StringVar(value=current.default_model)
        self._device_var = tk.StringVar(value=current.compute_device)
        self._compute_var = tk.StringVar(value=current.compute_type)
        self._output_dir_var = tk.StringVar(value=current.output_dir or "")

        self._build()

    def _build(self) -> None:
        wrap = ctk.CTkScrollableFrame(self, fg_color="transparent")
        wrap.pack(fill="both", expand=True, padx=12, pady=12)

        self._section(wrap, "Defaults")
        self._labeled_option(
            wrap, "Default model", self._model_var, MODEL_NAMES
        )

        # Output directory row
        out_row = ctk.CTkFrame(wrap, fg_color="transparent")
        out_row.pack(fill="x", pady=(8, 0))
        ctk.CTkLabel(out_row, text="Default output folder", width=160, anchor="w").pack(
            side="left"
        )
        self._output_entry = ctk.CTkEntry(
            out_row, textvariable=self._output_dir_var, placeholder_text="(same as input)"
        )
        self._output_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(out_row, text="Browse…", width=80, command=self._browse_output).pack(
            side="left"
        )

        self._section(wrap, "Compute")
        self._labeled_option(wrap, "Device", self._device_var, DEVICES)
        self._labeled_option(wrap, "Precision", self._compute_var, COMPUTE_TYPES)

        self._section(wrap, "Model cache")
        cache_row = ctk.CTkFrame(wrap, fg_color="transparent")
        cache_row.pack(fill="x", pady=(8, 0))
        ctk.CTkButton(
            cache_row, text="Open cache folder", command=self._open_cache_folder
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            cache_row,
            text="Clear cache",
            fg_color="transparent",
            text_color=theme.DANGER,
            border_color=theme.DANGER,
            border_width=1,
            hover_color="#FEE2E2",
            command=self._clear_cache,
        ).pack(side="left")

        self._section(wrap, "About")
        ctk.CTkLabel(
            wrap,
            text=(
                f"Whisper Transcriber v{VERSION}\n"
                "MIT licensed. Powered by faster-whisper / CTranslate2.\n"
                "Apple Silicon: CPU-only inference (no Metal backend yet)."
            ),
            justify="left",
            anchor="w",
            text_color=theme.MUTED,
            font=theme.small_font(),
        ).pack(fill="x", pady=(8, 0))

        # Footer actions.
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=12, pady=12)
        ctk.CTkButton(footer, text="Cancel", command=self.destroy).pack(side="right", padx=4)
        ctk.CTkButton(
            footer,
            text="Save",
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            command=self._save,
        ).pack(side="right", padx=4)

    def _section(self, parent: tk.Misc, title: str) -> None:
        ctk.CTkLabel(parent, text=title, font=theme.heading_font(), anchor="w").pack(
            fill="x", pady=(16, 4)
        )

    def _labeled_option(
        self, parent: tk.Misc, label: str, var: tk.StringVar, values
    ) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=(4, 0))
        ctk.CTkLabel(row, text=label, width=160, anchor="w").pack(side="left")
        ctk.CTkOptionMenu(row, variable=var, values=list(values)).pack(side="left")

    def _browse_output(self) -> None:
        chosen = filedialog.askdirectory(title="Choose default output folder")
        if chosen:
            self._output_dir_var.set(chosen)

    def _open_cache_folder(self) -> None:
        path = cache_root()
        path.mkdir(parents=True, exist_ok=True)
        if platform.system() == "Darwin":
            subprocess.run(["open", str(path)], check=False)
        elif platform.system() == "Windows":  # pragma: no cover - mac dev box
            subprocess.run(["explorer", str(path)], check=False)
        else:  # pragma: no cover
            subprocess.run(["xdg-open", str(path)], check=False)

    def _clear_cache(self) -> None:
        path = cache_root()
        if not path.exists():
            return
        shutil.rmtree(path)

    def _save(self) -> None:
        out_dir = self._output_dir_var.get().strip() or None
        new = Settings(
            default_model=self._model_var.get(),
            default_language=self._current.default_language,
            output_formats=list(self._current.output_formats),
            output_dir=out_dir,
            compute_device=self._device_var.get(),
            compute_type=self._compute_var.get(),
        )
        save_settings(new)
        self._on_save(new)
        self.destroy()


def open_settings(
    parent: tk.Misc, current: Settings, on_save: Callable[[Settings], None]
) -> SettingsPanel:
    return SettingsPanel(parent, current=current, on_save=on_save)
