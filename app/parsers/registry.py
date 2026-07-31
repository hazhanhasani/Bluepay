from __future__ import annotations

import re

from app.parsers.base import ParsedSms, normalize_text
from app.parsers.banks import GenericParser, PARSERS
from app.parsers.catalog import BANK_CODE_ALIASES, BANK_LABELS, bank_label, normalize_bank_code

GENERIC = GenericParser()
PARSER_BY_CODE = {parser.bank_code: parser for parser in PARSERS}


def _alias_pattern(alias: str) -> re.Pattern[str]:
    """Build a conservative boundary-aware pattern for a bank alias.

    Short aliases such as «دی» must not match inside names or ordinary words.
    Spaces in aliases are allowed to appear as repeated whitespace.
    """
    normalized = normalize_text(alias).lower().strip()
    escaped = re.escape(normalized).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<![\w\u0600-\u06ff]){escaped}(?![\w\u0600-\u06ff])", re.IGNORECASE)


def _profile_score(parser, sender: str, message: str) -> int:
    sender_text = normalize_text(sender).lower()
    message_text = normalize_text(message).lower()
    best = 0

    for alias in parser.aliases:
        alias_text = normalize_text(alias).lower().strip()
        if not alias_text:
            continue
        pattern = _alias_pattern(alias_text)
        alias_len = len(alias_text.replace(" ", ""))

        # Sender/title supplied by Android is the strongest signal.
        if pattern.search(sender_text):
            score = 200 + min(alias_len, 30)
            if alias_text in {sender_text, sender_text.strip("+()[] -_")}:
                score += 40
            best = max(best, score)

        # Full bank names in message body are useful, but short aliases must not
        # win merely because they occur inside a person's name or normal prose.
        if pattern.search(message_text):
            if alias_len <= 3 and not any(prefix in alias_text for prefix in ("بانک", "موسسه", "مؤسسه")):
                score = 15
            else:
                score = 80 + min(alias_len, 30)
            best = max(best, score)

    return best


def detect_bank_parser(sender: str, message: str):
    scored = [(_profile_score(parser, sender, message), parser) for parser in PARSERS]
    score, parser = max(scored, key=lambda item: item[0], default=(0, None))
    return parser if parser is not None and score > 0 else None


def parse_bank_sms(sender: str, message: str, bank_hint: str | None = None) -> ParsedSms:
    if bank_hint:
        code = normalize_bank_code(bank_hint)
        parser = PARSER_BY_CODE.get(code)
        if parser:
            parsed = parser.parse(sender, message)
            parsed.bank_code = code
            parsed.confidence = min(100, parsed.confidence + 15)
            return parsed

    parser = detect_bank_parser(sender, message)
    if parser:
        parsed = parser.parse(sender, message)
        # Detection is already based on a scored alias match. Ensure parser
        # confidence reflects this without depending on its old substring matcher.
        parsed.bank_code = parser.bank_code
        if parsed.is_credit:
            parsed.confidence = min(100, parsed.confidence + 10)
        return parsed
    return GENERIC.parse(sender, message)


__all__ = [
    "BANK_CODE_ALIASES",
    "BANK_LABELS",
    "bank_label",
    "normalize_bank_code",
    "detect_bank_parser",
    "parse_bank_sms",
]
