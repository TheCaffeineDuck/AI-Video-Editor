"""Waveform peak generation off the UI thread.

Pure-data utility — no Qt — so it runs equally well from a worker thread
and from a synchronous test. Wraps a single ``ffmpeg`` invocation that
decodes the source media to mono 16-bit PCM at 22 kHz and bins the
samples into ``(min, max)`` pairs per bucket.

Why 22 kHz: visualization is a low-information signal; 44.1 kHz reads
are twice as long for no visible benefit at strip widths up to a few
thousand pixels. Why mono: same reason — a single peak strip per side
isn't useful at this scale.

Why ``(min, max)`` rather than ``abs_max``: keeps the painter free to
draw symmetric or asymmetric peaks without round-tripping a re-decode.

Why bare ``subprocess`` rather than ``ffmpeg-python``: one fewer
dependency. The ffmpeg command is short and stable.

Cache format: side-car ``<source>.peaks.npz`` next to the source
(mirrors the existing ``.transcribe.json`` convention). The npz is an
uncompressed zip with two members — ``peaks`` (the float32 array) and
``meta`` (a single-element object array carrying ``source_hash``,
``duration_s``, ``bucket_count``, ``schema_version``). Stale-detection
compares ``source_hash`` against :func:`core.cache.cache_key`; mismatch
forces a regeneration.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from core.audio import get_ffmpeg_path
from core.cache import cache_key

_LOG = logging.getLogger(__name__)

_SAMPLE_RATE = 22_050
_DEFAULT_BUCKETS = 4000

# Peaks-cache schema version. Bump this whenever the on-disk layout
# changes — for example: switching from ``(min, max)`` pairs to
# ``abs_max`` scalars, changing the default bucket count, or adding a
# field that older readers must regenerate to honor. Mismatched
# ``schema_version`` makes :func:`load_peaks_cache` return ``None`` so
# the controller falls through to a fresh generation. This is the same
# code path as a ``source_hash`` mismatch — there's no migration; we
# just regenerate.
_PEAKS_SCHEMA_VERSION = 1
PEAKS_SUFFIX = ".peaks.npz"


@dataclass(frozen=True)
class CachedPeaks:
    """A loaded ``.peaks.npz`` side-car: the array plus its metadata."""

    peaks: np.ndarray
    source_hash: str
    duration_s: float
    bucket_count: int


def peaks_path(source_path: Path) -> Path:
    """Return the on-disk cache path for ``source_path``."""
    return source_path.with_name(source_path.name + PEAKS_SUFFIX)


class PeaksCancelledError(RuntimeError):
    """Raised by :func:`generate_peaks` when its cancel event was set."""


def generate_peaks(
    source_path: Path,
    bucket_count: int = _DEFAULT_BUCKETS,
    on_progress: Callable[[float], None] | None = None,
    *,
    cancel_event: threading.Event | None = None,
) -> np.ndarray:
    """Decode ``source_path`` to PCM and reduce to ``(min, max)`` per bucket.

    Returns a ``(bucket_count, 2)`` ``float32`` array with values in
    ``[-1.0, 1.0]``. Raises :class:`FileNotFoundError` when ``source_path``
    is missing, :class:`subprocess.CalledProcessError` on ffmpeg failure,
    :class:`PeaksCancelledError` when ``cancel_event`` is set mid-decode.

    ``on_progress`` (if given) is called with a fraction in ``[0, 1]``
    after every chunk. The fraction is best-effort — duration may be
    unknown until decode completes — so callers should treat it as
    "useful for a busy indicator, not a precise ETA".
    """
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if bucket_count <= 0:
        raise ValueError(f"bucket_count must be positive, got {bucket_count}")

    ffmpeg = get_ffmpeg_path()
    # ``-vn`` skips video decode (irrelevant to peaks, faster). ``-map
    # 0:a?`` selects every audio stream optionally, so a video-only file
    # decodes to zero samples rather than ffmpeg-erroring out (we render
    # those as a flat strip below).
    cmd = [
        str(ffmpeg),
        "-nostdin",
        "-loglevel", "error",
        "-i", str(source_path),
        "-vn",
        "-map", "0:a?",
        "-f", "s16le",
        "-ac", "1",
        "-ar", str(_SAMPLE_RATE),
        "pipe:1",
    ]
    # Read in chunks so we can report progress and stream-process samples
    # without loading multi-GB files into memory whole.
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    cancelled = False
    try:
        all_samples = _read_pcm(process, on_progress, cancel_event)
    except PeaksCancelledError:
        cancelled = True
        raise
    except Exception:
        process.kill()
        raise
    finally:
        if cancelled:
            process.kill()
        # Drain stderr for diagnostics, then wait. ffmpeg dies promptly
        # when its stdout pipe closes.
        stderr = process.stderr.read() if process.stderr else b""
        process.wait()
    if process.returncode != 0:
        # Some sources (locationbird MP4s) are video-only; ffmpeg refuses
        # to write a 0-stream output container and exits non-zero. Detect
        # that specific case and return a flat strip rather than failing —
        # the editor still wants peaks on every editable source.
        stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
        if "does not contain any stream" in stderr_text:
            _LOG.info(
                "no audio stream in %s — returning flat peaks", source_path
            )
            return np.zeros((bucket_count, 2), dtype=np.float32)
        raise subprocess.CalledProcessError(
            process.returncode, cmd, output=None, stderr=stderr
        )
    if all_samples.size == 0:
        # Nothing decoded — return a zero array of the right shape so
        # the painter can render a flat line rather than crash.
        return np.zeros((bucket_count, 2), dtype=np.float32)
    return _reduce_to_buckets(all_samples, bucket_count)


def _read_pcm(
    process: subprocess.Popen,
    on_progress: Callable[[float], None] | None,
    cancel_event: threading.Event | None = None,
) -> np.ndarray:
    """Drain ffmpeg's stdout into one int16 array.

    Read in 1 MiB chunks. Concatenating a list of arrays is faster than
    repeated ``np.append``; the final ``np.concatenate`` happens once.
    """
    chunk_size = 1 << 20  # 1 MiB
    chunks: list[np.ndarray] = []
    bytes_read = 0
    assert process.stdout is not None
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise PeaksCancelledError("peak generation was cancelled")
        buf = process.stdout.read(chunk_size)
        if not buf:
            break
        # Trim odd byte at end of buffer if any (shouldn't happen for
        # 16-bit mono, but keeps frombuffer safe under partial reads).
        if len(buf) % 2:
            buf = buf[: -(len(buf) % 2)]
        if not buf:
            continue
        arr = np.frombuffer(buf, dtype=np.int16)
        chunks.append(arr)
        bytes_read += len(buf)
        if on_progress is not None:
            # Without a known total we cap the indicator at 0.95 so the
            # bar doesn't stall at 1.0 before we've reduced the data.
            on_progress(min(0.95, bytes_read / (50 * 1024 * 1024)))
    if not chunks:
        return np.zeros(0, dtype=np.int16)
    return np.concatenate(chunks)


def _reduce_to_buckets(samples: np.ndarray, bucket_count: int) -> np.ndarray:
    """Pool ``samples`` into ``bucket_count`` ``(min, max)`` pairs.

    Sample count is rarely a multiple of ``bucket_count``, so we use
    ``np.array_split`` (which produces near-equal-sized chunks differing
    by at most 1) and reduce each chunk independently.

    Output is ``float32`` in ``[-1, 1]`` — int16 sample range divided
    by 32768.0.
    """
    splits = np.array_split(samples, bucket_count)
    out = np.empty((bucket_count, 2), dtype=np.float32)
    for i, chunk in enumerate(splits):
        if chunk.size == 0:
            out[i, 0] = 0.0
            out[i, 1] = 0.0
            continue
        out[i, 0] = chunk.min()
        out[i, 1] = chunk.max()
    out /= 32768.0
    np.clip(out, -1.0, 1.0, out=out)
    return out


def save_peaks_cache(
    source_path: Path,
    peaks: np.ndarray,
    duration_s: float,
) -> Path:
    """Write ``peaks`` to the side-car cache. Returns the path written."""
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    target = peaks_path(source_path)
    meta = {
        "source_hash": cache_key(source_path),
        "duration_s": float(duration_s),
        "bucket_count": int(peaks.shape[0]),
        "schema_version": _PEAKS_SCHEMA_VERSION,
    }
    np.savez(
        target,
        peaks=peaks.astype(np.float32, copy=False),
        meta=np.array(meta, dtype=object),
    )
    return target


def load_peaks_cache(source_path: Path) -> CachedPeaks | None:
    """Return the cached peaks iff present and ``source_hash`` still matches.

    Returns ``None`` for any failure — missing file, unreadable npz,
    schema bump, or stale hash. The caller falls through to a fresh
    :func:`generate_peaks`.
    """
    target = peaks_path(source_path)
    if not target.is_file():
        return None
    try:
        with np.load(target, allow_pickle=True) as data:
            peaks = np.asarray(data["peaks"], dtype=np.float32)
            meta = data["meta"].item()
    except (OSError, ValueError, KeyError) as exc:
        _LOG.warning("could not read peaks cache %s: %s", target, exc)
        return None
    if not isinstance(meta, dict):
        return None
    if meta.get("schema_version") != _PEAKS_SCHEMA_VERSION:
        return None
    try:
        current_hash = cache_key(source_path)
    except FileNotFoundError:
        return None
    if meta.get("source_hash") != current_hash:
        return None
    return CachedPeaks(
        peaks=peaks,
        source_hash=str(meta["source_hash"]),
        duration_s=float(meta.get("duration_s", 0.0)),
        bucket_count=int(meta.get("bucket_count", peaks.shape[0])),
    )
