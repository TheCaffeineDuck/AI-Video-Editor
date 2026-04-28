"""Generate the Transcribe app icon (.icns) from a 1024x1024 source.

Renders a simple geometric mark via QPainter — a stylized waveform-and-T
glyph against the accent palette — into ``resources/icons/transcribe_1024.png``,
then drives the standard ``sips`` + ``iconutil`` recipe to emit the
multi-resolution ``.icns`` bundle alongside it.

Run from the repo root:

    .venv/bin/python scripts/make_icon.py

The generated ``.icns`` is committed under ``resources/icons/transcribe.icns``
and loaded via ``QApplication.setWindowIcon`` in ``ui_qt/app.py``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QImage, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QApplication

REPO_ROOT = Path(__file__).resolve().parent.parent
ICONS_DIR = REPO_ROOT / "resources" / "icons"
SOURCE_PNG = ICONS_DIR / "transcribe_1024.png"
ICONSET_DIR = ICONS_DIR / "Transcribe.iconset"
ICNS_PATH = ICONS_DIR / "transcribe.icns"


def _render_source(path: Path) -> None:
    """Paint a 1024x1024 PNG icon source."""
    size = 1024
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    radius = 220
    rect = QRectF(0, 0, size, size)
    gradient = QLinearGradient(QPointF(0, 0), QPointF(size, size))
    gradient.setColorAt(0.0, QColor("#3B82F6"))
    gradient.setColorAt(1.0, QColor("#2563EB"))
    painter.setBrush(QBrush(gradient))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(rect, radius, radius)

    pen = QPen(QColor(255, 255, 255, 235))
    pen.setWidth(46)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)

    cx = size / 2
    base_y = size * 0.66
    bar_widths = [(-360, 110), (-220, 220), (-80, 320), (60, 260), (200, 180), (340, 110)]
    for offset_x, h in bar_widths:
        x = cx + offset_x
        painter.drawLine(QPointF(x, base_y - h / 2), QPointF(x, base_y + h / 2))

    painter.setBrush(QColor(255, 255, 255, 255))
    painter.setPen(Qt.PenStyle.NoPen)
    crossbar_w = size * 0.55
    crossbar_h = size * 0.07
    crossbar_x = cx - crossbar_w / 2
    crossbar_y = size * 0.26
    painter.drawRoundedRect(
        QRectF(crossbar_x, crossbar_y, crossbar_w, crossbar_h),
        crossbar_h / 2,
        crossbar_h / 2,
    )
    stem_w = size * 0.085
    stem_h = size * 0.18
    stem_x = cx - stem_w / 2
    stem_y = crossbar_y + crossbar_h - 2
    painter.drawRoundedRect(
        QRectF(stem_x, stem_y, stem_w, stem_h),
        stem_w / 4,
        stem_w / 4,
    )

    painter.end()
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(str(path), "PNG")


def _build_iconset(source: Path, iconset_dir: Path) -> None:
    if iconset_dir.exists():
        shutil.rmtree(iconset_dir)
    iconset_dir.mkdir(parents=True, exist_ok=True)
    recipe = [
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    ]
    for px, name in recipe:
        out = iconset_dir / name
        subprocess.run(
            ["sips", "-z", str(px), str(px), str(source), "--out", str(out)],
            check=True,
            capture_output=True,
        )


def _build_icns(iconset_dir: Path, icns_path: Path) -> None:
    subprocess.run(
        ["iconutil", "-c", "icns", str(iconset_dir), "-o", str(icns_path)],
        check=True,
    )


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841
    _render_source(SOURCE_PNG)
    _build_iconset(SOURCE_PNG, ICONSET_DIR)
    _build_icns(ICONSET_DIR, ICNS_PATH)
    shutil.rmtree(ICONSET_DIR)
    print(f"wrote {ICNS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
