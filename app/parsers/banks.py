from app.parsers.base import BankSmsParser


class BluParser(BankSmsParser):
    bank_code = "blu"
    aliases = ("blubank", "blu", "بلوبانک", "بلو")


class MellatParser(BankSmsParser):
    bank_code = "mellat"
    aliases = ("bank mellat", "mellat", "ملت")


class MelliParser(BankSmsParser):
    bank_code = "melli"
    aliases = ("bank melli", "melli", "ملی")


class PasargadParser(BankSmsParser):
    bank_code = "pasargad"
    aliases = ("pasargad", "پاسارگاد")


class SamanParser(BankSmsParser):
    bank_code = "saman"
    aliases = ("saman", "سامان")


class TejaratParser(BankSmsParser):
    bank_code = "tejarat"
    aliases = ("tejarat", "تجارت")


class ParsianParser(BankSmsParser):
    bank_code = "parsian"
    aliases = ("parsian", "پارسیان")


class SepahParser(BankSmsParser):
    bank_code = "sepah"
    aliases = ("sepah", "سپاه")


class GenericParser(BankSmsParser):
    bank_code = "generic"
    aliases = ()

    def matches(self, sender: str, message: str) -> bool:
        return True
