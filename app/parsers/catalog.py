from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BankProfile:
    code: str
    label: str
    aliases: tuple[str, ...]
    legacy: bool = False


# فهرست عملیاتی بانک‌ها و مؤسسات بانکی ایران. ورودی‌های legacy نیز نگه داشته
# شده‌اند تا پیامک‌های قدیمی یا نام‌های ادغام‌شده همچنان قابل شناسایی باشند.
BANK_PROFILES: tuple[BankProfile, ...] = (
    BankProfile("melli", "ملی ایران", ("بانک ملی ایران", "بانک ملی", "bank melli", "bmi", "melli", "ملی")),
    BankProfile("sepah", "سپه", ("بانک سپه", "bank sepah", "sepah", "سپه", "بانک انصار", "ansar", "بانک قوامین", "ghavamin", "بانک حکمت ایرانیان", "hekmat", "موسسه کوثر", "kosar", "بانک مهر اقتصاد", "mehr eqtesad")),
    BankProfile("mellat", "ملت", ("بانک ملت", "bank mellat", "bankmellat", "mellat", "ملت")),
    BankProfile("tejarat", "تجارت", ("بانک تجارت", "tejarat bank", "tejarat", "تجارت")),
    BankProfile("saderat", "صادرات ایران", ("بانک صادرات ایران", "بانک صادرات", "bsi", "saderat", "صادرات")),
    BankProfile("refah", "رفاه کارگران", ("بانک رفاه کارگران", "بانک رفاه", "refah", "رفاه")),
    BankProfile("maskan", "مسکن", ("بانک مسکن", "bank maskan", "maskan", "مسکن")),
    BankProfile("keshavarzi", "کشاورزی", ("بانک کشاورزی", "bank keshavarzi", "bki", "keshavarzi", "کشاورزی")),
    BankProfile("sanat_madan", "صنعت و معدن", ("بانک صنعت و معدن", "sanat o madan", "sanat madan", "صنعت و معدن")),
    BankProfile("tosee_saderat", "توسعه صادرات", ("بانک توسعه صادرات", "بانک توسعه صادرات ایران", "edbi", "tosee saderat", "توسعه صادرات")),
    BankProfile("postbank", "پست بانک ایران", ("پست بانک ایران", "پست بانک", "post bank iran", "postbank", "پست‌بانک")),
    BankProfile("tosee_taavon", "توسعه تعاون", ("بانک توسعه تعاون", "tosee taavon", "توسعه تعاون")),
    BankProfile("eghtesad_novin", "اقتصاد نوین", ("بانک اقتصاد نوین", "اقتصادنوین", "enbank", "eghtesad novin", "اقتصاد نوین")),
    BankProfile("parsian", "پارسیان", ("بانک پارسیان", "parsian bank", "parsian", "پارسیان")),
    BankProfile("karafarin", "کارآفرین", ("بانک کارآفرین", "karafarin bank", "karafarin", "کارآفرین")),
    BankProfile("saman", "سامان", ("بانک سامان", "saman bank", "saman", "سامان")),
    BankProfile("pasargad", "پاسارگاد", ("بانک پاسارگاد", "bpi", "pasargad", "پاسارگاد")),
    BankProfile("sarmayeh", "سرمایه", ("بانک سرمایه", "sarmayeh bank", "sarmayeh", "سرمایه")),
    BankProfile("sina", "سینا", ("بانک سینا", "sina bank", "sina", "سینا")),
    BankProfile("shahr", "شهر", ("بانک شهر", "shahr bank", "city bank iran", "شهر")),
    BankProfile("ayandeh", "آینده", ("بانک آینده", "ayandeh bank", "ayandeh", "آینده")),
    BankProfile("gardeshgari", "گردشگری", ("بانک گردشگری", "tourism bank", "gardeshgari", "گردشگری")),
    BankProfile("dey", "دی", ("بانک دی", "dey bank", "day bank", "دی")),
    BankProfile("iran_zamin", "ایران زمین", ("بانک ایران زمین", "iranzamin", "iran zamin", "ایران زمین")),
    BankProfile("khavarmianeh", "خاورمیانه", ("بانک خاورمیانه", "middle east bank", "middleeastbank", "khavarmianeh", "خاورمیانه")),
    BankProfile("iran_venezuela", "مشترک ایران و ونزوئلا", ("بانک مشترک ایران و ونزوئلا", "iran venezuela bank", "ivbb", "ایران و ونزوئلا")),
    BankProfile("mehr_iran", "قرض‌الحسنه مهر ایران", ("بانک قرض الحسنه مهر ایران", "بانک مهر ایران", "mehr iran", "qmb", "مهر ایران")),
    BankProfile("resalat", "قرض‌الحسنه رسالت", ("بانک قرض الحسنه رسالت", "بانک رسالت", "resalat", "رسالت")),
    BankProfile("melal", "مؤسسه اعتباری ملل", ("موسسه اعتباری ملل", "مؤسسه اعتباری ملل", "melal", "ملل")),
    BankProfile("noor", "مؤسسه اعتباری نور", ("موسسه اعتباری نور", "مؤسسه اعتباری نور", "noor credit", "نور"), legacy=True),
    BankProfile("blu", "بلوبانک", ("بلوبانک", "بلو بانک", "blubank", "blu bank", "blu", "بلو")),
    BankProfile("tobank", "توبانک", ("توبانک", "to bank", "tobank")),
    BankProfile("vbank", "ویپاد / ترابانک پاسارگاد", ("ویپاد", "vpod", "wepod", "ترابانک پاسارگاد")),
    BankProfile("baam", "بام بانک ملی", ("بام بانک ملی", "سامانه بام", "baam", "بام")),
)

BANK_BY_CODE = {profile.code: profile for profile in BANK_PROFILES}
BANK_LABELS = {profile.code: profile.label for profile in BANK_PROFILES}


def _normalize_alias(value: str) -> str:
    return " ".join(value.strip().lower().replace("‌", " ").replace("بانك", "بانک").split())


BANK_CODE_ALIASES: dict[str, str] = {}
for profile in BANK_PROFILES:
    BANK_CODE_ALIASES[_normalize_alias(profile.code)] = profile.code
    BANK_CODE_ALIASES[_normalize_alias(profile.label)] = profile.code
    for alias in profile.aliases:
        BANK_CODE_ALIASES[_normalize_alias(alias)] = profile.code


def normalize_bank_code(value: str) -> str:
    normalized = _normalize_alias(value)
    without_bank = _normalize_alias(normalized.replace("بانک", "").replace("bank", ""))
    return BANK_CODE_ALIASES.get(normalized, BANK_CODE_ALIASES.get(without_bank, without_bank.replace(" ", "_")))


def bank_label(code: str) -> str:
    profile = BANK_BY_CODE.get(normalize_bank_code(code))
    return profile.label if profile else code.replace("_", " ").title()
