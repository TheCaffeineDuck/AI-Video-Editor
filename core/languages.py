"""Whisper's 99 supported languages with code → name lookup and search.

Sourced from the canonical OpenAI Whisper language map. Display names are
title-cased; codes match what ``faster_whisper`` accepts as ``language=``.
"""

from __future__ import annotations

# (code, display name) — alphabetical by display name except for English at top.
_LANGUAGES_RAW: tuple[tuple[str, str], ...] = (
    ("af", "Afrikaans"),
    ("am", "Amharic"),
    ("ar", "Arabic"),
    ("as", "Assamese"),
    ("az", "Azerbaijani"),
    ("ba", "Bashkir"),
    ("be", "Belarusian"),
    ("bn", "Bengali"),
    ("bo", "Tibetan"),
    ("br", "Breton"),
    ("bs", "Bosnian"),
    ("bg", "Bulgarian"),
    ("ca", "Catalan"),
    ("cs", "Czech"),
    ("cy", "Welsh"),
    ("da", "Danish"),
    ("de", "German"),
    ("el", "Greek"),
    ("en", "English"),
    ("es", "Spanish"),
    ("et", "Estonian"),
    ("eu", "Basque"),
    ("fa", "Persian"),
    ("fi", "Finnish"),
    ("fo", "Faroese"),
    ("fr", "French"),
    ("gl", "Galician"),
    ("gu", "Gujarati"),
    ("ha", "Hausa"),
    ("haw", "Hawaiian"),
    ("he", "Hebrew"),
    ("hi", "Hindi"),
    ("hr", "Croatian"),
    ("ht", "Haitian Creole"),
    ("hu", "Hungarian"),
    ("hy", "Armenian"),
    ("id", "Indonesian"),
    ("is", "Icelandic"),
    ("it", "Italian"),
    ("ja", "Japanese"),
    ("jw", "Javanese"),
    ("ka", "Georgian"),
    ("kk", "Kazakh"),
    ("km", "Khmer"),
    ("kn", "Kannada"),
    ("ko", "Korean"),
    ("la", "Latin"),
    ("lb", "Luxembourgish"),
    ("ln", "Lingala"),
    ("lo", "Lao"),
    ("lt", "Lithuanian"),
    ("lv", "Latvian"),
    ("mg", "Malagasy"),
    ("mi", "Maori"),
    ("mk", "Macedonian"),
    ("ml", "Malayalam"),
    ("mn", "Mongolian"),
    ("mr", "Marathi"),
    ("ms", "Malay"),
    ("mt", "Maltese"),
    ("my", "Burmese"),
    ("ne", "Nepali"),
    ("nl", "Dutch"),
    ("nn", "Norwegian Nynorsk"),
    ("no", "Norwegian"),
    ("oc", "Occitan"),
    ("pa", "Punjabi"),
    ("pl", "Polish"),
    ("ps", "Pashto"),
    ("pt", "Portuguese"),
    ("ro", "Romanian"),
    ("ru", "Russian"),
    ("sa", "Sanskrit"),
    ("sd", "Sindhi"),
    ("si", "Sinhala"),
    ("sk", "Slovak"),
    ("sl", "Slovenian"),
    ("sn", "Shona"),
    ("so", "Somali"),
    ("sq", "Albanian"),
    ("sr", "Serbian"),
    ("su", "Sundanese"),
    ("sv", "Swedish"),
    ("sw", "Swahili"),
    ("ta", "Tamil"),
    ("te", "Telugu"),
    ("tg", "Tajik"),
    ("th", "Thai"),
    ("tk", "Turkmen"),
    ("tl", "Tagalog"),
    ("tr", "Turkish"),
    ("tt", "Tatar"),
    ("uk", "Ukrainian"),
    ("ur", "Urdu"),
    ("uz", "Uzbek"),
    ("vi", "Vietnamese"),
    ("yi", "Yiddish"),
    ("yo", "Yoruba"),
    ("zh", "Chinese"),
)

# Sorted alphabetically by display name for the picker.
LANGUAGES: tuple[tuple[str, str], ...] = tuple(
    sorted(_LANGUAGES_RAW, key=lambda x: x[1].lower())
)

assert len(LANGUAGES) == 99, f"expected 99 languages, got {len(LANGUAGES)}"

# Sentinel for "auto-detect" — code is None.
AUTO_DETECT = (None, "Auto-detect")

# Public list including auto-detect at index 0.
LANGUAGE_OPTIONS: tuple[tuple[str | None, str], ...] = (AUTO_DETECT, *LANGUAGES)


def name_for(code: str | None) -> str:
    """Return the display name for a code, or 'Auto-detect' if code is None."""
    if code is None:
        return AUTO_DETECT[1]
    for c, n in LANGUAGES:
        if c == code:
            return n
    return code  # unknown code → echo it back


def filter_languages(
    query: str,
    *,
    options: tuple[tuple[str | None, str], ...] = LANGUAGE_OPTIONS,
) -> list[tuple[str | None, str]]:
    """Case-insensitive substring match on display name **or** code.

    Empty query returns the full list. ``Auto-detect`` is always returned at
    the top when its name contains the query.
    """
    q = query.strip().lower()
    if not q:
        return list(options)
    out: list[tuple[str | None, str]] = []
    for code, name in options:
        haystack = name.lower()
        if code is not None:
            haystack += f" {code.lower()}"
        if q in haystack:
            out.append((code, name))
    return out
