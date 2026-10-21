"""Deterministic helpers for the pipeline sheet. No AI here."""

import re
import unicodedata

_STAGE_RE = re.compile(r"^\s*(\d+)\s*[.)-]")


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
