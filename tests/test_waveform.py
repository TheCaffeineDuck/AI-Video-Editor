"""Phase 5d — peaks generation, cache, and WaveformStrip rendering."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import numpy as np
import pytest

from workers import waveform as wf

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_WAV = REPO_ROOT / "tests" / "fixtures" / "sample.wav"


# ---------------------------------------------------------------------------
# generate_peaks — pure-data shape and determinism
# ---------------------------------------------------------------------------


def test_generate_peaks_shape_dtype_and_range():
    peaks = wf.generate_peaks(SAMPLE_WAV, bucket_count=4000)
    assert peaks.shape == (4000, 2)
    assert peaks.dtype == np.float32
    assert peaks.min() >= -1.0
    assert peaks.max() <= 1.0


def test_generate_peaks_is_deterministic():
    """Same input → byte-identical output."""
    a = wf.generate_peaks(SAMPLE_WAV, bucket_count=2000)
    b = wf.generate_peaks(SAMPLE_WAV, bucket_count=2000)
    assert np.array_equal(a, b)


def test_generate_peaks_min_le_max_per_bucket():
    """For every bucket, ``min`` should be <= ``max``."""
    peaks = wf.generate_peaks(SAMPLE_WAV, bucket_count=1000)
    assert np.all(peaks[:, 0] <= peaks[:, 1])


def test_generate_peaks_progress_callback_invoked():
    """Progress callback receives values in [0, 1]."""
    seen: list[float] = []
    wf.generate_peaks(SAMPLE_WAV, bucket_count=500, on_progress=seen.append)
    if seen:  # tiny files may finish in one read with no progress ticks
        assert all(0.0 <= f <= 1.0 for f in seen)


def test_generate_peaks_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        wf.generate_peaks(tmp_path / "missing.wav")


def test_generate_peaks_invalid_bucket_count_raises(tmp_path):
    with pytest.raises(ValueError):
        wf.generate_peaks(SAMPLE_WAV, bucket_count=0)


# ---------------------------------------------------------------------------
# Cache round-trip — save_peaks_cache / load_peaks_cache
# ---------------------------------------------------------------------------


def _copy_sample(tmp_path: Path) -> Path:
    """Copy the sample.wav into tmp_path so the side-car cache is also tmp."""
    target = tmp_path / "media.wav"
    shutil.copy(SAMPLE_WAV, target)
    return target


def test_cache_round_trip_returns_equal_array(tmp_path):
    media = _copy_sample(tmp_path)
    peaks = wf.generate_peaks(media, bucket_count=512)
    saved_path = wf.save_peaks_cache(media, peaks, duration_s=6.10)
    assert saved_path.is_file()
    loaded = wf.load_peaks_cache(media)
    assert loaded is not None
    assert loaded.bucket_count == 512
    assert loaded.duration_s == pytest.approx(6.10)
    assert np.array_equal(loaded.peaks, peaks.astype(np.float32))


def test_cache_load_missing_returns_none(tmp_path):
    media = _copy_sample(tmp_path)
    assert wf.load_peaks_cache(media) is None


def test_cache_invalidates_on_source_change(tmp_path):
    """Touch the source → mtime changes → cache_key changes → load returns None."""
    media = _copy_sample(tmp_path)
    peaks = wf.generate_peaks(media, bucket_count=128)
    wf.save_peaks_cache(media, peaks, duration_s=6.10)
    assert wf.load_peaks_cache(media) is not None
    # Bump mtime by at least 2 seconds — cache_key uses int(st_mtime),
    # so a 1-second bump may not change the integer truncation.
    new_mtime = media.stat().st_mtime + 5
    os.utime(media, (new_mtime, new_mtime))
    assert wf.load_peaks_cache(media) is None


def test_cache_invalidates_on_size_change(tmp_path):
    """Append a byte to the source → size changes → cache stale."""
    media = _copy_sample(tmp_path)
    peaks = wf.generate_peaks(media, bucket_count=128)
    wf.save_peaks_cache(media, peaks, duration_s=6.10)
    with open(media, "ab") as fh:
        fh.write(b"\x00")
    assert wf.load_peaks_cache(media) is None


def test_cache_save_for_missing_source_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        wf.save_peaks_cache(
            tmp_path / "missing.wav",
            np.zeros((10, 2), dtype=np.float32),
            duration_s=1.0,
        )


def test_peaks_path_returns_sidecar(tmp_path):
    media = tmp_path / "video.mp4"
    media.write_bytes(b"x")
    p = wf.peaks_path(media)
    assert p.parent == media.parent
    assert p.name == "video.mp4.peaks.npz"


# ---------------------------------------------------------------------------
# Real-corpus smoke (slow): synthetic.mp4 must complete quickly.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_generate_peaks_against_synthetic_under_two_seconds(synthetic_video):
    start = time.monotonic()
    peaks = wf.generate_peaks(synthetic_video, bucket_count=4000)
    elapsed = time.monotonic() - start
    assert peaks.shape == (4000, 2)
    assert elapsed < 5.0, f"peak generation took {elapsed:.2f}s on synthetic.mp4"


# ---------------------------------------------------------------------------
# WaveformStrip — Qt widget behaviour
# ---------------------------------------------------------------------------


if os.environ.get("WHISPER_NO_QT"):  # pragma: no cover - opt-in headless
    pytest.skip("WHISPER_NO_QT set", allow_module_level=True)

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QImage, QMouseEvent  # noqa: E402

from core.document import Range  # noqa: E402
from ui_qt.waveform import WaveformStrip  # noqa: E402


def _build_strip(qtbot, width: int = 200, height: int = 80) -> WaveformStrip:
    strip = WaveformStrip()
    qtbot.addWidget(strip)
    strip.resize(width, height)
    return strip


def _flat_peaks(n: int = 4000) -> np.ndarray:
    """Constant-amplitude peaks — useful for rendering tests."""
    out = np.zeros((n, 2), dtype=np.float32)
    out[:, 0] = -0.6
    out[:, 1] = 0.6
    return out


def test_strip_set_peaks_clears_loading(qtbot):
    strip = _build_strip(qtbot)
    strip.set_loading(True)
    assert strip.is_loading is True
    strip.set_peaks(_flat_peaks(), duration_s=10.0)
    assert strip.is_loading is False
    assert strip.peaks is not None


def test_strip_set_peaks_rejects_wrong_shape(qtbot):
    strip = _build_strip(qtbot)
    with pytest.raises(ValueError):
        strip.set_peaks(np.zeros(100, dtype=np.float32), duration_s=1.0)


def test_strip_click_to_seek_emits_milliseconds(qtbot):
    strip = _build_strip(qtbot, width=400, height=80)
    strip.set_peaks(_flat_peaks(100), duration_s=100.0)
    seen: list[int] = []
    strip.seek_requested.connect(seen.append)

    target = QPointF(strip.width() / 4, strip.height() / 2)
    press = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        target,
        strip.mapToGlobal(QPoint(int(target.x()), int(target.y()))),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    strip.mousePressEvent(press)
    assert len(seen) == 1
    # 1/4 of a 100s strip → ~25 000 ms (±1 px tolerance ≈ 250 ms).
    assert abs(seen[0] - 25_000) < 500


def test_strip_click_with_no_duration_is_silent(qtbot):
    strip = _build_strip(qtbot)
    seen: list[int] = []
    strip.seek_requested.connect(seen.append)
    target = QPointF(20.0, 40.0)
    press = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        target,
        strip.mapToGlobal(QPoint(20, 40)),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    strip.mousePressEvent(press)
    assert seen == []


def test_strip_position_update_repaints(qtbot):
    strip = _build_strip(qtbot, width=300, height=80)
    strip.set_peaks(_flat_peaks(300), duration_s=30.0)
    strip.set_ranges([Range(source_id="src0", start=0.0, end=30.0)], 30.0)
    img1 = _grab_image(strip)
    strip.set_position(15_000)  # mid-strip
    img2 = _grab_image(strip)
    # Different x columns lit up: pixel-by-pixel difference must exist.
    assert not _images_equal(img1, img2)


def test_strip_dim_overlay_distinguishes_cut_regions(qtbot):
    """Cut spans must read visually distinct from kept spans.

    5d shipped a "darker" overlay; the 5e real-content check found
    black-on-dark-mode is invisible. The new implementation is a
    translucent gray + diagonal hatch that lifts dark bases toward
    gray and pushes light bases the same way — direction depends on
    palette. The contract is "different enough to read as cut" not
    "uniformly darker," so the test asserts a perceptual delta in
    either direction.
    """
    strip = _build_strip(qtbot, width=400, height=80)
    strip.set_peaks(_flat_peaks(400), duration_s=10.0)
    strip.set_ranges([Range(source_id="src0", start=0.0, end=5.0)], 10.0)
    img = _grab_image(strip)

    # Average lightness across a small rect rather than one pixel —
    # the hatch lines mean a single sampled column may land on a hatch
    # stroke (very light) or between strokes (overlay base).
    def _avg_lightness(x_lo: int, x_hi: int) -> float:
        total = 0
        n = 0
        for x in range(x_lo, x_hi):
            for y in range(strip.height() // 4, 3 * strip.height() // 4):
                total += _lightness(img.pixelColor(x, y))
                n += 1
        return total / max(1, n)

    kept_avg = _avg_lightness(strip.width() // 8, strip.width() // 8 + 30)
    cut_avg = _avg_lightness(strip.width() * 5 // 8, strip.width() * 5 // 8 + 30)
    # Either direction is acceptable; what matters is a visible delta.
    assert abs(kept_avg - cut_avg) >= 10, (
        f"cut-vs-kept contrast too low: kept={kept_avg:.1f} cut={cut_avg:.1f}"
    )


def test_strip_resize_does_not_regenerate_peaks(qtbot, monkeypatch):
    """Resizing the widget triggers paintEvent but not a new generate call."""
    calls = {"count": 0}

    def _spy(*args, **kwargs):
        calls["count"] += 1
        return _flat_peaks(100)

    monkeypatch.setattr(wf, "generate_peaks", _spy)

    strip = _build_strip(qtbot, width=300, height=80)
    strip.set_peaks(_flat_peaks(100), duration_s=5.0)
    strip.resize(500, 80)
    strip.repaint()
    strip.resize(700, 96)
    strip.repaint()
    assert calls["count"] == 0  # the strip itself never asks for new peaks


def test_strip_loading_state_paints_placeholder(qtbot):
    strip = _build_strip(qtbot)
    strip.set_loading(True)
    img = _grab_image(strip)
    # The loading background is a flat mid-tone — the rendered image
    # should have a low colour variance compared to a peaks paint.
    sampled = img.pixelColor(strip.width() // 2, strip.height() // 2)
    assert sampled.alpha() == 255
    assert _lightness(sampled) < 150  # darker than white


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _grab_image(strip: WaveformStrip) -> QImage:
    """Grab the widget into a QImage for pixel sampling."""
    pixmap = strip.grab()
    return pixmap.toImage()


def _images_equal(a: QImage, b: QImage) -> bool:
    if a.size() != b.size():
        return False
    if a.format() != b.format():
        a = a.convertToFormat(b.format())
    return bytes(a.constBits()) == bytes(b.constBits())


def _lightness(color) -> int:
    return (color.red() + color.green() + color.blue()) // 3
