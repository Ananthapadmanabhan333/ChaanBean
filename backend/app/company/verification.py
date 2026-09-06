"""Is this debtor actually the company the operator says it is?

Everything downstream of this module is aimed at a *named* business: a dunning
call, a demand notice, a defaulter listing. Aim one at the wrong company and the
harm is not a bad row in a table — it is a real business receiving a demand for
somebody else's debt, and a defamation claim answering it. So this refuses more
often than it confirms, and every refusal says why in a sentence a person can
read out loud.

**The provenance rule.** A lookup comes back either from a registry backend or
from an operator who opened the government portal, read it, and typed what they
saw. The second is honest data entry and useful context, but it is a *claim
about* the business, not verification *of* it. Self-declared data that reaches
`Tier.IDENTIFIER` is self-declared data wearing a verification label — and the
label is what the listing, the notice and the call are issued against.

That rule is therefore enforced twice on purpose. `cap_tier_for_provenance`
demotes it on the way out of resolution, and `_assert_registry_backed` refuses
again at the point of writing a profile. Either alone would do the job; two mean
that deleting one is caught by a test rather than by a customer.

**What gets written.** Fetches are append-only: re-running in September leaves
March's record untouched, because the status the platform acted on in March has
to stay reconstructable when someone asks why a notice went out. A profile is
*resolved* — stamped with a tier, carrying registry fields — only at
`Tier.IDENTIFIER`. Below it this writes an `EntityCandidate` and stops, because
a machine that merges on a good name match is exactly how one company's tax
record ends up attached to another. An unresolved profile row may still exist:
the operator's declaration, or the empty row a report has to hang off so that a
run which found nothing is recorded rather than lost. Neither carries anything a
registry said, and both stay `self_declared`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.company.identifiers import validate_cin, validate_gstin
from app.company.resolution import (
    EntityInput,
    Signal,
    SourceRecord,
    Tier,
    best_match,
    hash_pan,
    pan_from_gstin,
    state_from_gstin,
    token_set_similarity,
)
from app.identity import audit
from app.models import (
    Buyer,
    CompanyProfile,
    EntityCandidate,
    GstRecord,
    McaRecord,
    ProviderFetch,
    VerificationReport,
)
from app.providers.base import GstBackend, McaBackend, is_registry_provenance

ACTION_VERIFIED = "company.verified"

# The one status on either register meaning "this entity is currently what it
# says it is". Cancelled, suspended, struck off and *unstated* are all reasons to
# stop, which is why this is an allowlist rather than a list of bad values.
ACTIVE = "ACTIVE"

_PROVIDER_GST = "gst"
_PROVIDER_MCA = "mca"


class BuyerNotFound(LookupError):
    """No such buyer in this tenant. Named so a route can turn it into a 404."""


class ProvenanceViolation(RuntimeError):
    """Self-declared data reached the point of being written as verified.

    Unreachable while `cap_tier_for_provenance` stands in front of it. It exists
    so that removing the cap fails loudly here instead of quietly publishing a
    claim as a finding.
    """


@dataclass(frozen=True)
class VerificationOutcome:
    tier: str
    confidence: float
    publishable: bool
    profile_id: UUID | None
    candidate_id: UUID | None
    gst_status: str | None
    mca_status: str | None
    signals: list[dict]
    blockers: list[str]


# ------------------------------------------------------------------ provenance


def cap_tier_for_provenance(tier: Tier, provenance: str | None) -> Tier:
    """Operator-supplied evidence may corroborate; it may never identify.

    STRUCTURAL is the ceiling because that is what the evidence honestly is: a
    consistent-looking identifier that nobody checked. `Tier.publishable` is true
    only at IDENTIFIER, so capping here is also what keeps the record out of
    anything published.

    The provenance test is `app.providers.base.is_registry_provenance`, which
    matches the REGISTRY literal and nothing else. The reference implementation
    labelled its hand-typed adapters "V0 Public-Data Mode"; a rule written as
    "not USER_PROVIDED" would have waved that straight through.
    """
    if is_registry_provenance(provenance):
        return tier
    return Tier.STRUCTURAL if tier is Tier.IDENTIFIER else tier


def _assert_registry_backed(consulted: _Consulted | None, tier: Tier) -> None:
    if tier is Tier.IDENTIFIER and (consulted is None or not consulted.registry_backed):
        raise ProvenanceViolation(
            "refusing to write a verified profile from evidence that was not fetched "
            "from a registry; the provenance cap has been removed or bypassed"
        )


# ---------------------------------------------------------------- small things


def _clean(value: str | None) -> str | None:
    text = (value or "").strip().upper()
    return text or None


def _shares_a_word(left: str | None, right: str | None) -> bool:
    """Do these two names have a single word in common, once normalised?

    Deliberately the weakest test that exists. Name similarity decides nothing
    here — that is the whole point of the tier ladder — but *zero* overlap
    between the buyer on the invoice and the company the register holds is the
    signature of a mistyped identifier, and an identifier match is exactly as
    confident about the wrong number as about the right one.
    """
    return token_set_similarity(left or "", right or "") > 0.0


def _status_severity(status: str | None) -> int:
    """Nothing said < currently active < anything else."""
    text = (status or "").strip().upper()
    if not text:
        return 0
    return 1 if text == ACTIVE else 2


def _worse_status(statuses: list[str | None]) -> str | None:
    """One column, two registers, and the worse news has to be what survives.

    A struck-off company with a live GSTIN is still struck off, and a cancelled
    GST registration on a company the MCA still lists as Active is still
    cancelled. Last-writer-wins keeps whichever register happened to be consulted
    second, which on the shipped fixtures silently erases Verma Steel Works'
    cancelled registration — and with it the heaviest registry factor the credit
    engine has.
    """
    return max(statuses, key=_status_severity, default=None)


def _jsonable(value: Any) -> Any:
    """JSONB takes no dates, decimals or UUIDs, and a fetch must survive storage."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return value


def _signal(name: str, value: str, supports: bool, sentence: str, weight: float = 0.0) -> dict:
    return {
        "name": name,
        "value": value,
        "supports": supports,
        "weight": weight,
        "sentence": sentence,
    }


def _mask_pan(pan: str) -> str:
    """A PAN is a national identifier and is held hashed everywhere else.

    `resolution.Signal` carries the derived PAN in the clear, which is right for
    an in-memory comparison and wrong the moment the signal is written to JSONB
    or handed to a screen — both of which happen here.
    """
    return f"{'X' * max(len(pan) - 4, 0)}{pan[-4:]}"


def _sentence_for(signal: Signal) -> str:
    if signal.name in ("gstin", "cin"):
        # Deliberately does not say "registry". The same signal is produced by a
        # record an operator typed, and a sentence that called that a registry
        # match would be the label this module exists to withhold.
        return (
            f"The declared {signal.name.upper()} {signal.value} and the record's are the "
            f"same number — the strongest match there is, and only as good as the record "
            f"it matched."
        )
    if signal.name in ("pan", "pan_from_gstin"):
        return (
            f"Both identifiers embed the same PAN ({_mask_pan(signal.value)}), so this is "
            f"one legal person registered more than once — strong, but not the identifier "
            f"that was actually declared."
        )
    if signal.name == "state_code":
        return (
            f"The state codes disagree ({signal.value}). A GSTIN carries its state in its "
            f"first two digits, so this is evidence against the match rather than a gap."
        )
    if signal.name == "name_similarity":
        return (
            f"The declared name and the registry name share {signal.value} of their words. "
            f"Indian trade names repeat endlessly, so a name is never an identity."
        )
    if signal.name == "address":
        return (
            f"The addresses look {signal.value}. Shared commercial premises are entirely "
            f"normal here, so this corroborates weakly at best and never on its own."
        )
    return signal.note or f"{signal.name}: {signal.value}"


def _from_resolution(signal: Signal) -> dict:
    value = (
        _mask_pan(signal.value) if signal.name in ("pan", "pan_from_gstin") else signal.value
    )
    return _signal(signal.name, value, signal.supports, _sentence_for(signal), signal.weight)


# --------------------------------------------------------------- what was said


def _buyer_profile(
    session: Session, company_id: UUID, buyer_id: UUID
) -> CompanyProfile | None:
    """This buyer's row — never the tenant's own, which has no buyer attached."""
    return session.execute(
        select(CompanyProfile)
        .where(
            CompanyProfile.company_id == company_id,
            CompanyProfile.buyer_id == buyer_id,
        )
        .order_by(CompanyProfile.created_at.desc())
    ).scalars().first()


def _declared_value(kind: str, on_buyer: str | None, on_profile: str | None):
    """Reconcile the two places a declaration can live. Returns (value, conflict).

    `Buyer.gstin` is what an import or an invoice put there; the profile holds
    what the identity endpoint accepted, having checked the check digit. Where
    only one exists it wins. Where they disagree, nothing does: two different
    numbers for one debtor is a question about which company this is, and this
    module answers those by stopping.
    """
    buyer_value, profile_value = _clean(on_buyer), _clean(on_profile)
    if buyer_value and profile_value and buyer_value != profile_value:
        return None, (
            f"The {kind} on the buyer record ({buyer_value}) and the {kind} on their "
            f"declared identity ({profile_value}) are different numbers. A person has to "
            f"settle which company this is before anything is checked against a registry."
        )
    return profile_value or buyer_value, None


def _declared_identity(
    buyer: Buyer, profile: CompanyProfile | None
) -> tuple[EntityInput, list[str]]:
    """The claim, before anybody has checked any of it.

    The name compared is the buyer's own and never a legal name a previous run
    resolved. Feeding last run's answer back in is how a weak match launders
    itself into a confident one over three runs.

    PAN is absent on purpose: it is only ever held hashed, so there is nothing to
    compare, and `resolution` derives the PAN it needs from the GSTIN itself.
    """
    gstin, gstin_conflict = _declared_value(
        "GSTIN", buyer.gstin, profile.gstin if profile else None
    )
    cin, cin_conflict = _declared_value("CIN", buyer.cin, profile.cin if profile else None)
    # The address and state come from the profile only while nothing has been
    # resolved into it. `resolved_at` is the mark a verification run leaves, and
    # after one the same two columns hold the registry's own answer — feeding
    # that back in has `resolve` compare a record against itself, and an address
    # that matches itself is one of the two conditions that lifts a weak name
    # match to a strong one.
    as_declared = profile if (profile is not None and profile.resolved_at is None) else None
    declared = EntityInput(
        name=buyer.name,
        gstin=gstin,
        cin=cin,
        state_code=(as_declared.state_code if as_declared else None)
        or state_from_gstin(gstin),
        address=as_declared.registered_address if as_declared else None,
    )
    return declared, [c for c in (gstin_conflict, cin_conflict) if c]


# ------------------------------------------------------------ what was fetched


@dataclass(frozen=True)
class _Consulted:
    provider: str
    identifier: str
    provenance: str
    status: str | None
    lookup: Any
    record: Any  # GstRecord | McaRecord
    source: SourceRecord

    @property
    def registry_backed(self) -> bool:
        return is_registry_provenance(self.provenance)

    def summary(self) -> dict:
        return {
            "provider": self.provider,
            "identifier": self.identifier,
            "provenance": self.provenance,
            "status": self.status,
        }


def _record_fetch(
    session: Session,
    *,
    company_id: UUID,
    provider: str,
    resource: str,
    external_id: str,
    lookup: Any,
    now: datetime,
) -> ProviderFetch:
    """Keep the payload verbatim beside the parsed row (rule 10).

    Sources change shape and matching improves; re-parsing years of history
    without re-fetching it is only possible if the payload was kept.
    """
    raw = asdict(lookup) if is_dataclass(lookup) else dict(vars(lookup))
    fetch = ProviderFetch(
        company_id=company_id,
        provider=provider,
        resource=resource,
        external_id=external_id,
        raw=_jsonable(raw),
        parsed={
            "provenance": lookup.provenance,
            "status": getattr(lookup, "status", None),
        },
        fetched_at=now,
    )
    session.add(fetch)
    return fetch


def _provenance_notes(provider: str, identifier: str, provenance: str):
    label = provider.upper()
    if is_registry_provenance(provenance):
        return (
            _signal(
                f"{provider}_provenance",
                provenance,
                True,
                f"The {label} record for {identifier} was fetched from the registry backend.",
            ),
            [],
        )
    return (
        _signal(
            f"{provider}_provenance",
            provenance or "unstated",
            False,
            f"The {label} record for {identifier} was supplied by an operator, not fetched "
            f"from the registry.",
        ),
        [
            f"The {label} record for {identifier} was entered by an operator reading the "
            f"portal by hand rather than fetched from the registry. That is a claim about "
            f"this business, not verification of it."
        ],
    )


def _status_notes(provider: str, identifier: str, status: str | None):
    label = provider.upper()
    healthy = (status or "").strip().upper() == ACTIVE
    signal = _signal(
        f"{provider}_status",
        status or "unstated",
        healthy,
        f"The {label} record for {identifier} reads {status or 'no status at all'}.",
    )
    if healthy:
        return signal, []
    if not status:
        return signal, [
            f"The {label} record for {identifier} states no status, and an unstated status "
            f"is not an active one."
        ]
    return signal, [
        f"The {label} record for {identifier} reads {status}, not Active. Nothing may be "
        f"published against a registration the register itself no longer stands behind."
    ]


def _consult_gst(
    session: Session,
    *,
    company_id: UUID,
    gstin: str,
    backend: GstBackend | None,
    now: datetime,
):
    """Look one GSTIN up, keep what came back, and say what it is worth."""
    # Checked here rather than left to the backend's own guard, which can only
    # answer "no record". One mistyped character usually still yields a
    # structurally valid GSTIN belonging to a different real business, so the
    # refusal has to name the check that failed.
    check = validate_gstin(gstin)
    if not check.ok:
        return (
            None,
            [_signal("gstin_wellformed", gstin, False,
                     f"The declared GSTIN {gstin} is not usable: {check.detail}.")],
            [
                f"The declared GSTIN {gstin} was not checked against anything because it is "
                f"not a usable GSTIN: {check.detail}."
            ],
        )

    if backend is None:
        return (
            None,
            [_signal("gst_backend", "absent", False,
                     "No GST backend was configured, so nothing was consulted.")],
            [f"No GST registry backend is configured, so the declared GSTIN {check.value} "
             f"was never checked."],
        )

    lookup = backend.lookup(check.value)
    if lookup is None:
        return (
            None,
            [_signal("gst_lookup", check.value, False,
                     f"The GST registry has no record of {check.value}.")],
            [f"The declared GSTIN {check.value} was not found in the GST registry."],
        )

    found = lookup.gstin or check.value
    record = GstRecord(
        company_id=company_id,
        gstin=found,
        legal_name=lookup.legal_name,
        trade_name=lookup.trade_name,
        status=lookup.status,
        registration_date=lookup.registration_date,
        address=lookup.address,
        filing_history=_jsonable(list(lookup.filing_history or [])),
        provenance=lookup.provenance,
        fetched_at=now,
    )
    session.add(record)
    _record_fetch(
        session,
        company_id=company_id,
        provider=_PROVIDER_GST,
        resource="registration",
        external_id=found,
        lookup=lookup,
        now=now,
    )

    provenance_signal, blockers = _provenance_notes(_PROVIDER_GST, found, lookup.provenance)
    status_signal, status_blockers = _status_notes(_PROVIDER_GST, found, lookup.status)
    consulted = _Consulted(
        provider=_PROVIDER_GST,
        identifier=found,
        provenance=lookup.provenance,
        status=lookup.status,
        lookup=lookup,
        record=record,
        source=SourceRecord(
            name=lookup.legal_name or lookup.trade_name or "",
            gstin=found,
            state_code=lookup.state_code,
            address=lookup.address,
            source=_PROVIDER_GST,
            extra={"provenance": lookup.provenance},
        ),
    )
    return consulted, [provenance_signal, status_signal], blockers + status_blockers


def _consult_mca(
    session: Session,
    *,
    company_id: UUID,
    cin: str,
    backend: McaBackend | None,
    now: datetime,
):
    """As `_consult_gst`, with one caveat worth carrying: a CIN has no check
    digit, so a mistyped one can only be caught by failing to find it."""
    check = validate_cin(cin)
    if not check.ok:
        return (
            None,
            [_signal("cin_wellformed", cin, False,
                     f"The declared CIN {cin} is not usable: {check.detail}.")],
            [
                f"The declared CIN {cin} was not checked against anything because it is not "
                f"a usable CIN: {check.detail}."
            ],
        )

    if backend is None:
        return (
            None,
            [_signal("mca_backend", "absent", False,
                     "No MCA backend was configured, so nothing was consulted.")],
            [f"No MCA registry backend is configured, so the declared CIN {check.value} "
             f"was never checked."],
        )

    lookup = backend.lookup(check.value)
    if lookup is None:
        return (
            None,
            [_signal("mca_lookup", check.value, False,
                     f"The MCA register has no record of {check.value}.")],
            [f"The declared CIN {check.value} was not found in the MCA register."],
        )

    found = lookup.cin or check.value
    record = McaRecord(
        company_id=company_id,
        cin=found,
        legal_name=lookup.legal_name,
        status=lookup.status,
        incorporation_date=lookup.incorporation_date,
        registered_address=lookup.registered_address,
        directors=_jsonable(list(lookup.directors or [])),
        charges=_jsonable(list(lookup.charges or [])),
        provenance=lookup.provenance,
        fetched_at=now,
    )
    session.add(record)
    _record_fetch(
        session,
        company_id=company_id,
        provider=_PROVIDER_MCA,
        resource="company",
        external_id=found,
        lookup=lookup,
        now=now,
    )

    provenance_signal, blockers = _provenance_notes(_PROVIDER_MCA, found, lookup.provenance)
    status_signal, status_blockers = _status_notes(_PROVIDER_MCA, found, lookup.status)
    consulted = _Consulted(
        provider=_PROVIDER_MCA,
        identifier=found,
        provenance=lookup.provenance,
        status=lookup.status,
        lookup=lookup,
        record=record,
        source=SourceRecord(
            name=lookup.legal_name or "",
            cin=found,
            address=lookup.registered_address,
            source=_PROVIDER_MCA,
            extra={"provenance": lookup.provenance},
        ),
    )
    return consulted, [provenance_signal, status_signal], blockers + status_blockers


# --------------------------------------------------------------- what is kept


def _write_profile(
    session: Session,
    *,
    profile: CompanyProfile | None,
    buyer: Buyer,
    company_id: UUID,
    consulted: list[_Consulted],
    tier: Tier,
    confidence: float,
    now: datetime,
) -> CompanyProfile:
    """The row that says this buyer *is* that company.

    Only registry-backed lookups contribute a field. An operator-typed address or
    status copied into a profile stamped IDENTIFIER is the same laundering the
    tier cap exists to stop, one column further down.
    """
    def trusted(provider: str) -> _Consulted | None:
        return next(
            (c for c in consulted if c.provider == provider and c.registry_backed), None
        )

    gst, mca = trusted(_PROVIDER_GST), trusted(_PROVIDER_MCA)

    lookup_name = (gst.lookup.legal_name if gst else None) or (
        mca.lookup.legal_name if mca else None
    )
    # A registry name sharing no word with the buyer's own is either a trade name
    # nobody wrote down or — the case this guards — the wrong company behind a
    # checksum-valid identifier somebody mistyped. Overwriting the buyer's name
    # with it is how a stranger's identity ends up attached to this debt, so it
    # waits for the person the matching blocker sends it to.
    if lookup_name and not _shares_a_word(buyer.name, lookup_name):
        lookup_name = None
    legal_name = lookup_name or (profile.legal_name if profile else None) or buyer.name

    if profile is None:
        profile = CompanyProfile(
            company_id=company_id, buyer_id=buyer.id, legal_name=legal_name
        )
        session.add(profile)
    profile.legal_name = legal_name

    # Each register writes only its own columns — and clears them when it did not
    # answer. A number an operator typed, left in a row stamped IDENTIFIER, is
    # the same laundering the tier cap exists to stop, one column further down;
    # everything downstream reads the tier and never asks which field it covers.
    if gst is not None:
        profile.gstin = gst.lookup.gstin
        profile.state_code = gst.lookup.state_code or state_from_gstin(gst.lookup.gstin)
        trade = gst.lookup.trade_name
        if trade and trade not in (profile.trade_names or []):
            profile.trade_names = [*(profile.trade_names or []), trade]
    else:
        profile.gstin = None
        profile.state_code = None

    if mca is not None:
        profile.cin = mca.lookup.cin
        profile.incorporation_date = mca.lookup.incorporation_date
    else:
        profile.cin = None
        profile.incorporation_date = None

    # From a register or not at all — the registered office where the MCA holds
    # one, the GST address otherwise. An address a person typed is a claim, and
    # this row is about to be stamped IDENTIFIER.
    profile.registered_address = (mca.lookup.registered_address if mca else None) or (
        gst.lookup.address if gst else None
    )

    profile.status = _worse_status([c.lookup.status for c in (gst, mca) if c is not None])

    # Derived from the GSTIN the registry returned, never from whatever is left
    # on the row: a PAN hashed out of an unchecked number is an unchecked number
    # wearing the same label as a checked one.
    pan = pan_from_gstin(profile.gstin)
    # `pan` exists only in this frame. What lands in the row is a keyed digest
    # and four characters — enough to recognise, not enough to leak.
    profile.pan_hash, profile.pan_last4 = hash_pan(pan) if pan else (None, None)

    profile.resolution_tier = tier.value
    profile.confidence = confidence
    profile.resolved_at = now
    session.flush()
    return profile


def _write_candidate(
    session: Session,
    *,
    company_id: UUID,
    buyer: Buyer,
    matched: SourceRecord | None,
    tier: Tier,
    confidence: float,
    signals: list[dict],
    blockers: list[str],
    consulted: list[_Consulted],
) -> EntityCandidate:
    """A proposal for a person to accept or reject. Never a merge.

    `matched` is None when resolution refused to pick between several equally
    good records. That is the case a person most needs to see, so it still
    produces a candidate — named for the buyer, carrying every record consulted.
    """
    kind, value = None, None
    if matched is not None:
        if matched.gstin:
            kind, value = "gstin", matched.gstin
        elif matched.cin:
            kind, value = "cin", matched.cin

    candidate = EntityCandidate(
        company_id=company_id,
        buyer_id=buyer.id,
        candidate_name=((matched.name if matched else None) or buyer.name)[:300],
        identifier_kind=kind,
        identifier_value=value,
        score=confidence,
        tier=tier.value,
        signals={
            "signals": signals,
            "blockers": blockers,
            "sources": [c.summary() for c in consulted],
        },
        status="PROPOSED",
    )
    session.add(candidate)
    session.flush()
    return candidate


def _write_report(
    session: Session,
    *,
    company_id: UUID,
    profile: CompanyProfile,
    payload: dict,
    sources: list[dict],
    confidence: float,
    issued_by: UUID | None,
    now: datetime,
) -> VerificationReport:
    """Issue a new report and demote the previous one.

    No issued report is ever edited — only the pointer saying which one is live
    moves, so "what did we say in March" stays answerable in September.
    """
    for prior in session.execute(
        select(VerificationReport).where(
            VerificationReport.company_id == company_id,
            VerificationReport.profile_id == profile.id,
            VerificationReport.is_current.is_(True),
        )
    ).scalars():
        prior.is_current = False

    report = VerificationReport(
        company_id=company_id,
        profile_id=profile.id,
        issued_at=now,
        # The report is the record of what was claimed, so it names who claimed
        # it rather than leaning on the separate audit row to say.
        issued_by=issued_by,
        payload=_jsonable(payload),
        sources=_jsonable(sources),
        confidence=confidence,
        is_current=True,
    )
    session.add(report)
    session.flush()
    return report


# ------------------------------------------------------------------ the answer


def verify_buyer(
    session: Session,
    buyer_id: UUID,
    *,
    company_id: UUID,
    actor_label: str | None,
    gst: GstBackend | None,
    mca: McaBackend | None,
    now: datetime,
    issued_by: UUID | None = None,
) -> VerificationOutcome:
    """Check a buyer's declared identity against the registries.

    Flushes but never commits: the caller owns the transaction, so a route that
    fails afterwards takes the fetch rows down with it.

    Raises `BuyerNotFound` when the buyer is not this tenant's, and
    `ProvenanceViolation` if self-declared evidence ever reaches a profile write.
    """
    buyer = session.execute(
        select(Buyer).where(Buyer.id == buyer_id, Buyer.company_id == company_id)
    ).scalar_one_or_none()
    if buyer is None:
        raise BuyerNotFound(f"no buyer {buyer_id} in company {company_id}")

    profile = _buyer_profile(session, company_id, buyer_id)
    declared, conflicts = _declared_identity(buyer, profile)

    signals: list[dict] = []
    blockers: list[str] = []
    consulted: list[_Consulted] = []

    if conflicts:
        signals.append(
            _signal(
                "declared_identifier",
                "conflicting",
                False,
                "The buyer record and the buyer's declared identity name different "
                "identifiers, so it is not settled which company is being verified.",
            )
        )
        blockers.extend(conflicts)
        return _conclude(
            session, buyer=buyer, company_id=company_id, actor_label=actor_label, now=now,
            tier=Tier.NONE, confidence=0.0, publishable=False, profile=profile,
            profile_id=None, candidate_id=None, consulted=consulted,
            signals=signals, blockers=blockers, issued_by=issued_by,
        )

    # A name is not an identity. "Sharma Traders" is hundreds of unrelated
    # businesses, so with no declared identifier there is nothing a registry
    # could confirm and nothing worth guessing at.
    if not declared.gstin and not declared.cin:
        signals.append(
            _signal(
                "declared_identifier",
                "none",
                False,
                "Neither a GSTIN nor a CIN was declared for this buyer.",
            )
        )
        blockers.append(
            "This buyer has no declared GSTIN or CIN, so there is nothing to check against "
            "a registry. A trade name on its own is not an identity."
        )
        return _conclude(
            session, buyer=buyer, company_id=company_id, actor_label=actor_label, now=now,
            tier=Tier.NONE, confidence=0.0, publishable=False, profile=profile,
            profile_id=None, candidate_id=None, consulted=consulted,
            signals=signals, blockers=blockers, issued_by=issued_by,
        )

    if declared.gstin:
        found, new_signals, new_blockers = _consult_gst(
            session, company_id=company_id, gstin=declared.gstin, backend=gst, now=now
        )
        signals.extend(new_signals)
        blockers.extend(new_blockers)
        if found is not None:
            consulted.append(found)

    if declared.cin:
        found, new_signals, new_blockers = _consult_mca(
            session, company_id=company_id, cin=declared.cin, backend=mca, now=now
        )
        signals.extend(new_signals)
        blockers.extend(new_blockers)
        if found is not None:
            consulted.append(found)

    matched, resolution = best_match(declared, [c.source for c in consulted])
    winner = next((c for c in consulted if c.source is matched), None)
    signals.extend(_from_resolution(s) for s in resolution.signals)

    tier = cap_tier_for_provenance(resolution.tier, winner.provenance if winner else None)
    if tier is not resolution.tier:
        signals.append(
            _signal(
                "provenance_cap",
                f"{resolution.tier.value} -> {tier.value}",
                False,
                "The evidence would have identified this company, but the record behind it "
                "was operator-supplied rather than fetched from a registry, so it is capped "
                "below the tier that may be published.",
            )
        )

    # An identifier match is decisive about the *record*. It says nothing about
    # whether that was the right identifier to type, and a checksum-valid GSTIN
    # belonging to a stranger matches their record perfectly. Zero words in
    # common between the buyer we are chasing and the company on the register is
    # the only thing on the screen that would show it.
    if (
        tier is Tier.IDENTIFIER
        and winner is not None
        and not _shares_a_word(buyer.name, winner.source.name)
    ):
        blockers.append(
            f"The {winner.provider.upper()} record for {winner.identifier} is registered to "
            f"{winner.source.name or 'a company with no name on the register'}, which shares "
            f"no word with {buyer.name}. An identifier matches exactly whether or not it was "
            f"the right one to type, so a person has to confirm this is the same company."
        )

    if resolution.vetoed:
        blockers.append(
            f"The match was refused: {resolution.veto_reason}. "
            f"A person has to decide this one."
        )
    if tier is not Tier.IDENTIFIER:
        blockers.append(
            f"The evidence reached {tier.value}, not IDENTIFIER. Only an identifier confirmed "
            f"against a registry may be published about a company."
        )

    # `Tier.publishable` is the resolution ladder's own rule; `blockers` is
    # everything else that stops publication. Both have to clear.
    publishable = tier.publishable and not blockers

    profile_id: UUID | None = None
    candidate_id: UUID | None = None

    if tier is Tier.IDENTIFIER:
        _assert_registry_backed(winner, tier)
        profile = _write_profile(
            session,
            profile=profile,
            buyer=buyer,
            company_id=company_id,
            consulted=consulted,
            tier=tier,
            confidence=resolution.confidence,
            now=now,
        )
        profile_id = profile.id
        # Attach the evidence to the entity it resolved, now that there is one.
        # Below IDENTIFIER these stay unattached: an unattached registry record
        # is honest, and a wrongly attached one is the whole failure mode.
        for c in consulted:
            if c.registry_backed:
                c.record.profile_id = profile.id
    elif consulted:
        candidate_id = _write_candidate(
            session,
            company_id=company_id,
            buyer=buyer,
            matched=matched,
            tier=tier,
            confidence=resolution.confidence,
            signals=signals,
            blockers=blockers,
            consulted=consulted,
        ).id

    return _conclude(
        session, buyer=buyer, company_id=company_id, actor_label=actor_label, now=now,
        tier=tier, confidence=resolution.confidence, publishable=publishable,
        profile=profile, profile_id=profile_id, candidate_id=candidate_id,
        consulted=consulted, signals=signals, blockers=blockers, issued_by=issued_by,
    )


def _conclude(
    session: Session,
    *,
    buyer: Buyer,
    company_id: UUID,
    actor_label: str | None,
    now: datetime,
    tier: Tier,
    confidence: float,
    publishable: bool,
    profile: CompanyProfile | None,
    profile_id: UUID | None,
    candidate_id: UUID | None,
    consulted: list[_Consulted],
    signals: list[dict],
    blockers: list[str],
    issued_by: UUID | None = None,
) -> VerificationOutcome:
    """Issue the report, record who asked, and hand back the reasoning."""
    gst_status = next((c.status for c in consulted if c.provider == _PROVIDER_GST), None)
    mca_status = next((c.status for c in consulted if c.provider == _PROVIDER_MCA), None)

    outcome = VerificationOutcome(
        tier=tier.value,
        confidence=confidence,
        publishable=publishable,
        profile_id=profile_id,
        candidate_id=candidate_id,
        gst_status=gst_status,
        mca_status=mca_status,
        signals=signals,
        blockers=blockers,
    )

    # A run that reached nothing still issues a report: "we checked and found
    # nothing" and "nobody has ever checked" are different facts, and a caller
    # that cannot tell them apart will eventually treat the second as the first —
    # which is exactly what the 404 on the read route is there to preserve.
    #
    # A report hangs off a profile row, so a buyer nobody has declared an
    # identity for gets an empty one here rather than losing the run: it carries
    # no identifiers and stays `self_declared`, so nothing downstream reads it as
    # a resolution.
    if profile is None:
        profile = CompanyProfile(
            company_id=company_id,
            buyer_id=buyer.id,
            legal_name=buyer.name,
            resolution_tier="self_declared",
        )
        session.add(profile)
        session.flush()

    _write_report(
        session,
        company_id=company_id,
        profile=profile,
        payload={
            "buyer_id": str(buyer.id),
            "buyer_name": buyer.name,
            "tier": outcome.tier,
            "confidence": outcome.confidence,
            "publishable": outcome.publishable,
            "gst_status": outcome.gst_status,
            "mca_status": outcome.mca_status,
            "signals": outcome.signals,
            "blockers": outcome.blockers,
            "candidate_id": str(candidate_id) if candidate_id else None,
            "verified_at": now.isoformat(),
        },
        sources=[c.summary() for c in consulted],
        confidence=confidence,
        issued_by=issued_by,
        now=now,
    )

    audit.record(
        session,
        action=ACTION_VERIFIED,
        company_id=company_id,
        actor_label=actor_label,
        entity_type="buyer",
        entity_id=buyer.id,
        after={
            "tier": outcome.tier,
            "confidence": outcome.confidence,
            "publishable": outcome.publishable,
            "profile_id": str(profile_id) if profile_id else None,
            "candidate_id": str(candidate_id) if candidate_id else None,
            "gst_status": outcome.gst_status,
            "mca_status": outcome.mca_status,
            "blockers": outcome.blockers,
        },
        detail=f"{buyer.name}: {outcome.tier}, "
        f"{'publishable' if publishable else 'not publishable'}",
    )
    return outcome
