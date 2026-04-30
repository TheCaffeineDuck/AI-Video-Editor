"""Sync group lifecycle tools — create / list / read / set_offset.

Phase 7 surface. Sync groups capture per-camera offsets against an
audio master so multi-cam highlights can pull video from a chosen
camera while audio comes from the master, regardless of when each
device started recording.

Path:

    create_sync_group → list_sync_groups → read_sync_group
                                          ↓
                                     set_sync_offset (manual override)

Cross-correlation runs synchronously over MCP — for podcast-grade
audio (lav mics, conversational content) it lands in well under a
minute on a typical M-series Mac. Longer files raise the wall clock
roughly linearly with ``search_window_s``; the default 60 s is the
sweet spot for getting a reliable lock without blocking Claude
Desktop for too long.
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.cache import cache_key
from core.sync import (
    CONFIDENCE_GOOD,
    SyncEstimationError,
    build_sync_group,
    list_sync_groups_for_document,
    set_manual_offset,
    write_sync_group,
)
from core.sync import (
    read_sync_group as core_read_sync_group,
)
from mcp_server import errors
from mcp_server.schemas import (
    CreateSyncGroupRequest,
    CreateSyncGroupResult,
    ListSyncGroupsRequest,
    ListSyncGroupsResult,
    ReadSyncGroupRequest,
    SetSyncOffsetRequest,
    SetSyncOffsetResult,
    SyncGroupOut,
    SyncSourceOut,
)
from mcp_server.tools.document import _load_document

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Wire ↔ core translation
# ---------------------------------------------------------------------------


def _camera_to_out(cam) -> SyncSourceOut:  # type: ignore[no-untyped-def]
    return SyncSourceOut(
        source_path=str(cam.source_path),
        source_hash=cam.source_hash,
        offset_s=float(cam.offset_s),
        manual_override=bool(cam.manual_override),
        confidence=(None if cam.confidence is None else float(cam.confidence)),
    )


def _group_to_out(group) -> SyncGroupOut:  # type: ignore[no-untyped-def]
    return SyncGroupOut(
        sync_group_id=group.sync_group_id,
        description=group.description,
        audio_master_path=str(group.audio_master_path),
        audio_master_hash=group.audio_master_hash,
        cameras=[_camera_to_out(c) for c in group.cameras.values()],
        created_at=group.created_at.isoformat(),
        estimated_at=(
            None if group.estimated_at is None else group.estimated_at.isoformat()
        ),
    )


# ---------------------------------------------------------------------------
# Tool: create_sync_group
# ---------------------------------------------------------------------------


async def create_sync_group(req: CreateSyncGroupRequest) -> CreateSyncGroupResult:
    """Run cross-correlation against ``audio_master_path`` for each camera; persist.

    The audio master and every camera are validated for existence
    before estimation runs. Cameras whose estimation surfaces a
    :class:`core.sync.SyncEstimationError` (silent audio, corrupt
    file) land in the result with ``offset_s=0.0`` and
    ``manual_override=False`` — the operator follows up via
    ``set_sync_offset``. Low-confidence cameras (peak-to-noise below
    :data:`core.sync.CONFIDENCE_GOOD`) are listed in
    ``low_confidence_cameras`` so the GUI / human can flag them.
    """
    _, doc_path, _ = _load_document(req.json_path)

    audio_master = Path(req.audio_master_path)
    if not audio_master.is_file():
        errors.raise_mcp(
            errors.FILE_NOT_FOUND,
            f"audio master file not found: {audio_master}",
            data={"audio_master_path": str(audio_master)},
        )
    cameras: list[Path] = []
    for cp in req.camera_paths:
        p = Path(cp)
        if not p.is_file():
            errors.raise_mcp(
                errors.FILE_NOT_FOUND,
                f"camera file not found: {p}",
                data={"camera_path": str(p)},
            )
        cameras.append(p)
    if not cameras:
        errors.raise_mcp(
            errors.INVALID_SYNC_GROUP,
            "create_sync_group needs at least one camera_path",
            data={},
        )

    try:
        group = build_sync_group(
            doc_path,
            audio_master,
            cameras,
            description=req.description,
            max_lag_s=float(req.max_lag_s),
            search_window_s=float(req.search_window_s),
        )
    except SyncEstimationError as exc:
        errors.raise_mcp(
            errors.SYNC_ESTIMATION_FAILED,
            f"sync estimation failed: {exc}",
            data={"audio_master_path": str(audio_master)},
        )

    materialized, sync_path = write_sync_group(doc_path, group)
    low_conf = [
        str(c.source_path)
        for c in materialized.cameras.values()
        if c.confidence is None or c.confidence < CONFIDENCE_GOOD
    ]

    _LOG.info(
        "create_sync_group: id=%s cameras=%d low_confidence=%d",
        materialized.sync_group_id,
        len(materialized.cameras),
        len(low_conf),
    )
    return CreateSyncGroupResult(
        sync_group_id=materialized.sync_group_id,
        sync_group_path=str(sync_path),
        cameras=[_camera_to_out(c) for c in materialized.cameras.values()],
        low_confidence_cameras=low_conf,
    )


# ---------------------------------------------------------------------------
# Tool: list_sync_groups
# ---------------------------------------------------------------------------


async def list_sync_groups(req: ListSyncGroupsRequest) -> ListSyncGroupsResult:
    """Directory scan of ``<doc>.sync/*.sync.json``."""
    _, doc_path, _ = _load_document(req.json_path)
    groups = list_sync_groups_for_document(doc_path)
    return ListSyncGroupsResult(
        sync_groups=[_group_to_out(g) for g in groups]
    )


# ---------------------------------------------------------------------------
# Tool: read_sync_group
# ---------------------------------------------------------------------------


async def read_sync_group(req: ReadSyncGroupRequest) -> SyncGroupOut:
    _, doc_path, _ = _load_document(req.json_path)
    try:
        group = core_read_sync_group(doc_path, req.sync_group_id)
    except FileNotFoundError as exc:
        errors.raise_mcp(
            errors.SYNC_GROUP_NOT_FOUND,
            str(exc),
            data={"sync_group_id": req.sync_group_id},
        )
    return _group_to_out(group)


# ---------------------------------------------------------------------------
# Tool: set_sync_offset
# ---------------------------------------------------------------------------


async def set_sync_offset(req: SetSyncOffsetRequest) -> SetSyncOffsetResult:
    """Manually override one camera's offset; persist."""
    _, doc_path, _ = _load_document(req.json_path)
    try:
        group = core_read_sync_group(doc_path, req.sync_group_id)
    except FileNotFoundError as exc:
        errors.raise_mcp(
            errors.SYNC_GROUP_NOT_FOUND,
            str(exc),
            data={"sync_group_id": req.sync_group_id},
        )
    cam_path = Path(req.camera_path)
    if str(cam_path) not in group.cameras:
        errors.raise_mcp(
            errors.INVALID_SYNC_GROUP,
            (
                f"camera_path={req.camera_path!r} is not registered in "
                f"sync group {req.sync_group_id!r}; known cameras: "
                f"{sorted(group.cameras)!r}"
            ),
            data={
                "sync_group_id": req.sync_group_id,
                "camera_path": str(cam_path),
            },
        )
    # Re-hash the camera in case the file was replaced since the group
    # was authored — the manual override should be against the current
    # file's identity.
    try:
        cache_key(cam_path)
    except FileNotFoundError as exc:
        errors.raise_mcp(
            errors.FILE_NOT_FOUND,
            str(exc),
            data={"camera_path": str(cam_path)},
        )
    updated = set_manual_offset(group, cam_path, float(req.offset_s))
    materialized, _ = write_sync_group(doc_path, updated)
    cam = materialized.cameras[str(cam_path)]
    return SetSyncOffsetResult(
        sync_group_id=materialized.sync_group_id,
        camera=_camera_to_out(cam),
    )
