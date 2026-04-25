"""Tests for core.model_loader — download progress wiring."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from core import model_loader


def test_already_downloaded_skips_network_and_calls_progress(monkeypatch, tmp_path):
    """When a model is already in cache, no HTTP call is made; progress goes 0→1."""
    monkeypatch.setattr(model_loader, "is_downloaded", lambda _name: True)
    progress_calls: list[tuple[float, str]] = []

    def record(fraction, label):
        progress_calls.append((fraction, label))

    def fake_snapshot_download(*args, **kwargs):
        raise AssertionError("snapshot_download should not be called when cached")

    with patch("huggingface_hub.snapshot_download", side_effect=fake_snapshot_download):
        model_loader.download_model("tiny", on_progress=record)

    assert progress_calls == [(1.0, "tiny already downloaded")]


def test_download_invokes_snapshot_download_with_tqdm_class(monkeypatch):
    """Not-yet-downloaded path passes a tqdm_class kwarg and reports completion."""
    monkeypatch.setattr(model_loader, "is_downloaded", lambda _name: False)
    progress_calls: list[tuple[float, str]] = []

    def record(fraction, label):
        progress_calls.append((fraction, label))

    captured = {}

    def fake_snapshot_download(repo_id, **kwargs):
        captured["repo_id"] = repo_id
        captured["tqdm_class"] = kwargs.get("tqdm_class")
        return Path("/dev/null")

    with patch("huggingface_hub.snapshot_download", side_effect=fake_snapshot_download):
        model_loader.download_model("tiny", on_progress=record)

    assert captured["repo_id"] == "Systran/faster-whisper-tiny"
    assert captured["tqdm_class"] is not None
    # Final completion call is always emitted.
    assert progress_calls[-1] == (1.0, "tiny ready")


def test_progress_tqdm_emits_fraction_on_update(monkeypatch):
    """The injected tqdm_class fires on_progress(fraction, label) on each update."""
    monkeypatch.setattr(model_loader, "is_downloaded", lambda _name: False)
    seen: list[tuple[float, str]] = []

    def fake_snapshot_download(_repo_id, *, tqdm_class, **_):
        # Drive a few updates ourselves.
        bar = tqdm_class(total=100, desc="weights")
        bar.update(25)
        bar.update(50)
        bar.update(25)
        bar.close()

    with patch("huggingface_hub.snapshot_download", side_effect=fake_snapshot_download):
        model_loader.download_model("tiny", on_progress=lambda f, lbl: seen.append((f, lbl)))

    fractions = [f for f, _ in seen if 0.0 <= f <= 1.0]
    # First reading at 25%, last at 100% (before the final "ready" call).
    assert fractions[0] == pytest.approx(0.25)
    assert fractions[-2] == pytest.approx(1.0)
    assert seen[-1] == (1.0, "tiny ready")


def test_unknown_model_raises():
    with pytest.raises(KeyError):
        model_loader.download_model("not-a-model")
