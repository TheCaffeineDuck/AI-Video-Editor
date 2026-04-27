"""Video preview pane: QMediaPlayer + QAudioOutput + QVideoWidget.

Phase 5b deliverable. Exposes the smallest set of playback affordances
that read as "this is a working video player": play/pause toggle,
horizontal seek slider, mm:ss / mm:ss timing label.

Out of scope (Decision 9 keeps the door open for `python-vlc` if codec
gaps surface): frame-stepping, speed control, fullscreen, alt audio
tracks, subtitle rendering. The play/pause button is the only manual
interactivity; the slider does seek + scrub via Qt's built-in signals.

Cleanup: callers that drop a VideoViewport on the floor should call
:meth:`release` first. ``QMediaPlayer.stop()`` + clearing the source +
``setVideoOutput(None)`` is the macOS-friendly teardown — leaving them
implicit risks a black flash and a leaked render layer when the next
viewport is shown in the same window.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)


def _format_ms(ms: int) -> str:
    """Format milliseconds as ``M:SS`` (or ``H:MM:SS`` past one hour)."""
    if ms < 0:
        ms = 0
    total_seconds = ms // 1000
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:d}:{seconds:02d}"


class VideoViewport(QWidget):
    """Composite widget: QVideoWidget + transport controls + seek slider.

    Signals:
        position_changed(int): media position in milliseconds, forwarded
            from :class:`QMediaPlayer.positionChanged`. Useful for the
            transcript / waveform views to follow the playhead.
    """

    position_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._video = QVideoWidget(self)
        self._video.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        # Black background reads as "video pane" before the first frame paints.
        self._video.setStyleSheet("background-color: black;")

        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)
        self._player.setVideoOutput(self._video)

        self._play_btn = QPushButton("Play")
        self._play_btn.setFixedWidth(80)
        self._play_btn.clicked.connect(self.toggle_play)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 0)
        self._slider.sliderMoved.connect(self._player.setPosition)
        # `valueChanged` rather than `sliderReleased` so click-on-track
        # also seeks — without this, click-to-seek is a no-op.
        self._slider.valueChanged.connect(self._maybe_seek_from_value)
        self._suppress_value_seek = False

        self._timing = QLabel("0:00 / 0:00")
        self._timing.setMinimumWidth(110)

        controls = QHBoxLayout()
        controls.addWidget(self._play_btn)
        controls.addWidget(self._slider, stretch=1)
        controls.addWidget(self._timing)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._video, stretch=1)
        outer.addLayout(controls)

        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.playbackStateChanged.connect(self._on_playback_state_changed)

    # ----- public API -----

    @property
    def player(self) -> QMediaPlayer:
        """Direct access to the QMediaPlayer (tests + advanced callers)."""
        return self._player

    def set_source(self, path: Path | None) -> None:
        """Load ``path`` into the player. ``None`` clears the source."""
        from PySide6.QtCore import QUrl

        self._player.stop()
        if path is None:
            self._player.setSource(QUrl())
            return
        self._player.setSource(QUrl.fromLocalFile(str(path)))

    def toggle_play(self) -> None:
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def seek_ms(self, position_ms: int) -> None:
        """Seek by milliseconds. Convenience for transcript-driven seeks (5c)."""
        self._player.setPosition(int(position_ms))

    def release(self) -> None:
        """Drop player resources cleanly before the widget is deleted.

        On macOS, dropping a QVideoWidget that's still wired to a
        QMediaPlayer can leave the underlying CALayer alive briefly,
        which paints a phantom black rect when the next central widget
        appears. Clear the wiring explicitly to keep teardown clean.
        """
        try:
            self._player.stop()
            self._player.setVideoOutput(None)
            self._player.setAudioOutput(None)
            from PySide6.QtCore import QUrl

            self._player.setSource(QUrl())
        except RuntimeError:
            # Underlying C++ object already gone — nothing to do.
            pass

    # ----- internal slots -----

    def _maybe_seek_from_value(self, value: int) -> None:
        """Seek when the slider value changes from a click on the track.

        We suppress this when *we* are setting the slider's value from
        ``positionChanged``; otherwise every position update triggers a
        redundant seek.
        """
        if self._suppress_value_seek:
            return
        if self._slider.isSliderDown():
            return  # sliderMoved already handled this drag.
        self._player.setPosition(value)

    def _on_position_changed(self, position_ms: int) -> None:
        if not self._slider.isSliderDown():
            self._suppress_value_seek = True
            self._slider.setValue(position_ms)
            self._suppress_value_seek = False
        self._update_timing(position_ms, self._player.duration())
        self.position_changed.emit(position_ms)

    def _on_duration_changed(self, duration_ms: int) -> None:
        self._slider.setRange(0, max(0, duration_ms))
        self._update_timing(self._player.position(), duration_ms)

    def _on_playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._play_btn.setText("Pause")
        else:
            self._play_btn.setText("Play")

    def _update_timing(self, position_ms: int, duration_ms: int) -> None:
        self._timing.setText(f"{_format_ms(position_ms)} / {_format_ms(duration_ms)}")
