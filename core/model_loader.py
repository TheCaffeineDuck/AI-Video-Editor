"""Download Whisper model weights with a real progress callback.

faster_whisper triggers the download lazily inside ``WhisperModel(name)`` with
no progress hook. We mimic that by calling ``huggingface_hub.snapshot_download``
ourselves, attaching a custom tqdm class that forwards to the UI.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from core.models import get_model, is_downloaded

log = logging.getLogger(__name__)


ProgressCallback = Callable[[float, str], None]


def _make_progress_tqdm(on_progress: ProgressCallback, label: str):
    """Build a tqdm subclass that calls ``on_progress(fraction, label)`` on each update."""
    from tqdm.auto import tqdm as base_tqdm

    class _ProgressTqdm(base_tqdm):
        def update(self, n: int = 1) -> Any:  # noqa: D401 - matches tqdm
            ret = super().update(n)
            try:
                if self.total:
                    fraction = max(0.0, min(1.0, self.n / self.total))
                    on_progress(fraction, label)
            except Exception:  # noqa: BLE001 - never let progress reporting break a download
                log.exception("progress callback failed")
            return ret

    return _ProgressTqdm


def download_model(name: str, on_progress: ProgressCallback | None = None) -> None:
    """Ensure ``name``'s weights are present in the HF cache, reporting progress."""
    info = get_model(name)
    if is_downloaded(name):
        if on_progress is not None:
            on_progress(1.0, f"{name} already downloaded")
        return

    from huggingface_hub import snapshot_download

    label = f"Downloading {name} model ({info.size_mb} MB)…"
    tqdm_class = _make_progress_tqdm(on_progress, label) if on_progress else None
    snapshot_download(info.repo_id, tqdm_class=tqdm_class)
    if on_progress is not None:
        on_progress(1.0, f"{name} ready")
