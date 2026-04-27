"""Editor pane: nested QSplitter holding video, transcript, waveform.

Topology (Decision 1 + Decision 5):

    EditorPane
    └── outer QSplitter   (Vertical for video_top, Horizontal for video_left)
        ├── VideoViewport
        └── inner QSplitter  (always Vertical)
            ├── TranscriptView
            └── WaveformPlaceholder

Only the *outer* splitter flips with the layout toggle. The inner
splitter is always vertical so the waveform stays directly under the
transcript regardless of layout — the user-visible promise of
Decision 5 ("waveform is the strip below the transcript").

Layout toggle: a button on the pane's local toolbar flips
``settings.layout`` between ``"video_top"`` and ``"video_left"``,
persists via :func:`core.settings.save_settings`, and reorients the
outer splitter live (no widget rebuild).

Phase 5b is the skeleton — interactive transcript editing lands in 5c,
and waveform rendering in 5d. Out of scope here: per-word click
targets, drag-selection cuts, splitter-size persistence (5e), keyboard
shortcuts beyond what the slider provides for free, and menu bar
integration (5f).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from core.document import Document
from core.settings import DEFAULT_LAYOUT, LAYOUT_CHOICES, Settings, save_settings
from ui_qt.components.transcript_view import TranscriptView
from ui_qt.components.video_viewport import VideoViewport
from ui_qt.waveform import WaveformPlaceholder


def _orientation_for(layout: str) -> Qt.Orientation:
    """Map a settings layout string to the outer splitter's orientation."""
    if layout == "video_left":
        return Qt.Orientation.Horizontal
    return Qt.Orientation.Vertical  # video_top (default)


def _toggle(layout: str) -> str:
    return "video_left" if layout == "video_top" else "video_top"


class EditorPane(QWidget):
    """The editor view: video + transcript + waveform-placeholder.

    Holds its own (mutable) reference to the active :class:`Settings` so
    the toggle button can persist across restarts. The MainWindow
    receives the updated Settings via the ``layout_changed`` signal —
    keeping settings ownership in one place upstream.

    Signals:
        back_to_transcribe: emitted when the user clicks the toolbar
            "Back" button (5b's escape hatch — full menu integration is
            5f's job).
        layout_changed(Settings): emitted after the layout toggle
            persists; carries the freshly-saved Settings object.
    """

    back_to_transcribe = Signal()
    layout_changed = Signal(Settings)

    def __init__(
        self,
        document: Document,
        *,
        settings: Settings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._document = document
        self._settings = settings

        self._build_toolbar()
        self._build_splitters()

        self._render_document()
        self._wire_video_source()

    # ----- public surface -----

    @property
    def document(self) -> Document:
        return self._document

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def video_viewport(self) -> VideoViewport:
        return self._video

    @property
    def transcript_view(self) -> TranscriptView:
        return self._transcript

    @property
    def outer_splitter(self) -> QSplitter:
        return self._outer

    @property
    def inner_splitter(self) -> QSplitter:
        return self._inner

    def release(self) -> None:
        """Tear down the embedded media player before the pane is destroyed."""
        try:
            self._video.release()
        except RuntimeError:
            pass

    # ----- layout / build -----

    def _build_toolbar(self) -> None:
        self._outer_layout = QVBoxLayout(self)
        self._outer_layout.setContentsMargins(0, 0, 0, 0)

        bar = QToolBar()
        bar.setMovable(False)

        self._back_btn = QPushButton("← Back to Transcribe")
        self._back_btn.setFlat(True)
        self._back_btn.clicked.connect(self.back_to_transcribe.emit)
        bar.addWidget(self._back_btn)

        spacer = QWidget()
        spacer.setSizePolicy(spacer.sizePolicy().horizontalPolicy(),
                             spacer.sizePolicy().verticalPolicy())
        bar.addWidget(spacer)

        self._layout_btn = QPushButton(self._layout_button_label(self._settings.layout))
        self._layout_btn.setFlat(True)
        self._layout_btn.clicked.connect(self._handle_layout_toggle)
        bar.addWidget(self._layout_btn)

        self._outer_layout.addWidget(bar)

    def _build_splitters(self) -> None:
        self._outer = QSplitter(_orientation_for(self._settings.layout), self)
        self._video = VideoViewport(self._outer)

        self._inner = QSplitter(Qt.Orientation.Vertical, self._outer)
        self._transcript = TranscriptView(self._inner)
        self._waveform = WaveformPlaceholder(self._inner)
        self._inner.addWidget(self._transcript)
        self._inner.addWidget(self._waveform)
        # Transcript dominates the inner split; the waveform strip stays a strip.
        self._inner.setStretchFactor(0, 1)
        self._inner.setStretchFactor(1, 0)
        self._inner.setSizes([400, 80])

        self._outer.addWidget(self._video)
        self._outer.addWidget(self._inner)
        self._outer.setStretchFactor(0, 1)
        self._outer.setStretchFactor(1, 1)

        # Container for the splitter so we can wrap it in a frame later if needed.
        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.addWidget(self._outer)
        self._outer_layout.addWidget(body, stretch=1)

        # Status row showing resolved playback path.
        self._status = QLabel("")
        self._status.setStyleSheet("color: #6B7280; padding: 4px 8px;")
        self._outer_layout.addWidget(self._status)

    # ----- behavior -----

    def _render_document(self) -> None:
        self._transcript.set_document_model(self._document)

    def _wire_video_source(self) -> None:
        if not self._document.sources:
            self._status.setText("(no source media on this document)")
            self._video.set_source(None)
            return
        primary = next(iter(self._document.sources.values()))
        self._status.setText(str(primary.path))
        if primary.path.is_file():
            self._video.set_source(primary.path)
        else:
            self._status.setText(f"(source missing: {primary.path})")
            self._video.set_source(None)

    def _handle_layout_toggle(self) -> None:
        new_layout = _toggle(self._settings.layout)
        if new_layout not in LAYOUT_CHOICES:
            new_layout = DEFAULT_LAYOUT
        self._settings.layout = new_layout
        save_settings(self._settings)
        self._outer.setOrientation(_orientation_for(new_layout))
        self._layout_btn.setText(self._layout_button_label(new_layout))
        self.layout_changed.emit(self._settings)

    @staticmethod
    def _layout_button_label(layout: str) -> str:
        if layout == "video_left":
            return "Layout: video left  ↔"
        return "Layout: video top  ↕"
