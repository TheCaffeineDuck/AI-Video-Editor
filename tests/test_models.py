"""Tests for core.models."""

from __future__ import annotations

from pathlib import Path

import pytest

from core import models

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_has_all_five_sizes_from_spec():
    expected = {"tiny", "base", "small", "medium", "large-v3"}
    assert set(models.MODEL_REGISTRY) == expected


def test_registry_repo_ids_match_spec():
    assert models.MODEL_REGISTRY["tiny"].repo_id == "Systran/faster-whisper-tiny"
    assert models.MODEL_REGISTRY["base"].repo_id == "Systran/faster-whisper-base"
    assert models.MODEL_REGISTRY["small"].repo_id == "Systran/faster-whisper-small"
    assert models.MODEL_REGISTRY["medium"].repo_id == "Systran/faster-whisper-medium"
    assert models.MODEL_REGISTRY["large-v3"].repo_id == "Systran/faster-whisper-large-v3"


def test_get_model_returns_info():
    info = models.get_model("base")
    assert info.name == "base"
    assert info.size_mb == 145


def test_get_model_unknown_raises():
    with pytest.raises(KeyError):
        models.get_model("not-a-model")


# ---------------------------------------------------------------------------
# cache_root
# ---------------------------------------------------------------------------


def test_cache_root_default_points_to_huggingface_hub(monkeypatch):
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    expected = Path.home() / ".cache" / "huggingface" / "hub"
    assert models.cache_root() == expected


def test_cache_root_honors_hf_home(monkeypatch, tmp_path):
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    assert models.cache_root() == tmp_path / "hub"


def test_cache_root_honors_explicit_hub_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path / "explicit"))
    assert models.cache_root() == tmp_path / "explicit"


# ---------------------------------------------------------------------------
# is_downloaded
# ---------------------------------------------------------------------------


def test_is_downloaded_false_when_cache_empty(monkeypatch, tmp_path):
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path))
    assert models.is_downloaded("tiny") is False


def test_is_downloaded_false_when_snapshots_dir_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path))
    repo_dir = tmp_path / "models--Systran--faster-whisper-tiny"
    repo_dir.mkdir()
    assert models.is_downloaded("tiny") is False


def test_is_downloaded_true_when_required_files_present(monkeypatch, tmp_path):
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path))
    snap = tmp_path / "models--Systran--faster-whisper-tiny" / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    for f in ("model.bin", "tokenizer.json", "config.json"):
        (snap / f).write_text("x")
    assert models.is_downloaded("tiny") is True


def test_is_downloaded_false_when_partial_snapshot(monkeypatch, tmp_path):
    """A snapshot missing one required file should not count as downloaded."""
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path))
    snap = tmp_path / "models--Systran--faster-whisper-tiny" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "model.bin").write_text("x")
    (snap / "config.json").write_text("x")
    # tokenizer.json missing
    assert models.is_downloaded("tiny") is False


def test_cache_path_for_returns_models_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path))
    assert models.cache_path_for("base") == tmp_path / "models--Systran--faster-whisper-base"
