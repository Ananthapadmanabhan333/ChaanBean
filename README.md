# ChaanBean

**Outbound credit recovery for the Indian B2B market.**

Invoices in. A deterministic escalation ladder out — reminder, call, firmer
notice, legal escalation — and a recorded, machine-readable **refusal** whenever
a contact should not happen.

Voice is **one-way and template-driven**: no speech recognition, no dialogue, no
language model in the call path, no GPU. That constraint is deliberate and it
shapes the whole system. The exact words said to a debtor are evidence, and
evidence has to be reproducible from a template id plus merge fields — not from
a sampled generation that cannot be replayed.

---

## Contents

- [What this is, and what it refuses to be](#what-this-is-and-what-it-refuses-to-be)
- [Run it in five minutes](#run-it-in-five-minutes)
- [The product surface](#the-product-surface)
- [How a contact actually happens](#how-a-contact-actually-happens)
- [The policy engine](#the-policy-engine)
- [Credit eligibility](#credit-eligibility)
- [Multi-tenancy and security](#multi-tenancy-and-security)
- [Identity](#identity)
- [Money and language](#money-and-language)
- [Architecture](#architecture)
- [Testing](#testing)
- [Deployment](#deployment)
- [What is not built](#what-is-not-built)
- [This branch](#this-branch)

---

## What this is, and what it refuses to be

The failure that ends a collections business is not a slow campaign. It is a
debtor demonstrating to a regulator that they were contacted after opting out,
and the operator being unable to prove otherwise.

Four rules follow, and they explain most of what looks unusual in this codebase:

| Rule | How it shows up in the code |
|---|---|
| **Every decision must be reconstructable** | Append-only event logs; a policy engine with no I/O; versioned scorecards |
| **Every refusal must name itself** | `BlockReason` is a closed enum in one file — 23 members, each with a test |
| **No generated text reaches a debtor** | Deterministic template merge; no LLM in the call path |
| **Fail closed** | Unknown DND blocks. A stale scrub blocks. Missing audio blocks. There is no default-allow path |

A system that dials whenever it is unsure is easy to build and impossible to
defend. This one refuses, and says why.

---

## Run it in five minutes

Nothing here needs a vendor account. The whole system runs on local adapters.

```bash
docker compose up -d postgres redis
cd backend && pip install -r requirements.txt
python -m app.db init      # schema, RLS policies, database roles
python -m app.seed         # 20 Indian buyers, including refusable cases
python -m app.demo         # run a campaign end to end
uvicorn app.main:app --port 8000
```

Then open **http://localhost:8000/**.

Sign in as `owner@acme.test` / `acme-demo-pass`. The `legal@acme.test` account
holds `legal_approver` and nothing else — approving legal content is not an
administrative privilege, and that role separation is real.

**Docker is not required.** It only runs Postgres and Redis. WSL Ubuntu with
`apt install postgresql-16 redis-server` works identically. Avoid managed cloud
Postgres for *development*: `crp_worker` needs `BYPASSRLS`, which requires
superuser, and Supabase/Neon do not grant it — you would lose the ability to run
the tests that prove tenant isolation. It is fine for *hosting*, where the app
connects as the unprivileged `crp_app`.

### What the demo shows

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

---

## The product surface

A multi-tenant operations console at `/`, served as a **single HTML file** from
the same origin as the API. No build step, no bundler, no framework.

| View | What it is for |
|---|---|
| **Overview** | Outstanding, ageing by bucket, the contact funnel, the escalation ladder, and the top refusal reasons |
| **Credit buyers** | Everyone who owes you money. Add a buyer with their numbers and an opening invoice; run a credit check |
| **Campaigns** | Create, enrol, start and pause; per-campaign progress |
| **Refusals** | Every block, with the reason that caused it and a plain-English explanation |
| **Calls** | Placed, connected, delivered, acknowledged — four separate facts, never averaged |
| **Messages** | SMS, WhatsApp and email with per-channel delivery state |
| **Accounts** | The ledger, in lakh and crore |
| **Company** | Your own profile — the creditor of record a debtor is told is calling — plus caller IDs and their carrier-approval state |

### Adding a credit buyer

Phone numbers are normalised to E.164 however they are typed — `098765 43210`
and `+91-44-2851-4000` both become dialable. A number that cannot be parsed
**rejects the whole buyer**, because a buyer saved with the bad number silently
dropped is one who is never called, and nobody finds out until the campaign has
already run.

A new number is recorded as **`DND unknown`**, never as clear. Consent is not
something a form can assert; it comes from a scrub, and until then the policy
engine will not dial it.

Money is typed the way people type it. `4,50,000` is four lakh fifty thousand —
a parser assuming Western grouping reads it as forty-five thousand, an order of
magnitude out, in a number that ends up in a legal notice.

---

## How a contact actually happens

```
Scheduler tick
   ↓  claim due targets — SELECT … FOR UPDATE SKIP LOCKED
Build policy context — from stored rows only
   ↓
Policy engine ──────────────► BLOCKED + a named reason (no contact attempted)
   ↓ allowed
Escalation level
   ├── L1 courtesy  → SMS / WhatsApp        (cheapest channel first)
   ├── L2 firm      → voice call
   └── L3 legal     → requires a prior *delivered* contact at L2
                      AND an approved template version
                      AND plays only after a keypress
   ↓
Append to the event log → project to Call / Message → reports
```

**Why L3 requires prior delivered contact.** Escalating to legal language
because three calls went unanswered is unfair and evidentially weak — an
unanswered call is not evidence the debtor knows about the debt. The ladder
advances on delivery, not on attempts.

**Why L3 plays only after a keypress.** Playing a debt amount to whoever picks
up a shared office line is a third-party disclosure problem.

---

## The policy engine

`app/policy/engine.py` is a **pure function** from context to decision. No
database, no network, no clock of its own — the evaluation time is passed in.
An import-linter contract enforces this: the module may not import `app.db`,
`app.providers`, or even `sqlalchemy`, and CI fails if anyone tries.

That purity is what makes every production decision reproducible from stored
context, which is the difference between an auditable system and an opinion.

A representative slice of the 23 refusal reasons:

| Reason | Fails closed because |
|---|---|
| `CONSENT_WITHDRAWN` | Absolute. No override path exists, for any role |
| `DND_UNKNOWN` | Never scrubbed is not the same as clear |
| `DND_CHECK_STALE` | A scrub older than 7 days is not current |
| `L3_TEMPLATE_NOT_APPROVED` | Legal wording with no recorded approval is not legal wording |
| `QUIET_HOURS` | Outside permitted contact hours, in the campaign's own timezone |
| `AUDIO_MISSING` | A silent call to a debtor is worse than no call |
| `ALLOWLIST_MISS` | Non-production must never dial a real number |

The suite asserts that **every** enum member has a case, and fails when a new
member is added without one. Each guard has been watched to fail with the guard
removed — a test that has never failed proves nothing.

---

## Credit eligibility

Any buyer can be assessed for a credit limit. The engine is a **transparent
weighted scorecard**, not a learned model.

That is a deliberate choice. Every output has to survive the question *"why did
you refuse them?"* — asked by your customer, by the buyer, or by a regulator. A
scorecard answers by construction: each factor states its value, its weight and
its contribution in points; the same inputs always produce the same output; and
`MODEL_VERSION` records which rules ran, so a decision made in March can be
reconstructed in December. An opaque model can do none of that, and for a credit
decision that is disqualifying rather than merely inconvenient.

Three properties worth knowing:

**It recommends; it never decides.** The vocabulary is
`STRONG · ACCEPTABLE · CAUTION · REFER` — never APPROVE/REJECT. A person records
the limit they grant, through a separate endpoint under a separate permission,
and the suggestion is stored alongside the decision. *Where the two disagree is
the audit trail.*

**Arrears override a high score.** A long history and strong turnover produce a
high number. None of that makes it sensible to extend more credit to someone who
has not paid what they already owe you — and averaged into a score, that fact
disappears. Arrears, a 90-day item and a HIGH risk band are hard blockers that
force `REFER` and a zero ceiling regardless of score.

**Declared figures are capped.** What the buyer owes *you* is read from your
ledger, never from the submitted form — letting an applicant supply their own
payment history would defeat the check. Declared turnover carries half weight
unless someone attests to having seen a GST return or bank statement, because
otherwise the way to unlock a large limit is to type a large number.

---

## Multi-tenancy and security

Isolation is enforced by **PostgreSQL Row-Level Security**, not by `WHERE`
clauses in application code. A forgotten predicate is a cross-tenant leak; a
forgotten policy fails closed.

```
Request → verified token → Principal (company_id from claims only)
   ↓ transaction opens
set_config('app.company_id', …, true)     ← transaction-scoped
   ↓
Query — with no tenant predicate in the SQL
   ↓
RLS policy: company_id = current_setting('app.company_id')
   ↓
Only this tenant's rows
```

**Three database roles:**

| Role | RLS | Used by |
|---|---|---|
| `crp_app` | **NOBYPASSRLS** | The application, on every tenant request |
| `crp_worker` | BYPASSRLS | Cross-tenant workers; token→tenant resolution |
| `crp` | superuser | DDL and migrations only |

The application **must not** connect as the table owner. `FORCE ROW LEVEL
SECURITY` is set on every tenant table, because without it the owning role
bypasses the policy silently — and a superuser bypasses RLS even with FORCE.
This was found the hard way: RLS was completely inert while the app connected as
the bootstrap superuser, and every policy was decoration.

`set_config(..., true)` is transaction-scoped rather than `SET LOCAL`, which
takes no bind parameters. That also makes the design **safe behind a
transaction-mode connection pooler** — a pooled connection cannot leak a tenant
into the next request.

The policy uses `NULLIF(current_setting('app.company_id', true), '')::uuid`, so
an unbound session sees **no rows** rather than all rows.

Isolation is tested in **both directions**: the row is invisible to the other
tenant, *and* demonstrably present when read past the policy — because a test
that only asserts absence also passes when the table is empty.

---

## Identity

Two interchangeable backends selected by `AUTH_BACKEND`: **local** (bcrypt +
our own JWTs) or **supabase**.

Supabase answers *who this person is*. It does **not** answer what they may do
or which tenant they belong to. A Supabase JWT carries `user_metadata`, and
`user_metadata` is writable by the user through the client SDK — so a token
claiming `user_metadata.role = "legal_approver"` proves nothing. If
authorization read from there, any signed-up user could approve L3 legal content
by editing their own profile.

Exactly two claims are taken from the token: the verified `sub` and `email`.
Roles and tenancy come from our own tables. This is mutation-tested.

A JWKS fetch failure is **503** (our outage — retry) while a token matching no
published key is **401** (re-authenticate). Collapsing the two makes a client
with a stale token retry forever instead of signing in again.

### Roles

| Role | Notably holds | Notably lacks |
|---|---|---|
| `viewer` | read | every write |
| `operator` | buyers, imports, campaigns, credit *checks* | granting credit; approving L3 |
| `admin` / `owner` | users, API keys, settings, credit *decisions* | **approving L3 legal content** |
| `legal_approver` | approving L3 content, audit read | everything else |

`TEMPLATE_APPROVE_L3` belongs to no administrative role, **including owner**.
Approving the words said to a debtor is a legal act, not an administrative
privilege — so the person who can grant themselves permissions still cannot
approve legal content.

---

## Money and language

**Money is integer paise everywhere**, with an explicit field name
(`amount_paise`) — never a float, never a bare `amount`. Floats lose paise, and
lost paise in a legal notice is a wrong number in a legal notice.

**Indian digit grouping is a correctness issue, not a formatting preference.**
Parsing lives in exactly one place (`ingestion/normalise.py`) and rendering in
exactly one place (`render/numbers.py`): `₹42,00,000`, never `₹4,200,000`;
"forty-two lakh", never "four million".

---

## Architecture

A **modular monolith**, not microservices. Module boundaries are enforced in CI
by import-linter — the build fails on a violation, because a boundary that is
only a convention stops being a boundary in week three.

Three contracts:

1. **Domain modules are layered** — imports flow downward only, from `app.api`
   through `app.scheduler`, `app.telephony`, `app.calls`, `app.comms`,
   `app.ingestion`, `app.render`, `app.tts`, `app.storage`, `app.policy`,
   `app.trade`, `app.identity`, down to `app.models`.
2. **Providers depend on nothing in the domain** — which is what makes a vendor
   swap a configuration change rather than a refactor.
3. **The policy engine touches no I/O.**

Full treatment with diagrams: **[docs/architecture.md](docs/architecture.md)** —
context and container diagrams, the enforced module graph, the call lifecycle
and its three reconciliation layers, the data model, the tenancy model, failure
modes, and a 14-entry decision log recording what was rejected and why.

### Voice, and why events are the source of truth

`call_events` is authoritative; the `Call` row is a **projection** of it.

ARI has no event replay, so events arrive late, duplicated, out of order, or
never. The projection is idempotent, order-independent and forward-only, and a
test proves a `Call` row can be rebuilt identically from its event log alone.

Three independent reconciliation layers cover the gaps — reconnect diff, a stale
sweeper, and Asterisk's own CDR. When all three come up empty the call resolves
to **`UNKNOWN`**, not `NO_ANSWER`: `NO_ANSWER` is a positive claim that the phone
rang and nobody picked up, and the system cannot prove the phone did not ring.
The attempt still consumes the debtor's frequency budget, because the
alternative is retrying someone who may already have been called.

Carrier faults, by contrast, do **not** consume that budget — a 503 from the
trunk is our problem, not evidence the debtor was contacted.

### Concurrency

| Mechanism | Guarantees |
|---|---|
| `SELECT … FOR UPDATE SKIP LOCKED` | Two workers never claim the same target |
| Derived idempotency keys | A retry cannot double-dial — for voice the key is the ARI `channelId` |
| Unique index on `calls.idempotency_key` | Durable duplicate protection |
| Redis token bucket (Lua) | The CPS ceiling holds across any window, not just aligned ones |
| Per-tenant channel cap | One tenant's 5,000-row campaign cannot starve everyone else |

The rate limiter is a token bucket rather than a fixed window, because a fixed
window lets through up to 2× the rate across a boundary — five calls at
12:00:00.9 and five more at 12:00:01.0 is ten calls inside one second. Against a
carrier that caps CPS that is the burst that gets a trunk throttled, and it is
invisible in testing because it only happens when a batch straddles a second.

When Redis is unavailable the limiter **fails closed**: origination is refused.
A telephony system that keeps dialling after losing track of how fast it is
dialling is the one that gets its trunk suspended.

---

## Testing

```bash
cd backend && pytest -q && lint-imports
```

**354 tests, 3 import-linter contracts.** The ones that matter most, each
watched to fail with its guard removed:

- every `BlockReason` has a case, and the suite fails when a new one is added
  without one
- an unapproved L3 template is refused
- RLS isolation proven in both directions
- two concurrent workers, one buyer at a cap of one → exactly one call
- two concurrent payments against one invoice → no double allocation
- a call is rebuilt identically by replaying its event log
- a buyer already in arrears gets `REFER` and a zero credit ceiling despite a
  score above 90

> If `pytest` reports *skipped* rather than *passed*, the database is
> unreachable and the run proved nothing. That is exactly the failure mode the
> CI job on this branch exists to catch.

### Verified against real Asterisk

`spike/call_test.py` places a genuine SIP call through Asterisk 20.9.3 — not the
stub — and asserts what a stub cannot:

```
3. originating a real SIP call        HTTP 200
4. idempotency: same channelId again  HTTP 409 — REFUSED (correct)
5. playing the generated audio        PlaybackFinished received: True
7. StasisStart yes  PlaybackStarted yes  PlaybackFinished yes  StasisEnd yes
```

That last block catches "we wrote the wrong audio format", which otherwise
reaches a debtor as static on a legal call.

One nuance it surfaced: ARI's 409 only holds **while the channel is live**. Once
destroyed the id is reusable — so 409 is a concurrent-duplicate guard, not a
durable record. Durability comes from the unique `calls.idempotency_key`.

---

## Deployment

See **[docs/deployment.md](docs/deployment.md)**. The short version:

Vercel runs the **API and portal only**. Two components cannot run on
serverless, and a Vercel-only deployment looks healthy while placing no calls:

- `app/worker.py` is a tick loop — serverless is request-scoped, so nothing
  advances the ladder
- `app/telephony/events.py` holds a persistent websocket to Asterisk, and call
  events are the source of truth

Asterisk itself needs UDP/RTP and a stateful host. For the whole product on one
platform, use Railway, Render or Fly.io, which run the API *and* the worker from
the existing `docker-compose.yml`.

---

## What is not built

ASR · conversational AI · GPU · predictive dialling · Kafka/ClickHouse ·
Kubernetes · mobile app · **live PSTN calling** (blocked on carrier
provisioning, not on code).

Company intelligence, risk scoring, legal matching, pre-legal assessment, the
registry and skip-tracing are **built against fixture providers**: the
interfaces and the decision logic are complete and tested, and the licensed data
sources drop in behind them.

- [docs/architecture.md](docs/architecture.md) — the system, in full
- [docs/deployment.md](docs/deployment.md) — hosting, and what Vercel can and cannot run
- [docs/api-sourcing.md](docs/api-sourcing.md) — where to get every API, free tier first
- [docs/provisioning-checklist.md](docs/provisioning-checklist.md) — what to start today
- [docs/going-live.md](docs/going-live.md) — the switch-over
- [docs/runbook.md](docs/runbook.md) — what breaks and what to do
- [docs/supabase-auth.md](docs/supabase-auth.md) — the auth wiring

---

## This branch

`security-hardening` carries work that is **written and compiling but not yet
executed against a database**, because the local Postgres was down when it was
produced. Read this section before merging.

**1. RLS retrofit — the significant one.** An audit found that of 57 tenant
tables, only `credit_assessments` carried an RLS policy created by its own
migration. The other 56 were protected only when someone remembered to run
`python -m app.db rls` by hand. A database built from `alembic upgrade head`
alone therefore had **no tenant isolation on 56 tables**. Migration
`d1a4f7c2e8b6` applies `ENABLE` + `FORCE ROW LEVEL SECURITY` and a
`tenant_isolation` policy to every one of them, with role-guarded grants.

**2. The audit log becomes genuinely append-only.** `app/db.py` granted
`crp_app` UPDATE and DELETE on every table including `audit_log`, and re-granted
them each time the RLS command ran — so the trail that would be the defence in a
DPDP or defamation complaint was editable by the application role. Now revoked,
with a trigger behind it.

**3. Auth hardening.** Signed expiring invitation tokens (previously a tenant
admin could pre-seed any email address and capture that person's first sign-in
into their own tenant), `tokens_valid_from` for real session revocation,
company-scoped email lookups, and API-key scope validation.

**4. CI.** The repository had **no** `.github/workflows` at all, so the RLS test
had never executed anywhere but one laptop. The new pipeline runs Postgres and
Redis as services and fails — rather than skips — when the database is missing.

### Before merging

```bash
cd backend
alembic upgrade head        # the RLS retrofit must apply cleanly
pytest -q                   # all 354 must pass, not skip
lint-imports
```

One builder made a judgement call worth reviewing: `crp_worker` keeps `DELETE`
on `audit_log`, because it traced that the test-suite teardown connects as that
role and a revoke would fail teardown at the ACL check before any trigger fired.
The trigger exempts roles carrying `rolsuper` or `rolbypassrls`.
