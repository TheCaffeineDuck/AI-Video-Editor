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
# §4.9 (model "base", auto language, auto device + precision).
DEFAULT_MODEL = "base"
DEFAULT_OUTPUT_FORMATS = ("txt", "srt")
DEFAULT_LANGUAGE: str | None = None
DEFAULT_DEVICE = "auto"
DEFAULT_COMPUTE_TYPE = "int8"  # spec note: int8 on this CPU-only platform


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
