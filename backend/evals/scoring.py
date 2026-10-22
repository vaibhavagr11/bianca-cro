"""Pure helpers for the eval: statement listing, Sheet bullet parsing, score aggregation."""

import re
from collections import Counter

PROFILE_SECTIONS = ["summary", "momentum_reason", "needs", "buying_signals", "objections", "blockers",
                    "open_commitments", "recommended_actions"]


def profile_statements(profile: dict) -> list[dict]:
    """Flatten a profile into checkable statements: section, text, cited refs."""
    out = []
    for section in PROFILE_SECTIONS:
        value = profile.get(section)
        items = value if isinstance(value, list) else [value] if value else []
        for it in items:
            if section == "open_commitments":
                text = f"{it['text']} (owner: {it.get('owner') or '?'}; due: {it.get('due') or '-'})"
            elif section == "recommended_actions":
                text = f"Recommended: {it['action']}. Why: {it['why']}"
            else:
                text = it["text"]
            out.append({"section": section, "text": text, "evidence": it.get("evidence", [])})
    return out


_BULLET = re.compile(r"^\s*(?:[-•*]|\d+[.)])\s*")


def sheet_bullets(*cells: str | None) -> list[str]:
    """Split the founders' 'Next step' / 'Last progress' cells into individual items."""
    bullets = []
    for cell in cells:
        for line in (cell or "").splitlines():
            text = _BULLET.sub("", line).strip()
            if len(text) >= 8:
                bullets.append(text)
    return bullets


def rate(verdicts: list[str], good: str = "supported", partial: str = "partly_supported") -> dict:
    """Counts plus strict (good only) and lenient (good + partial) rates."""
    c = Counter(verdicts)
    n = len(verdicts)
    return {
        "n": n,
        **{k: c.get(k, 0) for k in (good, partial)},
        "other": n - c.get(good, 0) - c.get(partial, 0),
        "strict": round(c.get(good, 0) / n, 3) if n else None,
        "lenient": round((c.get(good, 0) + c.get(partial, 0)) / n, 3) if n else None,
    }
