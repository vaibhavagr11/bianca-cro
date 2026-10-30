"""Deterministic logic for the pipeline sheet: parsing rows and working out what changed. No AI, no DB."""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field

_STAGE_RE = re.compile(r"^\s*(\d+)\s*[.)-]")

# Sheet header (lowercased, trimmed) -> our field name. Unlisted columns are ignored.
COLUMN_MAP = {
    "empresa": "company",
    "owner": "owner_code",
    "status": "stage",
    "country": "country",
    "primary buyer": "primary_buyer",
    "nombre contacto (rol)": "contacts_text",
    "last progress": "last_progress",
    "next step": "next_step",
}
REQUIRED_COLUMN = "empresa"

# Fields compared between syncs; a change in any of them is logged to opportunity_changes.
TRACKED_FIELDS = ("owner_code", "stage", "country", "primary_buyer", "contacts_text", "last_progress", "next_step")

# Refuse a sync that would remove more than this share of active deals (wrong tab, empty file...).
MAX_REMOVAL_SHARE = 0.5


class PipelineValidationError(ValueError):
    """The payload can't be trusted as a pipeline snapshot. Nothing is written."""


class SyncRefused(Exception):
    """The payload is well-formed but applying it looks dangerous. Nothing is written."""


def normalize_name(name: str) -> str:
    """Match key for company names: 'FEMSA  Arg ' and 'femsa arg' map to the same company."""
    no_accents = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return " ".join(no_accents.lower().split())


def parse_stage(stage: str | None) -> int | None:
    """'2. In Talks' -> 2. Returns None when the sheet value has no leading number."""
    if not stage:
        return None
    match = _STAGE_RE.match(stage)
    return int(match.group(1)) if match else None


@dataclass(frozen=True)
class PipelineRow:
    company: str
    normalized_name: str
    fields: dict[str, str | None]  # keyed by TRACKED_FIELDS

    @property
    def row_hash(self) -> str:
        payload = json.dumps([self.company, [self.fields[f] for f in TRACKED_FIELDS]], ensure_ascii=False)
        return hashlib.sha256(payload.encode()).hexdigest()


def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_rows(raw_rows: list[dict]) -> list[PipelineRow]:
    """Map sheet rows to PipelineRows. Skips rows with a blank company; rejects missing columns and duplicates."""
    if not raw_rows:
        raise PipelineValidationError("payload has no rows")

    headers = {h.strip().lower() for row in raw_rows for h in row}
    if REQUIRED_COLUMN not in headers:
        raise PipelineValidationError("required column 'Empresa' not found")

    rows: list[PipelineRow] = []
    seen: dict[str, int] = {}
    for raw in raw_rows:
        mapped = {COLUMN_MAP[h.strip().lower()]: _clean(v) for h, v in raw.items() if h.strip().lower() in COLUMN_MAP}
        company = mapped.get("company")
        if not company:
            continue
        key = normalize_name(company)
        seen[key] = seen.get(key, 0) + 1
        rows.append(PipelineRow(company, key, {f: mapped.get(f) for f in TRACKED_FIELDS}))

    duplicates = sorted(k for k, n in seen.items() if n > 1)
    if duplicates:
        raise PipelineValidationError(f"duplicate companies: {', '.join(duplicates)}")
    if not rows:
        raise PipelineValidationError("no rows with a company name")
    return rows


@dataclass(frozen=True)
class ExistingDeal:
    opportunity_id: str
    normalized_name: str
    fields: dict[str, str | None]
    row_hash: str
    removed: bool


@dataclass
class SyncPlan:
    creates: list[PipelineRow] = field(default_factory=list)
    # (opportunity_id, new row, [(field, old, new)], restored)
    updates: list[tuple[str, PipelineRow, list[tuple[str, str | None, str | None]], bool]] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)  # opportunity ids seen again with no change
    removals: list[ExistingDeal] = field(default_factory=list)


def plan_sync(existing: list[ExistingDeal], incoming: list[PipelineRow], guard: bool = True) -> SyncPlan:
    """Compare the stored pipeline with a fresh snapshot. Pure function: no I/O.

    With guard (the default), refuse a snapshot that would remove too many deals."""
    by_key = {deal.normalized_name: deal for deal in existing}
    plan = SyncPlan()

    for row in incoming:
        deal = by_key.get(row.normalized_name)
        if deal is None:
            plan.creates.append(row)
        elif deal.row_hash == row.row_hash and not deal.removed:
            plan.unchanged.append(deal.opportunity_id)
        else:
            changes = [(f, deal.fields.get(f), row.fields[f]) for f in TRACKED_FIELDS if deal.fields.get(f) != row.fields[f]]
            plan.updates.append((deal.opportunity_id, row, changes, deal.removed))

    incoming_keys = {row.normalized_name for row in incoming}
    plan.removals = [d for d in existing if not d.removed and d.normalized_name not in incoming_keys]

    active = sum(1 for d in existing if not d.removed)
    if guard and active and len(plan.removals) > active * MAX_REMOVAL_SHARE:
        raise SyncRefused(f"would remove {len(plan.removals)} of {active} active deals")
    return plan


def describe_plan(plan: SyncPlan, existing: list[ExistingDeal], raw_rows: list[dict]) -> dict:
    """What a sync would do, for a dry run: columns read, deals created / removed, and every changed field."""
    headers = sorted({h.strip() for row in raw_rows for h in row})
    read = {h.strip().lower() for h in headers}
    active = sum(1 for d in existing if not d.removed)
    return {
        "columns_read": [h for h in headers if h.lower() in COLUMN_MAP],
        "columns_ignored": [h for h in headers if h.lower() not in COLUMN_MAP],
        "columns_missing": [h for h in COLUMN_MAP if h not in read],
        "created": [r.company for r in plan.creates],
        "removed": [d.normalized_name for d in plan.removals],
        "restored": [r.company for _, r, _, restored in plan.updates if restored],
        "changes": [{"company": r.company, "field": f, "old": old, "new": new}
                    for _, r, changes, _ in plan.updates for f, old, new in changes],
        "changed_deals": len(plan.updates),
        "unchanged": len(plan.unchanged),
        "would_be_refused": bool(active and len(plan.removals) > active * MAX_REMOVAL_SHARE),
    }
