"""Pydantic input/output models for the MCP tool surface.

Each tool's input and output is a Pydantic model. Inputs are validated
against the model's JSON schema before the handler runs (the MCP SDK
does this automatically when we register the tool with
``inputSchema=Model.model_json_schema()``); outputs are dumped via
``model.model_dump(mode="json")`` and returned as ``structuredContent``.

Shapes are intentionally narrow — every field has a documented purpose.
6b can extend them additively, but renaming or repurposing fields
breaks the client contract and should be avoided.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# transcribe
# ---------------------------------------------------------------------------


class TranscribeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_path: str = Field(
        description=(
            "Absolute path to a video or audio file to transcribe. "
            "Required; relative paths are not resolved against any "
            "implicit working directory."
        )
    )
    output_path: str | None = Field(
        default=None,
        description=(
            "Absolute path to write the resulting .transcribe.json. "
            "Defaults to <source_dir>/<source_stem>.transcribe.json — the "
            "same convention the GUI uses, so a file written by the MCP "
            "server is loadable by the editor without changes."
        ),
    )
    model: str | None = Field(
        default=None,
        description=(
            "faster-whisper model name (e.g. 'base', 'small', 'medium'). "
            "Defaults to Settings.default_model."
        ),
    )
    language: str | None = Field(
        default=None,
        description=(
            "ISO 639-1 language code (e.g. 'en', 'es') or null for "
            "autodetect. Defaults to Settings.default_language."
        ),
    )


class TranscribeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_path: str
    duration_s: float
    word_count: int
    language_detected: str | None
    cache_hit: bool = Field(
        description=(
            "True when the .transcribe.json already existed with a "
            "matching source_hash and inference was skipped."
        )
    )


# ---------------------------------------------------------------------------
# load_document
# ---------------------------------------------------------------------------


class JsonPathRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    json_path: str = Field(
        description="Absolute path to a .transcribe.json file."
    )


class DocumentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    source_path: str
    duration_s: float
    word_count: int
    range_count: int
    # ``schema_version`` is the raw on-disk version. v1 / v2 / v3 are
    # ints; v3.1 (Phase 6b-1) is a float — the field is widened to
    # ``int | float`` so clients can branch on the major.minor value
    # without re-parsing the JSON. Pydantic emits the union as
    # ``oneOf`` in the JSON schema; clients can coerce as needed.
    schema_version: int | float
    created_at: str


# ---------------------------------------------------------------------------
# get_transcript
# ---------------------------------------------------------------------------


class GetTranscriptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    json_path: str = Field(
        description="Absolute path to a .transcribe.json file."
    )
    include_struck: bool = Field(
        default=True,
        description=(
            "When true (default), every word is returned with a `struck` "
            "flag set to true if that word's timing falls outside any "
            "kept range (i.e., would be removed by render). When false, "
            "only words inside kept ranges are returned and `struck` is "
            "always false."
        ),
    )


class TranscriptWord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    word: str
    start_s: float
    end_s: float
    segment_idx: int
    struck: bool


class TranscriptResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    words: list[TranscriptWord]


# ---------------------------------------------------------------------------
# get_ranges
# ---------------------------------------------------------------------------


class RangeOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_s: float
    end_s: float
    reason: str = ""


class RangesResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ranges: list[RangeOut]
    total_kept_s: float
    total_cut_s: float
    is_source_monotonic: bool = True


# ---------------------------------------------------------------------------
# get_timeline (Phase 6a v3-aware tool)
# ---------------------------------------------------------------------------


class ClipOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_path: str
    source_start_s: float
    source_end_s: float
    reason: str = ""


class TimelineResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clips: list[ClipOut]
    total_duration_s: float
    is_source_monotonic: bool


# ---------------------------------------------------------------------------
# apply_cuts / restore_ranges
# ---------------------------------------------------------------------------


class CutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_s: float
    end_s: float
    reason: str | None = None


class ApplyCutsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    json_path: str
    cuts: list[CutRequest]


class ApplyCutsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applied_count: int
    skipped_count: int
    ranges_after: list[RangeOut]
    total_kept_s: float
    total_cut_s: float


class RestoreRequestItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_s: float
    end_s: float


class RestoreRangesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    json_path: str
    ranges: list[RestoreRequestItem]


class RestoreResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applied_count: int
    skipped_count: int
    ranges_after: list[RangeOut]
    total_kept_s: float
    total_cut_s: float


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------


class RenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    json_path: str = Field(description="Absolute path to a .transcribe.json file.")
    output_path: str = Field(
        description=(
            "Absolute path where the rendered media file is written. "
            "The container is inferred from the extension; common choices: "
            ".mp4, .mov, .mkv, .m4a."
        )
    )
    pad_lead: float | None = Field(
        default=None,
        description=(
            "Seconds of source audio to keep before each kept range (default "
            "Settings.default_pad_lead, typically 0.10). Widening the kept "
            "range trims less aggressively at cuts."
        ),
    )
    pad_trail: float | None = Field(
        default=None,
        description=(
            "Seconds of source audio to keep after each kept range (default "
            "Settings.default_pad_trail, typically 0.10)."
        ),
    )
    audio_fade_ms: int | None = Field(
        default=None,
        description=(
            "Linear afade in milliseconds applied at every internal segment "
            "join (default Settings.default_audio_fade_ms, typically 30). "
            "Set 0 to disable. Values >50 are audible as a dissolve."
        ),
    )


class RenderResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_path: str
    duration_s: float
    file_size_bytes: int
    render_time_s: float


# ---------------------------------------------------------------------------
# Phase 6b-2 — Proposal lifecycle
#
# The MCP-friendly shape of a move is intentionally flatter than
# :class:`core.editing.MoveClipSpan`: every nested ClipAnchor is a
# {source_path, source_start_s, source_end_s} object, dataclass-y but
# Pydantic-validated. Translation between the wire shape and the
# internal MoveClipSpan happens in mcp_server/tools/proposals.py so
# the dataclass surface stays unaware of Pydantic.
# ---------------------------------------------------------------------------


class ClipAnchorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_path: str = Field(
        description=(
            "Absolute path of the source media this clip slices from. Must "
            "match an entry in doc.sources by exact path-string equality."
        )
    )
    source_start_s: float = Field(
        description=(
            "Start of the clip's source-time interval in seconds. Anchors "
            "compare with exact float equality, so use the values returned "
            "by get_timeline rather than re-computing them."
        )
    )
    source_end_s: float = Field(
        description="End of the clip's source-time interval in seconds."
    )


class MoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    move_id: str | None = Field(
        default=None,
        description=(
            "Optional caller-supplied id for this move. If null, the "
            "proposal writer auto-assigns a sequential id (m000, m001, …). "
            "Free-form strings are accepted for descriptive ids "
            "('swap-take-2-take-3')."
        ),
    )
    span: list[ClipAnchorRequest] = Field(
        description=(
            "The contiguous run of clips to relocate, in playlist order. "
            "Each anchor must match a clip currently in the document's "
            "main_timeline; the matched indices must be consecutive."
        ),
        min_length=1,
    )
    target: ClipAnchorRequest | None = Field(
        default=None,
        description=(
            "Anchor of the clip the span should land BEFORE. null moves "
            "the span to the end of the playlist. The target must NOT "
            "appear in the span (no self-cycle)."
        ),
    )
    reason: str = Field(
        description=(
            "Free-form rationale. Must pass core.edit_events.is_valid_reason: "
            "starts with one of (manual, filler, silence, rearrange, "
            "highlight, narrative, trim, best-take, undo:) or is at least "
            "8 non-whitespace chars of free-form text."
        )
    )


class ProposeMovesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    json_path: str = Field(description="Absolute path to a .transcribe.json file.")
    moves: list[MoveRequest] = Field(
        description="Ordered list of moves to propose against the document.",
        min_length=1,
    )


class ProposeMovesResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(
        description=(
            "Stable id for this proposal. Timestamp-prefixed "
            "(YYYYMMDDTHHMMSS-<8 hex>) so listings sort chronologically."
        )
    )
    proposal_path: str = Field(
        description="Absolute path of the persisted proposal file on disk."
    )
    parent_document_state_hash: str | None = Field(
        description=(
            "Sha256 over the full Document JSON (including edit_log) at "
            "proposal time. apply_proposal raises STALE_PROPOSAL when "
            "the live doc's content hash has drifted, including from "
            "intra-doc edits. Null when the proposal was authored "
            "without a hash (test path)."
        )
    )
    move_ids: list[str] = Field(
        description="The materialized (post-auto-assignment) move_ids in order."
    )


class ListProposalsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    json_path: str = Field(description="Absolute path to a .transcribe.json file.")


class ProposalSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    parent_document_state_hash: str | None
    move_count: int
    created_at: str
    latest_apply_result_id: str | None = Field(
        description=(
            "The most recent apply-result id for this proposal, or null "
            "if no apply-result has been written. Useful for jumping "
            "straight from a listing to the most recent outcome."
        )
    )


class ListProposalsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposals: list[ProposalSummary]


class ReadProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    json_path: str = Field(description="Absolute path to a .transcribe.json file.")
    proposal_id: str


class ClipAnchorOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_path: str
    source_start_s: float
    source_end_s: float


class MoveOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    move_id: str
    span: list[ClipAnchorOut]
    target: ClipAnchorOut | None
    reason: str


class ReadProposalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    parent_document_state_hash: str | None
    created_at: str
    moves: list[MoveOut]


class ApplyProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    json_path: str = Field(description="Absolute path to a .transcribe.json file.")
    proposal_id: str
    move_ids: list[str] | None = Field(
        default=None,
        description=(
            "Subset of moves to apply. null applies every move (the "
            "default). An empty list applies nothing — every outcome is "
            "marked skipped, and the document is rewritten unchanged "
            "(but the apply-result file still records the run). The "
            "filter is order-irrelevant: moves apply in proposal order, "
            "not the order ids appear here."
        ),
    )


class MoveOutcomeOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    move_id: str
    index: int
    applied: bool
    skipped: bool
    error: str | None
    error_code: str | None
    post_state_hash: str | None
    human_rejection_reason: str | None


class ApplyProposalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apply_result_id: str
    apply_result_path: str
    proposal_id: str
    document_pre_hash: str
    document_post_hash: str
    applied_count: int
    skipped_count: int
    failed_count: int
    outcomes: list[MoveOutcomeOut]


class ListApplyResultsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    json_path: str = Field(description="Absolute path to a .transcribe.json file.")
    proposal_id: str | None = Field(
        default=None,
        description=(
            "Filter to apply-results for this specific proposal. null "
            "returns every apply-result for the document."
        ),
    )


class ApplyResultSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apply_result_id: str
    proposal_id: str
    created_at: str
    applied_count: int
    skipped_count: int
    failed_count: int


class ListApplyResultsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apply_results: list[ApplyResultSummary]


class ReadApplyResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    json_path: str = Field(description="Absolute path to a .transcribe.json file.")
    apply_result_id: str


class ReadApplyResultResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apply_result_id: str
    proposal_id: str
    created_at: str
    document_pre_hash: str
    document_post_hash: str
    move_ids_filter: list[str] | None
    outcomes: list[MoveOutcomeOut]


# ---------------------------------------------------------------------------
# Phase 6c-2: highlight lifecycle
# ---------------------------------------------------------------------------


class SubSpanSpec(BaseModel):
    """One ordered fragment in a multi-fragment highlight.

    Wire shape mirrors :class:`core.highlight.SubSpan`. ``source_path``
    names a camera (or the only source for a single-camera highlight);
    the ``[source_start_s, source_end_s)`` interval is in *that
    camera's* time, not audio-master time. The renderer applies any
    relevant sync-group offset internally.
    """

    model_config = ConfigDict(extra="forbid")

    source_path: str = Field(
        description=(
            "Absolute path of the camera (or single-source) media. "
            "When a sync_group_id is set on the parent highlight, this "
            "must be one of the cameras registered in that group."
        )
    )
    source_start_s: float = Field(
        description=(
            "Start of the fragment in *camera time* (seconds). "
            "Source-time, not audio-master-time — the renderer "
            "translates via the sync group's offset when needed."
        )
    )
    source_end_s: float = Field(
        description=(
            "End of the fragment in camera time (seconds). Strictly "
            "greater than source_start_s."
        )
    )
    reason: str = Field(
        default="",
        description=(
            "Optional fragment-level rationale (e.g., 'wide for laughter "
            "beat'). Distinct from the highlight's overall reason. "
            "Free-form; not validated by is_valid_reason because empty "
            "is a valid 'no comment'."
        ),
    )


class HighlightSpec(BaseModel):
    """One highlight to author against a parent document.

    Phase 7 widens the wire shape: a highlight is now a tuple of
    fragments (``sub_spans``), each naming its own source path and
    interval. Optional ``sync_group_id`` ties the highlight to a
    :class:`core.sync.SyncGroup` whose audio master overrides the
    cameras' audio at render time.

    Backward compatibility: the legacy single-span fields
    (``source_path`` / ``source_start_s`` / ``source_end_s``) are
    still accepted. When provided alongside an empty/missing
    ``sub_spans``, the propose tool packs them into a one-fragment
    sub_span list. Mixing both is rejected to keep the contract
    crisp.
    """

    model_config = ConfigDict(extra="forbid")

    sub_spans: list[SubSpanSpec] = Field(
        default_factory=list,
        description=(
            "Ordered fragments to concatenate. Each fragment names a "
            "source_path (camera) and a [source_start_s, source_end_s) "
            "interval in camera time. A single-camera highlight has one "
            "fragment; a multi-cam highlight has the playlist of camera "
            "+ interval picks. Either set this or the legacy single-span "
            "fields below, not both."
        ),
    )
    sync_group_id: str | None = Field(
        default=None,
        description=(
            "Id of the sync group whose audio master should drive this "
            "highlight's audio. null means 'use camera audio' (the "
            "single-camera default). When set, every sub_span's "
            "source_path must be a camera registered in the named group; "
            "INVALID_HIGHLIGHT fires on a mismatch."
        ),
    )
    source_path: str | None = Field(
        default=None,
        description=(
            "DEPRECATED single-span shortcut. Legacy callers may pass "
            "source_path / source_start_s / source_end_s instead of "
            "sub_spans for a one-fragment highlight; the propose tool "
            "translates internally. Mixing this with sub_spans is "
            "rejected."
        ),
    )
    source_start_s: float | None = Field(
        default=None,
        description="DEPRECATED — see source_path.",
    )
    source_end_s: float | None = Field(
        default=None,
        description="DEPRECATED — see source_path.",
    )
    reason: str = Field(
        description=(
            "Free-form rationale. Must pass core.edit_events.is_valid_reason: "
            "starts with one of (manual, filler, silence, rearrange, "
            "highlight, narrative, trim, best-take, undo:) or is at least "
            "8 non-whitespace chars of free-form text."
        )
    )
    reframe_mode: str = Field(
        default="speaker_locked",
        description=(
            "How to crop the source frame onto the 9:16 output. "
            "'speaker_locked' (default) face-locks once per camera at the "
            "first fragment's midpoint; 'center' takes a static centered "
            "crop. Anything else raises INVALID_HIGHLIGHT. Note: the "
            "model can't see camera frames, so its angle picks should be "
            "driven by structural cues (alternation, pacing) — face "
            "detection happens at render time and isn't a model concern."
        ),
    )
    captions_enabled: bool = Field(
        default=False,
        description=(
            "When True, burns SRT captions derived from the parent doc's "
            "word timestamps. For sync-group highlights the captions "
            "come from the audio master's transcript (the parent doc by "
            "convention). Default False."
        ),
    )


class SubSpanOut(BaseModel):
    """Wire shape for one fragment of a stored highlight."""

    model_config = ConfigDict(extra="forbid")

    source_path: str
    source_start_s: float
    source_end_s: float
    reason: str = ""


class HighlightOut(BaseModel):
    """Wire shape for a stored highlight (returned by list/read)."""

    model_config = ConfigDict(extra="forbid")

    highlight_id: str
    created_at: str
    sub_spans: list[SubSpanOut] = Field(
        description=(
            "Ordered fragments. A single-camera highlight has one entry; "
            "a multi-cam highlight has the playlist."
        )
    )
    sync_group_id: str | None = Field(
        description=(
            "Id of the sync group driving audio for this highlight, or "
            "null if camera audio is used directly."
        )
    )
    reason: str
    reframe_mode: str
    captions_enabled: bool
    rendered_output_path: str | None = Field(
        description=(
            "Absolute path to the rendered <id>.highlight.mp4, or null "
            "if the highlight has not yet been rendered."
        )
    )
    parent_source_hashes: dict[str, str] = Field(
        description=(
            "core.cache.cache_key value per unique source path at "
            "authoring time. Compared against the live cache_key at "
            "apply time; mismatch raises STALE_HIGHLIGHT. Tracks source "
            "FILES only — intra-doc edits don't invalidate, file "
            "replacement does."
        )
    )


class ProposeHighlightsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    json_path: str = Field(description="Absolute path to a .transcribe.json file.")
    highlights: list[HighlightSpec] = Field(
        description=(
            "Ordered list of highlight specs to author. Validation is "
            "all-or-nothing: a single bad spec raises INVALID_HIGHLIGHT "
            "(with the offending index) and no highlights are persisted."
        ),
        min_length=1,
    )


class ProposeHighlightsResultEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    highlight_id: str
    json_path: str = Field(
        description="Absolute path of the persisted <id>.highlight.json."
    )


class ProposeHighlightsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    highlights: list[ProposeHighlightsResultEntry]


class ListHighlightsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    json_path: str = Field(description="Absolute path to a .transcribe.json file.")


class HighlightSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    highlight_id: str
    sub_spans: list[SubSpanOut] = Field(
        description=(
            "Ordered fragments — each carrying source_path, "
            "source_start_s, source_end_s, and an optional reason. "
            "A single-camera highlight has one entry."
        )
    )
    sync_group_id: str | None = Field(
        description=(
            "Id of the sync group driving audio, or null when the "
            "highlight uses camera audio directly."
        )
    )
    reframe_mode: str
    captions_enabled: bool
    reason: str
    rendered_output_path: str | None


class ListHighlightsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    highlights: list[HighlightSummary]


class ReadHighlightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    json_path: str = Field(description="Absolute path to a .transcribe.json file.")
    highlight_id: str


class ApplyHighlightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    json_path: str = Field(description="Absolute path to a .transcribe.json file.")
    highlight_id: str


class ApplyHighlightResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    render_result_id: str = Field(
        description=(
            "Stable id for the render-result sidecar this run wrote "
            "(timestamp-prefixed). Distinct from highlight_id — re-running "
            "apply_highlight on the same highlight emits a fresh "
            "render-result file each call."
        )
    )
    output_path: str = Field(
        description=(
            "Absolute path to the rendered <id>.highlight.mp4. Always "
            "the same path for a given highlight_id; re-renders overwrite "
            "this file in place."
        )
    )


class CropBoxOut(BaseModel):
    """The 9:16 crop window applied before scale=1080:1920."""

    model_config = ConfigDict(extra="forbid")

    x: int
    y: int
    w: int
    h: int


class RenderResultSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    render_result_id: str
    highlight_id: str
    created_at: str
    output_path: str
    face_detection_used: str = Field(
        description=(
            "Aggregate face-detect outcome across all unique sources "
            "in the highlight. 'speaker_locked' if every camera locked, "
            "'speaker_locked_fallback_to_center' if at least one fell "
            "back, or 'center' (caller asked for center)."
        )
    )
    sync_group_id: str | None = Field(
        description=(
            "Id of the sync group used at render time, or null for "
            "single-camera highlights."
        )
    )
    wall_clock_s: float


class ListHighlightRendersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    json_path: str = Field(description="Absolute path to a .transcribe.json file.")
    highlight_id: str | None = Field(
        default=None,
        description=(
            "Filter to render-results for one highlight. null returns "
            "every render-result for the document."
        ),
    )


class ListHighlightRendersResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    render_results: list[RenderResultSummary]


class ReadHighlightRenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    json_path: str = Field(description="Absolute path to a .transcribe.json file.")
    render_result_id: str


class ReadHighlightRenderResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    render_result_id: str
    highlight_id: str
    created_at: str
    output_path: str
    parent_source_hashes: dict[str, str] = Field(
        description=(
            "core.cache.cache_key matched at render time, keyed by "
            "source path string. Single-source renders carry one entry."
        )
    )
    face_detection_used: str
    crop_box: CropBoxOut = Field(
        description=(
            "The crop window applied to the *first unique source*. Kept "
            "for single-camera compat; multi-cam renders also populate "
            "crop_boxes_by_source below."
        )
    )
    crop_boxes_by_source: dict[str, CropBoxOut] = Field(
        description=(
            "Per-unique-source crop windows. Single-camera renders have "
            "one entry; multi-cam renders have one per camera."
        )
    )
    sync_group_id: str | None
    wall_clock_s: float


# ---------------------------------------------------------------------------
# Phase 7 — sync group lifecycle
# ---------------------------------------------------------------------------


class SyncSourceOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_path: str
    source_hash: str
    offset_s: float = Field(
        description=(
            "Seconds to add to camera time to land on audio-master time. "
            "Convention: master_time = camera_time + offset_s."
        )
    )
    manual_override: bool
    confidence: float | None = Field(
        description=(
            "Cross-correlation peak-to-noise ratio. >5 is reliable; "
            "2.5–5 is marginal; null for manually-set offsets."
        )
    )


class SyncGroupOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sync_group_id: str
    description: str
    audio_master_path: str
    audio_master_hash: str
    cameras: list[SyncSourceOut]
    created_at: str
    estimated_at: str | None


class CreateSyncGroupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    json_path: str = Field(description="Absolute path to a .transcribe.json file.")
    audio_master_path: str = Field(
        description=(
            "Absolute path to the audio master file (the file whose "
            "audio drives every sync-group highlight render)."
        )
    )
    camera_paths: list[str] = Field(
        description=(
            "Absolute paths of camera media files. Each camera's audio "
            "is cross-correlated against the master to estimate its "
            "offset; offsets land in the persisted sync group."
        ),
        min_length=1,
    )
    description: str = Field(
        default="",
        description="Optional human-readable label for the group.",
    )
    max_lag_s: float = Field(
        default=30.0,
        description=(
            "Maximum offset to search, in seconds. Raise for shoots "
            "where cameras started minutes apart."
        ),
    )
    search_window_s: float = Field(
        default=60.0,
        description=(
            "How much of each track to use for correlation. 60 s is "
            "enough for conversational audio; raise for sparse content."
        ),
    )


class CreateSyncGroupResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sync_group_id: str
    sync_group_path: str
    cameras: list[SyncSourceOut]
    low_confidence_cameras: list[str] = Field(
        description=(
            "Source paths whose offset estimation produced a low-confidence "
            "result (peak-to-noise < CONFIDENCE_GOOD). The offset is still "
            "recorded; the operator may want to set a manual override."
        )
    )


class ListSyncGroupsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    json_path: str = Field(description="Absolute path to a .transcribe.json file.")


class ListSyncGroupsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sync_groups: list[SyncGroupOut]


class ReadSyncGroupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    json_path: str = Field(description="Absolute path to a .transcribe.json file.")
    sync_group_id: str


class SetSyncOffsetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    json_path: str = Field(description="Absolute path to a .transcribe.json file.")
    sync_group_id: str
    camera_path: str = Field(
        description=(
            "Absolute path of the camera whose offset should be "
            "overridden. Must match a registered camera in the named "
            "group; otherwise INVALID_SYNC_GROUP fires."
        )
    )
    offset_s: float = Field(
        description=(
            "Manual offset in seconds. Replaces any previous estimate; "
            "the camera's record is marked manual_override=true and the "
            "confidence is cleared."
        )
    )


class SetSyncOffsetResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sync_group_id: str
    camera: SyncSourceOut
