"""Result card: transcript preview, output paths, Open Folder / Copy / New."""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ui_qt.style import accent_button_qss, muted_label_qss


class ResultCard(QFrame):
    new_transcription = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        layout = QVBoxLayout(self)

        title = QLabel("Transcript")
        font = title.font()
        font.setPointSize(16)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)

        self._meta = QLabel("")
        self._meta.setStyleSheet(muted_label_qss())
        self._meta.setWordWrap(True)
        layout.addWidget(self._meta)

        self._textbox = QTextEdit()
        self._textbox.setReadOnly(True)
        layout.addWidget(self._textbox)

        button_row = QHBoxLayout()
        self._open_btn = QPushButton("Open folder")
        self._open_btn.clicked.connect(self._open_folder)
        self._copy_btn = QPushButton("Copy all")
        self._copy_btn.clicked.connect(self._copy_all)
        self._new_btn = QPushButton("New transcription")
        self._new_btn.setStyleSheet(accent_button_qss())
        self._new_btn.clicked.connect(self.new_transcription.emit)
        button_row.addStretch()
        button_row.addWidget(self._open_btn)
        button_row.addWidget(self._copy_btn)
        button_row.addWidget(self._new_btn)
        layout.addLayout(button_row)

        self._output_dir: Path | None = None
        self._transcript_text: str = ""

    def show_result(
        self,
        *,
        transcript: str,
        output_files: dict[str, Path],
        language: str,
        elapsed_seconds: float,
    ) -> None:
        self._transcript_text = transcript
        self._textbox.setPlainText(transcript)

        word_count = len(transcript.split())
        first_path = next(iter(output_files.values()))
        self._output_dir = first_path.parent
        formats = ", ".join(sorted(output_files.keys()))
        self._meta.setText(
            f"{first_path.parent}    ·    {word_count} words    ·    "
            f"language: {language}    ·    {elapsed_seconds:.1f}s    ·    {formats}"
        )

    def _open_folder(self) -> None:
        if self._output_dir is None or not self._output_dir.exists():
            return
        system = platform.system()
        if system == "Darwin":
            subprocess.run(["open", str(self._output_dir)], check=False)
        elif system == "Windows":  # pragma: no cover - mac dev box
            subprocess.run(["explorer", str(self._output_dir)], check=False)
        else:  # pragma: no cover
            subprocess.run(["xdg-open", str(self._output_dir)], check=False)

    def _copy_all(self) -> None:
        if not self._transcript_text:
            return
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._transcript_text)
