"""Merging approved template text with a debtor's data, and hashing the result.

Nothing here is clever, and that is deliberate. Template merge is deterministic
string interpolation: no model, no inference, no fallback. A legal call that says
"your outstanding of  is overdue" because a field was missing is worse than a
call that never happened, so an unknown or unfilled placeholder is an error.

The hash covers the voice configuration as well as the text, because changing
voice must miss the cache. Otherwise you serve last month's voice from a stale
asset and cannot explain what the debtor actually heard.
"""

from __future__ import annotations

import hashlib
import re
import string
import unicodedata
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from app.render.numbers import format_inr, paise_to_words

# Zero-width and bidirectional marks. Invisible, and they change the hash — two
# messages that look identical would otherwise generate two assets.
_INVISIBLE = dict.fromkeys(
    [0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0xFEFF], None
)

ALLOWED_FIELDS = frozenset(
    {
        "buyer_name",
        "amount_words",
        "amount_display",
        "invoice_ref",
        "days_past_due",
        "company_name",
        "due_date_words",
    }
)

_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


class RenderError(ValueError):
    """The message could not be rendered exactly. Never rendered approximately."""


@dataclass(frozen=True)
class MergeContext:
    buyer_name: str
    amount_paise: int
    invoice_ref: str
    days_past_due: int
    company_name: str
    due_date: date | None = None

    def as_fields(self) -> dict[str, str]:
        return {
            "buyer_name": self.buyer_name,
            "amount_words": paise_to_words(self.amount_paise),
            "amount_display": format_inr(self.amount_paise),
            "invoice_ref": self.invoice_ref,
            "days_past_due": str(self.days_past_due),
            "company_name": self.company_name,
            "due_date_words": (
                f"{self.due_date.day} {_MONTHS[self.due_date.month - 1]} {self.due_date.year}"
                if self.due_date
                else ""
            ),
        }


def placeholders(body: str) -> set[str]:
    return {
        field
        for _, field, _, _ in string.Formatter().parse(body)
        if field is not None
    }


def render(body: str, ctx: MergeContext) -> str:
    """Substitute every placeholder, or refuse."""
    found = placeholders(body)
    unknown = found - ALLOWED_FIELDS
    if unknown:
        raise RenderError(f"unknown merge field(s): {', '.join(sorted(unknown))}")

    fields = ctx.as_fields()
    empty = [f for f in found if not fields.get(f)]
    if empty:
        raise RenderError(f"no value for merge field(s): {', '.join(sorted(empty))}")

    text = body.format(**fields)
    if "{" in text or "}" in text:
        raise RenderError("rendered text still contains an unsubstituted brace")
    return text


def normalise(text: str) -> str:
    """Deterministic cleanup. The same logical message must produce the same bytes."""
    text = unicodedata.normalize("NFC", text)
    text = text.translate(_INVISIBLE)
    text = text.replace(" ", " ")
    text = re.sub(r"\s*([,.;:!?])", r"\1", text)  # no space before punctuation
    text = re.sub(r"([,.;:!?])(?=\S)", r"\1 ", text)  # one space after
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def message_hash(
    *,
    company_id: UUID | str,
    template_version_id: UUID | str,
    final_text: str,
    voice_id: str,
    engine: str,
    sample_rate: int,
) -> str:
    """SHA-256 over content *and* voice configuration *and* tenant.

    `company_id` is in the hash so two tenants with identical template text never
    share a stored object — otherwise one tenant's call evidence points at
    another tenant's file.
    """
    payload = "\x1f".join(
        [
            str(company_id),
            str(template_version_id),
            final_text,
            voice_id,
            engine,
            str(sample_rate),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
