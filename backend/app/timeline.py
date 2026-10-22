"""Opportunity timeline: emails, meetings, notes and pipeline changes for one deal, newest first. No AI."""

from datetime import datetime, timezone

GMAIL_THREAD_URL = "https://mail.google.com/mail/u/0/#all/{}"
DOC_URL = "https://docs.google.com/document/d/{}"


def _iso(value) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _people(participants: dict, internal: set[str]) -> list[str]:
    names = []
    for key in ("from", "to", "cc", "attendees"):
        for p in participants.get(key) or []:
            address = (p.get("address") or "").lower()
            if address and address.rsplit("@", 1)[-1] not in internal:
                label = p.get("name") or address
                if label not in names:
                    names.append(label)
    return names


def build_timeline(records: list[dict], changes: list[dict], internal: set[str], now: datetime) -> list[dict]:
    """records: source_records rows for the company; changes: opportunity_changes rows. Newest first."""
    items: list[dict] = []

    threads: dict[str, list[dict]] = {}
    for r in records:
        if r["source"] == "gmail":
            threads.setdefault(r["thread_id"] or r["external_id"], []).append(r)
            continue
        cls = r["classification"] or {}
        meta = r["meta"] or {}
        item = {
            "source": r["source"],
            "record_ids": [str(r["id"])],
            "occurred_at": _iso(r["occurred_at"]),
            "title": r["subject"],
            "summary": cls.get("reason"),
            "category": cls.get("category"),
            "people": _people(r["participants"] or {}, internal),
            "link": DOC_URL.format(meta["doc_id"]) if r["source"] == "notes" and meta.get("doc_id") else None,
        }
        if r["source"] == "calendar":
            item["status"] = meta.get("status", "confirmed")
            item["upcoming"] = bool(r["occurred_at"] and r["occurred_at"] > now)
        if r["source"] == "notes":
            item["date_estimated"] = bool(meta.get("date_inferred"))
        items.append(item)

    for thread_id, messages in threads.items():
        messages.sort(key=lambda m: m["occurred_at"] or now)
        last = messages[-1]
        cls = last["classification"] or {}
        people: list[str] = []
        for m in messages:
            people += [p for p in _people(m["participants"] or {}, internal) if p not in people]
        items.append({
            "source": "gmail",
            "record_ids": [str(m["id"]) for m in messages],
            "occurred_at": _iso(last["occurred_at"]),
            "first_at": _iso(messages[0]["occurred_at"]),
            "title": last["subject"],
            "summary": cls.get("reason"),
            "category": cls.get("category"),
            "people": people,
            "messages": len(messages),
            "link": GMAIL_THREAD_URL.format(thread_id),
        })

    for c in changes:
        items.append({
            "source": "pipeline",
            "occurred_at": _iso(c["detected_at"]),
            "title": f"{c['field']}: {c['old_value']} → {c['new_value']}" if c["field"] not in ("created", "removed",
                                                                                              "restored")
            else f"deal {c['field']}",
            "field": c["field"],
        })

    items.sort(key=lambda i: i["occurred_at"] or "", reverse=True)
    return items


def summarize(items: list[dict], now: datetime) -> dict:
    """Header numbers for a deal: counts by source, last contact, next meeting."""
    now_iso = now.isoformat()
    past = [i for i in items if i["source"] != "pipeline" and i["occurred_at"] and i["occurred_at"] <= now_iso
            and i.get("status") != "cancelled"]
    upcoming = sorted((i for i in items if i.get("upcoming") and i.get("status") != "cancelled"),
                      key=lambda i: i["occurred_at"])
    counts: dict[str, int] = {}
    for i in items:
        counts[i["source"]] = counts.get(i["source"], 0) + 1
    return {
        "counts": counts,
        "last_activity_at": max((i["occurred_at"] for i in past), default=None),
        "next_meeting": ({"at": upcoming[0]["occurred_at"], "title": upcoming[0]["title"]} if upcoming else None),
    }


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
