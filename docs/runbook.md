# Operations runbook

What to check, what breaks, and what to do about it. Written for the person on
call at 2am who did not write this system.

---

## Processes

Three, and they fail independently. That is deliberate.

| Process | Command | If it stops |
|---|---|---|
| API | `uvicorn app.main:app` | The portal is down. **No debtor is affected.** |
| Scheduler | `python -m app.worker` | Nothing new is dispatched. In-flight calls still resolve. |
| ARI consumer | `python -m app.telephony.events` | Calls place but nothing observes them. **See below — this is the one that matters.** |

**Run exactly one ARI consumer.** Two clients on the same Stasis app get
duplicate or split event delivery depending on the Asterisk version, and neither
failure is visible from outside. Scale by adding schedulers, never consumers.

---

## Daily checks

```bash
python -m app.db check          # RLS on every tenant table; crp_app not BYPASSRLS
curl -s localhost:8000/health   # database, redis, and which backends are live
```

`db check` failing on the roles line is an emergency: it means tenant isolation
is inert and one customer can read another's debtor list.

---

## The failure modes, in order of how often they happen

### 1. Calls stuck in DIALING

**Cause.** The ARI websocket dropped. ARI has no event replay, so the hangup is
gone permanently — reconnecting does not recover it.

**What already handles it.** Three layers, in order: the reconnect diff (on
websocket reconnect), the stale sweeper (on a timer), and Asterisk's own CDR.

**What to do.** Nothing, for the first few minutes. Then:

```sql
SELECT id, status, originated_at FROM calls
WHERE status IN ('DIALING','ANSWERED') AND originated_at < now() - interval '10 minutes';
```

If rows persist past the stale threshold, the sweeper is not running — check the
scheduler process. Calls that cannot be resolved become `UNKNOWN` and **count
against the debtor's cap**, because you cannot prove the phone did not ring.

### 2. Nothing is being dialled

Almost always correct behaviour, not a fault. Look at the block reasons first:

```bash
curl -s localhost:8000/api/reports/full -H "Authorization: Bearer $TOKEN" | jq .blocks
```

The usual answers, in order of frequency:

| Reason | Meaning | Action |
|---|---|---|
| `DND_UNKNOWN` | Imported numbers start unscrubbed | Run a DND scrub. This is correct, not a bug |
| `OUTSIDE_CALLING_WINDOW` | Out of hours in the campaign timezone | Wait |
| `WEEKEND_NOT_PERMITTED` | It is Saturday | Wait |
| `DND_CHECK_STALE` | Scrub older than 7 days | Re-scrub |
| `L3_TEMPLATE_NOT_APPROVED` | No `legal_approver` has approved the L3 text | Get it approved. Do not work around this |
| `DAILY_CAP_REACHED` | Working as designed | Nothing |

### 3. One-way audio

The call connects and the debtor hears silence. This is a SIP/NAT problem, never
an application one.

1. `external_media_address` and `external_signaling_address` must be set in
   `pjsip.conf` on any cloud VM. Without them Asterisk advertises its private IP
   in SDP.
2. `direct_media = no`. With direct media Asterisk drops out of the path and
   cannot play anything.
3. Compare a `sngrep` capture against the one kept from the first working call.
   That capture is the only thing that tells you what changed.

### 4. Audio plays as noise, or not at all

The `.sln` is the wrong format or truncated.

```bash
python spike/call_test.py    # end-to-end: synthesis, staging, play, events
```

`AUDIO_NOT_STAGED` blocking calls is the system working: staging is a policy
gate, and a call whose audio is missing blocks rather than dialling and hoping.

### 5. Carrier rejects every call

Check, in this order: the caller ID is `carrier_approved` in `caller_ids`; the
dial string matches what the carrier specified (`ARI_ENDPOINT_TEMPLATE`); you
are within their concurrent-channel and CPS limits.

Repeated 503s mean you exceeded the concurrency cap. Lower
`Throttle.max_concurrent` — being throttled by a carrier takes hours to clear
and costs calls the frequency caps have already spent.

---

## Things you must not do

- **Do not disable the contact allowlist to "test in staging".** It is the only
  thing between a test run and the production debtor list.
- **Do not approve L3 content yourself to unblock a campaign.** The
  `legal_approver` role is separate on purpose.
- **Do not run a second ARI consumer.**
- **Do not `UPDATE calls SET status=...` to clear a queue.** `call_events` is the
  source of truth; edit the projection and the next replay overwrites you. If a
  call genuinely needs resolving, insert a worker event and let the projection
  do it.
- **Do not grant the application role BYPASSRLS** to fix a "missing data" bug.
  The missing data is RLS working.

---

## Recovery

**Rebuild a call from its events** (the projection is derived, so this is safe):

```python
from app.calls import projection
from app.db import admin_session
with admin_session() as s:
    projection.rebuild(s, s.get(Call, call_id))
```

**Restore.** `pg_restore`, then `python -m app.db rls` — RLS policies and roles
are not part of a table dump, and a restore without them leaves every tenant
table unprotected.

**Pause everything, immediately:**

```sql
UPDATE campaigns SET status = 'PAUSED';
```

The scheduler picks that up on its next tick. In-flight calls finish; nothing
new starts.
