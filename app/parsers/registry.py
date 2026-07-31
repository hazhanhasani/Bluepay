from __future__ import annotations

from app.parsers.base import ParsedSms
from app.parsers.banks import (
    BluParser,
    GenericParser,
    MellatParser,
    MelliParser,
    ParsianParser,
    PasargadParser,
    SamanParser,
    SepahParser,
    TejaratParser,
)

PARSERS = [
    BluParser(),
    MellatParser(),
    MelliParser(),
    PasargadParser(),
    SamanParser(),
    TejaratParser(),
    ParsianParser(),
    SepahParser(),
]
GENERIC = GenericParser()


def parse_bank_sms(sender: str, message: str) -> ParsedSms:
    for parser in PARSERS:
        if parser.matches(sender, message):
            return parser.parse(sender, message)
    return GENERIC.parse(sender, message)


BANK_CODE_ALIASES = {
    "blu": "blu", "blubank": "blu", "بلو": "blu", "بلوبانک": "blu",
    "mellat": "mellat", "ملت": "mellat",
    "melli": "melli", "ملی": "melli",
    "pasargad": "pasargad", "پاسارگاد": "pasargad",
    "saman": "saman", "سامان": "saman",
    "tejarat": "tejarat", "تجارت": "tejarat",
    "parsian": "parsian", "پارسیان": "parsian",
    "sepah": "sepah", "سپاه": "sepah",
}


def normalize_bank_code(value: str) -> str:
    normalized = value.strip().lower().replace("بانک", "").strip()
    return BANK_CODE_ALIASES.get(normalized, normalized)
