"""Whisper model registry, cache path resolution, and download status."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModelInfo:
    name: str           # friendly name shown in the UI
    repo_id: str        # Hugging Face repo id used by faster_whisper
    size_mb: int        # approximate disk footprint
    speed_label: str    # human-readable inference speed on CPU


# Spec section 5.4
MODEL_REGISTRY: dict[str, ModelInfo] = {
    "tiny": ModelInfo("tiny", "Systran/faster-whisper-tiny", 75, "~10× realtime on CPU"),
    "base": ModelInfo("base", "Systran/faster-whisper-base", 145, "~5× realtime on CPU"),
    "small": ModelInfo("small", "Systran/faster-whisper-small", 484, "~2× realtime on CPU"),
    "medium": ModelInfo("medium", "Systran/faster-whisper-medium", 1500, "~1× realtime on CPU"),
    "large-v3": ModelInfo(
        "large-v3", "Systran/faster-whisper-large-v3", 3000, "~0.3× realtime on CPU"
    ),
}

MODEL_NAMES: tuple[str, ...] = tuple(MODEL_REGISTRY.keys())


def get_model(name: str) -> ModelInfo:
    """Return registry entry for ``name`` or raise ``KeyError``."""
    try:
        return MODEL_REGISTRY[name]
    except KeyError as e:
        raise KeyError(f"unknown model {name!r}; expected one of {list(MODEL_NAMES)}") from e


def cache_root() -> Path:
    """Return the Hugging Face Hub cache root used by faster-whisper.

    Honors ``HF_HOME`` (sets ``$HF_HOME/hub``) and ``HUGGINGFACE_HUB_CACHE``
    (used directly), falling back to ``~/.cache/huggingface/hub``.
    """
    explicit = os.environ.get("HUGGINGFACE_HUB_CACHE")
    if explicit:
        return Path(explicit)
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        return Path(hf_home) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _repo_cache_dir(repo_id: str) -> Path:
    """Hugging Face stores ``foo/bar`` as ``models--foo--bar`` under the hub root."""
    safe = repo_id.replace("/", "--")
    return cache_root() / f"models--{safe}"


# Files faster-whisper actually loads. Presence of these inside the repo's
# snapshots tree is a strong signal the model is fully downloaded.
_REQUIRED_MODEL_FILES = ("model.bin", "tokenizer.json", "config.json")


def is_downloaded(name: str) -> bool:
    """Return True if a non-empty snapshot containing the required files exists."""
    info = get_model(name)
    snapshots_dir = _repo_cache_dir(info.repo_id) / "snapshots"
    if not snapshots_dir.is_dir():
        return False
    for snapshot in snapshots_dir.iterdir():
        if not snapshot.is_dir():
            continue
        if all((snapshot / f).exists() for f in _REQUIRED_MODEL_FILES):
            return True
    return False


def cache_path_for(name: str) -> Path:
    """Return the per-repo cache directory for ``name`` (may not exist yet)."""
    return _repo_cache_dir(get_model(name).repo_id)
