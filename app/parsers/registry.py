from __future__ import annotations

from app.parsers.base import ParsedSms
from app.parsers.banks import GenericParser, PARSERS
from app.parsers.catalog import BANK_CODE_ALIASES, BANK_LABELS, bank_label, normalize_bank_code

GENERIC = GenericParser()
PARSER_BY_CODE = {parser.bank_code: parser for parser in PARSERS}


def parse_bank_sms(sender: str, message: str, bank_hint: str | None = None) -> ParsedSms:
    if bank_hint:
        code = normalize_bank_code(bank_hint)
        parser = PARSER_BY_CODE.get(code)
        if parser:
            parsed = parser.parse(sender, message)
            # Hint ارسالی از Rule اختصاصی گوشی، برای تشخیص بانک سیگنال معتبری است.
            parsed.bank_code = code
            parsed.confidence = min(100, parsed.confidence + 15)
            return parsed

    for parser in PARSERS:
        if parser.matches(sender, message):
            return parser.parse(sender, message)
    return GENERIC.parse(sender, message)


__all__ = [
    "BANK_CODE_ALIASES",
    "BANK_LABELS",
    "bank_label",
    "normalize_bank_code",
    "parse_bank_sms",
]
