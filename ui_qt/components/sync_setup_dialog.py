"""Multi-cam sync setup dialog (Phase 7).

The dialog walks the operator through:

1. Pick the audio master file (the podcast mic that drives the final
   audio).
2. Add one or more camera files. Each camera's audio gets
   cross-correlated against the master at "Estimate Offsets" time.
3. Eyeball the recovered offsets and confidences. Low-confidence rows
   highlight in :data:`~ui_qt.style.DANGER`; high-confidence rows in
   :data:`~ui_qt.style.SUCCESS`. The operator can override any value
   by typing into its offset field — manual overrides are flagged.
4. Save the group; the dialog persists a
   ``<doc>.transcribe.json.sync/<id>.sync.json`` file via
   :func:`core.sync.write_sync_group`.

Estimation runs synchronously on the GUI thread for simplicity. For
podcast-grade audio (lav or board feed) the typical 2–4 cameras land
in well under a minute, which is acceptable for a manual one-off
setup. If the cost becomes a problem we can move estimation onto a
:class:`QThread` worker, mirroring the highlight render worker.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.sync import (
    CONFIDENCE_GOOD,
    CONFIDENCE_MARGINAL,
    SyncEstimationError,
    build_sync_group,
    set_manual_offset,
    write_sync_group,
)
from ui_qt.style import ACCENT, DANGER, MUTED, SUCCESS

_LOG = logging.getLogger(__name__)


class SyncSetupDialog(QDialog):
    """Modal dialog for authoring + persisting a sync group.

    The host calls :meth:`exec` and, on accept, :attr:`saved_group_id`
    holds the id of the persisted sync group (or remains ``""`` when
    the operator cancelled / didn't save).
    """

    def __init__(
        self,
        document_path: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._document_path = document_path
        self._cameras: list[Path] = []
        self._audio_master: Path | None = None
        self._estimated_offsets: dict[str, float] = {}
        self._estimated_confidences: dict[str, float | None] = {}
        self.saved_group_id: str = ""
        self.setWindowTitle("Multi-cam sync setup")
        self.setMinimumWidth(560)
        self._build_ui()

    # ----- build ----------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        intro = QLabel(
            "Estimate per-camera offsets against an audio master so "
            "multi-cam highlights can pull video from any camera while "
            "audio comes from the master."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {MUTED};")
        outer.addWidget(intro)

        # Audio master picker.
        master_row = QHBoxLayout()
        master_row.setSpacing(6)
        master_row.addWidget(QLabel("Audio master:"))
        self._master_edit = QLineEdit()
        self._master_edit.setReadOnly(True)
        self._master_edit.setPlaceholderText("(no master selected)")
        master_row.addWidget(self._master_edit, 1)
        master_btn = QPushButton("Pick…")
        master_btn.clicked.connect(self._on_pick_master)
        master_row.addWidget(master_btn)
        outer.addLayout(master_row)

        # Description (optional).
        desc_row = QHBoxLayout()
        desc_row.setSpacing(6)
        desc_row.addWidget(QLabel("Label:"))
        self._desc_edit = QLineEdit()
        self._desc_edit.setPlaceholderText("e.g. ep42 main pod (optional)")
        desc_row.addWidget(self._desc_edit, 1)
        outer.addLayout(desc_row)

        # Camera table.
        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Camera", "Offset (s)", "Confidence"])
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.setColumnWidth(0, 280)
        self._table.setColumnWidth(1, 110)
        self._table.setColumnWidth(2, 110)
        self._table.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked
            | QTableWidget.EditTrigger.SelectedClicked
        )
        outer.addWidget(self._table, 1)

        # Camera add/remove + estimate buttons.
        ops_row = QHBoxLayout()
        ops_row.setSpacing(6)
        add_cam_btn = QPushButton("Add camera…")
        add_cam_btn.clicked.connect(self._on_add_camera)
        ops_row.addWidget(add_cam_btn)
        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(self._on_remove_camera)
        ops_row.addWidget(remove_btn)
        self._estimate_btn = QPushButton("Estimate offsets")
        self._estimate_btn.setStyleSheet(
            f"QPushButton {{ background-color: {ACCENT}; color: white; "
            "padding: 4px 12px; border-radius: 4px; }}"
        )
        self._estimate_btn.clicked.connect(self._on_estimate)
        ops_row.addWidget(self._estimate_btn)
        ops_row.addStretch(1)
        outer.addLayout(ops_row)

        # Status line.
        self._status_lbl = QLabel("")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet(f"color: {MUTED};")
        outer.addWidget(self._status_lbl)

        # OK / Cancel.
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self._save_btn = buttons.button(QDialogButtonBox.StandardButton.Save)
        self._save_btn.setEnabled(False)
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    # ----- master / camera pickers ----------------------------------------

    def _on_pick_master(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Pick audio master",
            str(Path.home()),
            "Media files (*.wav *.m4a *.mp3 *.aac *.mp4 *.mov *.mkv);;All files (*)",
        )
        if not path:
            return
        self._audio_master = Path(path)
        self._master_edit.setText(str(self._audio_master))
        self._refresh_save_enabled()

    def _on_add_camera(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Add camera files",
            str(Path.home()),
            "Video files (*.mp4 *.mov *.mkv *.avi *.m4v);;All files (*)",
        )
        if not paths:
            return
        for p in paths:
            cam = Path(p)
            if cam in self._cameras:
                continue
            self._cameras.append(cam)
            self._append_table_row(cam)
        self._refresh_save_enabled()

    def _on_remove_camera(self) -> None:
        rows = sorted({i.row() for i in self._table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        for row in rows:
            self._table.removeRow(row)
            del self._cameras[row]
        self._refresh_save_enabled()

    def _append_table_row(self, cam_path: Path) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        name_item = QTableWidgetItem(str(cam_path))
        name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._table.setItem(row, 0, name_item)
        offset_item = QTableWidgetItem("0.0")
        self._table.setItem(row, 1, offset_item)
        conf_item = QTableWidgetItem("—")
        conf_item.setFlags(conf_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._table.setItem(row, 2, conf_item)

    # ----- estimate -------------------------------------------------------

    def _on_estimate(self) -> None:
        if self._audio_master is None or not self._cameras:
            self._status_lbl.setText(
                "Pick an audio master and at least one camera first."
            )
            self._status_lbl.setStyleSheet(f"color: {DANGER};")
            return
        self._status_lbl.setText("Estimating offsets…")
        self._status_lbl.setStyleSheet(f"color: {ACCENT};")
        self._estimate_btn.setEnabled(False)
        try:
            group = build_sync_group(
                self._document_path,
                self._audio_master,
                self._cameras,
                description=self._desc_edit.text(),
            )
        except SyncEstimationError as exc:
            self._status_lbl.setText(f"Estimation failed: {exc}")
            self._status_lbl.setStyleSheet(f"color: {DANGER};")
            self._estimate_btn.setEnabled(True)
            return
        self._estimated_offsets = {
            str(k): v.offset_s for k, v in group.cameras.items()
        }
        self._estimated_confidences = {
            str(k): v.confidence for k, v in group.cameras.items()
        }
        self._refresh_offsets_into_table()
        self._estimate_btn.setEnabled(True)
        self._status_lbl.setText(
            "Estimation complete. Edit the offset column to override; "
            "low-confidence rows are flagged in red."
        )
        self._status_lbl.setStyleSheet(f"color: {SUCCESS};")
        self._refresh_save_enabled()

    def _refresh_offsets_into_table(self) -> None:
        for row, cam in enumerate(self._cameras):
            offset = self._estimated_offsets.get(str(cam), 0.0)
            confidence = self._estimated_confidences.get(str(cam))
            offset_item = self._table.item(row, 1)
            if offset_item is None:
                offset_item = QTableWidgetItem()
                self._table.setItem(row, 1, offset_item)
            offset_item.setText(f"{offset:.4f}")
            conf_item = self._table.item(row, 2)
            if conf_item is None:
                conf_item = QTableWidgetItem()
                self._table.setItem(row, 2, conf_item)
            if confidence is None:
                conf_item.setText("—")
                color = MUTED
            else:
                conf_item.setText(f"{confidence:.1f}")
                color = (
                    SUCCESS
                    if confidence >= CONFIDENCE_GOOD
                    else (DANGER if confidence < CONFIDENCE_MARGINAL else ACCENT)
                )
            conf_item.setForeground(_QColorFromHex(color))

    # ----- save -----------------------------------------------------------

    def _on_save(self) -> None:
        if self._audio_master is None or not self._cameras:
            return
        # Snapshot the table's offset overrides; any value that differs
        # from the auto-estimate is a manual override.
        try:
            group = build_sync_group(
                self._document_path,
                self._audio_master,
                self._cameras,
                description=self._desc_edit.text(),
            )
        except SyncEstimationError as exc:
            QMessageBox.critical(
                self,
                "Sync setup",
                f"Could not estimate offsets: {exc}",
            )
            return
        for row, cam in enumerate(self._cameras):
            offset_item = self._table.item(row, 1)
            if offset_item is None:
                continue
            try:
                manual = float(offset_item.text())
            except ValueError:
                QMessageBox.critical(
                    self,
                    "Sync setup",
                    f"Offset for camera {cam.name!r} is not a number: "
                    f"{offset_item.text()!r}",
                )
                return
            estimated = self._estimated_offsets.get(str(cam), 0.0)
            if abs(manual - estimated) > 1e-6:
                group = set_manual_offset(group, cam, manual)
        materialized, _ = write_sync_group(self._document_path, group)
        self.saved_group_id = materialized.sync_group_id
        self.accept()

    # ----- helpers --------------------------------------------------------

    def _refresh_save_enabled(self) -> None:
        ready = self._audio_master is not None and bool(self._cameras)
        self._save_btn.setEnabled(ready)


def _QColorFromHex(hex_str: str):  # noqa: N802 — Qt helper
    """Tiny adapter so the dialog doesn't import QColor at module level.

    QColor isn't worth a top-level import in a single-call helper; the
    indirection also lets unit tests stub the color resolution if they
    want. ``hex_str`` is already a "#rrggbb" string per
    :mod:`ui_qt.style`.
    """
    from PySide6.QtGui import QColor

    return QColor(hex_str)
