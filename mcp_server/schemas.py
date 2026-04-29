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


class HighlightSpec(BaseModel):
    """One highlight to author against a parent document.

    Mirrors :class:`core.highlight.Highlight` minus the bookkeeping
    fields (id, created_at, parent paths/hash, rendered_output_path) —
    those are filled in by ``propose_highlights`` at materialization
    time. Field validation is owed to the core dataclass; this model
    is the wire shape only.
    """

    model_config = ConfigDict(extra="forbid")

    source_path: str = Field(
        description=(
            "Absolute path of the media file the highlight samples from. "
            "Almost always equals the parent doc's primary source path; "
            "stored explicitly so a future multi-source parent can carry "
            "highlights from a specific source."
        )
    )
    source_start_s: float = Field(
        description=(
            "Start of the highlight span in source time (seconds). Must "
            "lie within [0, source_duration]; spec validation runs at "
            "propose time and surfaces as INVALID_HIGHLIGHT with the "
            "offending index in the message."
        )
    )
    source_end_s: float = Field(
        description=(
            "End of the highlight span in source time (seconds). Must be "
            "strictly greater than source_start_s; zero-duration spans "
            "are rejected."
        )
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
            "'speaker_locked' (default) face-locks once at span midpoint; "
            "'center' takes a static centered crop. Anything else raises "
            "INVALID_HIGHLIGHT."
        ),
    )
    captions_enabled: bool = Field(
        default=False,
        description=(
            "When True, burns SRT captions derived from the parent doc's "
            "word timestamps into the rendered output. Default False — "
            "caption styling is fixed per build (Arial 56, white-on-black, "
            "bottom-center, 80px margin); per-highlight overrides are "
            "Phase 7+."
        ),
    )


class HighlightOut(BaseModel):
    """Wire shape for a stored highlight (returned by list/read)."""

    model_config = ConfigDict(extra="forbid")

    highlight_id: str
    created_at: str
    source_path: str
    source_start_s: float
    source_end_s: float
    reason: str
    reframe_mode: str
    captions_enabled: bool
    rendered_output_path: str | None = Field(
        description=(
            "Absolute path to the rendered <id>.highlight.mp4, or null "
            "if the highlight has not yet been rendered. Set by "
            "apply_highlight (and by the GUI Render button). The file's "
            "presence at the path is not re-checked at read time — "
            "callers that need a fresh existence check should stat it."
        )
    )
    parent_source_hash: str = Field(
        description=(
            "core.cache.cache_key value of the source media at authoring "
            "time (sha256 over absolute_path + mtime + size). Compared "
            "against the live cache_key at apply time; mismatch raises "
            "STALE_HIGHLIGHT. Tracks the source FILE only — intra-doc "
            "edits don't invalidate, file replacement does."
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
    source_start_s: float
    source_end_s: float
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
            "One of 'speaker_locked' (face detected and used), "
            "'speaker_locked_fallback_to_center' (face detect failed; "
            "renderer fell back), or 'center' (caller asked for center)."
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
    parent_source_hash: str
    face_detection_used: str
    crop_box: CropBoxOut
    wall_clock_s: float
