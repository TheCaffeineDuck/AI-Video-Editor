"""Qt drop zone: native drag-and-drop OR click-to-browse for a media file.

Mirrors :class:`ui.components.drop_zone.DropZone` from the customtkinter
UI but uses Qt's native drag/drop event handlers — no third-party DnD
shim needed. Emits a ``file_selected`` signal carrying a :class:`Path`,
or ``invalid_file`` carrying a human-readable reason. Both signals are
fired synchronously from the event handler.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDropEvent, QMouseEvent
from PySide6.QtWidgets import QFileDialog, QFrame, QLabel, QVBoxLayout, QWidget

from ui.state import SUPPORTED_EXTENSIONS, is_supported_media
from ui_qt.style import drop_zone_qss, muted_label_qss

_DIALOG_FILTER = (
    "Media files (*.mp4 *.mov *.mkv *.mp3 *.wav *.m4a);;All files (*.*)"
)


class DropZone(QFrame):
    """A dashed-bordered card. Drop a file or click to open the file picker."""

    file_selected = Signal(Path)
    invalid_file = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)
        self.setMinimumHeight(220)
        self.setStyleSheet(drop_zone_qss(active=False))

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._icon = QLabel("\U0001F4C1")  # 📁
        font = self._icon.font()
        font.setPointSize(36)
        self._icon.setFont(font)
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._icon)

        self._title = QLabel("Drop a media file here")
        title_font = self._title.font()
        title_font.setPointSize(16)
        title_font.setBold(True)
        self._title.setFont(title_font)
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._title)

        self._sub = QLabel("or click to browse")
        self._sub.setStyleSheet(muted_label_qss())
        self._sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._sub)

        formats = "  ·  ".join(
            ext.lstrip(".").upper() for ext in sorted(SUPPORTED_EXTENSIONS)
        )
        self._formats = QLabel(formats)
        self._formats.setStyleSheet(muted_label_qss())
        self._formats.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._formats)

    # ----- drag/drop -----

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setStyleSheet(drop_zone_qss(active=True))
        else:
            event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self.setStyleSheet(drop_zone_qss(active=False))
        event.accept()

    def dropEvent(self, event: QDropEvent) -> None:
        self.setStyleSheet(drop_zone_qss(active=False))
        urls = event.mimeData().urls()
        if not urls:
            event.ignore()
            return
        path = Path(urls[0].toLocalFile())
        event.acceptProposedAction()
        self._accept(path)

    # ----- click-to-browse -----

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            chosen, _ = QFileDialog.getOpenFileName(
                self, "Select a media file", "", _DIALOG_FILTER
            )
            if chosen:
                self._accept(Path(chosen))
        super().mousePressEvent(event)

    # ----- shared accept logic -----

    def _accept(self, path: Path) -> None:
        if not is_supported_media(path):
            self.invalid_file.emit(
                f"Unsupported file type: {path.suffix or '(no extension)'}"
            )
            return
        if not path.is_file():
            self.invalid_file.emit(f"File not found: {path}")
            return
        self.file_selected.emit(path)

    # ----- after-load preview -----

    def show_loaded(
        self, *, name: str, duration_seconds: float, size_bytes: int
    ) -> None:
        mins = int(duration_seconds // 60)
        secs = duration_seconds - mins * 60
        size_mb = size_bytes / (1024 * 1024)
        self._icon.setText("\U0001F39E")  # 🎞
        self._title.setText(name)
        self._sub.setText(f"{mins:d}:{secs:05.2f}    ·    {size_mb:.1f} MB")
        self._formats.setText("Click to choose a different file")

    def show_idle(self) -> None:
        self._icon.setText("\U0001F4C1")
        self._title.setText("Drop a media file here")
        self._sub.setText("or click to browse")
        formats = "  ·  ".join(
            ext.lstrip(".").upper() for ext in sorted(SUPPORTED_EXTENSIONS)
        )
        self._formats.setText(formats)
