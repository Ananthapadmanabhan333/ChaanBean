# Going live

Every vendor sits behind an adapter with a working local implementation. Going
live is a settings change plus credentials — not a development phase. That was
the point of the architecture, and this document is the proof of it.

Work down the list. Each step is independently reversible.

---

## The switches

| Setting | Local (today) | Production | What changes |
|---|---|---|---|
| `TTS_BACKEND` | `local` | `polly` | `app/tts/polly.py` replaces espeak/piper |
| `STORAGE_BACKEND` | `local` | `s3` | `app/storage/s3.py` replaces the filesystem |
| `TELEPHONY_BACKEND` | `fake` | `asterisk` | the stub ARI server is replaced by the real one |
| `SMS_BACKEND` | `fake` | provider | `FakeProvider` replaced per channel |
| `WHATSAPP_BACKEND` | `fake` | provider | — |
| `EMAIL_BACKEND` | `fake` | provider | — |

No other code changes. If a step here requires editing application logic,
something has drifted from the design and that is worth stopping to fix.

---

## 1. Speech (needs AWS only)

```bash
TTS_BACKEND=polly
AWS_REGION=ap-south-1
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
DEFAULT_VOICE_ID=Kajal
```

Then **regenerate, do not reuse**. The message hash includes voice and engine, so
switching backends misses the cache by design — you will not serve espeak audio
from a stale row. Existing assets stay valid for the calls that already played
them, which is what makes them evidence.

Verify one asset before a campaign: check `duration_ms == byte_size / 16` and
listen to it.

## 2. Storage (needs AWS only)

```bash
STORAGE_BACKEND=s3
S3_BUCKET=crp-audio-prod
```

Enable **bucket versioning** first. Copy existing assets across, or let them
regenerate — either is fine, but decide deliberately rather than discovering
half the campaign has no audio.

## 3. Telephony (needs the carrier)

This is the only step that waits on someone else.

1. Uncomment the `vobiz-trunk` block in `asterisk/conf/pjsip.conf` and fill in
   the credentials the carrier gave you.
2. Set the dial string the carrier specified:
   ```bash
   TELEPHONY_BACKEND=asterisk
   ARI_ENDPOINT_TEMPLATE=PJSIP/{number}@vobiz-trunk
   ARI_BASE_URL=http://127.0.0.1:8088/ari
   ARI_PASSWORD=<a real one>
   ```
3. Set the throttles to the carrier's stated limits, below their ceiling:
   ```python
   Throttle(redis, calls_per_second=<theirs>, max_concurrent=<theirs minus headroom>)
   ```
4. On a cloud VM, set `external_media_address` and `external_signaling_address`
   in `pjsip.conf`. Asterisk otherwise advertises the private IP in SDP and the
   call connects with **one-way audio** — it rings, it answers, and the debtor
   hears nothing.
5. Insert a `caller_ids` row with `carrier_approved=true`. The dispatcher refuses
   to originate with an unapproved CLI, which is the failure the carrier would
   otherwise hand you as a rejected call.

### Verify in this order

```bash
ss -tlnp | grep 8088          # ARI must be loopback or private. Look, do not assume.
curl -u crp:<pw> http://127.0.0.1:8088/ari/asterisk/info
```

Then one call to a phone you own, with `sngrep` running. Keep the capture.

## 4. Messaging

Per channel, once its approvals exist. SMS additionally needs each template's
`dlt_template_id` set — the Policy Engine refuses an unregistered SMS template
with `SMS_TEMPLATE_NOT_REGISTERED` rather than letting the provider reject it at
send time.

## 5. The allowlist — do this last, and deliberately

Outside production, the system refuses to contact anyone not on
`CONTACT_ALLOWLIST` whenever a real delivery backend is configured. In staging
with a live trunk, that guard is the only thing between a test run and the
production debtor list.

```bash
CONTACT_ALLOWLIST=+919812345678,+919887654321
CONTACT_ALLOWLIST_ENFORCED=true
```

It switches off only by setting `ENV=production`. That is intentional: there is
no flag that quietly disables it in staging.

## 6. Before the first campaign against real debtors

- [ ] `python -m app.db check` — every tenant table has enforced RLS, and
      `crp_app` is neither superuser nor BYPASSRLS
- [ ] `pytest` green against the production configuration
- [ ] `EscalationPolicy` thresholds replaced with the business's signed-off
      numbers. **What ships are placeholders**: 7 days to L1, 7 more to L2,
      14 more to L3, 3 attempts per level, ₹25,000 L3 floor
- [ ] L1 and L2 templates approved; L3 approved by a named `legal_approver`
- [ ] Calling hours confirmed against the current TRAI position
- [ ] One human has listened to a real call end to end
- [ ] The scheduler runs as its own process:
      `python -c "from app.db import SessionLocal; from app.scheduler.tick import run_forever; run_forever(SessionLocal)"`

---

## What stays the same

Worth stating plainly, because it is the return on the adapter work: the Policy
Engine, the ledger, allocation, ageing, the escalation ladder, the event log, the
projection, the reconciler, idempotency and every test go to production
**unchanged**. They were never talking to a vendor.
