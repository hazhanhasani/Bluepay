from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_text(text: str) -> str:
    text = text.translate(PERSIAN_DIGITS)
    text = text.replace("٬", ",").replace("،", ",").replace("٫", ".")
    return " ".join(text.split())


def digits_only(value: str) -> str:
    return re.sub(r"\D", "", value.translate(PERSIAN_DIGITS))


@dataclass(slots=True)
class ParsedSms:
    bank_code: str
    is_credit: bool
    amount_rial: int | None
    card_last4: str | None
    reference_number: str | None
    transaction_at: datetime
    confidence: int
    reason: str


class BankSmsParser:
    bank_code = "generic"
    aliases: tuple[str, ...] = ()

    credit_words = ("واریز", "بستانکار", "افزایش موجودی", "دریافت", "انتقال به", "واریزي", "deposit", "credit")
    debit_words = ("برداشت", "بدهکار", "خرید", "کسر", "کاهش موجودی", "debit", "purchase")

    def matches(self, sender: str, message: str) -> bool:
        haystack = f"{sender} {message}".lower()
        return any(alias.lower() in haystack for alias in self.aliases)

    def parse(self, sender: str, message: str) -> ParsedSms:
        text = normalize_text(message)
        lower = text.lower()
        has_credit = any(word.lower() in lower for word in self.credit_words)
        has_debit = any(word.lower() in lower for word in self.debit_words)
        is_credit = has_credit and not has_debit

        amount_rial, amount_score = self.extract_amount(text)
        card_last4 = self.extract_card_last4(text)
        reference = self.extract_reference(text)

        score = 20
        if self.matches(sender, text):
            score += 20
        if is_credit:
            score += 25
        if amount_rial:
            score += amount_score
        if card_last4:
            score += 10
        if reference:
            score += 5
        if has_debit:
            score = min(score, 20)

        return ParsedSms(
            bank_code=self.bank_code,
            is_credit=is_credit,
            amount_rial=amount_rial,
            card_last4=card_last4,
            reference_number=reference,
            transaction_at=datetime.now(timezone.utc),
            confidence=min(score, 100),
            reason="parsed" if is_credit and amount_rial else "not_a_confident_credit",
        )

    @staticmethod
    def extract_amount(text: str) -> tuple[int | None, int]:
        patterns = [
            r"(?:مبلغ|به مبلغ|واریز(?:ی)?|بستانکار)\s*[:：-]?\s*([0-9][0-9,\. ]{2,})\s*(ریال|تومان)?",
            r"([0-9][0-9,\. ]{3,})\s*(ریال|تومان)",
        ]
        for index, pattern in enumerate(patterns):
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            raw = digits_only(match.group(1))
            if not raw:
                continue
            value = int(raw)
            unit = (match.group(2) or "ریال").lower() if len(match.groups()) >= 2 else "ریال"
            if "تومان" in unit:
                value *= 10
            if value > 0:
                return value, 25 if index == 0 else 18
        return None, 0

    @staticmethod
    def extract_card_last4(text: str) -> str | None:
        patterns = [
            r"(?:کارت|حساب|سپرده)\s*(?:شماره)?\s*[:：-]?\s*(?:[*xX-]*)(\d{4})(?!\d)",
            r"(?:\*{2,}|x{2,})(\d{4})(?!\d)",
            r"\b\d{12}(\d{4})\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def extract_reference(text: str) -> str | None:
        match = re.search(r"(?:پیگیری|مرجع|شناسه|reference|ref)\s*[:：-]?\s*([A-Za-z0-9-]{4,})", text, flags=re.IGNORECASE)
        return match.group(1) if match else None
