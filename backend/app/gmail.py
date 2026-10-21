"""Deterministic Gmail handling: safety filters, quote stripping, secret redaction. No AI, no DB.

Filters decide what the AI must never see. Whether a thread is sales-related is the classifier's job, not ours.
"""

import hashlib
import json
import re
from dataclasses import dataclass

MAX_BODY_CHARS = 8000

# Gmail system labels we never ingest, even if n8n's search lets them through.
SKIP_LABELS = {"SPAM", "TRASH", "DRAFT", "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_UPDATES"}

_AUTOMATED_SENDER = re.compile(
    r"^(no-?reply|do-?not-?reply|notifications?|mailer-daemon|postmaster|calendar-notification)@", re.I
)
_CALENDAR_SUBJECT = re.compile(
    r"^(accepted|declined|tentatively accepted|invitation|updated invitation|canceled event|cancelled event)\b.*:",
    re.I,
)

# Where quoted earlier messages start; everything after the first match is dropped.
_QUOTE_MARKERS = [
    re.compile(r"^On .{0,300}?wrote:\s*$", re.M | re.S),  # Gmail, possibly wrapped over two lines
    re.compile(r"^El .{0,300}?escribi[oó]:\s*$", re.M | re.S),  # Gmail in Spanish
    re.compile(r"^-{5,}\s*Original Message\s*-{5,}", re.M | re.I),
    re.compile(r"^[-_]{20,}\s*$", re.M),  # Outlook separator line
    re.compile(r"^>", re.M),
]

_SECRET_PATTERNS = [
    re.compile(r"https?://(?:www\.)?(?:password\.link|onetimesecret\.com|privnote\.com|pwpush\.com)\S*", re.I),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}"),  # OpenAI/Stripe-style keys
    re.compile(r"\b(?:ghp|gho|ghs|github_pat)_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\b[A-Za-z0-9_\-+/=]{40,}\b"),  # long opaque tokens
]
REDACTED = "[redacted]"


@dataclass(frozen=True)
class CleanMessage:
    external_id: str
    thread_id: str | None
    occurred_at: str | None
    subject: str | None
    participants: dict[str, list[dict[str, str]]]
    body: str

    @property
    def content_hash(self) -> str:
        payload = json.dumps([self.subject, self.participants, self.body], ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()


def addresses(field) -> list[dict[str, str]]:
    """n8n's Gmail address field ({value: [{address, name}]}) -> [{address, name}], lowercased addresses."""
    if not isinstance(field, dict):
        return []
    out = []
    for entry in field.get("value") or []:
        address = (entry.get("address") or "").strip().lower()
        if address:
            out.append({"address": address, "name": (entry.get("name") or "").strip()})
    return out


def domain(address: str) -> str:
    return address.rsplit("@", 1)[-1]


def strip_quoted(text: str) -> str:
    cut = len(text)
    for marker in _QUOTE_MARKERS:
        match = marker.search(text)
        if match and match.start() > 0:
            cut = min(cut, match.start())
    return text[:cut].strip()


def redact_secrets(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def skip_reason(message: dict, internal_domains: set[str], excluded_label_ids: set[str]) -> str | None:
    """Why this message must not be stored or shown to the AI, or None if it passes."""
    labels = set(message.get("labelIds") or [])
    if labels & excluded_label_ids:
        return "excluded_label"
    if labels & SKIP_LABELS:
        return "system_label"

    senders = addresses(message.get("from"))
    if any(_AUTOMATED_SENDER.match(s["address"]) for s in senders):
        return "automated"
    if _CALENDAR_SUBJECT.match((message.get("subject") or "").strip()):
        return "automated"

    everyone = senders + addresses(message.get("to")) + addresses(message.get("cc"))
    if not everyone:
        return "no_participants"
    if all(domain(p["address"]) in internal_domains for p in everyone):
        return "internal_only"

    if not (message.get("text") or "").strip():
        return "empty"
    return None


def clean(message: dict) -> CleanMessage:
    body = redact_secrets(strip_quoted(message.get("text") or ""))[:MAX_BODY_CHARS]
    return CleanMessage(
        external_id=str(message["id"]),
        thread_id=message.get("threadId"),
        occurred_at=message.get("date"),
        subject=redact_secrets(message.get("subject") or "") or None,
        participants={
            "from": addresses(message.get("from")),
            "to": addresses(message.get("to")),
            "cc": addresses(message.get("cc")),
        },
        body=body,
    )


def process(messages: list[dict], internal_domains: set[str], excluded_label_ids: set[str]):
    """Split a batch into messages to store and a count of skips by reason."""
    kept: list[CleanMessage] = []
    skipped: dict[str, int] = {}
    seen: set[str] = set()
    for message in messages:
        if not message.get("id") or message["id"] in seen:
            skipped["duplicate_or_no_id"] = skipped.get("duplicate_or_no_id", 0) + 1
            continue
        seen.add(message["id"])
        reason = skip_reason(message, internal_domains, excluded_label_ids)
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
        else:
            kept.append(clean(message))
    return kept, skipped
