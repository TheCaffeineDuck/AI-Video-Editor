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
    schema_version: int
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
