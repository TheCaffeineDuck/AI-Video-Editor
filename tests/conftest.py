"""Shared pytest fixtures."""

from __future__ import annotations

import os
import tkinter as tk

import pytest


def _can_open_display() -> bool:
    """Return True if a Tk display can actually be opened.

    On macOS this is almost always True under a normal user session; in
    headless CI it isn't. Skip UI tests cleanly when no display is available.
    """
    if os.environ.get("WHISPER_NO_TK"):
        return False
    try:
        root = tk.Tk()
    except tk.TclError:
        return False
    root.destroy()
    return True


@pytest.fixture
def tk_root():
    """Hidden Tk root for UI construction tests.

    Yields a withdrawn root, destroys it on teardown so each test starts clean.
    Skips the test (with a clear message) when no display is available.
    """
    if not _can_open_display():
        pytest.skip("no Tk display available")
    root = tk.Tk()
    root.withdraw()
    try:
        yield root
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
