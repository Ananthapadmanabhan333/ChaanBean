"""Indian numbering.

A debt spoken as "four million rupees" to an Indian debtor is not merely odd, it
is a number they have to stop and convert — on a call that is already
adversarial. The Indian system groups by thousand, then lakh (10^5), then crore
(10^7), and the digit grouping follows: 2, 2, then 3 from the right.

Written directly rather than via a general-purpose library, because the common
ones default to the Western short scale and produce "four million" — which is
precisely the bug this module exists to prevent.

A note on the source spec: it gives `4200000` as "forty-two lakh" while also
calling it paise. Those cannot both hold — 4200000 paise is 42,000 rupees. The
intent is clearly the *reading of the digits*, so this module keeps the two
apart: `rupees_to_words(4_200_000)` is "forty-two lakh", and `paise_to_words`
converts first and says so.
"""

from __future__ import annotations

_UNITS = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
)
_TENS = (
    "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety",
)

CRORE = 10_000_000
LAKH = 100_000


def _two_digits(n: int) -> str:
    if n < 20:
        return _UNITS[n]
    tens, units = divmod(n, 10)
    return _TENS[tens] if units == 0 else f"{_TENS[tens]}-{_UNITS[units]}"


def _three_digits(n: int) -> str:
    hundreds, rest = divmod(n, 100)
    parts = []
    if hundreds:
        parts.append(f"{_UNITS[hundreds]} hundred")
    if rest:
        parts.append(_two_digits(rest))
    return " ".join(parts)


def rupees_to_words(rupees: int) -> str:
    """Read an integer in the Indian system: crore, lakh, thousand, hundred."""
    if rupees < 0:
        return f"minus {rupees_to_words(-rupees)}"
    if rupees == 0:
        return "zero"

    parts: list[str] = []
    crore, rest = divmod(rupees, CRORE)
    if crore:
        # Beyond 99 crore the unit repeats — 1000 crore, not a new name. That is
        # how the number is actually said.
        parts.append(f"{rupees_to_words(crore)} crore")
    lakh, rest = divmod(rest, LAKH)
    if lakh:
        parts.append(f"{_two_digits(lakh)} lakh")
    thousand, rest = divmod(rest, 1000)
    if thousand:
        parts.append(f"{_two_digits(thousand)} thousand")
    if rest:
        parts.append(_three_digits(rest))
    return " ".join(parts)


def paise_to_words(paise: int) -> str:
    """Speak a paise amount as rupees and paise."""
    if paise < 0:
        return f"minus {paise_to_words(-paise)}"

    rupees, remainder = divmod(paise, 100)
    if rupees == 0 and remainder == 0:
        return "zero rupees"

    parts = []
    if rupees:
        parts.append(f"{rupees_to_words(rupees)} {'rupee' if rupees == 1 else 'rupees'}")
    if remainder:
        parts.append(f"{_two_digits(remainder)} {'paisa' if remainder == 1 else 'paise'}")
    return " and ".join(parts)


def group_indian(n: int) -> str:
    """Digit grouping: 2, 2, then 3 from the right. 4200000 -> 42,00,000."""
    sign = "-" if n < 0 else ""
    digits = str(abs(n))
    if len(digits) <= 3:
        return sign + digits
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return sign + ",".join(groups + [tail])


def format_inr(paise: int) -> str:
    """Display form. ₹42,00,000 — never ₹4,200,000."""
    sign = "-" if paise < 0 else ""
    rupees, remainder = divmod(abs(paise), 100)
    body = group_indian(rupees)
    if remainder:
        return f"{sign}₹{body}.{remainder:02d}"
    return f"{sign}₹{body}"
