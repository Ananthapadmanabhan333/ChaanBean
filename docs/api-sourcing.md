# Where to get every API — free tier first, then paid

Every external dependency sits behind an adapter with a working local
implementation, so **nothing here blocks development**. This is a procurement
order, not a build order.

> **Prices and free tiers change.** Every figure below is indicative and needs
> checking on the provider's own pricing page before you commit. Where a number
> matters to a decision, it is marked *verify*.

**Read this first:** the two items with multi-week lead times are the **SIP
trunk** and **TRAI DLT registration**. Start both this week regardless of where
the build is. Everything else can be obtained in a day when you need it.

---

## Order to actually do this in

| When | What | Why now |
|---|---|---|
| **Week 1** | SIP trunk KYC, TRAI DLT registration | Weeks of lead time, entirely outside your control |
| **Week 1** | Check WhatsApp policy for collections | If it is not permitted, your channel plan changes |
| Week 2 | AWS account, Polly, SES | Instant, but the free tier clock starts |
| Week 2–4 | GST / MCA verification API | Days to weeks depending on KYC |
| Month 2+ | Court records | Most expensive, least urgent |

---

## 1. Text to speech

**Currently using:** `espeak-ng` locally — free, unlimited, and sounds like a
1990s screen reader. Fine for testing, not for a debtor.

### Free

| Provider | Free tier | Indian voices | Notes |
|---|---|---|---|
| **AWS Polly** | 5M chars/month standard, **1M/month neural**, 12 months | Kajal, Aditi (Hindi/Indian English) | Already implemented. `Kajal` is neural-only |
| Google Cloud TTS | 4M chars/month standard, 1M WaveNet — **ongoing, not 12-month** | Strong Hindi, Tamil, Telugu, Bengali | Better ongoing free tier than AWS |
| Azure Speech | 0.5M chars/month | Good Indic coverage | |
| **Sarvam AI** | Trial credits | Indian-language specialist | Indian company, Indic-first — worth trying for regional voices |
| Piper (self-hosted) | Unlimited, free | Community Indic models | Runs on CPU. Better than espeak, no per-character cost ever |

### Paid, at volume

- **AWS Polly neural** ≈ $16 / 1M characters (*verify*). A 300-character
  reminder is roughly ₹0.40 per unique message.
- **Google WaveNet** ≈ $16 / 1M characters (*verify*).
- **Self-hosted Piper**: server cost only. At high volume this wins outright.

**What this codebase already does about cost:** audio is cached by
`(company_id, message_hash)`, so re-attempts cost nothing. Only genuinely unique
text is billed. 500 debtors with different names and amounts is 500
generations; calling those same 500 three times each is still 500.

**Recommendation:** start on Polly's free tier (already implemented, one settings
change). If TTS spend becomes visible on the bill, move to self-hosted Piper —
the `TtsBackend` seam means that is one class.

---

## 2. Voice calls — the long pole

**This is the one with real lead time, and the one with legal constraints.**

Indian telecom regulation means PSTN termination generally has to go through a
licensed Indian operator. A foreign SIP trunk terminating Indian domestic calls
is not a grey area you want to be in. **Use an Indian provider.** *(Confirm the
current position with counsel — this is regulatory, not technical.)*

### Free

There is no meaningful free tier for outbound Indian PSTN. What exists:

| Provider | Free | Reality |
|---|---|---|
| **Twilio** | ~$15 trial credit | Indian numbers need a regulatory bundle + KYC. Trial calls only to verified numbers |
| **Plivo** | Trial credit | Same shape |
| **Exotel / Ozonetel** | Demo, not self-serve | Indian, sales-led |
| **Asterisk + local SIP** | **Free, unlimited** | What you have now. Everything except PSTN |

You can build and demo the entire product on the last row — which is exactly
what `spike/call_test.py` proves.

### Paid — SIP trunk (recommended for this architecture)

You already run Asterisk, so you want a **SIP trunk**, not a CPaaS API. It is
substantially cheaper per minute and you keep call control.

| Provider | Notes |
|---|---|
| **Vobiz** | Named in the original plan |
| **Voxbeam / DIDForSale** | International wholesale, check India termination terms |
| **Knowlarity, Servetel/Acefone, MyOperator** | Indian, SIP trunk plus DIDs |
| **Tata Tele / Airtel IQ** | Enterprise, slow to onboard, best rates at scale |

Indicative Indian outbound: **₹0.30–0.70 per minute** (*verify*), plus a monthly
DID rental of a few hundred rupees.

### Paid — CPaaS (if you abandon Asterisk)

Twilio/Plivo/Exotel programmable voice, roughly **₹1.20–2.50/min** (*verify*) —
two to four times the trunk rate, in exchange for not running Asterisk. Given
Asterisk is already built and tested here, the trunk is the better trade.

### The five questions to ask any carrier

These change configuration, and each is expensive to discover during an incident:

1. Auth mode — IP allowlist or digest? *(shapes `pjsip.conf`)*
2. Exact dial-string format *(sets `ARI_ENDPOINT_TEMPLATE`)*
3. Concurrent channel limit *(sets `Throttle.max_concurrent`)*
4. Calls per second limit *(sets `Throttle.calls_per_second`)*
5. Do they scrub DND at their end? *(yours stays either way)*

---

## 3. SMS — needs TRAI DLT first

**You cannot send commercial SMS in India without DLT registration.** No provider
can sell you a way around it.

### The DLT step (do this in week 1)

Register on any operator's DLT portal — Jio, Airtel, VI or BSNL; registration is
shared across operators. One-time entity registration is around **₹5,000**
(*verify*). Then register your **header** (sender ID) and **each template**.

**Templates need re-approval whenever the copy changes.** That is why this
codebase treats template management as a workflow and refuses to send an
unregistered template with `SMS_TEMPLATE_NOT_REGISTERED` rather than letting the
provider reject it.

### Free

| Provider | Free tier |
|---|---|
| **MSG91** | Trial credits |
| **Fast2SMS** | Small free credit |
| Twilio | Part of the $15 trial |

### Paid

| Provider | Notes |
|---|---|
| **MSG91** | Popular with Indian SMBs, good docs |
| **Gupshup** | Also your WhatsApp BSP — one vendor for both |
| **Kaleyra** | Enterprise |
| **Textlocal, Netcore, Karix** | Established |

Transactional SMS: **₹0.12–0.25 per message** (*verify*). Promotional costs more
and has tighter rules — collections reminders are generally transactional, but
**confirm your template category with the provider**, because misclassification
gets templates rejected.

---

## 4. WhatsApp — check the policy before you build on it

> **Meta's Business Messaging Policy restricts debt collection.** Confirm your
> exact use case is permitted **before** making WhatsApp a primary channel.
> Products have been caught by this after building around it.

If permitted, it is the best channel in India: near-universal, high open rates,
supports documents.

### Free

Meta gives **1,000 free service conversations per month** (*verify* — Meta
changed this model in 2025). Business-initiated conversations are billed.

### Paid — you need a BSP

| BSP | Notes |
|---|---|
| **Gupshup** | Large Indian BSP; also does SMS |
| **AiSensy, Interakt, WATI** | SMB-friendly, low monthly minimum |
| **360dialog** | Cheap, close to Meta pricing, developer-oriented |
| **Twilio, Kaleyra** | Enterprise |

Cost is Meta's per-conversation rate (utility conversations in India are a few
paise to ~₹0.35, *verify*) plus the BSP's margin or platform fee.

---

## 5. Email

The cheapest channel by a wide margin, and useful evidence.

### Free

| Provider | Free tier |
|---|---|
| **Brevo** | 300 emails/day, ongoing |
| **SendGrid** | 100/day, ongoing |
| **Resend** | 3,000/month, 100/day |
| **AWS SES** | 3,000 message charges/month for 12 months |
| **Mailgun** | Trial |

### Paid

**AWS SES at ~$0.10 per 1,000 emails** (*verify*) is the cheapest at any volume,
and you are already in AWS for Polly. Set up SPF, DKIM and DMARC on day one —
collections email without them goes to spam, and a notice that lands in spam is
a notice you cannot prove was read.

---

## 6. GST verification

**Free-ish**

- The **GST portal** has a public "Search Taxpayer" page — free, no API,
  rate-limited, not for automation.
- **KnowYourGST, AppyFlow** — free tiers on GSTIN verification (*verify*).
- **RapidAPI** has several GSTIN-verification endpoints with small free tiers.
  Fine for a proof of concept, not for production volume.

**Paid**

| Provider | Notes |
|---|---|
| **Masters India** | GSP, full GST APIs including filing status |
| **ClearTax / Cygnet / Vayana** | GSPs, enterprise |
| **Signzy, SurePass, IDfy** | KYC platforms with GSTIN verification, easiest to start |
| **Perfios (formerly Karza)** | Broad Indian business-data coverage |

Roughly **₹1–5 per verification** (*verify*), with volume tiers. Filing-history
data — the useful signal for risk scoring — usually needs a proper GSP
relationship rather than a verification endpoint.

**Note on terms:** MCA and GST data often carry usage terms affecting whether
you may **resell derived reports**. If you plan to sell verification reports,
read those terms before you build the product around them.

---

## 7. MCA / company data

**Free**

- **MCA21 portal** — public search, per-document fees, no official API.
- **Zauba Corp, Tofler** — free web views, paid for bulk or API.

**Paid**

| Provider | Notes |
|---|---|
| **Probe42** | Best-known Indian company-data API; tiered plans |
| **Tofler, Signzy, SurePass, Perfios** | Company data plus directors and charges |

Roughly **₹5–50 per company report** (*verify*) depending on depth.

---

## 8. Court records — the hardest and most expensive

**Free**

- **eCourts** (`ecourts.gov.in`) — free public search across district courts.
  No official API. Scraping it is legally and practically fragile.
- **Indian Kanoon** — excellent judgment coverage, has an inexpensive API
  (per-query pricing, *verify*). Judgments, not pending-case status.

**Paid**

| Provider | Notes |
|---|---|
| **Perfios/Karza litigation check** | Structured, entity-matched |
| **Signzy** | Litigation screening |
| **Manupatra, SCC Online, CaseMine** | Legal research, less API-shaped |
| **Legalkart / Legistify** | Case-tracking services |

**Before you buy any of this:** the module in this codebase is built so that
*matching* is the hard part, not fetching. Court records identify parties by
clerk-typed name strings with no identifier, so buying a feed does not give you
a legal history — it gives you candidates a human must confirm. Budget for the
review workflow, not just the data.

---

## 9. Phone intelligence and DND

- **DND scrubbing**: via your DLT/telecom provider, or the carrier. Ask whether
  the SIP trunk provider scrubs at their end — several do.
- **Number validation**: `phonenumbers` (already used here) is free and handles
  format validity. For line-type and portability, **Twilio Lookup** is about
  $0.005–0.01 per lookup (*verify*).
- **Numverify** — 100 free lookups/month.

---

## 10. Hosting

**Free / cheap**

- **AWS free tier** — 12 months, `ap-south-1` (Mumbai) for latency and data
  residency.
- **Oracle Cloud** has a genuinely permanent free ARM tier — enough for
  Postgres plus the app.
- **Hetzner** — cheapest serious VMs, but EU-only, which is a **data-residency
  question for Indian debtor data**. Check with counsel before using it for
  production.

**Production shape**

| Piece | Sizing |
|---|---|
| App + scheduler | 2 vCPU / 4 GB |
| **Asterisk** | Its own VM, public IP, 2 vCPU. Do not co-locate — media is latency-sensitive |
| PostgreSQL | Managed (RDS `ap-south-1`) is worth the money for RLS-critical data |
| Redis | Small managed instance or same VM |

Realistically **₹8,000–20,000/month** for a small production deployment
(*verify*), dominated by the managed database.

---

## The genuinely free path

You can run the entire product, end to end, on this:

| Capability | Free option | Limit |
|---|---|---|
| TTS | Piper or espeak-ng self-hosted | None |
| Voice | Asterisk + softphone | No PSTN |
| SMS | — | Blocked on DLT, no way around it |
| Email | Brevo 300/day | 300/day |
| GST | Manual portal lookup | Not automatable |
| Hosting | Oracle Cloud free ARM | Modest |

That is enough to demo to customers, run pilots against your own numbers, and
prove the product. **The first thing you must actually pay for is the SIP
trunk**, and it is also the thing with the longest lead time — which is why it
is first on the list.

---

## Minimum viable paid setup

For a first real customer:

| Item | Indicative monthly |
|---|---|
| SIP trunk + 1 DID | ₹500 rental + usage |
| DLT registration | ₹5,000 one-off |
| SMS | ₹0.15 × volume |
| AWS (Polly + SES + hosting) | ₹5,000–10,000 |
| GST verification | ₹2/check × volume |
| **Total to start** | **≈ ₹15,000 one-off + ₹10,000/month** *(verify)* |

Court records and MCA data can wait until a customer asks for them and is
willing to pay — they are the most expensive inputs and the least load-bearing
for recovery itself.
