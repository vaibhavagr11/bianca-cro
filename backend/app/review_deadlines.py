"""Format clear calendar deadlines in War Room Next Step bullets."""

import re
from calendar import monthrange
from datetime import date, timedelta


_MONTHS = (
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
)
_MONTH_NUMBERS = {"sept": 9}
for _number, _name in enumerate(_MONTHS, 1):
    _MONTH_NUMBERS[_name.lower()] = _number
    _MONTH_NUMBERS[_name[:3].lower()] = _number

_MONTH = "(?:" + "|".join(sorted(_MONTH_NUMBERS, key=len, reverse=True)) + ")"
_DAY = r"\d{1,2}(?:st|nd|rd|th)?"
_CALENDAR_DATE = (
    rf"(?:\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}/\d{{1,2}}(?:/\d{{4}})?"
    rf"|{_MONTH}\.?\s+{_DAY}(?:,?\s+\d{{4}})?"
    rf"|{_DAY}\s+{_MONTH}(?:\.?,?\s+\d{{4}})?)"
)
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WEEKDAY_NUMBERS = {name: index for index, name in enumerate(_WEEKDAYS)}
_WEEKDAY_NUMBERS.update({name[:3]: index for index, name in enumerate(_WEEKDAYS)})
_WEEKDAY_NUMBERS.update({"tues": 1, "thur": 3, "thurs": 3})
_WEEKDAY = "(?:" + "|".join(sorted(_WEEKDAY_NUMBERS, key=len, reverse=True)) + ")"
_WEEKDAY_MODIFIER = r"(?:(?:this|next|coming)\s+)?"
_WEEKDAY_SEPARATOR = r"\.?(?:\s*,\s*|\s+)"
_COMBINED_DATE = rf"{_WEEKDAY_MODIFIER}{_WEEKDAY}{_WEEKDAY_SEPARATOR}{_CALENDAR_DATE}"
_DAY_OFFSETS = {"today": 0, "tonight": 0, "tomorrow": 1, "day after tomorrow": 2}
_COUNTS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")
_COUNT_VALUES = {name: index for index, name in enumerate(_COUNTS)} | {"a": 1}
_COUNT = r"(?:\d{1,4}|" + "|".join(_COUNT_VALUES) + ")"
_INTERVAL = rf"(?P<count>{_COUNT})\s+(?P<unit>days?|weeks?)"
_OFFSET_DATE = rf"(?:(?:in|within)\s+{_COUNT}\s+(?:days?|weeks?)|{_COUNT}\s+(?:days?|weeks?)\s+from\s+now)"
_MONTH_END = r"(?:the\s+)?end\s+of\s+(?:(?:this|the)\s+)?month"
_RELATIVE_DATE = (
    rf"(?:(?:the\s+)?day\s+after\s+tomorrow|today|tonight|tomorrow|"
    rf"{_WEEKDAY_MODIFIER}{_WEEKDAY}|{_MONTH_END}|{_OFFSET_DATE})"
)
_DATE = rf"(?:{_COMBINED_DATE}|{_CALENDAR_DATE}|{_RELATIVE_DATE})"
_TIMING = r"on\s+or\s+(?:before|around)|not?\s+later\s+than|before|by|at|on|around"
_DEADLINE_LABEL = (
    rf"(?:the\s+)?(?:deadline|due(?:\s+date)?|target\s+date)"
    rf"(?:\s*:\s*|\s+is\s+|\s+(?:{_TIMING})\s+|\s+)"
)
_SUFFIX_LABEL = rf"(?:{_DEADLINE_LABEL}|(?:complete|requested)\s+(?:{_TIMING})\s+)"
_SUFFIX = re.compile(
    rf"\s*(?:\({_SUFFIX_LABEL}(?P<parenthetical>{_DATE})\)"
    rf"|(?:[—–,;-]|(?<=[.!?]))\s*{_SUFFIX_LABEL}(?P<generated>{_DATE}))\s*(?P<punctuation>[.!?])?\s*$",
    re.IGNORECASE,
)
_INLINE = re.compile(
    rf"\b(?:(?:{_DEADLINE_LABEL}|(?:{_TIMING})\s+)(?P<date>{_DATE})"
    rf"|(?P<standalone>{_COMBINED_DATE}|{_RELATIVE_DATE}))(?![\w/]|\.\w|['’]s\b)",
    re.IGNORECASE,
)
_DATE_CONTINUATION = re.compile(
    rf"^\s*(?:(?:[-–—/]\s*|,?\s*(?:or|to|through|until|and)\s+)(?:(?:{_TIMING})\s+)?"
    rf"(?:{_DATE}(?![\w/])|{_DAY}\b|(?:(?:this|next)\s+(?:week|month|year))\b)|,?\s*\d{{2}}\b)",
    re.IGNORECASE,
)
_CLAUSE_BREAK = re.compile(r"[,;.!?]|\b(?:and|but|then)\b", re.IGNORECASE)
_BACKGROUND_CLAUSE = re.compile(
    r"\b(?:because|since|after|before|during|when|once|following(?!\s+up\b)|given|who|which|whereas|while)\b",
    re.IGNORECASE,
)
_PAST_ACTION = re.compile(
    r"\b(?:was|were|had|sent|received|replied|responded|met|spoke|emailed|occurred|happened)\b",
    re.IGNORECASE,
)
_FUTURE_ACTION = re.compile(r"\b(?:will|should|must|to be|needs? to|plans? to)\b", re.IGNORECASE)
_RELATIVE = re.compile(_RELATIVE_DATE, re.IGNORECASE)
_DATED_WEEKDAY = re.compile(
    rf"{_WEEKDAY_MODIFIER}(?P<weekday>{_WEEKDAY}){_WEEKDAY_SEPARATOR}(?P<calendar>{_CALENDAR_DATE})",
    re.IGNORECASE,
)
_WEEKDAY_DATE = re.compile(rf"(?:(this|next|coming)\s+)?({_WEEKDAY})", re.IGNORECASE)
_OFFSET = re.compile(rf"(?:in\s+|within\s+)?{_INTERVAL}(?:\s+from\s+now)?", re.IGNORECASE)
_UNCERTAIN_PREFIX = re.compile(r"\b(?:not|never|rather\s+than|instead\s+of|maybe|perhaps|possibly)\s*$", re.IGNORECASE)
_BARE_BLOCKER = re.compile(r"\b(?:about|for|from|of|last|until|each|every|any|either|some)\s*$", re.IGNORECASE)
_BARE_END = re.compile(
    r"^\s*(?:[.,;!?)\]]|(?:at|by|before)\s+(?:\d|noon\b|midnight\b)|(?:because|since|so\s+that)\b|$)",
    re.IGNORECASE,
)


def _parse_relative(value: str, reference: date | None) -> date | None:
    if reference is None:
        return None
    value = " ".join(value.casefold().split())
    try:
        day_phrase = value.removeprefix("the ")
        if day_phrase in _DAY_OFFSETS:
            return reference + timedelta(days=_DAY_OFFSETS[day_phrase])
        if weekday := _WEEKDAY_DATE.fullmatch(value):
            offset = (_WEEKDAY_NUMBERS[weekday[2]] - reference.weekday()) % 7
            if weekday[1] == "next" and offset == 0:
                offset = 7
            return reference + timedelta(days=offset)
        if offset := _OFFSET.fullmatch(value):
            count = offset["count"]
            days = int(count) if count.isdecimal() else _COUNT_VALUES[count]
            return reference + timedelta(days=days * (7 if offset["unit"].startswith("week") else 1))
        if re.fullmatch(_MONTH_END, value):
            return reference.replace(day=monthrange(reference.year, reference.month)[1])
    except (KeyError, OverflowError, ValueError):
        return None
    return None


def _parse_calendar_date(value: str, reference: date | None) -> date | None:
    try:
        if "-" in value:
            return date.fromisoformat(value)
        if "/" in value:
            first, second, *year = map(int, value.split("/"))
            if first != second and first <= 12 and second <= 12:
                return None
            month, day = sorted((first, second))
        else:
            value = re.sub(r"(?<=\d)(?:st|nd|rd|th)\b", "", value.lower())
            first, second, *year = value.replace(",", "").replace(".", "").split()
            day_text, month_text = (first, second) if first.isdecimal() else (second, first)
            month, day = _MONTH_NUMBERS[month_text], int(day_text)
        if year:
            return date(int(year[0]), month, day)
        if reference is None:
            return None
        # Use the source year except when the nearest occurrence crosses New Year.
        year_offset = 1 if reference.month - month > 6 else -1 if month - reference.month > 6 else 0
        return date(reference.year + year_offset, month, day)
    except (ValueError, KeyError):
        return None


def _parse_date(value: str, reference: date | None, *,
                calendar_date_hint: date | None = None, allow_relative: bool) -> date | None:
    if _RELATIVE.fullmatch(value):
        return _parse_relative(value, reference) if allow_relative else None

    combined = _DATED_WEEKDAY.fullmatch(value)
    parsed = _parse_calendar_date(combined["calendar"] if combined else value, calendar_date_hint or reference)
    if combined and parsed and parsed.weekday() != _WEEKDAY_NUMBERS.get(combined["weekday"].casefold()):
        return None
    return parsed


def _is_background_clause(prefix: str) -> bool:
    clause = _CLAUSE_BREAK.split(prefix)[-1]
    if _BACKGROUND_CLAUSE.search(clause):
        return True
    return bool(_PAST_ACTION.search(clause) and not _FUTURE_ACTION.search(clause))


def _is_bare_timing(prefix: str, remainder: str, *, has_date_continuation: bool) -> bool:
    # A standalone weekday can describe an event or modify a noun, as in "Friday's agenda".
    return not _BARE_BLOCKER.search(prefix) and bool(_BARE_END.match(remainder) or has_date_continuation)


def _remove_phrase(text: str, start: int, end: int) -> str:
    left, right = text[:start].rstrip(), text[end:].lstrip()
    if left.endswith("(") and right.startswith(")"):
        left, right = left[:-1].rstrip(), right[1:].lstrip()
    if left.endswith(",") and right[:1] in {",", ".", ";", "!", "?"}:
        left = left[:-1].rstrip()
        if right.startswith(","):
            right = right[1:].lstrip()
    if not right or right[0] in ".!?":
        left = left.rstrip(" ,;—–")
        if left.endswith(" -"):
            left = left[:-2].rstrip()
    if (not left or left.endswith(":")) and right.startswith((",", ".", ";", ":")):
        right = right[1:].lstrip()
    space = " " if left and right and right[0] not in ",.;:!?)]" else ""
    return left + space + right


def normalize_deadline(bullet: str, reference_date: date | None = None, *, allow_relative: bool = True) -> str:
    """Move a single clear deadline to the canonical suffix, using a stable date for omitted years."""
    text = bullet
    due = None
    suffix = _SUFFIX.search(text)
    if suffix:
        phrase = suffix["parenthetical"] or suffix["generated"]
        due = _parse_date(phrase, reference_date, allow_relative=allow_relative)
        if due is None:
            return bullet
        text = text[:suffix.start()].rstrip()
        if suffix["punctuation"] and text and text[-1] not in ".!?":
            text += suffix["punctuation"]

    spans = []
    for match in _INLINE.finditer(text):
        prefix, remainder = text[:match.start()], text[match.end():]
        has_date_continuation = bool(_DATE_CONTINUATION.match(remainder))
        if _UNCERTAIN_PREFIX.search(prefix) or _is_background_clause(prefix):
            continue
        if match["standalone"] and not _is_bare_timing(prefix, remainder, has_date_continuation=has_date_continuation):
            continue
        phrase = match["date"] or match["standalone"]
        candidate = _parse_date(phrase, reference_date, calendar_date_hint=due, allow_relative=allow_relative)
        if candidate is None or (due and candidate != due) or has_date_continuation:
            return bullet
        due = candidate
        spans.append(match.span())

    if due is None:
        return bullet
    for start, end in reversed(spans):
        text = _remove_phrase(text, start, end)
    if not text.strip(" .,:;!?-—–"):
        return bullet
    # Repair this malformed modal construction when it occurs in a dated next step.
    text = re.sub(r"\bshould following up\b", "should keep following up", text, flags=re.IGNORECASE)
    return f"{text} (complete by {_MONTHS[due.month - 1][:3]} {due.day}, {due.year:04d})"
