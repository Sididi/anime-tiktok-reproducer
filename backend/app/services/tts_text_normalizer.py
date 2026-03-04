from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

try:
    from num2words import num2words as _num2words
except Exception:  # pragma: no cover - defensive fallback when dependency is missing
    _num2words = None


_SUPPORTED_LANGUAGES = {"fr", "en", "es", "de"}

_HEIGHT_RE = re.compile(r"(?<!\w)(\d{1,3})\s?m\s?(\d{1,2})(?!\w)", re.IGNORECASE)
_ATTACHED_UNIT_RE = re.compile(r"(?<!\w)(\d+(?:[.,]\d+)?)\s?(kg|g|km|m|cm|mm|h|min|s)\b", re.IGNORECASE)
_PERCENT_RE = re.compile(r"(?<!\w)(\d+(?:[.,]\d+)?)\s?%(?!\w)")
_EURO_SUFFIX_RE = re.compile(r"(?<!\w)(\d+(?:[.,]\d+)?)\s?€")
_DOLLAR_PREFIX_RE = re.compile(r"(?<!\w)\$\s?(\d+(?:[.,]\d+)?)")
_DOLLAR_SUFFIX_RE = re.compile(r"(?<!\w)(\d+(?:[.,]\d+)?)\s?\$")

_UNITS: dict[str, dict[str, tuple[str, str]]] = {
    "fr": {
        "kg": ("kilogramme", "kilogrammes"),
        "g": ("gramme", "grammes"),
        "km": ("kilometre", "kilometres"),
        "m": ("metre", "metres"),
        "cm": ("centimetre", "centimetres"),
        "mm": ("millimetre", "millimetres"),
        "h": ("heure", "heures"),
        "min": ("minute", "minutes"),
        "s": ("seconde", "secondes"),
        "euro": ("euro", "euros"),
        "dollar": ("dollar", "dollars"),
    },
    "en": {
        "kg": ("kilogram", "kilograms"),
        "g": ("gram", "grams"),
        "km": ("kilometer", "kilometers"),
        "m": ("meter", "meters"),
        "cm": ("centimeter", "centimeters"),
        "mm": ("millimeter", "millimeters"),
        "h": ("hour", "hours"),
        "min": ("minute", "minutes"),
        "s": ("second", "seconds"),
        "euro": ("euro", "euros"),
        "dollar": ("dollar", "dollars"),
    },
    "es": {
        "kg": ("kilogramo", "kilogramos"),
        "g": ("gramo", "gramos"),
        "km": ("kilometro", "kilometros"),
        "m": ("metro", "metros"),
        "cm": ("centimetro", "centimetros"),
        "mm": ("milimetro", "milimetros"),
        "h": ("hora", "horas"),
        "min": ("minuto", "minutos"),
        "s": ("segundo", "segundos"),
        "euro": ("euro", "euros"),
        "dollar": ("dolar", "dolares"),
    },
    "de": {
        "kg": ("Kilogramm", "Kilogramm"),
        "g": ("Gramm", "Gramm"),
        "km": ("Kilometer", "Kilometer"),
        "m": ("Meter", "Meter"),
        "cm": ("Zentimeter", "Zentimeter"),
        "mm": ("Millimeter", "Millimeter"),
        "h": ("Stunde", "Stunden"),
        "min": ("Minute", "Minuten"),
        "s": ("Sekunde", "Sekunden"),
        "euro": ("Euro", "Euro"),
        "dollar": ("Dollar", "Dollar"),
    },
}

_PERCENT_WORD = {
    "fr": "pour cent",
    "en": "percent",
    "es": "por ciento",
    "de": "Prozent",
}


class TtsTextNormalizer:
    """Normalize risky text patterns before sending content to TTS engines."""

    @classmethod
    def resolve_language(cls, language: str | None) -> str:
        candidate = (language or "").strip().lower()
        if candidate in _SUPPORTED_LANGUAGES:
            return candidate
        return "en"

    @classmethod
    def normalize_text(cls, text: str, *, language: str | None) -> str:
        if not isinstance(text, str):
            return ""
        normalized_language = cls.resolve_language(language)
        normalized = text.strip()
        if not normalized:
            return ""

        normalized = _HEIGHT_RE.sub(
            lambda match: cls._height_phrase(match.group(1), match.group(2), normalized_language),
            normalized,
        )
        normalized = _EURO_SUFFIX_RE.sub(
            lambda match: cls._currency_phrase(match.group(1), normalized_language, "euro"),
            normalized,
        )
        normalized = _DOLLAR_PREFIX_RE.sub(
            lambda match: cls._currency_phrase(match.group(1), normalized_language, "dollar"),
            normalized,
        )
        normalized = _DOLLAR_SUFFIX_RE.sub(
            lambda match: cls._currency_phrase(match.group(1), normalized_language, "dollar"),
            normalized,
        )
        normalized = _PERCENT_RE.sub(
            lambda match: cls._percent_phrase(match.group(1), normalized_language),
            normalized,
        )
        normalized = _ATTACHED_UNIT_RE.sub(
            lambda match: cls._unit_phrase(match.group(1), match.group(2).lower(), normalized_language),
            normalized,
        )

        # Final safety net: split any remaining digit/letter collisions.
        normalized = re.sub(r"(?<=\d)(?=[A-Za-zÀ-ÖØ-öø-ÿ])", " ", normalized)
        normalized = re.sub(r"(?<=[A-Za-zÀ-ÖØ-öø-ÿ])(?=\d)", " ", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized

    @classmethod
    def _height_phrase(cls, meters_raw: str, centimeters_raw: str, language: str) -> str:
        meters_words = cls._number_words(meters_raw, language)
        centimeters_words = cls._number_words(centimeters_raw, language)
        meter_unit = cls._unit_word("m", meters_raw, language)
        return f"{meters_words} {meter_unit} {centimeters_words}"

    @classmethod
    def _unit_phrase(cls, number_raw: str, unit: str, language: str) -> str:
        number_words = cls._number_words(number_raw, language)
        unit_word = cls._unit_word(unit, number_raw, language)
        return f"{number_words} {unit_word}"

    @classmethod
    def _currency_phrase(cls, number_raw: str, language: str, currency_key: str) -> str:
        number_words = cls._number_words(number_raw, language)
        currency_word = cls._unit_word(currency_key, number_raw, language)
        return f"{number_words} {currency_word}"

    @classmethod
    def _percent_phrase(cls, number_raw: str, language: str) -> str:
        number_words = cls._number_words(number_raw, language)
        return f"{number_words} {_PERCENT_WORD.get(language, _PERCENT_WORD['en'])}"

    @classmethod
    def _unit_word(cls, unit: str, number_raw: str, language: str) -> str:
        lang_units = _UNITS.get(language, _UNITS["en"])
        singular_plural = lang_units.get(unit)
        if singular_plural is None:
            return unit
        if cls._is_singular(number_raw):
            return singular_plural[0]
        return singular_plural[1]

    @classmethod
    def _is_singular(cls, number_raw: str) -> bool:
        value = cls._to_decimal(number_raw)
        if value is None:
            return False
        return value == Decimal(1)

    @classmethod
    def _number_words(cls, number_raw: str, language: str) -> str:
        decimal_value = cls._to_decimal(number_raw)
        if decimal_value is None:
            return number_raw
        if _num2words is None:
            return str(decimal_value.normalize()) if decimal_value % 1 else str(int(decimal_value))

        try:
            if decimal_value == decimal_value.to_integral_value():
                return str(_num2words(int(decimal_value), lang=language))
            return str(_num2words(str(decimal_value.normalize()), lang=language))
        except Exception:
            # Never block normalization on conversion failures.
            return number_raw

    @classmethod
    def _to_decimal(cls, value: str) -> Decimal | None:
        raw = value.strip().replace(" ", "")
        if not raw:
            return None
        # Conservative parsing: accept comma or dot as decimal separator.
        if raw.count(",") > 1 or raw.count(".") > 1:
            return None
        normalized = raw.replace(",", ".")
        try:
            return Decimal(normalized)
        except InvalidOperation:
            return None
