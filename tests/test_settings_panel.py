"""Construction + save-flow tests for the Settings panel modal."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.settings import Settings


@pytest.fixture
def isolated_settings_dir(monkeypatch, tmp_path):
    """Redirect settings.json writes into a tmp_path so tests don't touch the real config."""
    monkeypatch.setenv("WHISPER_SETTINGS_DIR", str(tmp_path))
    return tmp_path


def test_settings_panel_constructs(tk_root, isolated_settings_dir):
    from ui.components.settings_panel import SettingsPanel

    panel = SettingsPanel(tk_root, current=Settings(), on_save=lambda _s: None)
    try:
        assert panel.winfo_exists()
        assert panel._model_var.get() == "base"
    finally:
        panel.destroy()


def test_settings_panel_save_invokes_callback_and_writes_file(
    tk_root, isolated_settings_dir, tmp_path
):
    from ui.components.settings_panel import SettingsPanel

    saved: list[Settings] = []
    panel = SettingsPanel(
        tk_root, current=Settings(default_model="tiny"), on_save=saved.append
    )
    panel._model_var.set("small")
    panel._device_var.set("cpu")
    panel._compute_var.set("float32")
    panel._output_dir_var.set(str(tmp_path / "out"))
    panel._save()

    assert len(saved) == 1
    new = saved[0]
    assert new.default_model == "small"
    assert new.compute_device == "cpu"
    assert new.compute_type == "float32"
    assert new.output_dir == str(tmp_path / "out")

    # File on disk should match.
    written = isolated_settings_dir / "settings.json"
    assert written.is_file()


def test_open_settings_helper_returns_panel(tk_root, isolated_settings_dir):
    from ui.components.settings_panel import open_settings

    panel = open_settings(tk_root, current=Settings(), on_save=lambda _s: None)
    try:
        assert panel.winfo_exists()
    finally:
        panel.destroy()


def test_clear_cache_handles_missing_dir(tk_root, isolated_settings_dir, monkeypatch, tmp_path):
    """Clear-cache button should not raise when the cache dir doesn't exist."""
    from ui.components import settings_panel as sp_mod
    from ui.components.settings_panel import SettingsPanel

    missing = tmp_path / "no_cache_here"
    monkeypatch.setattr(sp_mod, "cache_root", lambda: missing)

    panel = SettingsPanel(tk_root, current=Settings(), on_save=lambda _s: None)
    try:
        panel._clear_cache()  # should be a no-op, not raise
        assert not missing.exists()
    finally:
        panel.destroy()


def test_app_apply_settings_updates_widgets(tk_root, isolated_settings_dir):
    from ui.app import App

    app = App(root=tk_root, settings=Settings())
    new = Settings(
        default_model="small",
        default_language="es",
        output_formats=["txt", "vtt"],
    )
    app._apply_settings(new)
    assert app.settings == new
    assert app.model_picker.value == "small"
    assert app.language_picker.selected_code == "es"
    assert set(app.output_picker.formats) == {"txt", "vtt"}


def test_app_loads_settings_from_disk(tk_root, isolated_settings_dir):
    """When no settings instance is passed, App reads from the per-user file."""
    from core.settings import save_settings
    from ui.app import App

    save_settings(Settings(default_model="tiny", default_language="ja"))
    app = App(root=tk_root)
    assert app.settings.default_model == "tiny"
    assert app.settings.default_language == "ja"


def test_app_error_event_renders_banner(tk_root, isolated_settings_dir):
    from ui.app import App
    from ui.state import AppState, ErrorEvent

    app = App(root=tk_root, settings=Settings())
    sample = Path(__file__).resolve().parent / "fixtures" / "sample.wav"
    app._handle_file_selected(sample)
    app.state.start_transcribing()
    app.event_queue.put(ErrorEvent(message="boom"))
    app.pump_once()
    assert app.state.state == AppState.ERROR
    assert app.state.error_message == "boom"
    # Retry button text shows "Retry" in ERROR state.
    assert "Retry" in app.transcribe_btn.cget("text")
