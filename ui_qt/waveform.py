"""Waveform strip — peaks + cut dimming + playhead, with click-to-seek.

Phase 5d replaces the 5b placeholder. Decision 5 is non-negotiable: the
strip is **navigation + visualization only** — no drag handles, no cut
creation. Three orthogonal layers compose on every paint:

1. **Peaks.** One vertical bar per pixel column. The painter
   downsamples the (typically 4000-bucket) peak array to ``width()`` at
   render time so the cached array is independent of the widget's
   current size.
2. **Cut dimming.** Every region *not* covered by a kept :class:`Range`
   is overlaid with a translucent rectangle, so the cut spans read as
   "muted" against the kept spans.
3. **Playhead.** A 1px vertical line at the time-mapped column,
   coloured with the palette's highlight. Drawn last so it sits above
   peaks and dimming.

Loading state: when the controller flips ``set_loading(True)`` the
strip paints a flat mid-tone with centered text. Used while peak
generation runs on the worker thread for a fresh source (no cache).

The class kept the 5b placeholder's shape — fixed-height, no resize
controls — and just upgraded the body. The original ``WaveformPlaceholder``
name is kept as an alias for the small number of test imports that
referred to it; the EditorPane uses :class:`WaveformStrip` directly.
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
from PySide6.QtCore import QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import QWidget

from core.document import Range

_DIM_OVERLAY = QColor(0, 0, 0, 130)  # ~50% black, reads as "muted"
_LOADING_BG = QColor(60, 60, 60)
_LOADING_TEXT = QColor(220, 220, 220)
_PEAK_FALLBACK = QColor(160, 160, 160)


class WaveformStrip(QWidget):
    """Thin waveform-and-cut-region strip below the transcript.

    Signals:
        seek_requested(int): user clicked at some x; the time at that
            column is reported in milliseconds. EditorPane forwards this
            to the video player. Click-only — no drag handles, no
            range creation per Decision 5.
    """

    seek_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(64)
        self.setMaximumHeight(96)
        # Force the widget to take the full strip width — vertical
        # sizing is fixed, horizontal sizing is dictated by the splitter.
        self.setSizePolicy(self.sizePolicy().horizontalPolicy(),
                           self.sizePolicy().verticalPolicy())

        self._peaks: np.ndarray | None = None
        self._duration_s: float = 0.0
        self._ranges: list[Range] = []
        self._position_ms: int = 0
        self._loading: bool = False

    # ----- public API -----

    def set_peaks(self, peaks: np.ndarray, duration_s: float) -> None:
        """Install the peak array. ``peaks`` is ``(N, 2)`` of (min, max).

        ``duration_s`` is the source media's duration; column-x ↔ time
        mapping uses it directly. Calling this implicitly clears the
        loading flag.
        """
        if peaks.ndim != 2 or peaks.shape[1] != 2:
            raise ValueError(
                f"peaks must be shape (N, 2); got {peaks.shape}"
            )
        self._peaks = np.asarray(peaks, dtype=np.float32)
        self._duration_s = max(0.0, float(duration_s))
        self._loading = False
        self.update()

    def set_ranges(self, ranges: Iterable[Range], total_duration_s: float) -> None:
        """Update the keep-range list. Cuts are everything not covered."""
        self._ranges = list(ranges)
        if total_duration_s > 0:
            self._duration_s = float(total_duration_s)
        self.update()

    def set_position(self, ms: int) -> None:
        """Update the playhead position in milliseconds."""
        if ms < 0:
            ms = 0
        if ms == self._position_ms:
            return
        self._position_ms = int(ms)
        self.update()

    def set_loading(self, loading: bool) -> None:
        """Flip the "Generating waveform…" placeholder on/off."""
        if loading == self._loading:
            return
        self._loading = bool(loading)
        self.update()

    @property
    def peaks(self) -> np.ndarray | None:
        return self._peaks

    @property
    def ranges(self) -> list[Range]:
        return list(self._ranges)

    @property
    def position_ms(self) -> int:
        return self._position_ms

    @property
    def is_loading(self) -> bool:
        return self._loading

    # ----- paint -----

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 (Qt API)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            rect = self.rect()

            if self._loading or self._peaks is None:
                self._paint_loading(painter, rect)
                return

            self._paint_peaks(painter, rect)
            self._paint_cut_dimming(painter, rect)
            self._paint_playhead(painter, rect)
        finally:
            painter.end()

    def _paint_loading(self, painter: QPainter, rect: QRect) -> None:
        painter.fillRect(rect, _LOADING_BG)
        painter.setPen(_LOADING_TEXT)
        painter.drawText(
            rect, Qt.AlignmentFlag.AlignCenter, "Generating waveform…"
        )

    def _paint_peaks(self, painter: QPainter, rect: QRect) -> None:
        # Background: palette base so peaks read as "ink on paper".
        painter.fillRect(rect, self.palette().base())

        peaks = self._peaks
        if peaks is None or peaks.size == 0:
            return
        width = rect.width()
        if width <= 0:
            return
        height = rect.height()
        mid_y = height / 2.0

        bucket_count = peaks.shape[0]
        # Each pixel column samples one bucket's (min, max). For
        # bucket_count > width we down-sample by taking the most-extreme
        # min and most-extreme max across all buckets that map to this
        # column — preserves visible peaks rather than averaging them away.
        # For bucket_count < width we step the bucket index slowly across
        # columns; either way it's an O(width) loop.
        col_pen = self.palette().windowText().color()
        if not col_pen.isValid():
            col_pen = _PEAK_FALLBACK
        painter.setPen(QPen(col_pen, 1))

        if bucket_count >= width:
            # Group buckets into columns.
            edges = np.linspace(0, bucket_count, width + 1, dtype=np.int64)
            for x in range(width):
                lo, hi = edges[x], edges[x + 1]
                if hi <= lo:
                    hi = lo + 1
                slice_ = peaks[lo:hi]
                col_min = float(slice_[:, 0].min())
                col_max = float(slice_[:, 1].max())
                self._draw_peak_column(
                    painter, x + rect.x(), mid_y, col_min, col_max, height
                )
        else:
            # bucket_count < width: stretch buckets across columns.
            for x in range(width):
                idx = int(x * bucket_count / max(1, width))
                if idx >= bucket_count:
                    idx = bucket_count - 1
                col_min = float(peaks[idx, 0])
                col_max = float(peaks[idx, 1])
                self._draw_peak_column(
                    painter, x + rect.x(), mid_y, col_min, col_max, height
                )

    @staticmethod
    def _draw_peak_column(
        painter: QPainter,
        x: int,
        mid_y: float,
        col_min: float,
        col_max: float,
        height: float,
    ) -> None:
        """Draw one vertical bar from ``mid + col_min*half`` to ``mid + col_max*half``."""
        half = height / 2.0
        # Clip into widget; col_min is negative-or-zero, col_max non-negative.
        top = mid_y + col_max * (-half)  # max is positive amplitude → above midline
        bottom = mid_y + col_min * (-half)  # min is negative → below midline
        # If a column's amplitude is exactly 0, draw a 1-pixel midline.
        if abs(top - bottom) < 0.5:
            top = mid_y - 0.5
            bottom = mid_y + 0.5
        painter.drawLine(QPointF(x, top), QPointF(x, bottom))

    def _paint_cut_dimming(self, painter: QPainter, rect: QRect) -> None:
        """Overlay translucent rectangles on every CUT region.

        Cut regions = the complement of ``self._ranges`` over ``[0, duration_s]``.
        Iterating gaps between sorted keep-ranges is the simplest
        formulation; it hides the union/sort details in one pass and
        works whether the ranges are contiguous or contain gaps.
        """
        if self._duration_s <= 0:
            return
        # Sort by start so gap-iteration is straightforward. Defensive
        # copy because the live document order is also "ascending start
        # in normal use," but we don't *enforce* that contractually.
        kept = sorted(self._ranges, key=lambda r: r.start)
        cursor = 0.0
        for r in kept:
            if r.start > cursor:
                self._dim_segment(painter, rect, cursor, r.start)
            cursor = max(cursor, r.end)
        if cursor < self._duration_s:
            self._dim_segment(painter, rect, cursor, self._duration_s)

    def _dim_segment(
        self,
        painter: QPainter,
        rect: QRect,
        start_s: float,
        end_s: float,
    ) -> None:
        x0 = self._x_for_seconds(rect, start_s)
        x1 = self._x_for_seconds(rect, end_s)
        if x1 <= x0:
            return
        painter.fillRect(
            QRectF(x0, rect.y(), x1 - x0, rect.height()),
            _DIM_OVERLAY,
        )

    def _paint_playhead(self, painter: QPainter, rect: QRect) -> None:
        if self._duration_s <= 0:
            return
        x = self._x_for_seconds(rect, self._position_ms / 1000.0)
        x = max(rect.x(), min(rect.x() + rect.width() - 1, x))
        color = self.palette().highlight().color()
        painter.setPen(QPen(color, 1))
        painter.drawLine(QPointF(x, rect.y()), QPointF(x, rect.y() + rect.height()))

    def _x_for_seconds(self, rect: QRect, seconds: float) -> float:
        if self._duration_s <= 0:
            return float(rect.x())
        fraction = max(0.0, min(1.0, seconds / self._duration_s))
        return rect.x() + fraction * rect.width()

    # ----- input -----

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt API)
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        if self._duration_s <= 0 or self.width() <= 0:
            super().mousePressEvent(event)
            return
        x = event.position().x() - self.rect().x()
        fraction = max(0.0, min(1.0, x / max(1, self.width())))
        ms = int(round(fraction * self._duration_s * 1000.0))
        self.seek_requested.emit(ms)
        super().mousePressEvent(event)


# Backwards-compat alias for any imports that still reference the 5b name.
# 5d's editor pane uses :class:`WaveformStrip` directly; this alias keeps
# the surface from breaking if a stray import outside the editor pane
# survives the migration.
WaveformPlaceholder = WaveformStrip
