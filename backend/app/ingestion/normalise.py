"""Turning what people actually type into something the ledger can trust.

Recovery quality is capped by invoice-data quality. Every downstream guarantee —
the right amount, the right debtor, the right phone — depends entirely on what
gets through this module.

The governing rule is **reject rather than guess**. A silently mis-parsed amount
becomes a legal notice for the wrong sum, and a mis-parsed number becomes a call
to a stranger. Both are worse than an import that stops and asks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberType

INDIA = "IN"

_HONORIFICS = (
    "shri", "sri", "smt", "mr", "mrs", "ms", "m/s", "messrs", "dr", "prof",
)

# 4,50,000 (Indian) vs 450,000 (Western). Deciding by shape rather than by locale
# guess: Indian grouping puts two digits in every group after the first.
_INDIAN_GROUPING = re.compile(r"^\d{1,2}(,\d{2})+(,\d{3})$")
_WESTERN_GROUPING = re.compile(r"^\d{1,3}(,\d{3})+$")

_LAKH = re.compile(r"^([\d.]+)\s*(lakh|lakhs|lac|lacs)$", re.I)
_CRORE = re.compile(r"^([\d.]+)\s*(crore|crores|cr)$", re.I)


class NormalisationError(ValueError):
    """The value could not be read confidently. Never guessed around."""


# ------------------------------------------------------------------------ phones


@dataclass(frozen=True)
class NormalisedPhone:
    e164: str
    number_type: str  # mobile | fixed_line | unknown
    original: str


def _phone_type(parsed) -> str:
    kind = phonenumbers.number_type(parsed)
    if kind in (PhoneNumberType.MOBILE, PhoneNumberType.FIXED_LINE_OR_MOBILE):
        return "mobile"
    if kind == PhoneNumberType.FIXED_LINE:
        return "fixed_line"
    return "unknown"


def split_phone_cell(raw: str) -> list[str]:
    """One cell routinely holds two numbers. Split before parsing, not after."""
    if not raw:
        return []
    parts = re.split(r"[;,/]| or |\band\b", raw, flags=re.I)
    return [p.strip() for p in parts if p and p.strip()]


def normalise_phone(raw: str, *, region: str = INDIA) -> NormalisedPhone:
    """Parse to E.164, or refuse.

    `number_type` is captured because a landline is far more likely to be a shared
    office phone — which is the third-party disclosure risk that gates L3 content
    behind a keypress.
    """
    if raw is None or not str(raw).strip():
        raise NormalisationError("empty phone number")

    text = str(raw).strip()
    # Strip separators people use that phonenumbers does not expect, but keep a
    # leading + because it carries the country code.
    cleaned = re.sub(r"[^\d+]", "", text)
    if not cleaned:
        raise NormalisationError(f"no digits in phone {raw!r}")

    candidates = [cleaned]
    # `91-9876543210` and `919876543210` both mean +91…, but only if the rest is
    # the right length; adding the + lets the library judge rather than us.
    if not cleaned.startswith("+"):
        if cleaned.startswith("91") and len(cleaned) == 12:
            candidates.append("+" + cleaned)
        if cleaned.startswith("0"):
            candidates.append(cleaned.lstrip("0"))

    for candidate in candidates:
        try:
            parsed = phonenumbers.parse(candidate, region)
        except NumberParseException:
            continue
        if phonenumbers.is_valid_number(parsed):
            return NormalisedPhone(
                e164=phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164),
                number_type=_phone_type(parsed),
                original=text,
            )

    raise NormalisationError(f"not a valid {region} phone number: {raw!r}")


# ----------------------------------------------------------------------- amounts


def normalise_amount(raw) -> int:
    """Parse a money cell to integer paise, or refuse.

    Indian digit grouping is the trap: `4,50,000` is four lakh fifty thousand,
    and a parser that assumes Western grouping reads it as forty-five thousand —
    an error of an order of magnitude, in a number that ends up in a legal notice.
    """
    if raw is None:
        raise NormalisationError("empty amount")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw * 100
    text = str(raw).strip()
    if not text:
        raise NormalisationError("empty amount")

    negative = False
    # Accounting notation: (45000) means -45000.
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()

    text = re.sub(r"(?i)^(rs\.?|inr|₹)\s*", "", text).strip()
    text = re.sub(r"(?i)[/\-]+$", "", text).strip()  # trailing "/-"

    # A leading minus. Tally exports sales as negative from the ledger's point of
    # view, so this is the common case on a real feed rather than an oddity.
    if text.startswith("-"):
        negative = not negative
        text = text[1:].strip()
    elif text.startswith("+"):
        text = text[1:].strip()

    if re.search(r"(?i)\bcr\b|\bcredit\b", text) and not _CRORE.match(text):
        negative = True
    text = re.sub(r"(?i)\s*\b(dr|debit|cr|credit)\b\s*$", "", text).strip()

    lakh = _LAKH.match(text)
    crore = _CRORE.match(text)
    if lakh:
        value = float(lakh.group(1)) * 100_000
    elif crore:
        value = float(crore.group(1)) * 10_000_000
    else:
        integer_part, _, decimal_part = text.partition(".")
        integer_part = integer_part.strip()
        if "," in integer_part:
            if _INDIAN_GROUPING.match(integer_part):
                pass  # 4,50,000
            elif _WESTERN_GROUPING.match(integer_part):
                pass  # 450,000
            else:
                raise NormalisationError(f"ambiguous digit grouping in {raw!r}")
            integer_part = integer_part.replace(",", "")
        if not integer_part.isdigit():
            raise NormalisationError(f"cannot parse amount {raw!r}")
        if decimal_part and not decimal_part.isdigit():
            raise NormalisationError(f"cannot parse amount {raw!r}")
        if decimal_part and len(decimal_part) > 2:
            raise NormalisationError(f"more precision than paise in {raw!r}")
        paise = int(integer_part) * 100 + int((decimal_part or "0").ljust(2, "0"))
        return -paise if negative else paise

    paise = round(value * 100)
    return -paise if negative else paise


# ------------------------------------------------------------------------- dates


def normalise_date(raw, *, fmt: str = "%d/%m/%Y") -> date:
    """Parse with an explicit format.

    `01/02/2026` is ambiguous and no amount of cleverness resolves it. The format
    belongs to the connection, and the interpretation is shown in the preview so
    a human confirms before anything commits.
    """
    if raw is None or not str(raw).strip():
        raise NormalisationError("empty date")
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw

    text = str(raw).strip()
    for candidate in (fmt, "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, candidate).date()
        except ValueError:
            continue
    raise NormalisationError(f"cannot parse date {raw!r} with format {fmt!r}")


# ------------------------------------------------------------------------- names


@dataclass(frozen=True)
class NormalisedName:
    name: str
    honorific: str | None


def normalise_name(raw) -> NormalisedName:
    """Trim, collapse whitespace, lift honorifics out. Never 'correct' a spelling —
    a debtor's name is not ours to improve."""
    if raw is None or not str(raw).strip():
        raise NormalisationError("empty name")

    text = re.sub(r"\s+", " ", str(raw).strip())
    honorific = None
    for candidate in _HONORIFICS:
        pattern = re.compile(rf"^{re.escape(candidate)}\.?\s+", re.I)
        match = pattern.match(text)
        if match:
            honorific = match.group(0).strip().rstrip(".")
            text = text[match.end():].strip()
            break

    if not text:
        raise NormalisationError(f"name is only an honorific: {raw!r}")
    return NormalisedName(name=text, honorific=honorific)
