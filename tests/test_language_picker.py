"""Tests for the language list, filter logic, and picker widget."""

from __future__ import annotations

import pytest

from core.languages import (
    AUTO_DETECT,
    LANGUAGE_OPTIONS,
    LANGUAGES,
    filter_languages,
    name_for,
)

# ---------------------------------------------------------------------------
# Language list invariants
# ---------------------------------------------------------------------------


def test_99_languages():
    assert len(LANGUAGES) == 99


def test_options_includes_auto_detect_first():
    assert LANGUAGE_OPTIONS[0] == AUTO_DETECT
    assert LANGUAGE_OPTIONS[0] == (None, "Auto-detect")


def test_languages_sorted_alphabetically():
    names = [n for _, n in LANGUAGES]
    assert names == sorted(names, key=str.lower)


def test_codes_unique():
    codes = [c for c, _ in LANGUAGES]
    assert len(set(codes)) == len(codes)


# ---------------------------------------------------------------------------
# filter_languages — searchable behavior per spec §4.6
# ---------------------------------------------------------------------------


def test_filter_empty_returns_all():
    assert filter_languages("") == list(LANGUAGE_OPTIONS)
    assert filter_languages("   ") == list(LANGUAGE_OPTIONS)


def test_filter_spa_finds_spanish():
    results = filter_languages("spa")
    names = [n for _, n in results]
    assert "Spanish" in names


def test_filter_is_case_insensitive():
    a = filter_languages("SPA")
    b = filter_languages("Spa")
    c = filter_languages("spa")
    assert a == b == c
    assert any(name == "Spanish" for _, name in a)


def test_filter_matches_by_code():
    results = filter_languages("zh")
    names = [n for _, n in results]
    assert "Chinese" in names


def test_filter_no_match_returns_empty():
    assert filter_languages("xyzzy_nope") == []


def test_filter_partial_substring():
    results = filter_languages("port")
    names = [n for _, n in results]
    assert "Portuguese" in names


# ---------------------------------------------------------------------------
# name_for
# ---------------------------------------------------------------------------


def test_name_for_known_code():
    assert name_for("en") == "English"
    assert name_for("es") == "Spanish"


def test_name_for_none_is_auto_detect():
    assert name_for(None) == "Auto-detect"


def test_name_for_unknown_returns_code():
    assert name_for("xx") == "xx"


# ---------------------------------------------------------------------------
# Widget construction (requires Tk display)
# ---------------------------------------------------------------------------


def test_language_picker_constructs(tk_root):
    from ui.components.language_picker import LanguagePicker

    lp = LanguagePicker(tk_root, initial_code=None)
    assert lp.selected_code is None


def test_language_picker_set_code_fires_callback(tk_root):
    from ui.components.language_picker import LanguagePicker

    seen: list[str | None] = []
    lp = LanguagePicker(tk_root, on_change=seen.append)
    lp.set_code("es")
    assert lp.selected_code == "es"
    assert seen == ["es"]


def test_language_picker_button_label_reflects_code(tk_root):
    from ui.components.language_picker import LanguagePicker

    lp = LanguagePicker(tk_root, initial_code="ja")
    assert "Japanese" in lp._button.cget("text")


def test_language_picker_dialog_filter_updates_listbox(tk_root):
    from ui.components.language_picker import LanguagePickerDialog

    dlg = LanguagePickerDialog(tk_root, current_code=None)
    try:
        dlg._entry.insert(0, "spa")
        dlg._refresh()
        names_in_listbox = [
            dlg._listbox.get(i) for i in range(dlg._listbox.size())
        ]
        assert "Spanish" in names_in_listbox
        # Filter should be tight, not 99 entries.
        assert len(names_in_listbox) < 10
    finally:
        dlg.destroy()


def test_language_picker_dialog_select_first_records_result(tk_root):
    from ui.components.language_picker import LanguagePickerDialog

    dlg = LanguagePickerDialog(tk_root, current_code=None)
    try:
        dlg._entry.insert(0, "english")
        dlg._refresh()
        dlg._select_first()
        assert dlg.result is not None
        assert dlg.result[1] == "English"
    finally:
        # _select_first calls destroy(); guard the second call.
        try:
            dlg.destroy()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Output format checkboxes — at least one must be selected
# ---------------------------------------------------------------------------


def test_output_format_picker_default_selection(tk_root):
    from ui.components.output_formats import OutputFormatPicker

    p = OutputFormatPicker(tk_root)
    # Phase 4e: fresh installs include the editable-project artifact.
    assert set(p.formats) == {"txt", "srt", "json"}
    assert p.has_selection is True


def test_output_format_picker_clearing_all_loses_selection(tk_root):
    from ui.components.output_formats import OutputFormatPicker

    p = OutputFormatPicker(tk_root, initial=())
    assert p.formats == []
    assert p.has_selection is False


def test_output_format_picker_set_formats_fires_change(tk_root):
    from ui.components.output_formats import OutputFormatPicker

    seen: list[list[str]] = []
    p = OutputFormatPicker(tk_root, on_change=lambda fs: seen.append(list(fs)))
    p.set_formats(["vtt"])
    assert p.formats == ["vtt"]
    assert seen and seen[-1] == ["vtt"]


def test_output_format_picker_json_toggle_round_trip(tk_root):
    """Phase 4e: turn json off, check it's gone; turn back on, check it's back."""
    from ui.components.output_formats import OutputFormatPicker

    p = OutputFormatPicker(tk_root)  # default has json on
    assert "json" in p.formats

    p.set_formats(["txt", "srt"])  # json off
    assert "json" not in p.formats
    assert p.formats == ["txt", "srt"]

    p.set_formats(["txt", "srt", "json"])  # json back on
    assert "json" in p.formats


@pytest.mark.parametrize(
    ("formats", "expected_disabled"),
    [
        ([], True),
        (["txt"], False),
        (["txt", "srt"], False),
    ],
)
def test_app_transcribe_button_respects_format_selection(tk_root, formats, expected_disabled):
    from core.settings import Settings
    from ui.app import App
    from ui.state import AppState

    app = App(root=tk_root, settings=Settings(output_formats=list(formats)))
    # Drive into FILE_LOADED so the button gets enabled-or-not.
    sample = (
        __import__("pathlib").Path(__file__).resolve().parent / "fixtures" / "sample.wav"
    )
    app._handle_file_selected(sample)
    assert app.state.state == AppState.FILE_LOADED
    state = app.transcribe_btn.cget("state")
    if expected_disabled:
        assert str(state) == "disabled"
    else:
        assert str(state) == "normal"
