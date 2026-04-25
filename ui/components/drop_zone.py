"""Drop zone widget: drag-and-drop OR click-to-browse for a media file."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk

from ui import theme
from ui.state import SUPPORTED_EXTENSIONS, is_supported_media

_DIALOG_FILETYPES = [
    ("Media files", "*.mp4 *.mov *.mkv *.mp3 *.wav *.m4a"),
    ("All files", "*.*"),
]


class DropZone(ctk.CTkFrame):
    """A dashed-bordered card that accepts drops or opens a file picker on click.

    Tests instantiate this under a hidden Tk root; DND target registration is
    deferred to :meth:`register_dnd`, which silently no-ops when the root
    isn't a TkinterDnD-enabled root.
    """

    def __init__(
        self,
        parent: tk.Misc,
        *,
        on_file_selected: Callable[[Path], None],
        on_invalid_file: Callable[[str], None] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(
            parent,
            corner_radius=12,
            border_width=2,
            border_color=theme.MUTED,
            fg_color="transparent",
            **kwargs,
        )
        self.on_file_selected = on_file_selected
        self.on_invalid_file = on_invalid_file or (lambda _msg: None)

        self._build()
        self._bind_click_to_browse()

    # ----- layout -----

    def _build(self) -> None:
        wrap = ctk.CTkFrame(self, fg_color="transparent")
        wrap.pack(expand=True)

        self._icon_label = ctk.CTkLabel(wrap, text="📁", font=("Apple Color Emoji", 36))
        self._icon_label.pack()

        self._title_label = ctk.CTkLabel(
            wrap, text="Drop a file here", font=theme.heading_font()
        )
        self._title_label.pack(pady=(8, 0))

        self._sub_label = ctk.CTkLabel(
            wrap, text="or click to browse", font=theme.body_font(), text_color=theme.MUTED
        )
        self._sub_label.pack(pady=(2, 0))

        formats = "  ·  ".join(ext.lstrip(".").upper() for ext in sorted(SUPPORTED_EXTENSIONS))
        self._formats_label = ctk.CTkLabel(
            wrap, text=formats, font=theme.small_font(), text_color=theme.MUTED
        )
        self._formats_label.pack(pady=(12, 0))

    def _bind_click_to_browse(self) -> None:
        for w in (self, self._icon_label, self._title_label, self._sub_label,
                  self._formats_label):
            w.bind("<Button-1>", self._open_file_dialog)

    # ----- file selection -----

    def _open_file_dialog(self, _event=None) -> None:
        chosen = filedialog.askopenfilename(
            title="Select a media file", filetypes=_DIALOG_FILETYPES
        )
        if not chosen:
            return
        self._accept(Path(chosen))

    def _accept(self, path: Path) -> None:
        if not is_supported_media(path):
            self.on_invalid_file(f"Unsupported file type: {path.suffix or '(no extension)'}")
            return
        if not path.is_file():
            self.on_invalid_file(f"File not found: {path}")
            return
        self.on_file_selected(path)

    # ----- DND wiring (best-effort; safe to call regardless of root type) -----

    def register_dnd(self) -> bool:
        """Register this widget as a TkinterDnD drop target. Returns True on success."""
        try:
            from tkinterdnd2 import DND_FILES
        except ImportError:
            return False
        try:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<DropEnter>>", self._on_drag_enter)
            self.dnd_bind("<<DropLeave>>", self._on_drag_leave)
            self.dnd_bind("<<Drop>>", self._on_drop)
            return True
        except (AttributeError, tk.TclError):
            # Root isn't a TkinterDnD root; nothing to register.
            return False

    def _on_drag_enter(self, _event):
        try:
            self.configure(border_color=theme.ACCENT)
        except tk.TclError:
            pass

    def _on_drag_leave(self, _event):
        try:
            self.configure(border_color=theme.MUTED)
        except tk.TclError:
            pass

    def _on_drop(self, event):
        self._on_drag_leave(event)
        # tkinterdnd2 returns a Tcl-list-formatted string. Use splitlist to
        # parse it correctly even when paths contain spaces / braces.
        try:
            parts = self.tk.splitlist(event.data)
        except (AttributeError, tk.TclError):
            parts = [event.data] if isinstance(event.data, str) else []
        if not parts:
            return
        # Take the first dropped file; the spec covers a single-file workflow.
        self._accept(Path(parts[0]))

    # ----- preview state shown after a file is loaded -----

    def show_loaded(self, *, name: str, duration_seconds: float, size_bytes: int) -> None:
        mins = int(duration_seconds // 60)
        secs = duration_seconds - mins * 60
        size_mb = size_bytes / (1024 * 1024)
        self._icon_label.configure(text="🎞")
        self._title_label.configure(text=name)
        self._sub_label.configure(text=f"{mins:d}:{secs:05.2f}    ·    {size_mb:.1f} MB")
        self._formats_label.configure(text="Click to choose a different file")

    def show_idle(self) -> None:
        self._icon_label.configure(text="📁")
        self._title_label.configure(text="Drop a file here")
        self._sub_label.configure(text="or click to browse")
        formats = "  ·  ".join(ext.lstrip(".").upper() for ext in sorted(SUPPORTED_EXTENSIONS))
        self._formats_label.configure(text=formats)
