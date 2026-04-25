"""Theme constants and one-time appearance setup for the UI."""

from __future__ import annotations

import customtkinter as ctk

# Spec §4.1
ACCENT = "#3B82F6"          # blue-500
ACCENT_HOVER = "#2563EB"    # blue-600
DANGER = "#DC2626"          # red-600
SUCCESS = "#10B981"         # emerald-500
MUTED = "#6B7280"           # gray-500

WINDOW_TITLE = "Whisper Transcriber"
WINDOW_DEFAULT_SIZE = (760, 560)
WINDOW_MIN_SIZE = (640, 480)

FONT_FAMILY = "SF Pro Text"  # falls back to system default if missing
FONT_SIZE_BASE = 13
FONT_SIZE_HEADING = 16
FONT_SIZE_SMALL = 11

_themed = False


def apply_theme() -> None:
    """Apply the customtkinter appearance + color theme. Idempotent."""
    global _themed
    if _themed:
        return
    ctk.set_appearance_mode("System")
    ctk.set_default_color_theme("blue")
    _themed = True


def heading_font() -> tuple[str, int, str]:
    return (FONT_FAMILY, FONT_SIZE_HEADING, "bold")


def body_font() -> tuple[str, int]:
    return (FONT_FAMILY, FONT_SIZE_BASE)


def small_font() -> tuple[str, int]:
    return (FONT_FAMILY, FONT_SIZE_SMALL)
