from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


def validate_public_https_url(value: str) -> tuple[bool, str]:
    try:
        parsed = urlparse(value.strip())
    except Exception:
        return False, "آدرس نامعتبر است."
    if parsed.scheme != "https":
        return False, "برای امنیت، آدرس Callback باید با https:// شروع شود."
    if not parsed.hostname:
        return False, "دامنه Callback مشخص نیست."
    host = parsed.hostname.lower()
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} or host.endswith(".local"):
        return False, "آدرس محلی برای Callback مجاز نیست."
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False, "آدرس IP خصوصی یا داخلی برای Callback مجاز نیست."
    except ValueError:
        pass
    return True, value.strip()
