"""Settings dialog for the Qt UI.

Phase 5e completes the dialog from 5a's stub: three tabs
(Transcription / Editor / Advanced) covering every Settings field
that the UI is expected to expose. The dialog reads/writes the same
``settings.json`` file the customtkinter app uses, so launching the
Qt app on an existing install preserves the user's choices.

Tab placement (Decision 6):

- **Transcription**: model, language passthrough, output dir, device,
  precision, model-cache controls.
- **Editor**: layout (video on top / left), default pad-lead, pad-trail,
  audio fade.
- **Advanced**: autosave interval (0 = off, special-text shown as
  ``"Off"``).

Live propagation: when the dialog ``Save``-button fires, the saved
:class:`Settings` is emitted on ``settings_saved``. MainWindow wires
that into ``_apply_settings``, which forwards layout changes to the
running :class:`EditorPane` and autosave-interval changes to its
:class:`DocumentSession`. Other render-default fields take effect on
the next render — no propagation needed.
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
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.models import MODEL_NAMES, cache_root
from core.settings import LAYOUT_CHOICES, Settings, save_settings

VERSION = "0.1.0"
DEVICES = ("auto", "cpu", "cuda")
COMPUTE_TYPES = ("auto", "int8", "float16", "float32")

# Layout combo: human-readable label ↔ stored Settings value. The order
# matches LAYOUT_CHOICES so first-selected stays "video_top".
_LAYOUT_LABELS: dict[str, str] = {
    "video_top": "Video on top",
    "video_left": "Video on left",
}


class SettingsDialog(QDialog):
    """Modal Settings sheet. ``settings_saved`` fires with the new Settings."""

    settings_saved = Signal(Settings)

    def __init__(
        self, parent: QWidget | None = None, *, current: Settings
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumSize(560, 460)
        self._current = current

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._build_transcription_tab(), "Transcription")
        tabs.addTab(self._build_editor_tab(), "Editor")
        tabs.addTab(self._build_advanced_tab(), "Advanced")
        self._tabs = tabs
        layout.addWidget(tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ----- accessors (used by tests + MainWindow live wiring) -----

    @property
    def tabs(self) -> QTabWidget:
        return self._tabs

    @property
    def layout_combo(self) -> QComboBox:
        return self._layout_combo

    @property
    def pad_lead_spin(self) -> QDoubleSpinBox:
        return self._pad_lead_spin

    @property
    def pad_trail_spin(self) -> QDoubleSpinBox:
        return self._pad_trail_spin

    @property
    def audio_fade_spin(self) -> QSpinBox:
        return self._audio_fade_spin

    @property
    def autosave_spin(self) -> QSpinBox:
        return self._autosave_spin

    # ----- tabs -----

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

    def _build_editor_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        self._layout_combo = QComboBox()
        for choice in LAYOUT_CHOICES:
            self._layout_combo.addItem(_LAYOUT_LABELS[choice], userData=choice)
        idx = self._layout_combo.findData(self._current.layout)
        if idx >= 0:
            self._layout_combo.setCurrentIndex(idx)
        form.addRow("Layout", self._layout_combo)

        self._pad_lead_spin = QDoubleSpinBox()
        self._pad_lead_spin.setRange(0.0, 2.0)
        self._pad_lead_spin.setSingleStep(0.05)
        self._pad_lead_spin.setSuffix(" s")
        self._pad_lead_spin.setDecimals(2)
        self._pad_lead_spin.setValue(float(self._current.default_pad_lead))
        form.addRow("Default pad lead", self._pad_lead_spin)

        self._pad_trail_spin = QDoubleSpinBox()
        self._pad_trail_spin.setRange(0.0, 2.0)
        self._pad_trail_spin.setSingleStep(0.05)
        self._pad_trail_spin.setSuffix(" s")
        self._pad_trail_spin.setDecimals(2)
        self._pad_trail_spin.setValue(float(self._current.default_pad_trail))
        form.addRow("Default pad trail", self._pad_trail_spin)

        self._audio_fade_spin = QSpinBox()
        self._audio_fade_spin.setRange(0, 500)
        self._audio_fade_spin.setSingleStep(10)
        self._audio_fade_spin.setSuffix(" ms")
        self._audio_fade_spin.setValue(int(self._current.default_audio_fade_ms))
        form.addRow("Default audio fade", self._audio_fade_spin)

        return page

    def _build_advanced_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        self._autosave_spin = QSpinBox()
        self._autosave_spin.setRange(0, 600)
        self._autosave_spin.setSingleStep(10)
        self._autosave_spin.setSuffix(" s")
        # Special-text replaces the rendered value when the spinbox sits
        # at its minimum; "Off" reads better than "0 s" for the disabled
        # state (Decision 8 — autosave is off by default).
        self._autosave_spin.setSpecialValueText("Off")
        self._autosave_spin.setValue(int(self._current.autosave_interval_s))
        form.addRow("Autosave interval", self._autosave_spin)

        return page

    # ----- handlers -----

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
        layout_value = self._layout_combo.currentData() or self._current.layout
        new = Settings(
            default_model=self._model_combo.currentText(),
            default_language=self._current.default_language,
            output_formats=list(self._current.output_formats),
            output_dir=out_dir,
            compute_device=self._device_combo.currentText(),
            compute_type=self._compute_combo.currentText(),
            layout=layout_value,
            default_pad_lead=float(self._pad_lead_spin.value()),
            default_pad_trail=float(self._pad_trail_spin.value()),
            default_audio_fade_ms=int(self._audio_fade_spin.value()),
            autosave_interval_s=int(self._autosave_spin.value()),
            editor_splitter_state=self._current.editor_splitter_state,
            transcript_splitter_state=self._current.transcript_splitter_state,
        )
        save_settings(new)
        self.settings_saved.emit(new)
        self.accept()
