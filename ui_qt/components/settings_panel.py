"""Settings dialog for the Qt UI.

Phase 5a ships the Transcription tab only — model, language, output
directory, device/precision. The Editor and Advanced tabs land in
Phase 5f. The dialog reads/writes the same settings.json file the
customtkinter app uses, so launching the Qt app on an existing install
preserves the user's choices.
"""

from __future__ import annotations

import platform
import shutil
import subprocess

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.models import MODEL_NAMES, cache_root
from core.settings import Settings, save_settings

VERSION = "0.1.0"
DEVICES = ("auto", "cpu", "cuda")
COMPUTE_TYPES = ("auto", "int8", "float16", "float32")


class SettingsDialog(QDialog):
    """Modal Settings sheet. ``settings_saved`` fires with the new Settings."""

    settings_saved = Signal(Settings)

    def __init__(
        self, parent: QWidget | None = None, *, current: Settings
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumSize(560, 420)
        self._current = current

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._build_transcription_tab(), "Transcription")
        layout.addWidget(tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _build_transcription_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        self._model_combo = QComboBox()
        self._model_combo.addItems(list(MODEL_NAMES))
        self._model_combo.setCurrentText(self._current.default_model)
        form.addRow("Default model", self._model_combo)

        out_row = QHBoxLayout()
        self._output_dir = QLineEdit(self._current.output_dir or "")
        self._output_dir.setPlaceholderText("(same folder as input)")
        out_row.addWidget(self._output_dir)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_output)
        out_row.addWidget(browse)
        form.addRow("Default output folder", out_row)

        self._device_combo = QComboBox()
        self._device_combo.addItems(list(DEVICES))
        self._device_combo.setCurrentText(self._current.compute_device)
        form.addRow("Device", self._device_combo)

        self._compute_combo = QComboBox()
        self._compute_combo.addItems(list(COMPUTE_TYPES))
        self._compute_combo.setCurrentText(self._current.compute_type)
        form.addRow("Precision", self._compute_combo)

        cache_row = QHBoxLayout()
        open_cache = QPushButton("Open cache folder")
        open_cache.clicked.connect(self._open_cache_folder)
        clear_cache = QPushButton("Clear cache")
        clear_cache.clicked.connect(self._clear_cache)
        cache_row.addWidget(open_cache)
        cache_row.addWidget(clear_cache)
        cache_row.addStretch()
        form.addRow("Model cache", cache_row)

        about = QLabel(
            f"Whisper Transcriber v{VERSION}\n"
            "MIT licensed. Powered by faster-whisper / CTranslate2.\n"
            "Apple Silicon: CPU-only inference (no Metal backend yet)."
        )
        about.setWordWrap(True)
        form.addRow("About", about)

        return page

    def _browse_output(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose default output folder"
        )
        if chosen:
            self._output_dir.setText(chosen)

    def _open_cache_folder(self) -> None:
        path = cache_root()
        path.mkdir(parents=True, exist_ok=True)
        if platform.system() == "Darwin":
            subprocess.run(["open", str(path)], check=False)
        elif platform.system() == "Windows":  # pragma: no cover
            subprocess.run(["explorer", str(path)], check=False)
        else:  # pragma: no cover
            subprocess.run(["xdg-open", str(path)], check=False)

    def _clear_cache(self) -> None:
        path = cache_root()
        if path.exists():
            shutil.rmtree(path)

    def _save(self) -> None:
        out_dir = self._output_dir.text().strip() or None
        new = Settings(
            default_model=self._model_combo.currentText(),
            default_language=self._current.default_language,
            output_formats=list(self._current.output_formats),
            output_dir=out_dir,
            compute_device=self._device_combo.currentText(),
            compute_type=self._compute_combo.currentText(),
            layout=self._current.layout,
            default_pad_lead=self._current.default_pad_lead,
            default_pad_trail=self._current.default_pad_trail,
            default_audio_fade_ms=self._current.default_audio_fade_ms,
            autosave_interval_s=self._current.autosave_interval_s,
        )
        save_settings(new)
        self.settings_saved.emit(new)
        self.accept()
