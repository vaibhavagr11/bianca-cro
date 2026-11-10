"""Bind each proposed email action to a verifier input and apply the verdict before rendering."""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app import brief, brief_v2
from app.brief_verification import VerificationAction, Verdict

Gate = Callable[[list[VerificationAction]], dict[str, Verdict]]
Describe = Callable[[Any], tuple[str, str, str | None]]


@dataclass(frozen=True)
class ActionSource:
    kind: str
    record_id: str | None
    occurred_at: datetime | None

    @classmethod
    def insight(cls, kind: str, row: dict) -> "ActionSource":
        record_id = row.get("origin_record_id")
        return cls(kind, str(record_id) if record_id else None, row.get("origin_occurred_at"))


def profile_source(profile: dict, entry: dict, insights: dict[str, dict]) -> ActionSource:
    refs = [ref for ref in entry.get("evidence") or [] if ref.startswith("I")]
    resolved = [insights.get(str((profile.get("refs") or {}).get(ref))) for ref in refs]
    anchors = [r for r in resolved if r and r.get("origin_record_id") and r.get("origin_occurred_at")]
    if anchors and len(anchors) == len(refs):
        return ActionSource.insight("recommendation", min(anchors, key=lambda r: r["origin_occurred_at"]))
    return ActionSource("recommendation", None, None)


@dataclass
class _Row:
    value: Any
    deal: str
    text: str
    ref: str | None
    key: str | None


@dataclass
class _Group:
    rows: list
    descriptions: list[_Row]
    notes: brief.VerificationNotes
    waiting: bool
    remove_prep: bool


class _Inventory:
    def __init__(self, companies: dict[str, str], sources: dict[str, ActionSource]):
        self.companies = companies
        self.sources = sources
        self.actions: dict[str, VerificationAction] = {}
        self.groups: list[_Group] = []

    def add(self, rows: list, describe: Describe, notes: brief.VerificationNotes, *, waiting: bool = False,
            remove_prep: bool = False) -> None:
        descriptions = []
        for value in rows:
            deal, text, ref = describe(value)
            key = None
            if text or not remove_prep:
                if not text:
                    text, ref = "Missing action details", None
                company_id = self.companies.get(deal, "")
                source = self.sources.get(ref) if ref and company_id else None
                if source is None:
                    source = ActionSource("waiting" if waiting else "recommendation", None, None)
                digest = hashlib.sha256(f"{company_id or deal}\0{ref or ''}\0{text}".encode()).hexdigest()[:24]
                key = f"{ref or 'unanchored'}:{digest}"
                self.actions[key] = VerificationAction(key, source.kind, text, company_id,
                                                       source.record_id, source.occurred_at)
            descriptions.append(_Row(value, deal, text, ref, key))
        self.groups.append(_Group(rows, descriptions, notes, waiting, remove_prep))

    def apply(self, gate: Gate) -> set[str]:
        verdicts = gate(sorted(self.actions.values(), key=lambda action: action.key))
        open_refs = set()
        for group in self.groups:
            visible = []
            for row in group.descriptions:
                verdict = verdicts.get(row.key)
                state = verdict.state if verdict else "unclear"
                if row.key and not self.actions[row.key].origin_record_id:
                    state = "unclear"
                expected = "waiting" if group.waiting else "still_owed"
                if row.key is None or state == expected:
                    visible.append(row.value)
                    if row.ref and state == "still_owed":
                        open_refs.add(row.ref)
                    continue
                if group.remove_prep:
                    visible.append((row.value[0], ""))
                if state == "handled":
                    continue
                notes = group.notes.waiting if state == "waiting" else group.notes.unclear
                if (row.deal, row.text) not in notes:
                    notes.append((row.deal, row.text))
            group.rows[:] = visible
        return open_refs


def verify_v1(sections: list[dict], ndl: list[brief.Row], companies: dict[str, str],
              sources: dict[str, ActionSource], gate: Gate) -> brief.VerificationNotes:
    inventory = _Inventory(companies, sources)

    def describe(row: brief.Row) -> tuple[str, str, str | None]:
        return row.company, row.what, row.verification_ref

    for section in sections:
        notes = section["verification"] = brief.VerificationNotes()
        inventory.add(section["today"], describe, notes)
        inventory.add(section["week"], describe, notes)
        inventory.add(section["meetings"], lambda row: (row[0].deal or row[0].title, row[1], None), notes,
                      remove_prep=True)
    ndl_notes = brief.VerificationNotes()
    inventory.add(ndl, describe, ndl_notes)
    inventory.apply(gate)
    return ndl_notes


def verify_v2(sections: list[brief_v2.Section], ndl: list[brief_v2.TodayRow], companies: dict[str, str],
              sources: dict[str, ActionSource], gate: Gate) -> brief.VerificationNotes:
    inventory = _Inventory(companies, sources)

    def describe(row: brief_v2.TodayRow) -> tuple[str, str, str | None]:
        return row.deal, row.text, row.done

    for section in sections:
        notes = section.verification = brief.VerificationNotes()
        inventory.add(section.today, describe, notes)
        inventory.add(section.highlights, lambda row: (row[0].deal, row[1].text, row[1].done), notes)
        inventory.add(section.followups, lambda row: (row.deal or "", row.text, row.done), notes)
        inventory.add(section.waiting, lambda row: (row.deal, brief_v2.waiting_text(row), f"wait:{row.thread_id}"),
                      notes, waiting=True)
        inventory.add(section.backlog.exceptions, lambda row: (row.deal, row.text, f"insight:{row.id}"), notes)
        inventory.add(section.unclear, lambda row: (row.deal, "Confirm deal status", None), notes)
        inventory.add(section.meetings, lambda row: (row[0].deal or row[0].title, row[1], None), notes, remove_prep=True)
    ndl_notes = brief.VerificationNotes()
    inventory.add(ndl, describe, ndl_notes)
    open_refs = inventory.apply(gate)
    for section in sections:
        section.more_followups = section.more_waiting = 0
        section.all_items = [i for i in section.all_items if f"insight:{i.id}" in open_refs]
    return ndl_notes
