"""Dates the founders write into the Sheet's Next step cell, read back as due dates. Pure: no AI, no DB.

The War Room Hub ends a Next step bullet with "(complete by Nov 16, 2026)" (see review_deadlines). A founder who
sets that date expects the deal to come back on it, so the brief reads the note: the bullet is due that day. A
bullet without the note has no date and is left alone.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

_BULLET = re.compile(r"^\s*(?:[-*•]\s+|\d+[.)]\s+)")
_NOTE = re.compile(r"\(\s*complete\s+by\s+(?P<date>[^()]{3,40}?)\s*\)\s*[.!?]?", re.IGNORECASE)
# What the Hub puts in front of a bullet and after it; none of it is part of the step's own wording.
_LABEL = re.compile(
    r"^(?:we owe|customer committed|commitment \(owner to confirm\)|next step \(owner to confirm\)|customer next step|"
    r"next step|customer asked|blocker)\s*:\s*", re.IGNORECASE)
_OWNER = re.compile(r"\(owner:[^)]*\)", re.IGNORECASE)
_HUB_DATE = re.compile(r"\s[—–-]\s(?:due|requested by)\s\d{4}-\d{2}-\d{2}\s*$", re.IGNORECASE)
_INLINE_DATE = re.compile(
    r"\b(?:by|before|on|due|until)\s+(?:the\s+)?(?:\d{4}-\d{2}-\d{2}|[a-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+[a-z]{3,9}\.?(?:,?\s+\d{4})?|(?:mon|tues?|wed(?:nes)?|thur?s?|fri|sat(?:ur)?|sun)(?:day)?)\b",
    re.IGNORECASE)
_MONTHS = {name: number for number, names in enumerate((
    ("jan", "ene"), ("feb",), ("mar",), ("apr", "abr"), ("may",), ("jun",), ("jul",), ("aug", "ago"),
    ("sep", "set"), ("oct",), ("nov",), ("dec", "dic")), start=1) for name in names}
_WORDS = re.compile(r"[^\W_]+")
MAX_LINE = 2000        # a Next step bullet longer than this is read up to here; a whole cell holds at most 50,000
MAX_CONTEXT = 400      # how much of the first bullet a context line (prep, quiet deals) may quote
_FILLER = frozenset("the a an to of for and with on in at by el la los las de del un una para con y".split())


@dataclass(frozen=True)
class SheetStep:
    text: str       # the bullet as the founder left it, without its bullet mark and date note
    due: date
    key: str        # its wording reduced for matching against an extracted item (see match_key)


def _year_for(month: int, day: int, today: date) -> date:
    """A date written without a year means its nearest occurrence (Jan 5 written in November is next January)."""
    shift = 1 if today.month - month > 6 else -1 if month - today.month > 6 else 0
    return date(today.year + shift, month, day)


def parse_date(value: str, today: date) -> date | None:
    """"Nov 16, 2026", "16 Nov 2026", "16 de noviembre de 2026", "2026-11-16", "Nov 16" -> a date.

    A slash date is read only when it is unambiguous (25/11/2026); 03/04/2026 could be March or April, so it is
    not guessed. Anything unreadable is None and the bullet is treated as undated.
    """
    text = " ".join(value.replace(",", " ").split()).lower()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return date.fromisoformat(text)
        if slash := re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?", text):
            first, second = int(slash[1]), int(slash[2])
            if first <= 12 and second <= 12 and first != second:
                return None
            day, month = (first, second) if first > 12 or first == second else (second, first)
            return date(int(slash[3]), month, day) if slash[3] else _year_for(month, day, today)
        words = [w.rstrip(".") for w in text.split() if w not in ("de", "del", "of", "the")]
        words = [re.sub(r"(?<=\d)(?:st|nd|rd|th)$", "", w) for w in words]
        if len(words) not in (2, 3):
            return None
        year = int(words[2]) if len(words) == 3 else None
        if year is not None and year < 100:
            year += 2000                # "Nov 16, 26" is 2026, not the year 26
        day_text, month_text = (words[0], words[1]) if words[0].isdigit() else (words[1], words[0])
        month = _MONTHS.get(month_text[:3])
        if month is None or not day_text.isdigit():
            return None
        return date(year, month, int(day_text)) if year else _year_for(month, int(day_text), today)
    except ValueError:
        return None


def match_key(text: str) -> str:
    """The step's own words: no label, owner note, date or filler, lowercased and without accents.

    "We owe: Send the checklist. (owner: Nico) (complete by Nov 6, 2026)" and the extracted "Send the checklist by
    Nov 6" both reduce to "send checklist".
    """
    text = _NOTE.sub(" ", _BULLET.sub("", " ".join(text.split())[:MAX_LINE]))
    text = _HUB_DATE.sub(" ", _OWNER.sub(" ", text))
    text = _INLINE_DATE.sub(" ", _LABEL.sub("", text.strip()))
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return " ".join(w for w in _WORDS.findall(plain) if w not in _FILLER)


def same_step(key: str, other: str) -> bool:
    """Same wording, or one is the other with a few words added (a founder's light edit in the Hub)."""
    if not key or not other:
        return False
    if key == other:
        return True
    a, b = set(key.split()), set(other.split())
    small, large = (a, b) if len(a) <= len(b) else (b, a)
    return len(small) >= 3 and len(small & large) >= 0.8 * len(small) and len(small) >= 0.5 * len(large)


def parse_cell(cell: str | None, today: date) -> list[SheetStep]:
    """Every bullet of a Next step cell that carries a readable "(complete by <date>)" note, in cell order."""
    steps = []
    for line in (cell or "").splitlines():
        line = " ".join(line.split())[:MAX_LINE]    # single spaces: the note pattern cannot backtrack over a long gap
        note = _NOTE.search(line)
        due = parse_date(note["date"], today) if note else None
        if due is None:
            continue
        text = " ".join(_NOTE.sub(" ", _BULLET.sub("", line)).split()).strip(" -–—")
        text = re.sub(r"^next step\s*:\s*", "", text, flags=re.IGNORECASE)
        if text:
            steps.append(SheetStep(text, due, match_key(line)))
    return steps


def first_bullet(cell: str | None) -> str:
    """The first line of the Next step cell as plain text, for context lines (prep, quiet deals)."""
    for line in (cell or "").splitlines():
        text = " ".join(_BULLET.sub("", line).split())
        if text:
            return text[:MAX_CONTEXT]
    return ""
