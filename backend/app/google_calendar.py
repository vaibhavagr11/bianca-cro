"""Deterministic Google Calendar handling: which events are customer meetings, cleaned for storage. No AI, no DB."""

import hashlib
import html
import json
import re
from dataclasses import dataclass

from app.gmail import domain, redact_secrets

MAX_DESCRIPTION_CHARS = 4000

# Not meetings with people.
SKIP_EVENT_TYPES = {"focusTime", "outOfOffice", "workingLocation", "birthday", "fromGmail"}

# Google Meet / dial-in boilerplate starts here; everything after is dropped.
_BOILERPLATE = re.compile(r"(-::~:~::~.*|Join with Google Meet.*|Join by phone.*|Únete con Google Meet.*)", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class CleanEvent:
    external_id: str
    starts_at: str | None
    subject: str | None
    participants: dict
    body: str
    meta: dict

    @property
    def content_hash(self) -> str:
        # "self" depends on whose calendar we read; leaving it out stops two calendars' copies of one meeting
        # from overwriting each other (and being re-classified) every day.
        people = {k: [{f: v for f, v in p.items() if f != "self"} for p in ps] for k, ps in self.participants.items()}
        payload = json.dumps([self.subject, self.starts_at, people, self.body, self.meta],
                             ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()


def _people(event: dict) -> list[dict]:
    """Human attendees (rooms and other resources excluded), plus the organizer if not listed."""
    people = [a for a in (event.get("attendees") or []) if a.get("email") and not a.get("resource")]
    organizer = event.get("organizer") or {}
    if organizer.get("email") and organizer["email"].lower() not in {p["email"].lower() for p in people}:
        people.append({**organizer, "organizer": True})
    return people


def skip_reason(event: dict, internal_domains: set[str], project_domains: set[str]) -> str | None:
    if not event.get("id"):
        return "no_id"
    if event.get("eventType") in SKIP_EVENT_TYPES:
        return "not_a_meeting"
    people = _people(event)
    if not [p for p in people if not p.get("self")]:
        return "no_other_attendees"
    domains = {domain(p["email"].lower()) for p in people}
    if domains & project_domains and domains <= internal_domains | project_domains:
        return "project_team"
    if domains <= internal_domains:
        return "internal_only"
    return None


def clean_description(text: str | None) -> str:
    if not text:
        return ""
    plain = html.unescape(_TAGS.sub(" ", text.replace("<br>", "\n")))
    plain = _BOILERPLATE.sub("", plain)
    return redact_secrets(re.sub(r"[ \t]+", " ", plain).strip())[:MAX_DESCRIPTION_CHARS]


def clean(event: dict) -> CleanEvent:
    start, end = event.get("start") or {}, event.get("end") or {}
    attendees = [
        {"address": p["email"].lower(), "name": (p.get("displayName") or "").strip(),
         "response": p.get("responseStatus"), "organizer": bool(p.get("organizer")), "self": bool(p.get("self"))}
        for p in _people(event)
    ]
    return CleanEvent(
        external_id=str(event["id"]),
        starts_at=start.get("dateTime") or start.get("date"),
        subject=redact_secrets(event.get("summary") or "") or None,
        participants={"attendees": attendees},
        body=clean_description(event.get("description")),
        meta={"status": event.get("status") or "confirmed", "ends_at": end.get("dateTime") or end.get("date"),
              "recurring": bool(event.get("recurringEventId"))},
    )


def process(events: list[dict], internal_domains: set[str], project_domains: set[str]):
    """Split a batch into meetings to store, ids of cancellations to apply, and skip counts by reason.

    Cancelled occurrences often arrive without attendees, so they are returned separately and only
    update meetings we already stored.
    """
    kept: list[CleanEvent] = []
    cancelled: list[str] = []
    skipped: dict[str, int] = {}
    seen: set[str] = set()
    for event in events:
        event_id = event.get("id")
        if event_id and event_id in seen:
            skipped["duplicate"] = skipped.get("duplicate", 0) + 1
            continue
        seen.add(event_id)
        reason = skip_reason(event, internal_domains, project_domains)
        if reason == "no_other_attendees" and event.get("status") == "cancelled":
            cancelled.append(str(event_id))
            continue
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
        else:
            kept.append(clean(event))
    return kept, cancelled, skipped
