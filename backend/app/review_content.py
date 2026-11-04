"""Pure review bullet generation and versioned, per-bullet source lineage.

Labels and dates are presentation only. Edges point to a source-content version,
and deduplicated bullet text keeps the union of the records that support it.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import date, datetime
from typing import Any
from urllib.parse import quote


SOURCE_NAMES = {"gmail": "Email", "notes": "Meeting notes", "calendar": "Calendar"}
CONTEXT_NAMES = {"need": "Customer need", "buying_signal": "Buying signal", "objection": "Objection",
                 "blocker": "Blocker"}
ACTION_TYPES = {"commitment", "next_step", "blocker"}
CONTEXT_TYPES = {"need", "buying_signal", "objection"}
DESTINATIONS = ("nextStep", "clientContext")
LINEAGE_VERSION = 1
Content = tuple[list[str], list[str], list[dict], dict[str, list[dict]]]


def _stamp(value: Any) -> str:
    return value.isoformat() if isinstance(value, (date, datetime)) else str(value or "")


def _squash(value: str) -> str:
    return " ".join(value.casefold().split())


def _hash(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def source_version(record: dict) -> dict:
    """Preserve existing receipts: extraction UUIDs and mailbox aliases are not content."""
    meta = record.get("meta") or {}
    identity = {"source": record["source"], "external_id": record["external_id"],
                "content_hash": record["content_hash"], "occurred_at": _stamp(record.get("occurred_at")),
                "subject": record.get("subject"),
                "calendar_status": meta.get("status") if record["source"] == "calendar" else None}
    return {"source": record["source"], "external_id": record["external_id"], "version": _hash(identity)}


def source_id(record: dict) -> str:
    return "source-" + _hash(source_version(record))


def bullet_id(destination: str, text: str) -> str:
    if destination not in DESTINATIONS or not isinstance(text, str) or not text.strip():
        raise ValueError("A valid destination and bullet text are required for lineage.")
    return "bullet-" + _hash({"destination": destination, "text": _squash(text)})


def _source_link(record: dict) -> str | None:
    if record["source"] == "gmail" and record.get("thread_id"):
        return "https://mail.google.com/mail/u/0/#all/" + quote(str(record["thread_id"]), safe="")
    if record["source"] == "notes" and (record.get("meta") or {}).get("doc_id"):
        return "https://docs.google.com/document/d/" + quote(str(record["meta"]["doc_id"]), safe="") + "/edit"
    return None


def _union(left: list[str], right: list[str]) -> list[str]:
    return sorted(set(left) | set(right))


def _merge_source(previous: dict, incoming: dict) -> dict:
    """Keep the original captured metadata; add newly validated quotes for this version."""
    result = copy.deepcopy(previous)
    quotes = result.setdefault("quotes", [result["quote"]] if result.get("quote") else [])
    for text in incoming.get("quotes", []):
        if text not in quotes:
            quotes.append(text)
    if quotes:
        result["quote"] = quotes[0]
    return result


def proposal_content(records: list[dict], insights: list[dict]) -> Content:
    """Use only source-supported insights; every resulting bullet has exact record edges."""
    groups: dict[str, list[str]] = {destination: [] for destination in DESTINATIONS}
    lineage: dict[str, list[dict]] = {destination: [] for destination in DESTINATIONS}
    positions: dict[tuple[str, str], int] = {}
    sources: dict[str, dict] = {}
    covered: set[str] = set()
    by_id = {str(record["id"]): record for record in records}

    def evidence(record: dict, passage: str) -> str:
        version = source_version(record)
        identifier = source_id(record)
        entry = {"id": identifier, "recordId": str(record["id"]), "source": record["source"],
                 "externalId": str(record["external_id"]), "version": version["version"],
                 "type": SOURCE_NAMES[record["source"]],
                 "date": _stamp(record.get("occurred_at"))[:10] or "Undated",
                 "recordedAt": _stamp(record.get("created_at")), "subject": str(record.get("subject") or ""),
                 "url": _source_link(record), "quotes": [passage], "quote": passage}
        sources[identifier] = _merge_source(sources[identifier], entry) if identifier in sources else entry
        return identifier

    def add(text: str, destination: str, identifiers: list[str]) -> None:
        key = (destination, _squash(text))
        if not key[1] or not identifiers:
            return
        if key in positions:
            node = lineage[destination][positions[key]]
            node["sourceIds"] = _union(node["sourceIds"], identifiers)
            return
        positions[key] = len(groups[destination])
        groups[destination].append(text)
        lineage[destination].append({"bulletId": bullet_id(destination, text), "text": text,
                                     "sourceIds": sorted(set(identifiers))})

    ordered = sorted(insights, key=lambda item: (item["type"] in ("commitment", "next_step"),
                                               _stamp(item.get("occurred_at")), item["text"]))
    for insight in ordered:
        if insight.get("type") not in ACTION_TYPES | CONTEXT_TYPES:
            continue
        if insight.get("status") in ("done", "dismissed", "possibly_done"):
            continue
        passage = insight.get("quote") or ""
        if not passage.strip():
            continue
        matched = [record for record_id in insight.get("source_record_ids") or []
                   if (record := by_id.get(str(record_id)))
                   and _squash(passage) in _squash(f"{record.get('subject') or ''} {record.get('body') or ''}")]
        # A calendar title alone proves scheduling, not a commercial promise or signal.
        substantive = [record for record in matched if record["source"] != "calendar"
                       or _squash(passage) in _squash(record.get("body") or "")]
        if not substantive:
            continue
        prefix = CONTEXT_NAMES.get(insight["type"])
        if insight["type"] in ("commitment", "next_step"):
            prefix = "We owe" if insight.get("owner_side") == "bianca" else (
                "Customer next step" if insight.get("owner_side") == "customer" else "Next step (owner to confirm)")
        text = f"{prefix}: {insight['text']}" if prefix else insight["text"]
        if insight.get("owner_name"):
            text += f" (owner: {insight['owner_name']})"
        if insight.get("due_date"):
            text += f" — due {_stamp(insight['due_date'])[:10]}"
        identifiers = []
        for record in substantive:
            covered.add(str(record["id"]))
            identifiers.append(evidence(record, passage))
        destination = "nextStep" if insight["type"] in ACTION_TYPES else "clientContext"
        add(text, destination, identifiers)

    for record in records:
        if str(record["id"]) in covered:
            continue
        subject = (record.get("subject") or "Untitled item").strip()
        day = _stamp(record.get("occurred_at"))[:10]
        when = f" ({day})" if day else " (date unconfirmed)"
        if record["source"] == "calendar":
            verb = "Meeting cancelled" if (record.get("meta") or {}).get("status") == "cancelled" else "Meeting scheduled"
            text = f"{verb}{when}: {subject}. Review the agenda and confirm any next actions."
        elif record["source"] == "notes":
            text = f"Commercial meeting notes recorded{when}: {subject}. Review the notes for context and next actions."
        else:
            text = f"Commercial email recorded{when}: {subject}. Review the message for context and any reply needed."
        passage = ((record.get("classification") or {}).get("evidence_quote") or "").strip()
        body = f"{record.get('subject') or ''} {record.get('body') or ''}"
        passage = passage if passage and _squash(passage) in _squash(body) else subject
        add(text, "nextStep", [evidence(record, passage)])

    return groups["nextStep"], groups["clientContext"], list(sources.values()), lineage


def validate_lineage(next_steps: list[str], context: list[str], sources: list[dict], lineage: dict,
                     *, allow_manual: bool = False) -> bool:
    """Check aligned IDs and actual source references without guessing from display labels."""
    if (not isinstance(next_steps, list) or not isinstance(context, list)
            or not all(isinstance(text, str) and text.strip() for text in next_steps + context)
            or not isinstance(sources, list) or not isinstance(lineage, dict)):
        return False
    identifiers = set()
    for source in sources:
        if not isinstance(source, dict) or not all(isinstance(source.get(key), str) and source[key]
                                                 for key in ("id", "source", "externalId", "version")):
            return False
        expected = "source-" + _hash({"source": source["source"], "external_id": source["externalId"],
                                      "version": source["version"]})
        if source["id"] != expected or source["id"] in identifiers:
            return False
        identifiers.add(source["id"])
    referenced = set()
    for destination, bullets in zip(DESTINATIONS, (next_steps, context)):
        nodes = lineage.get(destination)
        if not isinstance(nodes, list) or len(nodes) != len(bullets):
            return False
        for text, node in zip(bullets, nodes):
            if (not isinstance(node, dict) or node.get("bulletId") != bullet_id(destination, text)
                    or node.get("text") != text):
                return False
            ids = node.get("sourceIds")
            if (not isinstance(ids, list) or not all(isinstance(identifier, str) for identifier in ids)
                    or len(ids) != len(set(ids)) or any(identifier not in identifiers for identifier in ids)):
                return False
            if not ids and not (allow_manual and node.get("origin") == "founder_edit"):
                return False
            referenced.update(ids)
    return allow_manual or referenced == identifiers


def _parts(proposal: dict) -> Content:
    return (proposal.get("next_step_bullets", proposal.get("nextStepBullets", [])),
            proposal.get("client_context_bullets", proposal.get("clientContextBullets", [])),
            proposal.get("sources", []),
            proposal.get("bullet_lineage", proposal.get("bulletLineage", {})))


def _checked(content: Content) -> Content:
    if not isinstance(content, (tuple, list)) or len(content) != 4 or not validate_lineage(*content):
        raise ValueError("Bullet lineage is incomplete. Rebuild this proposal from its sources before reviewing it.")
    return copy.deepcopy(tuple(content))


def merge_content(existing: dict | None, new_content: Content) -> Content:
    """Keep unreviewed snapshots, append new bullets, and union deduplicated support edges."""
    incoming = _checked(new_content)
    if not existing or existing.get("lineage_version", existing.get("lineageVersion", 0)) != LINEAGE_VERSION:
        return incoming
    old = _checked(_parts(existing))
    groups = {destination: old[index] for index, destination in enumerate(DESTINATIONS)}
    lineage = old[3]
    positions = {destination: {_squash(text): index for index, text in enumerate(groups[destination])}
                 for destination in DESTINATIONS}
    sources = {source["id"]: source for source in old[2]}
    for source in incoming[2]:
        identifier = source["id"]
        sources[identifier] = _merge_source(sources[identifier], source) if identifier in sources else source
    for index, destination in enumerate(DESTINATIONS):
        for text, node in zip(incoming[index], incoming[3][destination]):
            key = _squash(text)
            if key in positions[destination]:
                previous = lineage[destination][positions[destination][key]]
                previous["sourceIds"] = _union(previous["sourceIds"], node["sourceIds"])
            else:
                positions[destination][key] = len(groups[destination])
                groups[destination].append(text)
                lineage[destination].append(node)
    return _checked((groups["nextStep"], groups["clientContext"], list(sources.values()), lineage))


def approved_lineage(proposal: dict, next_steps: list[str], context: list[str]) -> dict[str, list[dict]]:
    """Retain provenance only for unchanged text in the same approved destination."""
    has_lineage = proposal.get("lineage_version", proposal.get("lineageVersion", 0)) == LINEAGE_VERSION
    original = _checked(_parts(proposal)) if has_lineage else ([], [], [], {key: [] for key in DESTINATIONS})
    result = {destination: [] for destination in DESTINATIONS}
    for index, (destination, approved) in enumerate(zip(DESTINATIONS, (next_steps, context))):
        known = {_squash(text): node for text, node in zip(original[index], original[3][destination])}
        for text in approved:
            previous = known.get(_squash(text))
            result[destination].append({"bulletId": bullet_id(destination, text), "text": text,
                                        "sourceIds": list(previous["sourceIds"]) if previous else [],
                                        "origin": "source" if previous else "founder_edit"})
    return result
