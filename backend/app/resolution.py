"""Entity resolution: map people and email domains to pipeline companies, then link records by rules.

Deterministic. Evidence comes from records the classifier already linked with high confidence; a domain is
only trusted for rule-based linking when that evidence is strong and unambiguous, so a partner who shows up
in a customer's threads (e.g. a data vendor cc'd on a deal) doesn't get mapped to that customer.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from app.gmail import domain
from app.pipeline import normalize_name

FREEMAIL = {
    "gmail.com", "googlemail.com", "hotmail.com", "outlook.com", "live.com", "yahoo.com", "yahoo.com.ar",
    "yahoo.com.mx", "icloud.com", "me.com", "aol.com", "protonmail.com", "proton.me", "hotmail.com.ar",
    "msn.com", "gmx.com",
}
IGNORED_DOMAINS = {"resource.calendar.google.com", "group.calendar.google.com"}

MIN_SHARE = 0.9  # share of a domain's evidence that must point to one company


@dataclass
class Evidence:
    record_id: str
    company_id: str | None
    confident: bool  # classifier linked it with high confidence and no flags
    addresses: dict[str, str]  # external email -> display name
    occurred_at: str | None = None


@dataclass
class DomainVerdict:
    domain: str
    company_id: str
    votes: int
    share: float
    trusted: bool


@dataclass
class Resolution:
    domains: dict[str, DomainVerdict] = field(default_factory=dict)
    contact_company: dict[str, str | None] = field(default_factory=dict)


def external_addresses(participants: dict, internal: set[str], project: set[str]) -> dict[str, str]:
    """All non-Bianca, non-project-team people on a record (email or calendar), email -> name."""
    people = []
    for key in ("from", "to", "cc", "attendees"):
        people.extend(participants.get(key) or [])
    out = {}
    for p in people:
        email = (p.get("address") or "").lower()
        d = domain(email) if "@" in email else ""
        if d and d not in internal and d not in project and d not in IGNORED_DOMAINS:
            out.setdefault(email, (p.get("name") or "").strip())
    return out


def _name_in_domain(company_name: str, d: str) -> bool:
    label = d.split(".")[0].replace("-", "")
    return any(len(w) >= 4 and w.replace("-", "") in label for w in normalize_name(company_name).split())


def resolve(
    evidence: list[Evidence], company_names: dict[str, str], manual_domains: dict[str, str] | None = None
) -> Resolution:
    """Learn domain -> company and contact -> company from confidently linked records.

    manual_domains (domain -> company id) are confirmed by a person: always trusted, never overridden.
    """
    domain_votes: dict[str, Counter] = defaultdict(Counter)
    contact_votes: dict[str, Counter] = defaultdict(Counter)
    for ev in evidence:
        if not (ev.company_id and ev.confident):
            continue
        for d in {domain(a) for a in ev.addresses} - FREEMAIL:
            domain_votes[d][ev.company_id] += 1
        for a in ev.addresses:
            contact_votes[a][ev.company_id] += 1

    result = Resolution()
    for d, votes in domain_votes.items():
        company_id, top = votes.most_common(1)[0]
        share = top / sum(votes.values())
        if share < MIN_SHARE:
            continue
        # Trusted when the domain carries the company's name, or the evidence is plentiful and unanimous.
        trusted = _name_in_domain(company_names.get(company_id, ""), d) or (top >= 3 and share == 1.0)
        result.domains[d] = DomainVerdict(d, company_id, top, round(share, 2), trusted)
    for d, company_id in (manual_domains or {}).items():
        votes = domain_votes.get(d, Counter())[company_id]
        result.domains[d] = DomainVerdict(d, company_id, votes, 1.0, True)

    seen ={a for ev in evidence for a in ev.addresses}
    for a in seen:
        verdict = result.domains.get(domain(a))
        if verdict and verdict.trusted:
            result.contact_company[a] = verdict.company_id
        elif a in contact_votes:
            company_id, top = contact_votes[a].most_common(1)[0]
            ok = top / sum(contact_votes[a].values()) >= MIN_SHARE
            result.contact_company[a] = company_id if ok else None
        else:
            result.contact_company[a] = None
    return result


def rules_company(addresses: dict[str, str], res: Resolution) -> str | None:
    """Company implied by a record's people: exactly one candidate, or None when none/ambiguous."""
    candidates = set()
    for a in addresses:
        verdict = res.domains.get(domain(a))
        if verdict and verdict.trusted:
            candidates.add(verdict.company_id)
        elif domain(a) in FREEMAIL and res.contact_company.get(a):
            candidates.add(res.contact_company[a])
    return candidates.pop() if len(candidates) == 1 else None


def decide(classifier_company: str | None, rule_company: str | None) -> tuple[str | None, str | None]:
    """Final company and how it was decided. On disagreement the classifier's link stays, flagged for review."""
    if rule_company and classifier_company:
        return classifier_company, ("both" if rule_company == classifier_company else "conflict")
    if rule_company:
        return rule_company, "rules"
    if classifier_company:
        return classifier_company, "classifier"
    return None, None
