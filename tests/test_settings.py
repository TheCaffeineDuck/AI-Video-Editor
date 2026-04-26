"""Tests for core.settings — JSON persistence and defaults handling."""

from __future__ import annotations

import logging
from pathlib import Path

from core import settings as settings_mod
from core.settings import (
    DEFAULT_DEVICE,
    DEFAULT_MODEL,
    DEFAULT_OUTPUT_FORMATS,
    Settings,
    load_settings,
    save_settings,
)


def test_defaults_match_spec():
    s = Settings()
    assert s.default_model == DEFAULT_MODEL == "base"
    # Phase 4e: fresh installs include json (the editable-project artifact)
    # alongside the original txt+srt defaults.
    assert tuple(s.output_formats) == DEFAULT_OUTPUT_FORMATS == ("txt", "srt", "json")
    assert s.default_language is None  # auto-detect
    assert s.compute_device == DEFAULT_DEVICE == "auto"
    assert s.output_dir is None


def test_roundtrip_write_read_equal(tmp_path: Path):
    p = tmp_path / "settings.json"
    s = Settings(
        default_model="small",
        default_language="es",
        output_formats=["txt", "vtt"],
        output_dir=str(tmp_path / "outputs"),
        compute_device="cpu",
        compute_type="float32",
    )
    save_settings(s, path=p)
    loaded = load_settings(path=p)
    assert loaded == s


def test_save_creates_parent_dirs(tmp_path: Path):
    p = tmp_path / "deep" / "nested" / "settings.json"
    save_settings(Settings(), path=p)
    assert p.is_file()


def test_missing_file_returns_defaults(tmp_path: Path):
    p = tmp_path / "absent.json"
    s = load_settings(path=p)
    assert s == Settings()


def test_corrupt_file_returns_defaults_and_warns(tmp_path: Path, caplog):
    p = tmp_path / "settings.json"
    p.write_text("{this is not valid json")
    with caplog.at_level(logging.WARNING):
        s = load_settings(path=p)
    assert s == Settings()
    assert any("unreadable" in m for m in caplog.text.splitlines())


def test_wrong_shape_returns_defaults(tmp_path: Path, caplog):
    p = tmp_path / "settings.json"
    p.write_text('["not", "an", "object"]')
    with caplog.at_level(logging.WARNING):
        s = load_settings(path=p)
    assert s == Settings()


def test_unknown_keys_are_ignored(tmp_path: Path):
    p = tmp_path / "settings.json"
    p.write_text(
        '{"default_model": "tiny", "nonsense_field": 42, "extra": "ignored"}'
    )
    s = load_settings(path=p)
    assert s.default_model == "tiny"
    # Missing keys fall back to defaults.
    assert tuple(s.output_formats) == DEFAULT_OUTPUT_FORMATS


def test_invalid_output_formats_falls_back_to_defaults(tmp_path: Path):
    p = tmp_path / "settings.json"
    p.write_text('{"output_formats": "not a list"}')
    s = load_settings(path=p)
    assert tuple(s.output_formats) == DEFAULT_OUTPUT_FORMATS


def test_settings_dir_respects_env_override(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("WHISPER_SETTINGS_DIR", str(tmp_path / "x"))
    assert settings_mod.settings_dir() == tmp_path / "x"
    assert settings_mod.settings_file() == tmp_path / "x" / "settings.json"


def test_settings_dir_default_is_app_support_on_darwin(monkeypatch):
    monkeypatch.delenv("WHISPER_SETTINGS_DIR", raising=False)
    monkeypatch.setattr(settings_mod.platform, "system", lambda: "Darwin")
    expected = Path.home() / "Library" / "Application Support" / "WhisperTranscriber"
    assert settings_mod.settings_dir() == expected


# ---------------------------------------------------------------------------
# Phase 4e: backward-compatible settings load (no auto-migration to add json)
# ---------------------------------------------------------------------------


def test_existing_user_settings_without_json_are_preserved(tmp_path: Path):
    """A pre-Phase-4e settings.json saying ['txt', 'srt'] must NOT auto-migrate.

    Existing users keep what they had. The new default only applies to
    fresh installs (no settings.json on disk).
    """
    p = tmp_path / "settings.json"
    p.write_text('{"output_formats": ["txt", "srt"]}')
    s = load_settings(path=p)
    assert s.output_formats == ["txt", "srt"]


def test_fresh_install_default_includes_json(tmp_path: Path):
    """No settings.json on disk → fresh-install default has json on."""
    p = tmp_path / "absent.json"
    s = load_settings(path=p)
    assert "json" in s.output_formats
    assert tuple(s.output_formats) == ("txt", "srt", "json")


def test_existing_user_explicit_only_vtt_is_preserved(tmp_path: Path):
    """Make sure backward-compat is general, not just for the txt+srt case."""
    p = tmp_path / "settings.json"
    p.write_text('{"output_formats": ["vtt"]}')
    s = load_settings(path=p)
    assert s.output_formats == ["vtt"]
