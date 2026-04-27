"""Persisted user settings (spec 4.9).

Settings live at ``~/Library/Application Support/WhisperTranscriber/settings.json``
on macOS. Missing or corrupt files are treated as "use defaults" — never a
crash. The on-disk format is plain JSON for inspectability.
"""

from __future__ import annotations

import json
import logging
import os
import platform
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

APP_DIRNAME = "WhisperTranscriber"

# Defaults — spec §4.2 (output formats default to txt+srt, vtt off) and
# §4.9 (model "base", auto language, auto device + precision). Phase 4e
# adds "json" to the fresh-install default so new users get an editable
# project artifact alongside the subtitle/text exports. Existing users'
# saved output_formats are preserved verbatim — no auto-migration.
DEFAULT_MODEL = "base"
DEFAULT_OUTPUT_FORMATS = ("txt", "srt", "json")
DEFAULT_LANGUAGE: str | None = None
DEFAULT_DEVICE = "auto"
DEFAULT_COMPUTE_TYPE = "int8"  # spec note: int8 on this CPU-only platform

# Phase 5 editor defaults (surfaced in 5a even though most aren't read until
# 5b/5c/5f). Following the settings non-migration rule (4e): existing users
# keep what they had; new fields appear with these documented defaults when
# absent from disk.
DEFAULT_LAYOUT = "video_top"  # editor pane orientation; "video_top" | "video_left"
LAYOUT_CHOICES = ("video_top", "video_left")
DEFAULT_PAD_LEAD = 0.10
DEFAULT_PAD_TRAIL = 0.10
DEFAULT_AUDIO_FADE_MS = 30
DEFAULT_AUTOSAVE_INTERVAL_S = 0  # 0 = autosave OFF


def settings_dir() -> Path:
    """Return the per-user app-support directory for settings."""
    if os.environ.get("WHISPER_SETTINGS_DIR"):
        return Path(os.environ["WHISPER_SETTINGS_DIR"])
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIRNAME
    if system == "Windows":  # pragma: no cover - mac dev box
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / APP_DIRNAME
    # Linux / other
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / APP_DIRNAME


def settings_file() -> Path:
    return settings_dir() / "settings.json"


@dataclass
class Settings:
    """User-tweakable preferences. All fields have safe defaults."""

    default_model: str = DEFAULT_MODEL
    default_language: str | None = DEFAULT_LANGUAGE
    output_formats: list[str] = field(default_factory=lambda: list(DEFAULT_OUTPUT_FORMATS))
    output_dir: str | None = None  # None = same folder as the source file
    compute_device: str = DEFAULT_DEVICE
    compute_type: str = DEFAULT_COMPUTE_TYPE

    # Phase 5 editor preferences. Read from disk when present; missing keys
    # in older settings.json files fall back to the defaults below.
    layout: str = DEFAULT_LAYOUT
    default_pad_lead: float = DEFAULT_PAD_LEAD
    default_pad_trail: float = DEFAULT_PAD_TRAIL
    default_audio_fade_ms: int = DEFAULT_AUDIO_FADE_MS
    autosave_interval_s: int = DEFAULT_AUTOSAVE_INTERVAL_S

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Settings:
        """Lenient: ignore unknown keys, fall back to defaults for missing ones."""
        defaults = cls()
        kwargs = {}
        for f in cls.__dataclass_fields__:
            if f in data:
                kwargs[f] = data[f]
            else:
                kwargs[f] = getattr(defaults, f)
        # Normalize output_formats: must be a non-empty list of known formats.
        formats = kwargs.get("output_formats") or list(DEFAULT_OUTPUT_FORMATS)
        if not isinstance(formats, list) or not all(isinstance(x, str) for x in formats):
            formats = list(DEFAULT_OUTPUT_FORMATS)
        kwargs["output_formats"] = formats
        # Normalize layout: unknown value falls back to default rather than
        # propagating a typo into the editor's QSplitter orientation.
        if kwargs.get("layout") not in LAYOUT_CHOICES:
            kwargs["layout"] = DEFAULT_LAYOUT
        return cls(**kwargs)


def load_settings(path: Path | None = None) -> Settings:
    """Read settings from disk. Missing file → defaults. Corrupt file → defaults + warning."""
    p = path or settings_file()
    if not p.is_file():
        return Settings()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("settings file at %s is unreadable (%s); using defaults", p, exc)
        return Settings()
    if not isinstance(data, dict):
        log.warning("settings file at %s has wrong shape; using defaults", p)
        return Settings()
    return Settings.from_dict(data)


def save_settings(settings: Settings, path: Path | None = None) -> Path:
    """Write settings to disk, creating parent dirs as needed."""
    p = path or settings_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(settings.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    return p
