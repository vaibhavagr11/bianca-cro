"""Deterministic handling of Bianca's meeting-notes doc: split into meetings, date them, give stable IDs.

The doc is newest-first, meetings are separated by runs of blank lines, and headers mix Spanish/English
date formats (or have none). No AI here; the classifier fills gaps later.
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import date, timedelta

from app.gmail import redact_secrets
from app.pipeline import normalize_name

MAX_BODY_CHARS = 12_000

# Three or more blank lines separate meetings (two blank lines also occur inside a meeting).
_SECTION_BREAK = re.compile(r"\n(?:[ \t\u000b]*\n){3,}")
# Chunks starting like this are a continuation of the previous meeting, not a new one.
_CONTINUATION_WORDS = ("resumen", "next steps", "next step", "notas", "nota", "preguntas", "pregunta", "agenda",
                       "pre:", "comment", "feedback", "takeaways", "q/a")

_MONTHS = {
    "ene": 1, "enero": 1, "jan": 1, "january": 1,
    "feb": 2, "febrero": 2, "february": 2,
    "mar": 3, "marzo": 3, "march": 3,
    "abr": 4, "abril": 4, "apr": 4, "april": 4,
    "may": 5, "mayo": 5,
    "jun": 6, "junio": 6, "june": 6,
    "jul": 7, "julio": 7, "july": 7,
    "ago": 8, "agosto": 8, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "set": 9, "septiembre": 9, "setiembre": 9, "september": 9,
    "oct": 10, "octubre": 10, "october": 10,
    "nov": 11, "noviembre": 11, "november": 11,
    "dic": 12, "diciembre": 12, "dec": 12, "december": 12,
}
_DAY_MONTH_NAME = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:de\s+|of\s+)?([a-záéíóú]{3,10})\.?(?:,?\s+(?:de\s+)?(\d{4}))?",
                             re.I)
_MONTH_NAME_DAY = re.compile(r"\b([a-záéíóú]{3,10})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4}))?", re.I)
_NUMERIC = re.compile(r"(?<![\d:])(\d{1,2})[-/](\d{1,2})(?:[-/](\d{2,4}))?(?![\d:])")


@dataclass
class Section:
    header: str
    text: str
    meeting_date: date | None = None
    date_inferred: bool = False
    section_id: str = ""


def _make_date(day: int, month: int, year: int | None, today: date) -> date | None:
    try:
        if year is not None:
            year = year + 2000 if year < 100 else year
            found = date(year, month, day)
        else:
            found = date(today.year, month, day)
            if found > today + timedelta(days=7):  # no year given: the most recent past occurrence
                found = date(today.year - 1, month, day)
    except ValueError:
        return None
    return found if found <= today + timedelta(days=60) else None


def parse_header_date(lines: list[str], today: date) -> date | None:
    """Find a meeting date in the first two lines of a section, in any of the doc's formats."""
    for line in lines[:2]:
        for m in _DAY_MONTH_NAME.finditer(line):
            month = _MONTHS.get(m.group(2).lower())
            if month:
                found = _make_date(int(m.group(1)), month, int(m.group(3)) if m.group(3) else None, today)
                if found:
                    return found
        for m in _MONTH_NAME_DAY.finditer(line):
            month = _MONTHS.get(m.group(1).lower())
            if month:
                found = _make_date(int(m.group(2)), month, int(m.group(3)) if m.group(3) else None, today)
                if found:
                    return found
        for m in _NUMERIC.finditer(line):
            day, month = int(m.group(1)), int(m.group(2))
            if 1 <= month <= 12:
                found = _make_date(day, month, int(m.group(3)) if m.group(3) else None, today)
                if found:
                    return found
    return None


def _looks_like_header(first_line: str, has_date: bool) -> bool:
    line = first_line.strip()
    if not line:
        return False
    if has_date or "<>" in line:
        return True
    lowered = line.lower()
    if lowered.startswith(_CONTINUATION_WORDS) or line[0] in "-•*>(" or not (line[0].isupper() or line[0].isdigit()):
        return False
    return len(line) <= 60 and not line.endswith(":")


_SHORT_DATED_HEADER = re.compile(r"[A-ZÁÉÍÓÚ][\w .]{1,20}\s\d{1,2}-\d{1,2}")  # "Gus RG 15-10"


def is_meeting_header(line: str, today: date) -> bool:
    """A dated meeting header that can start a new meeting without the usual run of blank lines before it:
    'X <> Bianca … <date>', 'Company, 19 Agosto 2026' or 'Name 15-10'. A bullet, a 'Resumen …' line or a
    sentence that merely mentions a date is not one."""
    line = line.strip()
    if (not line or len(line) > 80 or line[0] in "-•*>(·–" or line[0].isdigit()
            or line.lower().startswith(_CONTINUATION_WORDS) or parse_header_date([line], today) is None):
        return False
    words = line.split()
    return ("<>" in line or bool(_SHORT_DATED_HEADER.fullmatch(line))
            or (len(words) <= 9 and "(" not in line and any(w.strip(",.").lower() in _MONTHS for w in words)))


def _chunks(content: str, today: date) -> list[str]:
    """Split at runs of blank lines, and also before a dated meeting header that follows a blank line."""
    out = []
    for chunk in _SECTION_BREAK.split(content.replace("\r\n", "\n")):
        lines, start = chunk.split("\n"), 0
        for i in range(1, len(lines)):
            if not lines[i - 1].strip() and is_meeting_header(lines[i], today):
                out.append("\n".join(lines[start:i]))
                start = i
        out.append("\n".join(lines[start:]))
    return out


def split_sections(content: str, today: date) -> list[Section]:
    """Doc text -> meetings in doc order (newest first), with dates and stable IDs."""
    sections: list[Section] = []
    for chunk in _chunks(content, today):
        lines = [ln for ln in chunk.strip().split("\n") if ln.strip()]
        if not lines:
            continue
        found = parse_header_date(lines, today)
        if sections and not _looks_like_header(lines[0], found is not None):
            sections[-1].text += "\n\n" + chunk.strip()  # continuation of the previous meeting
            continue
        sections.append(Section(header=lines[0].strip(), text=chunk.strip(), meeting_date=found))

    # Undated meetings take the date of the nearest dated meeting below them (the doc is newest-first).
    carried = None
    for s in reversed(sections):
        if s.meeting_date:
            carried = s.meeting_date
        elif carried:
            s.meeting_date, s.date_inferred = carried, True

    # Stable IDs: header + occurrence counted from the bottom, so new meetings added on top don't shift old IDs.
    seen: dict[str, int] = {}
    for s in reversed(sections):
        key = normalize_name(s.header)[:80]
        seen[key] = seen.get(key, 0) + 1
        s.section_id = hashlib.sha1(f"{key}|{seen[key]}".encode()).hexdigest()[:16]
    return sections


def recent_sections(sections: list[Section], today: date, window_days: int) -> list[Section]:
    """Top-down until the first meeting with an explicit date older than the window."""
    cutoff = today - timedelta(days=window_days)
    kept = []
    for s in sections:
        if s.meeting_date and not s.date_inferred and s.meeting_date < cutoff:
            break
        kept.append(s)
    return kept


def clean_body(section: Section) -> str:
    return redact_secrets(section.text)[:MAX_BODY_CHARS]
