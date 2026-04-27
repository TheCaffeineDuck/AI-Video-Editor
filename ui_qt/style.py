"""Theme / palette constants for the Qt UI.

Re-uses the colour values from :mod:`ui.theme` so the two apps look
related during the cross-over period. The Qt UI leans on Qt's native
widget look on macOS; the only QSS we ship is for the drop-zone's
dashed border and a few muted-text rules.
"""

from __future__ import annotations

ACCENT = "#3B82F6"
ACCENT_HOVER = "#2563EB"
DANGER = "#DC2626"
SUCCESS = "#10B981"
MUTED = "#6B7280"
DROP_ZONE_HOVER_BG = "#EFF6FF"

WINDOW_TITLE = "Whisper Transcriber"
WINDOW_DEFAULT_SIZE = (760, 600)
WINDOW_MIN_SIZE = (640, 480)


def drop_zone_qss(*, active: bool = False) -> str:
    """QSS for the drop-zone frame. ``active`` highlights during a drag."""
    border_color = ACCENT if active else MUTED
    background = DROP_ZONE_HOVER_BG if active else "transparent"
    return f"""
        QFrame#DropZone {{
            border: 2px dashed {border_color};
            border-radius: 12px;
            background-color: {background};
        }}
        QFrame#DropZone QLabel {{
            background: transparent;
        }}
    """


def muted_label_qss() -> str:
    return f"color: {MUTED};"


def accent_button_qss() -> str:
    return f"""
        QPushButton {{
            background-color: {ACCENT};
            color: white;
            border: none;
            border-radius: 6px;
            padding: 10px 16px;
            font-weight: 600;
        }}
        QPushButton:hover {{
            background-color: {ACCENT_HOVER};
        }}
        QPushButton:disabled {{
            background-color: #9CA3AF;
        }}
    """
