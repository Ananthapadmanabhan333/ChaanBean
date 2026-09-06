"""The ledger, and the one place that decides what a dead invoice means.

Three modules filter invoices by status and they do **not** agree. That
divergence used to be three independent literals; it is one decision here,
because getting it wrong in either direction is expensive:

* ``CANCELLED`` means the invoice should never have stood. It is void — not a
  movement between the two parties at all — so it is absent from every view.
* ``WRITTEN_OFF`` means the creditor has stopped expecting the money. That is an
  accounting decision on this side of the trade. It does not undo the sale and
  it does not extinguish the debt; the buyer's own books still carry the
  payable. So a written-off invoice vanishes from the collections views
  (``ageing``, ``allocation`` — "what are we chasing", "where can money land")
  and **stays on the statement**.

The statement half is the one that has to be got right. A statement is
reconstructed for a past period out of today's rows, so filtering on today's
status rewrites history: drop written-off invoices and a February statement
stops showing an invoice that was plainly live in February — in a document that
was already sent to the debtor and may be the evidence for a claim.
"""

from __future__ import annotations

from app.models import AccountStatus, InvoiceStatus

# Void — it never happened. Excluded from every view, statements included.
VOID_STATUSES = (InvoiceStatus.CANCELLED,)

# Not being pursued. Excluded from ageing and from anywhere money can land, but
# still a real debit on the statement.
UNCOLLECTABLE_STATUSES = (InvoiceStatus.CANCELLED, InvoiceStatus.WRITTEN_OFF)

# The account-side companion, and the reason it cannot simply be "SETTLED". A
# written-off account deliberately keeps the balance that is genuinely still
# owed — `recompute_invoice` does not zero it — so every reader that asks "what
# is outstanding" by excluding SETTLED alone reports an abandoned debt as live,
# and the credit check scores it as currently overdue.
CLOSED_ACCOUNT_STATUSES = (AccountStatus.SETTLED, AccountStatus.WRITTEN_OFF)

__all__ = ["CLOSED_ACCOUNT_STATUSES", "UNCOLLECTABLE_STATUSES", "VOID_STATUSES"]
