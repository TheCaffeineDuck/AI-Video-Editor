"""Searchable language picker.

The widget shows a button labelled with the current selection. Clicking it
opens a small modal dialog with an entry that filters a listbox below it as
the user types. Selecting an entry closes the dialog.

The pure-logic filter lives in :func:`core.languages.filter_languages` so it
can be unit-tested without a Tk display.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

import customtkinter as ctk

from core.languages import LANGUAGE_OPTIONS, filter_languages, name_for
from ui import theme


class LanguagePickerDialog(ctk.CTkToplevel):
    """Modal dialog with a search field + listbox. Result on ``self.result``."""

    def __init__(self, parent: tk.Misc, *, current_code: str | None = None) -> None:
        super().__init__(parent)
        self.title("Select language")
        self.geometry("320x420")
        self.transient(parent)
        self.grab_set()

        self.result: tuple[str | None, str] | None = None

        self._entry = ctk.CTkEntry(self, placeholder_text="Type to search…")
        self._entry.pack(fill="x", padx=12, pady=(12, 6))
        self._entry.bind("<KeyRelease>", self._refresh)
        self._entry.bind("<Return>", self._select_first)
        self._entry.bind("<Escape>", lambda _e: self.destroy())

        # Standard tk.Listbox — customtkinter doesn't ship one and it's fine.
        self._listbox = tk.Listbox(self, activestyle="dotbox")
        self._listbox.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._listbox.bind("<Double-Button-1>", self._select_clicked)
        self._listbox.bind("<Return>", self._select_clicked)

        self._results: list[tuple[str | None, str]] = list(LANGUAGE_OPTIONS)
        self._render()

        # Pre-select current.
        if current_code is not None:
            for i, (code, _) in enumerate(self._results):
                if code == current_code:
                    self._listbox.selection_set(i)
                    self._listbox.see(i)
                    break
        self._entry.focus_set()

    def _render(self) -> None:
        self._listbox.delete(0, "end")
        for _code, name in self._results:
            self._listbox.insert("end", name)

    def _refresh(self, _event=None) -> None:
        query = self._entry.get()
        self._results = filter_languages(query)
        self._render()

    def _select_first(self, _event=None) -> None:
        if not self._results:
            return
        self.result = self._results[0]
        self.destroy()

    def _select_clicked(self, _event=None) -> None:
        sel = self._listbox.curselection()
        if not sel:
            return
        self.result = self._results[sel[0]]
        self.destroy()


class LanguagePicker(ctk.CTkFrame):
    def __init__(
        self,
        parent: tk.Misc,
        *,
        initial_code: str | None = None,
        on_change: Callable[[str | None], None] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(parent, fg_color="transparent", **kwargs)
        self._on_change = on_change or (lambda _c: None)
        self._code: str | None = initial_code
        label = ctk.CTkLabel(self, text="Language:", font=theme.body_font())
        label.pack(side="left", padx=(0, 8))
        self._button = ctk.CTkButton(
            self, text=self._button_text(), width=180, command=self._open_dialog
        )
        self._button.pack(side="left")

    @property
    def selected_code(self) -> str | None:
        return self._code

    def set_code(self, code: str | None) -> None:
        self._code = code
        self._button.configure(text=self._button_text())
        self._on_change(self._code)

    def _button_text(self) -> str:
        return f"{name_for(self._code)} ▾"

    def _open_dialog(self) -> None:
        dlg = LanguagePickerDialog(self.winfo_toplevel(), current_code=self._code)
        # wait_window blocks; the dialog is grab_set so it's modal.
        self.wait_window(dlg)
        if dlg.result is not None:
            self.set_code(dlg.result[0])
