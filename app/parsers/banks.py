from __future__ import annotations

from app.parsers.base import BankSmsParser
from app.parsers.catalog import BANK_PROFILES, BankProfile


class ProfileBankParser(BankSmsParser):
    def __init__(self, profile: BankProfile):
        self.profile = profile
        self.bank_code = profile.code
        self.aliases = profile.aliases


PARSERS = [ProfileBankParser(profile) for profile in BANK_PROFILES]


class GenericParser(BankSmsParser):
    bank_code = "generic"
    aliases = ()

    def matches(self, sender: str, message: str) -> bool:
        return True
