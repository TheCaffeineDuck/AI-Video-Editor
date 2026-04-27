"""Combo box for picking a Whisper model size, with a downloaded-mark badge."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QWidget

from core.models import MODEL_NAMES, MODEL_REGISTRY, is_downloaded


class ModelPicker(QWidget):
    """Label + combo box. Emits ``model_changed(str)`` on selection."""

    model_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None, *, initial: str = "base") -> None:
        super().__init__(parent)
        if initial not in MODEL_REGISTRY:
            raise ValueError(f"unknown model {initial!r}")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Model:"))

        self._combo = QComboBox()
        self._populate()
        self._combo.setCurrentText(self._display_for(initial))
        self._combo.currentIndexChanged.connect(self._emit_change)
        layout.addWidget(self._combo)
        layout.addStretch()

    def _populate(self) -> None:
        self._combo.blockSignals(True)
        self._combo.clear()
        for name in MODEL_NAMES:
            self._combo.addItem(self._display_for(name), userData=name)
            info = MODEL_REGISTRY[name]
            tooltip = f"{info.size_mb} MB · {info.speed_label}"
            if name == "large-v3":
                tooltip += " · tight on 16 GB RAM"
            self._combo.setItemData(
                self._combo.count() - 1, tooltip, role=3  # Qt.ToolTipRole == 3
            )
        self._combo.blockSignals(False)

    @staticmethod
    def _display_for(name: str) -> str:
        badge = " ✓" if is_downloaded(name) else ""
        return f"{name}{badge}"

    def _emit_change(self) -> None:
        self.model_changed.emit(self.value)

    @property
    def value(self) -> str:
        return self._combo.currentData()

    def set_value(self, name: str) -> None:
        if name not in MODEL_REGISTRY:
            raise ValueError(f"unknown model {name!r}")
        idx = list(MODEL_NAMES).index(name)
        self._combo.setCurrentIndex(idx)

    def refresh_badges(self) -> None:
        """Re-check download status; preserve current selection."""
        current = self.value
        self._populate()
        self.set_value(current)
