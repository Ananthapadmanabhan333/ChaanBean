# Provisioning checklist

Everything here has a vendor lead time and **none of it blocks the build**. All of
it blocks go-live. Start the top three the day you read this; they are the ones
measured in weeks, and discovering that at the end costs a quarter.

Nothing in this list changes application code. Each item ends in a credential or
an approval that turns on an adapter already written and tested.

---

## 1. SIP carrier — the long pole

Voice cannot reach a real phone until this exists. Asterisk is self-hosted and
already works; only PSTN termination is missing.

- [ ] Trunk contract signed
- [ ] KYC completed (in India this is a documentary process, not a form)
- [ ] DID provisioned
- [ ] Caller ID whitelisted — an unapproved CLI is rejected outright
- [ ] India tariff confirmed, per-minute and per-attempt

**Ask the carrier these five questions explicitly.** Each one changes
configuration, and each is expensive to discover during an incident:

1. **Auth mode** — IP allowlist or digest? Determines `pjsip.conf` shape.
2. **Dial-string format** — what exactly goes before the number?
   Sets `ARI_ENDPOINT_TEMPLATE`.
3. **Concurrent channel limit** — sets `Throttle.max_concurrent`.
4. **Calls per second limit** — sets `Throttle.calls_per_second`. Exceeding it
   earns throttling, and a throttled trunk fails calls the cap has already spent.
5. **Do they scrub DND at their end?** If yes, ours becomes a second layer rather
   than the only one. Do not remove ours either way.

Also confirm whether they support `P-Asserted-Identity` and what they do with
`From` — the classic cause of a rejected caller ID.

## 2. AWS

Only Polly and S3 need this. Both have working local equivalents today.

- [ ] Account created, billing alarms set
- [ ] Region `ap-south-1` (Mumbai) — latency and data residency
- [ ] Polly voice chosen for each language you will actually use.
      `Kajal` is **neural-only**, so `Engine="neural"` is mandatory
- [ ] Polly synthesis quota checked against your largest campaign. A campaign
      opening with 500 unique names hits the default rate limit in seconds
- [ ] S3 bucket created, **versioning enabled** — an audio asset is evidence of
      what was played, and an unversioned overwrite destroys that silently
- [ ] IAM user scoped to `polly:SynthesizeSpeech` and that one bucket

## 3. Messaging

- [ ] **TRAI DLT registration** — entity, then header, then each template.
      Templates need re-approval whenever the copy changes, so treat template
      management as a workflow rather than a settings field
- [ ] SMS provider account (Gupshup, Kaleyra, or equivalent)
- [ ] **WhatsApp: confirm with Meta that your collections use case is permitted
      _before_ designing around it.** Their messaging policies restrict debt
      collection. Products have been caught by this after building on it, and
      finding out late rewrites the channel plan
- [ ] Email sending domain, with SPF, DKIM and DMARC

## 4. Legal and compliance — the one that gates L3

The Policy Engine is built to **refuse** rather than improvise, so an unapproved
L3 template blocks the call. That is deliberate, and it means this list is not
optional paperwork: without it, L3 does not run.

- [ ] **Approved L1, L2 and L3 message text**, reviewed by counsel, in every
      language you will use
- [ ] A named `legal_approver` in the system who approves L3 versions. The role
      exists separately from `admin` on purpose
- [ ] Confirmed calling hours. The schema caps them at 08:00–19:00 and an
      operator cannot widen that; confirm the ceiling is right
- [ ] Confirmed frequency caps, per buyer per day and per week
- [ ] Current DND/TRAI obligations, and the DND scrub source
- [ ] Retention policy for call audio and event logs
- [ ] DPDP Act position on debtor data, including the third-party disclosure
      risk that the L3 keypress gate exists to manage

## 5. Infrastructure

- [ ] PostgreSQL with the three roles: `crp_app` (NOBYPASSRLS — the application),
      `crp_worker` (BYPASSRLS — cross-tenant workers), and an owner for DDL.
      **Verify `crp_app` is neither superuser nor BYPASSRLS**, or every RLS
      policy is decoration: `python -m app.db check` reports this
- [ ] Redis for idempotency guards and throttling
- [ ] Asterisk host, with ARI bound to loopback or a private subnet.
      ARI credentials are unrestricted call-origination authority — an
      internet-reachable ARI is toll fraud waiting to happen. Verify the bind
      with `ss -tlnp | grep 8088` rather than assuming
- [ ] `cdr_adaptive_odbc` writing CDR into PostgreSQL. This is out-of-band truth
      when the event websocket drops, and the evidence trail for L3 calls
- [ ] Backups, with a restore actually tested

## 6. Before the first real call

- [ ] `CONTACT_ALLOWLIST` populated in staging, and enforcement confirmed on
- [ ] One end-to-end call to a phone **you own**, listened to by a human
- [ ] `sngrep` or `tcpdump` capture kept from that call. When audio goes one-way
      against the carrier later, a wire-level capture of a working call is the
      only thing that tells you what changed
- [ ] Escalation thresholds in `EscalationPolicy` replaced with the numbers the
      business signed off. **The shipped values are placeholders**
