"""Phase 5a — pytest-qt tests for the PySide6 UI scaffold.

Construction smoke tests for each component, plus a few wiring tests
on the main window: start state, drop-acceptance, button-enable flow,
DoneEvent → COMPLETE through the queue pump. The full transcribe-and-
inference flow is intentionally NOT exercised here; that needs real
audio + faster-whisper on the path, which already has slow-marker
coverage on the customtkinter side and would double the wall-clock
cost without testing anything new on the Qt path.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

# Headless guard — same shape as conftest's tk_root fixture. CI without
# a display should skip these cleanly rather than aborting on QApplication.
if os.environ.get("WHISPER_NO_QT"):  # pragma: no cover - opt-in headless
    pytest.skip("WHISPER_NO_QT set — skipping Qt UI tests", allow_module_level=True)

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QDropEvent  # noqa: E402

from ui.state import AppState, DoneEvent, ProgressEvent  # noqa: E402
from ui_qt.app import MainWindow  # noqa: E402
from ui_qt.components.drop_zone import DropZone  # noqa: E402
from ui_qt.components.language_picker import LanguagePicker  # noqa: E402
from ui_qt.components.model_picker import ModelPicker  # noqa: E402
from ui_qt.components.output_formats import OutputFormatPicker  # noqa: E402
from ui_qt.components.progress_card import ProgressCard  # noqa: E402
from ui_qt.components.result_card import ResultCard  # noqa: E402
from ui_qt.components.settings_panel import SettingsDialog  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE = REPO_ROOT / "tests" / "fixtures" / "sample.wav"


# ---------------------------------------------------------------------------
# Component construction smoke tests
# ---------------------------------------------------------------------------


def test_drop_zone_constructs(qtbot):
    dz = DropZone()
    qtbot.addWidget(dz)
    assert dz.objectName() == "DropZone"


def test_drop_zone_accept_valid_emits_file_selected(qtbot):
    dz = DropZone()
    qtbot.addWidget(dz)
    with qtbot.waitSignal(dz.file_selected, timeout=1000) as blocker:
        dz._accept(SAMPLE)
    assert blocker.args[0] == SAMPLE


def test_drop_zone_accept_invalid_emits_invalid_file(qtbot, tmp_path):
    dz = DropZone()
    qtbot.addWidget(dz)
    bogus = tmp_path / "foo.txt"
    bogus.write_text("x")
    with qtbot.waitSignal(dz.invalid_file, timeout=1000) as blocker:
        dz._accept(bogus)
    assert "Unsupported" in blocker.args[0]


def test_drop_zone_drop_event_emits_signal(qtbot):
    """Construct a real QDropEvent carrying a file URL — the event-handler
    path must reach ``file_selected`` just like the click-to-browse path."""
    dz = DropZone()
    qtbot.addWidget(dz)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(SAMPLE))])
    event = QDropEvent(
        QPointF(10.0, 10.0),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    with qtbot.waitSignal(dz.file_selected, timeout=1000) as blocker:
        dz.dropEvent(event)
    assert blocker.args[0] == SAMPLE


def test_drop_zone_show_loaded_updates_labels(qtbot):
    dz = DropZone()
    qtbot.addWidget(dz)
    dz.show_loaded(name="lecture.mp4", duration_seconds=125.5, size_bytes=2 * 1024 * 1024)
    assert dz._title.text() == "lecture.mp4"


def test_model_picker_constructs(qtbot):
    mp = ModelPicker()
    qtbot.addWidget(mp)
    assert mp.value == "base"


def test_model_picker_initial_arg_respected(qtbot):
    mp = ModelPicker(initial="tiny")
    qtbot.addWidget(mp)
    assert mp.value == "tiny"


def test_model_picker_unknown_initial_raises(qtbot):
    with pytest.raises(ValueError):
        ModelPicker(initial="huge")


def test_model_picker_set_value_changes_selection(qtbot):
    mp = ModelPicker()
    qtbot.addWidget(mp)
    mp.set_value("small")
    assert mp.value == "small"


def test_progress_card_constructs(qtbot):
    pc = ProgressCard()
    qtbot.addWidget(pc)
    pc.reset()
    assert pc._bar.value() == 0


def test_progress_card_set_progress_clamps(qtbot):
    pc = ProgressCard()
    qtbot.addWidget(pc)
    pc.reset()
    pc.set_progress(2.5)
    assert pc._bar.value() == 1000
    pc.set_progress(-1.0)
    assert pc._bar.value() == 0


def test_progress_card_append_stream_caps_lines(qtbot):
    pc = ProgressCard()
    qtbot.addWidget(pc)
    pc.reset()
    for i in range(10):
        pc.append_stream(f"line {i}")
    lines = [line for line in pc._stream.toPlainText().splitlines() if line]
    assert len(lines) == 3
    assert lines[-1] == "line 9"


def test_progress_card_cancel_emits_signal(qtbot):
    pc = ProgressCard()
    qtbot.addWidget(pc)
    with qtbot.waitSignal(pc.cancel_requested, timeout=1000):
        pc._handle_cancel()
    assert pc._cancel_btn.text() == "Cancelling…"


def test_result_card_constructs_and_shows_result(qtbot, tmp_path):
    rc = ResultCard()
    qtbot.addWidget(rc)
    output = tmp_path / "clip.txt"
    output.write_text("hello world")
    rc.show_result(
        transcript="hello world",
        output_files={"txt": output},
        language="en",
        elapsed_seconds=1.2,
    )
    assert rc._textbox.toPlainText() == "hello world"


def test_result_card_new_button_emits_signal(qtbot):
    rc = ResultCard()
    qtbot.addWidget(rc)
    with qtbot.waitSignal(rc.new_transcription, timeout=1000):
        rc._new_btn.click()


def test_output_format_picker_initial_selection(qtbot):
    op = OutputFormatPicker(initial=("txt", "srt"))
    qtbot.addWidget(op)
    assert sorted(op.formats) == ["srt", "txt"]
    assert op.has_selection


def test_output_format_picker_set_formats_changes_state(qtbot):
    op = OutputFormatPicker(initial=("txt",))
    qtbot.addWidget(op)
    op.set_formats(["json", "vtt"])
    assert sorted(op.formats) == ["json", "vtt"]


def test_output_format_picker_no_selection_reports_false(qtbot):
    op = OutputFormatPicker(initial=())
    qtbot.addWidget(op)
    assert op.has_selection is False


def test_language_picker_initial_code(qtbot):
    lp = LanguagePicker(initial_code="es")
    qtbot.addWidget(lp)
    assert lp.selected_code == "es"


def test_language_picker_auto_detect(qtbot):
    lp = LanguagePicker(initial_code=None)
    qtbot.addWidget(lp)
    assert lp.selected_code is None


# ---------------------------------------------------------------------------
# Settings panel — shared on-disk format with the tkinter app
# ---------------------------------------------------------------------------


def test_settings_dialog_save_writes_shared_file(qtbot, tmp_path, monkeypatch):
    """Saving from the Qt settings dialog must produce a settings.json the
    tkinter app's loader can read back as an equal Settings object."""
    from core import settings as settings_mod
    from core.settings import Settings, load_settings

    monkeypatch.setenv("WHISPER_SETTINGS_DIR", str(tmp_path))
    # Sanity: env override is honored.
    assert settings_mod.settings_dir() == tmp_path

    current = Settings(default_model="tiny", compute_device="cpu")
    dlg = SettingsDialog(current=current)
    qtbot.addWidget(dlg)

    captured: list[Settings] = []
    dlg.settings_saved.connect(captured.append)
    dlg._save()

    assert len(captured) == 1
    saved_path = tmp_path / "settings.json"
    assert saved_path.is_file()
    reloaded = load_settings(path=saved_path)
    assert reloaded.default_model == "tiny"
    assert reloaded.compute_device == "cpu"


# ---------------------------------------------------------------------------
# MainWindow — wiring & queue pump
# ---------------------------------------------------------------------------


@pytest.fixture
def main_window(qtbot, tmp_path, monkeypatch):
    """Build a MainWindow with settings isolated to tmp_path."""
    monkeypatch.setenv("WHISPER_SETTINGS_DIR", str(tmp_path))
    win = MainWindow()
    qtbot.addWidget(win)
    win._pump_timer.stop()  # drive pump_once manually in tests
    yield win


def test_main_window_starts_in_idle(main_window):
    assert main_window.state.state == AppState.IDLE
    # Phase 5b: starts on the transcribe pane, not the editor.
    assert main_window.transcribe_pane is not None
    assert main_window.editor_pane is None


def test_main_window_transcribe_disabled_until_file_loaded(main_window):
    pane = main_window.transcribe_pane
    assert pane is not None
    assert pane.transcribe_btn.isEnabled() is False
    main_window._handle_file_selected(SAMPLE)
    assert main_window.state.state == AppState.FILE_LOADED
    assert pane.transcribe_btn.isEnabled() is True


def test_main_window_invalid_file_sets_transient_error(main_window, tmp_path):
    bad = tmp_path / "foo.txt"
    bad.write_text("x")
    main_window._handle_file_selected(bad)
    assert main_window.state.error_message is not None
    assert main_window.state.state == AppState.IDLE


def test_main_window_pump_drives_done_event_to_complete(main_window, tmp_path):
    """No document attached → COMPLETE, transcribe pane shows the result card."""
    main_window._handle_file_selected(SAMPLE)
    main_window.state.start_transcribing()
    main_window.event_queue.put(
        DoneEvent(
            segments=[SimpleNamespace(text="hello world", start=0.0, end=1.0)],
            info=SimpleNamespace(language="en"),
            output_files={"txt": tmp_path / "x.txt"},
            elapsed=0.4,
            document=None,
        )
    )
    main_window.pump_once()
    assert main_window.state.state == AppState.COMPLETE
    pane = main_window.transcribe_pane
    assert pane is not None
    assert pane.result_card._textbox.toPlainText() == "hello world"
    # Without a document, no editor swap.
    assert main_window.editor_pane is None


def test_main_window_progress_event_updates_bar(main_window):
    main_window._handle_file_selected(SAMPLE)
    main_window.state.start_transcribing()
    main_window.event_queue.put(ProgressEvent(fraction=0.5))
    main_window.pump_once()
    assert main_window.state.progress == 0.5
    pane = main_window.transcribe_pane
    assert pane is not None
    assert pane.progress_card._bar.value() == 500


def test_main_window_new_transcription_resets(main_window, tmp_path):
    main_window._handle_file_selected(SAMPLE)
    main_window.state.start_transcribing()
    main_window.state.apply_event(
        DoneEvent(
            segments=[SimpleNamespace(text="x", start=0, end=1)],
            info=SimpleNamespace(language="en"),
            output_files={"txt": tmp_path / "x.txt"},
            elapsed=0.1,
            document=None,
        )
    )
    main_window._handle_new_transcription()
    assert main_window.state.state == AppState.IDLE
    assert main_window.state.media_path is None
