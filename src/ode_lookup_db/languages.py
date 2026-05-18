"""Map redump language names to ISO 639-1 codes.

Unmapped languages are stored as 'zz' in `languages` (with the original kept
in `languages_raw`) and logged so we can extend this table over time.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Lowercase keys; codes are lowercase ISO 639-1.
LANGUAGE_MAP: dict[str, str] = {
    "english": "en",
    "french": "fr",
    "german": "de",
    "spanish": "es",
    "italian": "it",
    "dutch": "nl",
    "portuguese": "pt",
    "swedish": "sv",
    "norwegian": "no",
    "danish": "da",
    "finnish": "fi",
    "polish": "pl",
    "czech": "cs",
    "slovak": "sk",
    "hungarian": "hu",
    "russian": "ru",
    "ukrainian": "uk",
    "greek": "el",
    "turkish": "tr",
    "japanese": "ja",
    "korean": "ko",
    "chinese": "zh",
    "arabic": "ar",
    "hebrew": "he",
    "catalan": "ca",
    "icelandic": "is",
    "romanian": "ro",
    "bulgarian": "bg",
    "croatian": "hr",
    "serbian": "sr",
    "slovenian": "sl",
}

UNMAPPED_CODE = "zz"


def to_iso_code(raw: str) -> str:
    """Return ISO 639-1 code or 'zz' for unmapped. Logs a warning on miss."""
    key = raw.strip().lower()
    code = LANGUAGE_MAP.get(key)
    if code is None:
        log.warning("Unmapped language: %r — using %r. Add to LANGUAGE_MAP.", raw, UNMAPPED_CODE)
        return UNMAPPED_CODE
    return code
