"""Pure review bullet generation and versioned, per-bullet source lineage.

Labels and dates are presentation only. Edges point to a source-content version,
and deduplicated bullet text keeps the union of the records that support it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import date, datetime
from typing import Any
from urllib.parse import quote

from app import extractor
from app.review_deadlines import normalize_deadline


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


def _supporting_record(insight: dict, passage: str, records: dict[str, dict]) -> dict | None:
    def supports(record: dict) -> bool:
        source = record["source"]
        # A calendar title or a Gmail subject carried through a thread cannot establish a new request.
        body_only = source == "calendar" or (source == "gmail" and insight["type"] == "next_step")
        return extractor.quote_in_record(passage, record, body_only=body_only)

    if (origin_id := insight.get("origin_record_id")) is not None:
        origin = records.get(str(origin_id))
        return origin if origin is not None and supports(origin) else None

    # Older insights identify a group of possible sources. Attribute the bullet only when one holds the quote.
    candidate_ids = {str(record_id) for record_id in insight.get("source_record_ids") or ()}
    matches = [records[record_id] for record_id in candidate_ids if record_id in records and supports(records[record_id])]
    return matches[0] if len(matches) == 1 else None


_OWNER_ORDER = {"bianca": 0, None: 1, "customer": 2}
_BLOCKER_ORDER = 3
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_FILLER = frozenset("a an and are as at be by for from in is it of on or that the their them they this to was "
                    "we were will with bianca customer".split())


def _terms(text: str) -> frozenset[str]:
    return frozenset(word for word in _WORD.findall(text.casefold()) if len(word) > 2 and word not in _FILLER)


def _same_step(left: frozenset[str], right: frozenset[str]) -> bool:
    """Two wordings of one step: at least three in four meaningful words of the shorter one appears in the other."""
    smaller = min(len(left), len(right))
    return smaller >= 4 and len(left & right) / smaller >= 0.75


def reviewed_content(records: list[dict], insights: list[dict], *, max_next_steps: int | None = None,
                     max_context: int | None = None) -> tuple[Content, list[dict]]:
    """Build the review bullets, and name the source versions whose only bullets were left off the card.

    Next Step leads with Bianca's own actions, newest first; customer-owed items and blockers follow.
    Two wordings of the same step become one bullet with both sources. When a limit is set, the newest
    bullets stay. Sources that supported only removed bullets are returned separately so a decision on
    the card can also retire them, instead of the old bullets returning on the next build.
    """
    by_id = {str(record["id"]): record for record in records}
    candidates: dict[str, list[dict]] = {destination: [] for destination in DESTINATIONS}
    positions: dict[tuple[str, str], dict] = {}

    ordered = sorted(insights, key=lambda item: (item["type"] in ("commitment", "next_step"),
                                               _stamp(item.get("occurred_at")), item["text"]))
    for insight in ordered:
        kind = insight["type"]
        text = insight["text"].strip()
        if kind not in ACTION_TYPES | CONTEXT_TYPES or insight.get("action_scope") not in extractor.SALES_SCOPES:
            continue
        if not text or insight.get("status") in ("done", "dismissed", "possibly_done"):
            continue
        passage = insight.get("quote") or ""
        if not (record := _supporting_record(insight, passage, by_id)):
            continue
        terms = _terms(text)
        prefix = CONTEXT_NAMES.get(kind)
        owner_side = insight.get("owner_side")
        if kind == "commitment":
            prefix = {"bianca": "We owe", "customer": "Customer committed"}.get(
                owner_side, "Commitment (owner to confirm)")
        elif kind == "next_step":
            prefix = {"bianca": "Next step", "customer": "Customer next step"}.get(
                owner_side, "Next step (owner to confirm)")
        text = f"{prefix}: {text}" if prefix else text
        if insight.get("owner_name"):
            text += f" (owner: {insight['owner_name']})"
        if kind in ("commitment", "next_step") and insight.get("due_date"):
            label = "requested by" if kind == "next_step" else "due"
            text += f" — {label} {_stamp(insight['due_date'])[:10]}"
        destination = "nextStep" if kind in ACTION_TYPES else "clientContext"
        key = (destination, _squash(text))
        if not key[1]:
            continue
        day = _stamp(insight.get("occurred_at"))[:10]
        rank = (_BLOCKER_ORDER if kind == "blocker" else _OWNER_ORDER.get(owner_side, 1)) if destination == "nextStep" else 0
        entry = positions.get(key)
        if entry is None:
            entry = {"text": text, "terms": terms, "day": day, "rank": rank, "evidence": []}
            positions[key] = entry
            candidates[destination].append(entry)
        entry["day"] = max(entry["day"], day)
        entry["evidence"].append((record, passage))

    # Prefer Bianca's own wording, then the newer source, when two bullets describe the same step.
    steps: list[dict] = []
    for entry in sorted(candidates["nextStep"], key=lambda item: (item["rank"], _reverse(item["day"]), item["text"])):
        twin = next((kept for kept in steps if _same_step(kept["terms"], entry["terms"])), None)
        if twin is None:
            steps.append(entry)
        else:
            twin["day"] = max(twin["day"], entry["day"])
            twin["evidence"].extend(entry["evidence"])
    candidates["nextStep"] = steps

    removed: list[dict] = []
    for destination, limit in zip(DESTINATIONS, (max_next_steps, max_context)):
        entries = candidates[destination]
        if limit is not None and limit > 0 and len(entries) > limit:
            newest = sorted(entries, key=lambda item: (_reverse(item["day"]), item["rank"], item["text"]))
            keep = {id(item) for item in newest[:limit]}
            removed.extend(item for item in entries if id(item) not in keep)
            entries = [item for item in entries if id(item) in keep]
        if destination == "nextStep":
            entries = sorted(entries, key=lambda item: (item["rank"], _reverse(item["day"]), item["text"]))
        candidates[destination] = entries

    groups: dict[str, list[str]] = {destination: [] for destination in DESTINATIONS}
    lineage: dict[str, list[dict]] = {destination: [] for destination in DESTINATIONS}
    sources: dict[str, dict] = {}

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

    for destination in DESTINATIONS:
        for entry in candidates[destination]:
            identifiers = sorted({evidence(record, passage) for record, passage in entry["evidence"]})
            groups[destination].append(entry["text"])
            lineage[destination].append({"bulletId": bullet_id(destination, entry["text"]), "text": entry["text"],
                                         "sourceIds": identifiers})

    retired: dict[str, dict] = {}
    for entry in removed:
        for record, _ in entry["evidence"]:
            if source_id(record) not in sources:
                retired[source_id(record)] = source_version(record)
    content = groups["nextStep"], groups["clientContext"], list(sources.values()), lineage
    return content, sorted(retired.values(), key=lambda item: (item["source"], item["external_id"], item["version"]))


def _reverse(day: str) -> tuple[int, ...]:
    """Sort key that puts the latest ISO day first and undated items last."""
    return tuple(-ord(char) for char in day) if day else (1,)


def proposal_content(records: list[dict], insights: list[dict], *, max_next_steps: int | None = None,
                     max_context: int | None = None) -> Content:
    """Build sales review bullets and attribute each one to the source of its quote."""
    return reviewed_content(records, insights, max_next_steps=max_next_steps, max_context=max_context)[0]


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


def review_next_step_bullets(proposal: dict, dated_source_ids: frozenset[str] = frozenset()) -> list[str]:
    """Format pending bullets using the dates captured with their proposal."""
    bullets, _, sources, lineage = _parts(proposal)
    edges = lineage.get("nextStep") if isinstance(lineage, dict) else None
    if not isinstance(edges, list) or not isinstance(sources, list) or len(edges) != len(bullets):
        return bullets

    source_dates = {
        source["id"]: extractor.parse_due(source.get("date"))
        for source in sources if isinstance(source, dict) and isinstance(source.get("id"), str)
        and isinstance(source.get("date"), str)
    }
    proposal_date = extractor.parse_due(_stamp(proposal.get("created_at"))[:10])
    formatted = []
    representatives = {}
    for bullet, edge in zip(bullets, edges):
        if not isinstance(bullet, str):
            return bullets
        source_ids = edge.get("sourceIds") if isinstance(edge, dict) else None
        if not isinstance(source_ids, list):
            source_ids = []
        dates = {identifier: source_dates.get(identifier) for identifier in source_ids if isinstance(identifier, str)}
        reference = max((day for day in dates.values() if day is not None), default=proposal_date)
        text = normalize_deadline(bullet, reference, allow_relative=False)
        if text == bullet:
            # Repeated suggestions can have different source days. Only use a relative date they agree on.
            anchors = {day for identifier, day in dates.items() if day is not None and identifier in dated_source_ids}
            resolved = {normalize_deadline(bullet, anchor) for anchor in anchors}
            if len(resolved) == 1:
                text = resolved.pop()
        # Keep the source indexes aligned when two phrasings become the same bullet.
        formatted.append(representatives.setdefault(_squash(text), text))
    return formatted


def approved_lineage(proposal: dict, next_steps: list[str], context: list[str], *,
                     review_next_steps: list[str] | None = None) -> dict[str, list[dict]]:
    """Retain provenance for original text and its automatic deadline formatting."""
    has_lineage = proposal.get("lineage_version", proposal.get("lineageVersion", 0)) == LINEAGE_VERSION
    original = _checked(_parts(proposal)) if has_lineage else ([], [], [], {key: [] for key in DESTINATIONS})
    result = {destination: [] for destination in DESTINATIONS}
    formatted_sources: dict[str, list[str]] = {}
    if review_next_steps is not None:
        for text, node in zip(review_next_steps, original[3]["nextStep"]):
            formatted_sources[text] = _union(formatted_sources.get(text, []), node["sourceIds"])

    for index, (destination, approved) in enumerate(zip(DESTINATIONS, (next_steps, context))):
        known = {_squash(text): node for text, node in zip(original[index], original[3][destination])}
        for text in approved:
            previous = known.get(_squash(text))
            source_ids = list(previous["sourceIds"]) if previous else []
            if destination == "nextStep" and text in formatted_sources:
                source_ids = _union(source_ids, formatted_sources[text])
            result[destination].append({"bulletId": bullet_id(destination, text), "text": text,
                                        "sourceIds": source_ids,
                                        "origin": "source" if source_ids else "founder_edit"})
    return result
