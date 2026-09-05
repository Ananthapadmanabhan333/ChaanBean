# ChaanBean

Outbound credit recovery for the Indian market. Invoices in, deterministic
escalation, multi-channel contact ending in AI voice calls, and a refusal —
with a recorded reason — whenever a call should not happen.

Voice is **one-way and template-driven**: no speech recognition, no dialogue, no
LLM in the call path, no GPU. That constraint is deliberate. It keeps the system
auditable, cheap, and provable in a dispute.

---

## Run it

Nothing here needs a vendor account.

```bash
docker compose up -d postgres redis
cd backend && pip install -r requirements.txt
python -m app.db init      # schema, RLS policies, database roles
python -m app.seed         # 20 Indian buyers, including refusable cases
python -m app.demo         # run a campaign end to end
uvicorn app.main:app --port 8000    # then open http://localhost:8000/
```

Sign in as `owner@acme.test` / `acme-demo-pass`. The `legal@acme.test` account
holds `legal_approver` and nothing else — approving legal content is not an
administrative privilege.

## What the demo shows

The refusals, mostly. A demo where every buyer gets called proves nothing about
a system whose main job is knowing when not to:

```
contacted        : 7            (6 SMS, 1 voice)
refused          : 11
    7  L3_TEMPLATE_NOT_APPROVED     legal content with no recorded approval
    1  DND_UNKNOWN                  never scrubbed — fails closed
    1  DND_REGISTERED               on the registry
    1  DND_CHECK_STALE              scrub older than 7 days
    1  CONSENT_WITHDRAWN            absolute; beats every other gate
```

## Architecture

```
Portal / API
     ↓
FastAPI  ──  PostgreSQL (RLS) · Redis · local or S3 storage
     ↓
Scheduler (tick worker, SELECT … FOR UPDATE SKIP LOCKED)
     ↓
Policy Engine  ──→ blocked, with a machine-readable reason
     ↓ allowed
Approved template version + merge fields
     ↓
  SMS · WhatsApp · Email · Voice
                             ↓
              TTS → storage → staged on the Asterisk box
                             ↓
              Asterisk (ARI) → SIP trunk → PSTN
     ↓
call_events / message_events → projections → dashboards
```

Modular monolith, not microservices. Module boundaries are enforced in CI by
import-linter (`lint-imports`), not by convention.

**[docs/architecture.md](docs/architecture.md)** is the full treatment: context
and container diagrams, the enforced module graph, call lifecycle and
reconciliation, the tenancy and security model, and a decision log recording
what was rejected and why.

## The portal

A multi-tenant operations console at `/`, served as a single HTML file from the
same origin as the API — no build step, no bundler.

| View | What it is for |
|---|---|
| Overview | Outstanding, ageing, contact funnel, escalation ladder, refusal reasons |
| Credit buyers | Everyone who owes you money; add a buyer with their numbers and opening balance |
| Campaigns · Calls · Messages | What ran, what connected, what was delivered |
| Refusals | Every block, with the reason that caused it |
| Accounts | The ledger, in lakh and crore |
| Company | Your own profile — the creditor of record a debtor is told is calling |

Two things the console does that are worth calling out:

**Adding a credit buyer** normalises phone numbers to E.164 however they are
typed, and refuses the whole buyer if a number cannot be parsed — a buyer saved
with the bad number dropped is silently never called. A new number is recorded as
`DND unknown` until a scrub says otherwise, never as clear: consent is not
something a form can assert.

**Credit eligibility** (`Check credit` on any buyer) runs a transparent weighted
scorecard and returns a recommendation, a suggested ceiling, and every factor
that produced them. It recommends; a person decides, through a separate endpoint
under a separate permission, and both numbers are stored — where they disagree is
the audit trail. Arrears with you are a hard blocker that a high score cannot
average away. See [architecture §11](docs/architecture.md#11-credit-intelligence)
for why this is a scorecard rather than a model.

## The parts worth knowing about

**`app/policy/engine.py`** — pure. No database, no network, no clock of its own.
Every production decision is reproducible from stored context, which is the
difference between an auditable system and an opinion. It is the only place
`BlockReason` is defined, and every one of its 23 members has a test.

**`app/trade/allocation.py`** — a payment is not "against an invoice". Part
payment across several invoices is the norm in Indian B2B trade. `outstanding_paise`
is derived and has exactly one writer; two code paths adjusting a balance is how
ledgers drift, and a drifted ledger tells a debtor they owe money they have paid.

**`app/calls/projection.py`** — `call_events` is the source of truth and `Call`
is a projection of it. ARI has no event replay, so events arrive late, duplicated,
out of order, or never. The projection is idempotent, order-independent and
forward-only, and a test proves the row can be rebuilt from the log alone.

**`app/calls/reconciler.py`** — three independent layers, because any one of them
can miss: reconnect diff, stale sweeper, and Asterisk's own CDR. When all three
come up empty the call resolves to `UNKNOWN` and still counts against the cap —
you cannot prove the phone did not ring.

**`app/render/numbers.py`** — `4200000` is "forty-two lakh", never "four million",
and `₹42,00,000`, never `₹4,200,000`.

## Rules that shaped the code

1. **Fail closed.** Unknown DND blocks. A scrub older than 7 days blocks. Missing
   audio blocks. An unapproved L3 template blocks. There is no default-allow path.
2. **Every refusal names itself.** `BlockReason` is a closed enum in one file.
3. **No model output reaches a policy decision or a debtor.** Template merge is
   deterministic string interpolation.
4. **Money is integer paise.** Never float.
5. **L3 plays only after a keypress.** Playing a debt amount to whoever answered
   a shared office line is a third-party disclosure problem.
6. **L3 requires prior delivered contact.** Escalating to legal content because
   three calls went unanswered is unfair and evidentially weak.
7. **Row-Level Security, not `WHERE` clauses,** and the application connects as a
   role that is neither superuser nor `BYPASSRLS` — otherwise the policies are
   decoration.
8. **Idempotency keys are derived, never random.** For voice the key is the ARI
   `channelId`, so Asterisk answers 409 rather than dialling twice.
9. **Carrier faults do not consume the debtor's frequency budget.**
10. **Delivered is several separate facts** — connected, played, acknowledged.

## Tests

```bash
cd backend && pytest -q && lint-imports
```

354 tests. The ones that matter most, and which have each been watched to fail
with the guard removed:

- every `BlockReason` has a case, and the suite fails when a new one is added
  without one
- an unapproved L3 template is refused
- RLS isolation, proven in both directions — the row is invisible to one tenant
  *and* demonstrably present when read past the policy
- two concurrent workers, one buyer at a cap of one → exactly one call
- two concurrent payments against one invoice → no double allocation
- a call is rebuilt identically by replaying its event log

## Verified against real Asterisk

`spike/call_test.py` places a genuine SIP call through Asterisk 20.9.3 — not the
stub — and asserts what a stub cannot:

```
3. originating a real SIP call        HTTP 200
4. idempotency: same channelId again  HTTP 409 — REFUSED (correct)
5. playing the generated audio        PlaybackFinished received: True
7. StasisStart yes  PlaybackStarted yes  PlaybackFinished yes  StasisEnd yes
```

That last block is the check that catches "we wrote the wrong audio format",
which otherwise reaches a debtor as static on a legal call.

One nuance it surfaced, now documented in `app/telephony/ari.py`: ARI's 409 only
holds **while the channel is live**. Once destroyed, the id is reusable — so 409
is a concurrent-duplicate guard, not a durable record. Durability comes from the
unique `calls.idempotency_key`.

## Not built

ASR · conversational AI · GPU · predictive dialling · Kafka/ClickHouse ·
Kubernetes · mobile app · **live PSTN calling** (blocked on carrier provisioning,
not on code).

Company intelligence, risk scoring, legal matching, pre-legal assessment, the
registry and skip-tracing are **built against fixture providers**: the interfaces
and the decision logic are complete and tested, and the licensed data sources
drop in behind them.

- [docs/architecture.md](docs/architecture.md) — the system, in full
- [docs/deployment.md](docs/deployment.md) — hosting, and what Vercel can and
  cannot run
- [docs/api-sourcing.md](docs/api-sourcing.md) — where to get every API, free
  tier first
- [docs/provisioning-checklist.md](docs/provisioning-checklist.md) — what to
  start today
- [docs/going-live.md](docs/going-live.md) — the switch-over
- [docs/runbook.md](docs/runbook.md) — what breaks and what to do
