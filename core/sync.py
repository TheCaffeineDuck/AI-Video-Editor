"""Audio sync — cross-correlate camera lav tracks against an audio master.

Phase 7 introduces the multi-cam highlight workflow. Setup is N camera
sources (each carrying its own audio scratch track from the camera mic
or a dedicated lav) plus a separate audio master file recorded by a
podcast mic / interface — typically the only audio that ends up in the
final render. To stitch fragments from multiple cameras while keeping
audio coming from the master, the renderer needs a per-camera offset
mapping camera-time to audio-master-time.

Convention (used everywhere in the codebase):

    audio_master_time_at_event = camera_time_at_event + offset_s

So if the audio master started 1.0 s *before* a camera, the camera
recorded the same content 1.0 s late, and the offset for that camera
is ``+1.0`` (audio time is 1.0 s ahead). The sign is "how much to add
to the camera clock to land on the audio master clock."

Estimation strategy: cross-correlate a 16 kHz mono PCM extraction of
each camera's audio against the audio master's same extraction, take
the lag at the correlation peak. The implementation uses
``numpy.fft`` (no scipy dependency) which is fast enough for the
typical search window (~30 s on each side at 16 kHz = ~1M samples).

Confidence is the ratio of the peak correlation value to the median
absolute correlation; values above ~3.0 are reliable in practice on
podcast lav audio. Below that, fall back to a manual override.

Storage: one :class:`SyncGroup` per "shoot" lives in
``<doc>.transcribe.json.sync/<sync_group_id>.sync.json``. The group
records the audio master path + hash, every camera's path + hash +
offset + manual flag, and the timestamp the offsets were estimated.
The renderer reads the group at apply time, validates each source's
hash, and uses the offsets to translate per-fragment camera windows
into audio master windows.

Manual override: callers (the GUI, an MCP tool, or a test) can call
:func:`write_sync_group` with an explicitly-set ``offset_s`` and
``manual_override=True`` for any camera; the next render uses that
value verbatim. Re-running estimation does not clobber a manual
offset unless the caller passes ``force=True``.
"""

from __future__ import annotations

import json
import logging
import secrets
import subprocess
import wave
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from core.audio import extract_wav_16k_mono, get_ffmpeg_path
from core.cache import cache_key
from core.document import UnsupportedSchemaError

_LOG = logging.getLogger(__name__)

SYNC_GROUP_SCHEMA_VERSION = 1
"""On-disk schema version for sync-group sidecar JSON files."""

DEFAULT_SAMPLE_RATE = 16000
"""Sample rate for the cross-correlation. 16 kHz keeps memory low and
captures every speech band; podcast voices live well within Nyquist."""

DEFAULT_MAX_LAG_S = 30.0
"""Default maximum offset to search, in seconds. 30 s is generous for a
typical "everyone hits record around the same time" workflow; if your
cameras start ten minutes apart, raise this explicitly. The search
window cost is O(N log N) on the FFT path so doubling lag is cheap."""

DEFAULT_SEARCH_WINDOW_S = 60.0
"""How much of each track to use when estimating the offset. 60 s of
content is enough for stable correlation on conversational audio (the
podcast use case); longer windows help when the audio is sparse but
add memory. Caller can override for sparse-content shoots."""

CONFIDENCE_GOOD = 5.0
"""Peak-to-noise ratio above which an offset is "trustworthy" without
manual review."""

CONFIDENCE_MARGINAL = 2.5
"""Peak-to-noise ratio between :data:`CONFIDENCE_MARGINAL` and
:data:`CONFIDENCE_GOOD` is logged as a soft warning; the renderer
still uses the offset, but the GUI may flag it for human verification."""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SyncEstimationError(RuntimeError):
    """Raised when cross-correlation could not produce a usable offset.

    Most commonly fires when a camera's audio is silent / corrupt /
    completely unrelated to the master (different room, different
    take). The remediation is to set the offset manually via
    :func:`set_manual_offset` after eyeballing waveforms in an audio
    editor.
    """


class StaleSyncGroupError(ValueError):
    """Raised when a sync group's stored hash for one of its sources
    no longer matches the live ``cache_key``.

    Mirrors :class:`core.highlight.StaleHighlightError`. Triggered
    when the audio master file or a camera file has been
    replaced/renamed since the offsets were estimated. Remediation
    is to re-author the sync group (or set the offset manually
    against the new file).
    """


# ---------------------------------------------------------------------------
# Cross-correlation core
# ---------------------------------------------------------------------------


def _read_wav_mono(path: Path) -> tuple[np.ndarray, int]:
    """Return ``(samples, sample_rate)`` for a 16-bit mono PCM wav file.

    Restricted to the shape produced by :func:`extract_wav_16k_mono`
    (16 kHz, mono, 16-bit PCM). Anything else raises — the caller is
    expected to extract first.
    """
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1:
            raise ValueError(
                f"sync expects mono wav, got {w.getnchannels()}-channel "
                f"file at {path}"
            )
        if w.getsampwidth() != 2:
            raise ValueError(
                f"sync expects 16-bit PCM wav, got {w.getsampwidth()*8}-bit "
                f"at {path}"
            )
        rate = w.getframerate()
        frame_count = w.getnframes()
        raw = w.readframes(frame_count)
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return samples, rate


def _cross_correlate_fft(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Full cross-correlation of two 1-D signals via FFT.

    Equivalent to ``numpy.correlate(a, b, mode='full')`` with
    ``method='fft'``: returns an array of length
    ``len(a) + len(b) - 1`` where index ``k`` corresponds to lag
    ``k - (len(b) - 1)``. Lag 0 sits at ``index = len(b) - 1``;
    positive lag means ``a`` is shifted RIGHT relative to ``b``
    (``a[t] ≈ b[t - lag]``).

    Implementation: convolve ``a`` with reverse(``b``) — that's the
    textbook FFT-based cross-correlation. ``numpy.fft.rfft`` is used
    because both inputs are real.
    """
    n = len(a) + len(b) - 1
    n_fft = 1 << (n - 1).bit_length()
    # Capital A / B follow signal-processing notation (uppercase
    # = frequency-domain). The lint suppressions below apply to those
    # convention-bound identifiers only.
    A = np.fft.rfft(a, n_fft)  # noqa: N806
    B = np.fft.rfft(b[::-1], n_fft)  # noqa: N806
    return np.fft.irfft(A * B, n_fft)[:n]


@dataclass(frozen=True)
class OffsetEstimate:
    """Result of one camera-vs-master cross-correlation pass."""

    offset_s: float
    """Seconds to add to camera time to land on audio-master time
    (sign convention from :mod:`core.sync`'s docstring)."""
    confidence: float
    """Peak-to-noise ratio of the correlation. >5 is reliable; 2.5–5
    is marginal; <2.5 should be discarded or manually overridden."""
    peak_correlation: float
    """Raw correlation value at the picked lag. Useful for debugging
    sparse audio."""


def estimate_offset(
    camera_audio_path: Path,
    audio_master_path: Path,
    *,
    max_lag_s: float = DEFAULT_MAX_LAG_S,
    search_window_s: float = DEFAULT_SEARCH_WINDOW_S,
    workdir: Path | None = None,
) -> OffsetEstimate:
    """Cross-correlate ``camera_audio_path`` against ``audio_master_path``.

    Both files are first extracted to 16 kHz mono PCM via ffmpeg
    (the camera's audio track is read directly off the camera video
    file; same for the master). The first ``search_window_s`` seconds
    of each are used for correlation — enough to lock onto in
    podcast-style audio without paying for the full duration.

    Returns the lag at the correlation peak as an
    :class:`OffsetEstimate`. Raises :class:`SyncEstimationError`
    when the inputs are too short or both signals are silent.

    ``workdir`` is the directory the temporary 16 kHz WAVs land in.
    Defaults to a sibling of ``audio_master_path``; callers (tests)
    can pass an explicit tmp dir.
    """
    if not camera_audio_path.is_file():
        raise FileNotFoundError(camera_audio_path)
    if not audio_master_path.is_file():
        raise FileNotFoundError(audio_master_path)

    workdir = workdir or audio_master_path.parent
    workdir.mkdir(parents=True, exist_ok=True)
    cam_wav = workdir / f".{camera_audio_path.stem}.{secrets.token_hex(3)}.16k.wav"
    mas_wav = workdir / f".{audio_master_path.stem}.{secrets.token_hex(3)}.16k.wav"
    try:
        extract_wav_16k_mono(camera_audio_path, cam_wav)
        extract_wav_16k_mono(audio_master_path, mas_wav)
        cam, cam_rate = _read_wav_mono(cam_wav)
        mas, mas_rate = _read_wav_mono(mas_wav)
    finally:
        for p in (cam_wav, mas_wav):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
    if cam_rate != mas_rate:
        raise SyncEstimationError(
            f"camera and master sample rates disagree: {cam_rate} vs {mas_rate}"
        )

    rate = cam_rate
    window_n = int(search_window_s * rate)
    cam = cam[:window_n]
    mas = mas[:window_n]

    if len(cam) < rate or len(mas) < rate:
        raise SyncEstimationError(
            "audio too short for sync estimation "
            f"(camera={len(cam)} master={len(mas)} samples)"
        )

    # Zero-mean and normalize so the correlation peak measures shape
    # alignment, not energy.
    cam = cam - float(np.mean(cam))
    mas = mas - float(np.mean(mas))
    cam_std = float(np.std(cam))
    mas_std = float(np.std(mas))
    if cam_std < 1e-6 or mas_std < 1e-6:
        raise SyncEstimationError(
            "one of camera / master is silent (or near-silent); "
            "cannot estimate offset"
        )
    cam = cam / cam_std
    mas = mas / mas_std

    # Cross-correlate master vs camera. Index ``k`` of the result
    # corresponds to lag ``k - (len(cam) - 1)``. Positive lag means
    # master is shifted RIGHT relative to camera, i.e., the master's
    # content arrives later in the camera's timeline — equivalently,
    # the camera is "ahead" of the master at the moment of match
    # (camera was rolling earlier).
    n = len(mas) + len(cam) - 1
    corr = _cross_correlate_fft(mas, cam)
    center = len(cam) - 1
    max_lag_samples = int(max_lag_s * rate)
    lo = max(0, center - max_lag_samples)
    hi = min(n, center + max_lag_samples + 1)
    window = corr[lo:hi]
    if window.size == 0:
        raise SyncEstimationError("max_lag_s window produced no candidates")
    peak_local = int(np.argmax(np.abs(window)))
    peak_global = lo + peak_local
    lag_samples = peak_global - center
    peak = float(window[peak_local])

    # Peak-to-noise: ratio of |peak| to the median |corr| over the
    # search window. Median is more robust than std on signals with
    # repetitive structure (music, hum) than a "ratio to std"
    # heuristic.
    noise = float(np.median(np.abs(window))) + 1e-9
    confidence = abs(peak) / noise

    if confidence < CONFIDENCE_MARGINAL:
        _LOG.warning(
            "low-confidence sync estimate for %s vs %s: confidence=%.2f peak=%.4f",
            camera_audio_path.name,
            audio_master_path.name,
            confidence,
            peak,
        )

    # Sign: ``lag_samples > 0`` means master is delayed relative to
    # camera (camera saw the same content earlier). To convert
    # camera_time to master_time we ADD that lag (master is +lag
    # samples ahead of where the camera is at the same content).
    # That matches the module's convention:
    #   master_time = camera_time + offset_s
    offset_s = lag_samples / rate
    return OffsetEstimate(
        offset_s=float(offset_s),
        confidence=float(confidence),
        peak_correlation=peak,
    )


# ---------------------------------------------------------------------------
# SyncGroup dataclass and persistence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SyncSource:
    """One camera entry inside a :class:`SyncGroup`.

    ``offset_s`` follows the convention in this module's docstring:
    audio-master-time = camera_time + ``offset_s``. ``manual_override``
    is set when the operator hand-set the value (overriding any
    previous estimate); re-estimation skips manual entries unless
    ``force=True``. ``confidence`` is the
    :class:`OffsetEstimate.confidence` from the auto-estimation, or
    ``None`` when the offset was set manually.
    """

    source_path: Path
    source_hash: str
    offset_s: float
    manual_override: bool = False
    confidence: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "source_path": str(self.source_path),
            "source_hash": self.source_hash,
            "offset_s": float(self.offset_s),
            "manual_override": bool(self.manual_override),
            "confidence": (
                None if self.confidence is None else float(self.confidence)
            ),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SyncSource:
        confidence_raw = data.get("confidence")
        return cls(
            source_path=Path(str(data["source_path"])),
            source_hash=str(data["source_hash"]),
            offset_s=float(data["offset_s"]),
            manual_override=bool(data.get("manual_override", False)),
            confidence=(None if confidence_raw is None else float(confidence_raw)),
        )


@dataclass(frozen=True)
class SyncGroup:
    """A collection of camera sources synced to one audio master.

    A single project ("podcast episode 42 shoot") typically has one
    sync group covering every camera that captured that conversation.
    Multiple sync groups per project are supported (e.g., a B-roll
    interview captured later in a different room) but the common
    case is one group per parent Document.

    Field semantics:

    - ``sync_group_id`` — sortable, mostly-unique id; also the
      filename stem for the sidecar.
    - ``audio_master_path`` / ``audio_master_hash`` — the file that
      drives the final cut's audio. Validated at render time.
    - ``cameras`` — dict keyed by camera source path string. Each
      entry carries the camera's own hash and the
      camera-to-master offset.
    - ``created_at`` / ``estimated_at`` — bookkeeping. ``estimated_at``
      shifts every time auto-estimation runs (or one entry is
      auto-overridden); manual override does not advance it.
    """

    sync_group_id: str
    audio_master_path: Path
    audio_master_hash: str
    cameras: dict[str, SyncSource]
    created_at: datetime
    estimated_at: datetime | None = None
    schema_version: int = SYNC_GROUP_SCHEMA_VERSION
    description: str = ""
    """Optional free-form label so the GUI can show 'Episode 42 main
    pod' instead of 'sg_a3b1...'. Empty string when unset."""

    def offset_for(self, camera_path: Path) -> float:
        """Return ``offset_s`` for ``camera_path`` (raise if unregistered)."""
        key = str(camera_path)
        entry = self.cameras.get(key)
        if entry is None:
            raise KeyError(
                f"camera {camera_path!r} not registered in sync group "
                f"{self.sync_group_id!r}; known: {list(self.cameras)!r}"
            )
        return entry.offset_s

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": int(self.schema_version),
            "sync_group_id": self.sync_group_id,
            "description": self.description,
            "audio_master_path": str(self.audio_master_path),
            "audio_master_hash": self.audio_master_hash,
            "cameras": {
                k: v.to_json() for k, v in self.cameras.items()
            },
            "created_at": self.created_at.isoformat(),
            "estimated_at": (
                None
                if self.estimated_at is None
                else self.estimated_at.isoformat()
            ),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SyncGroup:
        version_raw = data.get("schema_version")
        if version_raw is None:
            raise UnsupportedSchemaError(
                "SyncGroup JSON has no 'schema_version' field; cannot load."
            )
        version = int(version_raw)
        if version != SYNC_GROUP_SCHEMA_VERSION:
            raise UnsupportedSchemaError(
                f"SyncGroup schema_version={version!r} unsupported "
                f"(this build expects {SYNC_GROUP_SCHEMA_VERSION})."
            )
        cameras_raw = data.get("cameras", {})
        cameras = {
            str(k): SyncSource.from_json(v) for k, v in cameras_raw.items()
        }
        estimated_at_raw = data.get("estimated_at")
        return cls(
            sync_group_id=str(data["sync_group_id"]),
            audio_master_path=Path(str(data["audio_master_path"])),
            audio_master_hash=str(data["audio_master_hash"]),
            cameras=cameras,
            created_at=datetime.fromisoformat(str(data["created_at"])),
            estimated_at=(
                None
                if estimated_at_raw is None
                else datetime.fromisoformat(str(estimated_at_raw))
            ),
            schema_version=version,
            description=str(data.get("description", "")),
        )


_SYNC_DIR_SUFFIX = ".sync"


def sync_dir_for_document(document_path: Path) -> Path:
    """Return the sidecar directory ``<document_path>.sync``.

    Mirrors :func:`core.highlight.highlights_dir_for_document`.
    """
    p = Path(document_path)
    return p.with_name(p.name + _SYNC_DIR_SUFFIX)


def _sync_path(dir_: Path, sync_group_id: str) -> Path:
    return dir_ / f"{sync_group_id}.sync.json"


def _new_sync_group_id() -> str:
    """Sortable, mostly-unique id (timestamp + 4-byte random suffix)."""
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{ts}-{secrets.token_hex(4)}"


def write_sync_group(
    document_path: Path,
    group: SyncGroup,
) -> tuple[SyncGroup, Path]:
    """Persist ``group`` next to ``document_path``; return ``(materialized, path)``.

    "Materialized" means: ``sync_group_id`` is assigned (a fresh
    timestamp-prefixed id when blank).
    """
    dir_ = sync_dir_for_document(document_path)
    dir_.mkdir(parents=True, exist_ok=True)
    materialized = group
    if not materialized.sync_group_id:
        materialized = replace(materialized, sync_group_id=_new_sync_group_id())
    out_path = _sync_path(dir_, materialized.sync_group_id)
    out_path.write_text(
        json.dumps(materialized.to_json(), indent=2),
        encoding="utf-8",
    )
    return materialized, out_path


def read_sync_group(
    document_path: Path, sync_group_id: str
) -> SyncGroup:
    """Load ``<id>.sync.json`` from the document's sidecar directory."""
    dir_ = sync_dir_for_document(document_path)
    p = _sync_path(dir_, sync_group_id)
    if not p.is_file():
        raise FileNotFoundError(
            f"sync group {sync_group_id!r} not found at {p}"
        )
    return SyncGroup.from_json(json.loads(p.read_text(encoding="utf-8")))


def list_sync_groups_for_document(document_path: Path) -> list[SyncGroup]:
    """Return every sync group in the sidecar directory, chronologically."""
    dir_ = sync_dir_for_document(document_path)
    if not dir_.is_dir():
        return []
    out: list[SyncGroup] = []
    for entry in sorted(dir_.glob("*.sync.json")):
        try:
            out.append(SyncGroup.from_json(json.loads(entry.read_text(encoding="utf-8"))))
        except (ValueError, json.JSONDecodeError, UnsupportedSchemaError):
            continue
    return out


# ---------------------------------------------------------------------------
# High-level: build a sync group from raw paths
# ---------------------------------------------------------------------------


def build_sync_group(
    document_path: Path,
    audio_master_path: Path,
    camera_paths: list[Path],
    *,
    sync_group_id: str | None = None,
    description: str = "",
    max_lag_s: float = DEFAULT_MAX_LAG_S,
    search_window_s: float = DEFAULT_SEARCH_WINDOW_S,
    workdir: Path | None = None,
) -> SyncGroup:
    """Estimate offsets for every camera and return a :class:`SyncGroup`.

    Each camera's offset is set via :func:`estimate_offset`; the
    audio master's hash is captured. The result is *not* persisted
    automatically — callers (the GUI, an MCP tool) decide when to
    write it to disk via :func:`write_sync_group`.

    Cameras whose estimation raises :class:`SyncEstimationError` are
    still included in the group with ``offset_s=0.0`` and
    ``manual_override=False``; the GUI surfaces the warning and lets
    the operator set the value manually. We don't drop the camera
    because the group's identity ("here are the N files participating
    in this shoot") is more important than the per-camera success.
    """
    audio_master_path = audio_master_path.resolve()
    audio_master_hash = cache_key(audio_master_path)
    cameras: dict[str, SyncSource] = {}
    estimated_at = datetime.now(UTC)
    for cam in camera_paths:
        cam_resolved = cam.resolve()
        cam_hash = cache_key(cam_resolved)
        try:
            est = estimate_offset(
                cam_resolved,
                audio_master_path,
                max_lag_s=max_lag_s,
                search_window_s=search_window_s,
                workdir=workdir,
            )
            offset = est.offset_s
            confidence: float | None = est.confidence
        except SyncEstimationError as exc:
            _LOG.warning(
                "sync estimation failed for %s vs %s: %s — recording 0.0 offset",
                cam_resolved.name,
                audio_master_path.name,
                exc,
            )
            offset = 0.0
            confidence = None
        cameras[str(cam_resolved)] = SyncSource(
            source_path=cam_resolved,
            source_hash=cam_hash,
            offset_s=offset,
            manual_override=False,
            confidence=confidence,
        )
    return SyncGroup(
        sync_group_id=sync_group_id or "",
        audio_master_path=audio_master_path,
        audio_master_hash=audio_master_hash,
        cameras=cameras,
        created_at=datetime.now(UTC),
        estimated_at=estimated_at,
        description=description,
    )


def set_manual_offset(
    group: SyncGroup,
    camera_path: Path,
    offset_s: float,
) -> SyncGroup:
    """Return a new :class:`SyncGroup` with one camera's offset
    replaced by a manual value.

    The replaced :class:`SyncSource` carries ``manual_override=True``
    and ``confidence=None``. The group's ``estimated_at`` is left
    unchanged — manual override is its own kind of provenance, not a
    re-estimate.
    """
    key = str(camera_path)
    if key not in group.cameras:
        raise KeyError(
            f"camera {camera_path!r} not in sync group "
            f"{group.sync_group_id!r}"
        )
    old = group.cameras[key]
    new = SyncSource(
        source_path=old.source_path,
        source_hash=old.source_hash,
        offset_s=float(offset_s),
        manual_override=True,
        confidence=None,
    )
    new_cameras = {**group.cameras, key: new}
    return replace(group, cameras=new_cameras)


def validate_sync_group_freshness(group: SyncGroup) -> None:
    """Raise :class:`StaleSyncGroupError` if any source's hash drifted.

    The renderer calls this before pulling fragments. Catches the
    "we re-imported the audio master file and now its mtime is fresh"
    case — same heuristic the highlight stale-guard uses.
    """
    try:
        live_master = cache_key(group.audio_master_path)
    except FileNotFoundError as exc:
        raise StaleSyncGroupError(
            f"audio master {group.audio_master_path!r} is missing on disk: {exc}"
        ) from exc
    if live_master != group.audio_master_hash:
        raise StaleSyncGroupError(
            f"audio master {group.audio_master_path!r} cache_key drifted "
            f"({group.audio_master_hash!r} → {live_master!r}); "
            "re-estimate the sync group"
        )
    for key, cam in group.cameras.items():
        try:
            live = cache_key(cam.source_path)
        except FileNotFoundError as exc:
            raise StaleSyncGroupError(
                f"camera {key!r} is missing on disk: {exc}"
            ) from exc
        if live != cam.source_hash:
            raise StaleSyncGroupError(
                f"camera {key!r} cache_key drifted "
                f"({cam.source_hash!r} → {live!r}); re-estimate the sync group"
            )


# ---------------------------------------------------------------------------
# ffmpeg helpers used by the renderer's sync-group path
# ---------------------------------------------------------------------------


def extract_audio_master_window(
    audio_master_path: Path,
    out_path: Path,
    *,
    start_s: float,
    duration_s: float,
) -> Path:
    """Extract ``[start_s, start_s + duration_s]`` of the master to ``out_path``.

    Output is AAC stereo at 48 kHz — chosen to match the canonical
    multi-cam normalize encode profile so concat-demuxer is safe
    downstream. Uses ``-ss`` after ``-i`` for sub-frame accuracy
    (we re-encode anyway).
    """
    if start_s < 0.0:
        # Negative start happens when a camera is *ahead* of the master
        # at the requested fragment; in practice the operator either
        # set the offset wrong or the fragment is too close to the
        # master's start. We pad with silence at the head rather than
        # crash; the renderer expects ``out_path`` to land at exactly
        # ``duration_s``.
        silence_dur = -start_s
        body_dur = max(0.0, duration_s - silence_dur)
        ffmpeg = get_ffmpeg_path()
        cmd = [
            str(ffmpeg),
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-t",
            f"{silence_dur:.3f}",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-ss",
            "0",
            "-t",
            f"{body_dur:.3f}",
            "-i",
            str(audio_master_path),
            "-filter_complex",
            "[0:a][1:a]concat=n=2:v=0:a=1[a]",
            "-map",
            "[a]",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            str(out_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg head-pad-and-extract failed (rc={result.returncode}): "
                f"{result.stderr[-400:]}"
            )
        return out_path
    ffmpeg = get_ffmpeg_path()
    cmd = [
        str(ffmpeg),
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(audio_master_path),
        "-ss",
        f"{start_s:.3f}",
        "-t",
        f"{duration_s:.3f}",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-ac",
        "2",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg audio-master extract failed (rc={result.returncode}): "
            f"{result.stderr[-400:]}"
        )
    return out_path


__all__ = [
    "CONFIDENCE_GOOD",
    "CONFIDENCE_MARGINAL",
    "DEFAULT_MAX_LAG_S",
    "DEFAULT_SAMPLE_RATE",
    "DEFAULT_SEARCH_WINDOW_S",
    "OffsetEstimate",
    "StaleSyncGroupError",
    "SyncEstimationError",
    "SyncGroup",
    "SyncSource",
    "build_sync_group",
    "estimate_offset",
    "extract_audio_master_window",
    "list_sync_groups_for_document",
    "read_sync_group",
    "set_manual_offset",
    "sync_dir_for_document",
    "validate_sync_group_freshness",
    "write_sync_group",
]
