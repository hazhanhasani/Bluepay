from __future__ import annotations

import json
import re
from html import unescape
from urllib.parse import parse_qs, unquote, unquote_plus


class SmsPayloadError(ValueError):
    def __init__(self, code: str, detail: str, preview: str = "") -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.preview = preview[:500]


_UNRESOLVED_PLACEHOLDERS = (
    "{incoming number}",
    "{{incoming number}}",
    "{message body}",
    "{{message body}}",
    "<incoming number>",
    "<message body>",
    "[فیلد پویا: incoming number]",
    "[فیلد پویا: message body]",
)


def _is_unresolved_placeholder(value: str | None) -> bool:
    if not value:
        return False
    normalized = " ".join(value.strip().casefold().split())
    return any(token in normalized for token in _UNRESOLVED_PLACEHOLDERS)


def _decode_payload_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = unescape(text)
    if "%" in text:
        for _ in range(2):
            decoded = unquote(text)
            if decoded == text:
                break
            text = decoded
    text = (
        text.replace(r"\r\n", "\n")
        .replace(r"\n", "\n")
        .replace(r"\r", "\n")
        .replace(r"\t", "\t")
    )
    return text.strip()


def _split_combined_forwarded_message(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    match = re.match(
        r"(?is)^\s*(?:FROM|SENDER|فرستنده)\s*:\s*(?P<sender>[^\r\n]+)[\r\n]+(?P<message>.+)$",
        _decode_payload_text(value),
    )
    if not match:
        return None, None
    return match.group("sender").strip(), match.group("message").strip()


def _iter_string_values(value: object, *, depth: int = 0):
    if depth > 4:
        return
    if isinstance(value, dict):
        for nested in value.values():
            yield from _iter_string_values(nested, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _iter_string_values(nested, depth=depth + 1)
    elif isinstance(value, (str, int, float)):
        text = _decode_payload_text(value)
        if text:
            yield text
            if text[:1] in "[{":
                try:
                    nested = json.loads(text)
                except Exception:
                    nested = None
                if nested is not None:
                    yield from _iter_string_values(nested, depth=depth + 1)


def _first_payload_value(data: dict, *keys: str, skip_placeholders: bool = False):
    for key in keys:
        value = data.get(key)
        if value is None or not str(value).strip():
            continue
        normalized = _decode_payload_text(value)
        if skip_placeholders and _is_unresolved_placeholder(normalized):
            continue
        return normalized
    return None


def _looks_like_bank_sms(text: str) -> bool:
    folded = text.casefold()
    has_money = bool(re.search(r"\d[\d,\.٬ ]{2,}\s*(?:ریال|ريال|تومان)", text))
    has_event = any(
        word in folded
        for word in (
            "واریز",
            "واريز",
            "بستانکار",
            "بستانكار",
            "به حساب شما نشست",
            "به حسابتان نشست",
            "موجودی",
            "موجودي",
            "برداشت",
            "خرید",
            "خريد",
        )
    )
    return has_money and has_event


def _message_score(text: str) -> int:
    score = 0
    if _looks_like_bank_sms(text):
        score += 100
    if re.search(r"\d[\d,\.٬ ]{2,}\s*(?:ریال|ريال|تومان)", text):
        score += 35
    if any(word in text.casefold() for word in ("واریز", "واريز", "بستانکار", "به حساب شما نشست")):
        score += 35
    if "موجودی" in text or "موجودي" in text:
        score += 10
    if "\n" in text:
        score += 5
    return score + min(len(text), 400) // 80


def _sender_score(text: str) -> int:
    if len(text) > 180 or "\n" in text:
        return 0
    score = 0
    if re.search(r"\+?\d{7,15}", text):
        score += 50
    if any(word in text.casefold() for word in ("bank", "بانک", "بلوبانک", "بلو", "mellat", "melli")):
        score += 35
    return score


def _recover_sms_from_any_payload(data: dict, raw_text: str) -> tuple[str | None, str | None]:
    candidates = list(_iter_string_values(data))
    raw_variants = [raw_text, _decode_payload_text(raw_text)]
    if "%" in raw_text or "+" in raw_text:
        try:
            raw_variants.append(unquote_plus(raw_text))
        except Exception:
            pass
    for value in raw_variants:
        value = _decode_payload_text(value)
        if value and value not in candidates:
            candidates.append(value)

    for value in candidates:
        recovered_sender, recovered_message = _split_combined_forwarded_message(value)
        if recovered_sender and recovered_message and _looks_like_bank_sms(recovered_message):
            return recovered_sender, recovered_message

    messages = [(_message_score(value), value) for value in candidates if not _is_unresolved_placeholder(value)]
    senders = [(_sender_score(value), value) for value in candidates if not _is_unresolved_placeholder(value)]
    message_score, message = max(messages, default=(0, None), key=lambda item: item[0])
    sender_score, sender = max(senders, default=(0, None), key=lambda item: item[0])
    return (sender if sender_score > 0 else None, message if message_score >= 70 else None)


def _parse_text_sms_payload(raw: str) -> dict:
    text = _decode_payload_text(raw)
    if not text:
        return {}
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except Exception:
        pass

    marker = re.match(
        r"(?is)^\s*DEVICE\s*:\s*(?P<device>[^\r\n]*)[\r\n]+"
        r"\s*SENDER\s*:\s*(?P<sender>[^\r\n]*)[\r\n]+"
        r"\s*MESSAGE\s*:\s*[\r\n]*(?P<message>.*)$",
        text,
    )
    if marker:
        return {
            "device_id": marker.group("device").strip(),
            "sender": marker.group("sender").strip(),
            "message": marker.group("message").strip(),
        }

    sender, message = _split_combined_forwarded_message(text)
    if sender and message:
        return {"sender": sender, "message": message}
    return {"message": text}


def parse_sms_payload(
    raw_text: str,
    content_type: str = "",
    query_params: dict[str, str] | None = None,
) -> tuple[str, str, str | None, str | None]:
    content_type = (content_type or "").lower()
    data: dict = {}
    try:
        if "application/json" in content_type:
            try:
                value = json.loads(raw_text)
                if isinstance(value, dict):
                    data = value
            except Exception:
                data = _parse_text_sms_payload(raw_text)
        elif "application/x-www-form-urlencoded" in content_type:
            parsed = parse_qs(raw_text, keep_blank_values=True)
            data = {key: values[-1] if values else "" for key, values in parsed.items()}
            decoded_bare = unquote_plus(raw_text)
            if "=" not in raw_text or not any(str(value).strip() for value in data.values()):
                data = _parse_text_sms_payload(decoded_bare)
        else:
            data = _parse_text_sms_payload(raw_text)
    except Exception:
        data = _parse_text_sms_payload(raw_text)

    for wrapper in ("data", "payload", "messageData", "sms", "smsData", "notification", "event"):
        nested = data.get(wrapper)
        if isinstance(nested, dict):
            data = {**data, **nested}

    for key, value in (query_params or {}).items():
        data.setdefault(key, value)

    sender_keys = (
        "sender", "from", "source", "address", "incoming_number", "incomingNumber",
        "incoming", "number", "phone", "fromNumber", "from_number", "phoneNumber",
        "originator", "originalSender", "original_sender", "title", "senderName",
        "sender_name", "contactName", "contact_name",
    )
    message_keys = (
        "message", "body", "text", "msg", "content", "message_body", "messageBody",
        "sms_body", "smsBody", "originalBody", "original_body", "originalMessage",
        "original_message", "transformedBody", "transformed_body", "description",
    )
    supplied_sender = _first_payload_value(data, *sender_keys)
    supplied_message = _first_payload_value(data, *message_keys)
    had_unresolved_template = _is_unresolved_placeholder(supplied_sender) or _is_unresolved_placeholder(supplied_message)

    sender = _first_payload_value(data, *sender_keys, skip_placeholders=True)
    message = _first_payload_value(data, *message_keys, skip_placeholders=True)
    device_id = _first_payload_value(
        data,
        "device_id", "deviceId", "device", "phone_id", "phoneId", "sim", "sim_id",
        "subscriptionId", "deviceModel", "device_name", "deviceName",
    )
    bank_code = _first_payload_value(data, "bank_code", "bankCode", "bank")

    combined_sender, combined_message = _split_combined_forwarded_message(message)
    if combined_sender and combined_message:
        sender, message = combined_sender, combined_message

    recovered_sender, recovered_message = _recover_sms_from_any_payload(data, raw_text)
    if recovered_message and (not message or _is_unresolved_placeholder(message) or not _looks_like_bank_sms(message)):
        message = recovered_message
    if recovered_sender and (not sender or _is_unresolved_placeholder(sender)):
        sender = recovered_sender

    if not sender and message and _looks_like_bank_sms(message):
        sender = "unknown-forwarder"

    if _is_unresolved_placeholder(sender) or _is_unresolved_placeholder(message) or (had_unresolved_template and (not sender or not message)):
        raise SmsPayloadError(
            "SMS_TEMPLATE_NOT_RESOLVED",
            "متغیرهای SMS Forwarder جایگزین نشده‌اند. فیلدهای پویا را از خود برنامه درج کن یا قالب پیش‌فرض From/Message را بفرست.",
            raw_text,
        )
    if not sender or len(sender) > 180:
        raise SmsPayloadError("SMS_SENDER_MISSING", "شماره/نام فرستنده واقعی پیامک در درخواست موجود نیست", raw_text)
    if not message or len(message) < 3 or len(message) > 5000:
        raise SmsPayloadError("SMS_MESSAGE_MISSING", "متن واقعی پیامک در درخواست موجود نیست یا طول آن نامعتبر است", raw_text)
    if device_id and len(device_id) > 120:
        raise SmsPayloadError("SMS_DEVICE_TOO_LONG", "device_id بیش از حد طولانی است", raw_text)
    if bank_code and len(bank_code) > 60:
        raise SmsPayloadError("SMS_BANK_CODE_TOO_LONG", "bank_code بیش از حد طولانی است", raw_text)
    return sender, message, device_id, bank_code


__all__ = [
    "SmsPayloadError",
    "parse_sms_payload",
    "_parse_text_sms_payload",
    "_recover_sms_from_any_payload",
]
