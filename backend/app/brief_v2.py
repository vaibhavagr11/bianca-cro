"""Morning brief v2: compact, deal-level, owner-checked. Pure rules and rendering; brief_api gathers the data.

Per founder, in reading order: needs you today (time-critical only), deals to move (one line per prospect or
pilot: situation, one next move, date, evidence), customer accounts (one line each), meetings, follow-ups that may
be needed, deals whose status to confirm, and a one-line backlog summary. Shared: what changed (from new,
quote-checked insights) and Nico de León's urgent items.

Every rule here is deterministic. The existing planner call only shortens the suggested moves it picks and writes
meeting prep lines; it never decides urgency or ownership.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from app.brief import (
    AMBER, BLUE, FONT, INK, MUTED, RED, Meeting, _e, _empty, _h, _link, _td, _th, meetings_table, stage_short,
)

ACTIVE_DAYS = 14            # last recorded interaction within this = active
UNCLEAR_DAYS = 45           # 15..45 days = status unclear (confirm); older = dormant
REPLY_DAYS = 7              # customer email this recent + a question/request = reply needed
PROPOSAL_FOLLOWUP_DAYS = (7, 30)   # our proposal with no reply after this many days = follow-up may be needed
UNDATED_WINDOW_DAYS = 45
MAX_HIGHLIGHTS, MAX_CUSTOMERS, MAX_FOLLOWUPS, MAX_EXCEPTIONS, MAX_NAMES = 4, 4, 3, 1, 3
IN_TALKS_RANK = 2
REQUEST_FRESH_DAYS = 21       # a customer request this recent still counts as outstanding
CHANGE_QUOTA = {0: 3, 1: 2, 2: 0}  # what changed: prospects/pilots, customers, other
PROSPECT_RANKS, CUSTOMER_RANK = (1, 2, 3, 4), 5
CHANGE_TYPES = {"buying_signal": "Buying signal", "blocker": "New blocker", "objection": "Objection",
                "commitment": "New commitment", "next_step": "Next step agreed", "need": "New need"}
REQUEST_RE = re.compile(
    r"\?|\b(por favor|podr[ií]an|podr[ií]as|pod[eé]s|necesit\w*|pid(i[oó]|en|e)|solicit\w*|quier\w*|"
    r"please|could you|can you|would you|need|asked|requested|wants?)\b", re.I)
PURPLE = "#6941c6"


# ---------------------------------------------------------------- data

@dataclass
class Item:
    """An open Bianca-side commitment or next step (quote-checked extraction)."""
    id: str
    deal: str
    text: str
    due: date | None
    recorded: date
    owner_name: str | None
    route: str | None = None        # founder section it belongs to
    basis: str = "deal_owner"       # named | deal_owner | delegated
    delegated_to: str | None = None
    source: str = ""
    link: str | None = None
    bucket: str = ""
    kind: str = "next_step"         # owed | customer_request | proposed | next_step


@dataclass
class Note:
    """A profile statement (blocker, recommendation, summary): verified only if its evidence still resolves."""
    text: str
    why: str | None = None
    verified: bool = False
    basis: str | None = None
    link: str | None = None
    evidence: frozenset = frozenset()  # ids of the insights it cites (resolved)


@dataclass
class Unanswered:
    subject: str
    days: int
    link: str | None
    has_request: bool
    action: str | None = None       # what the latest customer message asks of us, if extraction found it


@dataclass
class DealView:
    deal: str
    stage: str | None
    rank: int | None
    owner: str | None               # pipeline (Sheet) owner
    last_contact: date | None = None
    items: list[Item] = field(default_factory=list)
    delegated: list[Item] = field(default_factory=list)
    blocker: Note | None = None
    recommendation: Note | None = None
    summary: Note | None = None
    unanswered: Unanswered | None = None
    developments: int = 0           # new or late-recorded developments since the last brief
    recommendations: list[Note] = field(default_factory=list)  # all of the profile's suggested moves


@dataclass
class Move:
    """The one next move shown for a deal, with what kind of thing it is."""
    tag: str                        # We owe | Reply needed | Customer asked | AI recommendation | Follow up | Confirm status
    text: str
    when: str                       # deadline or last recorded interaction
    link: str | None
    item_id: str | None = None


@dataclass
class TodayRow:
    deal: str
    stage: str | None
    tag: str                        # Due now | Reply needed
    text: str
    when: str
    owner: str
    link: str | None
    item_id: str | None = None


@dataclass
class ChangeRow:
    deal: str
    rank: int | None
    text: str
    source: str
    link: str | None
    kind: str = "new"               # new | late | sheet
    event: date | None = None       # when it happened (from the source, not when we stored it)


@dataclass
class SelectionRecord:
    """Why an eligible deal was or was not shown in "Deals to move" (dry-run audit, not the email)."""
    deal: str
    stage: str | None
    selected: bool
    position: int | None
    critical: list[str]
    signals: list[str]
    reason: str


# ---------------------------------------------------------------- rules (pure)

def norm(name: str | None) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().lower()


def resolve_owner(owner_name: str | None, deal_owner: str | None, team: dict[str, str],
                  founders: set[str], display: dict[str, str]) -> tuple[str | None, str, str | None]:
    """(section code, basis, delegated_to). A person named in the source wins over the deal owner; a named
    non-founder is shown to the deal owner as delegated, never as the founder's own commitment."""
    if not owner_name or not owner_name.strip():
        return deal_owner, "deal_owner", None
    code = team.get(norm(owner_name))
    if code and code in founders:
        return code, "named", None
    if code and code == deal_owner:
        return code, "named", None
    return deal_owner, "delegated", display.get(code, owner_name) if code else owner_name


def bucket(due: date | None, recorded: date, today: date, today_days: int, stale_days: int) -> str:
    """today (due, up to today_days late, or due tomorrow) | overdue | stale | week | later | undated | old_undated."""
    if due is None:
        return "undated" if (today - recorded).days <= UNDATED_WINDOW_DAYS else "old_undated"
    late = (today - due).days
    if late == -1:
        return "today"
    if late < 0:
        return "week" if -late <= 7 else "later"
    if late <= today_days:
        return "today"
    return "overdue" if late <= stale_days else "stale"


def has_request(text: str | None) -> bool:
    return bool(text and REQUEST_RE.search(text))


def item_kind(type_: str, due: date | None, speaker: str, quote: str, profile_side: str | None = None) -> str:
    """What an extracted Bianca-side item really is (the extractor does not record this).

    owed: a deadline, or a promise the current deal profile still lists as open on Bianca's side.
    progress: a "commitment" the profile reads as done on our side (it cites it only in an open item owned by the
    customer, e.g. "validate our fix"), or that it no longer lists as open although Bianca wrote it.
    customer_request: asked for by the customer. proposed: written in Bianca's own email, the next move is theirs.
    next_step: an agreed step from the meeting notes, not a promise.

    profile_side is how the current, evidence-checked profile lists the same insight among open commitments:
    "bianca", "customer", "none" (not listed), or None when there is no current profile to ask.
    """
    if due is not None:
        return "owed"
    if type_ == "commitment":
        if profile_side in (None, "bianca"):
            return "owed"
        if profile_side == "customer" or speaker == "bianca":
            return "progress"
    if speaker == "customer" or (speaker == "notes" and has_request(quote) and "?" not in quote):
        return "customer_request"
    if speaker == "bianca":
        return "proposed"
    return "next_step"


def deal_status(d: DealView, today: date) -> str:
    """active | unclear | dormant | untracked, from the last recorded interaction.

    Absence of records is not proof of inactivity: 15-45 days is "unclear" (ask), never "inactive".
    Deals we have never seen activity for are "untracked" and are not listed.
    """
    if d.last_contact is None:
        return "untracked"
    days = (today - d.last_contact).days
    return "active" if days <= ACTIVE_DAYS else "unclear" if days <= UNCLEAR_DAYS else "dormant"


def reply_label(u: Unanswered) -> str:
    return "reply_needed" if u.days <= REPLY_DAYS and u.has_request else "follow_up"


def reply_text(u: Unanswered) -> str:
    """What to do about an unanswered customer email: the extracted request if there is one, otherwise a neutral
    instruction. Never an action invented from the subject line."""
    return u.action or f"Review the customer's email and decide the response needed ({u.subject[:60]})"


def first_sentence(text: str, limit: int = 95) -> str:
    parts = re.split(r"(?<=[^\d\s][.!?])\s+(?=[A-Z¿¡\"'])", text.strip(), maxsplit=2)
    s = parts[0] if len(parts[0]) >= 30 or len(parts) == 1 else f"{parts[0]} {parts[1]}"  # "Call Ale." alone says too little
    return s if len(s) <= limit else s[:limit].rsplit(" ", 1)[0] + "…"


def _date(d: date) -> str:
    return f"{d:%b} {d.day}"


def addresses(evidence: frozenset, blocker: Note) -> bool:
    """A move addresses a blocker when it rests on the same source evidence (shared cited insights)."""
    return bool(evidence & blocker.evidence)


def primary_move(d: DealView, today: date, used: set[str]) -> Move:
    """One next move per deal.

    With a verified blocker (the situation shown next to it), the move must address that blocker: a promise,
    customer request or suggested move citing the same evidence. If none does, say the next action needs
    clarification rather than show an unrelated task. Without a blocker: obligations first, then the customer,
    then AI suggestions.
    """
    open_items = [i for i in d.items if i.id not in used]
    b = d.blocker if d.blocker and d.blocker.verified else None
    if b is not None:
        for i in sorted((i for i in open_items if i.kind in ("owed", "customer_request") and i.id in b.evidence),
                        key=lambda i: i.due or date.max):
            tag = "We owe" if i.kind == "owed" else "Customer asked"
            when = f"due {_date(i.due)}" if i.due else f"recorded {_date(i.recorded)}"
            return Move(tag, i.text, when, i.link, i.id)
        for r in d.recommendations:
            if r.verified and addresses(r.evidence, b):
                return Move("AI recommendation", first_sentence(r.text), _last(d, today), r.link)
        return Move("Clarify next action", "No recorded action or suggestion addresses this blocker yet",
                    _last(d, today), b.link)
    for i in sorted((i for i in open_items if i.kind == "owed" and i.bucket in ("overdue", "week")),
                    key=lambda i: i.due or date.max):
        late = (today - i.due).days
        return Move("We owe", i.text, f"due {_date(i.due)}" + (f", {late}d late" if late > 0 else ""), i.link, i.id)
    for i in sorted((i for i in open_items if i.kind == "customer_request" and i.bucket == "undated"),
                    key=lambda i: -i.recorded.toordinal()):
        return Move("Customer asked", i.text, f"asked {_date(i.recorded)}", i.link, i.id)
    if d.unanswered and reply_label(d.unanswered) == "follow_up":
        return Move("Follow up", reply_text(d.unanswered), f"email {d.unanswered.days}d ago", d.unanswered.link)
    if d.recommendation and d.recommendation.verified:
        return Move("AI recommendation", first_sentence(d.recommendation.text), _last(d, today), d.recommendation.link)
    for i in sorted((i for i in open_items if i.kind == "proposed"), key=lambda i: -i.recorded.toordinal()):
        age = (today - i.recorded).days
        if PROPOSAL_FOLLOWUP_DAYS[0] <= age <= PROPOSAL_FOLLOWUP_DAYS[1] and (d.last_contact or i.recorded) <= i.recorded:
            return Move("Follow up", f"No reply since we proposed: {i.text}", f"proposed {_date(i.recorded)}", i.link, i.id)
    if d.recommendation:
        return Move("AI recommendation", first_sentence(d.recommendation.text) + " (profile not verified)",
                    _last(d, today), None)
    return Move("Confirm status", "Check where this deal stands", _last(d, today), None)


def _last(d: DealView, today: date) -> str:
    return f"last recorded {_date(d.last_contact)} ({(today - d.last_contact).days}d)" if d.last_contact else "no record"


def situation(d: DealView) -> Note | None:
    """One evidence-backed line: the blocker if verified, else the profile summary's first sentence if verified."""
    for n in (d.blocker, d.summary):
        if n and n.verified:
            return Note(first_sentence(n.text), verified=True, link=n.link)
    return None


def is_new_insight(quote: str, new_text: str) -> bool:
    """An insight counts as new only if its quote appears in a record stored since the last brief."""
    q = " ".join(quote.split()).lower()
    return bool(q) and q in " ".join(new_text.split()).lower()


def classify_event(occurred: datetime | None, stored: datetime, cutoff: datetime, now: datetime,
                   lookback_days: int, reliable: bool = True) -> str:
    """seen | undated | scheduled | new | late | backfill, from the source event time, never the ingestion time.

    seen: we already had it at the previous brief. new: it happened after the previous brief. late: it happened up
    to lookback_days before that brief but reached us after it. backfill: older, newly recorded. undated: no
    reliable event time, so it is never presented as a development.
    """
    if stored <= cutoff:
        return "seen"
    if occurred is None or not reliable:
        return "undated"
    if occurred > now:
        return "scheduled"
    if occurred > cutoff:
        return "new"
    return "late" if occurred >= cutoff - timedelta(days=lookback_days) else "backfill"


def deal_signals(v: DealView, today: date, in_today: set[str], critical_ahead: int) -> tuple[list[str], list[str]]:
    """(time-critical reasons, evidence-backed signals). Recency alone is never a signal; it only breaks ties."""
    critical = []
    if v.deal in in_today:
        critical.append("needs you today (promise due now or customer waiting on a reply)")
    soon = [i for i in v.items if i.kind == "owed" and i.due and 0 < (i.due - today).days <= critical_ahead]
    if soon:
        critical.append(f"promise due {_date(min(i.due for i in soon))}")
    signals = []
    dated = [i for i in v.items if i.kind == "owed" and i.bucket in ("overdue", "week")]
    if dated:
        signals.append(f"deadline: {len(dated)} dated promise(s)")
    if any(i.kind == "customer_request" and i.bucket == "undated" and (today - i.recorded).days <= REQUEST_FRESH_DAYS
           for i in v.items):
        signals.append("outstanding customer request")
    if v.blocker and v.blocker.verified:
        signals.append("verified blocker")
    if v.developments:
        signals.append(f"{v.developments} new or late-recorded development(s)")
    if v.unanswered:
        signals.append(f"customer email waiting {v.unanswered.days}d")
    if v.recommendation and v.recommendation.verified:
        signals.append("next milestone: verified recommendation")
    elif any(i.kind == "proposed" and PROPOSAL_FOLLOWUP_DAYS[0] <= (today - i.recorded).days <= PROPOSAL_FOLLOWUP_DAYS[1]
             for i in v.items):
        signals.append("next milestone: our proposal awaiting reply")
    return critical, signals


def select_deals(eligible: list[DealView], today: date, in_today: set[str], critical_ahead: int,
                 in_talks_target: int, slots: int = MAX_HIGHLIGHTS) -> tuple[list[DealView], list[SelectionRecord]]:
    """Choose the displayed deals deterministically.

    1. Time-critical deals first; they are never displaced. 2. Then active In Talks deals with at least one
    evidence-backed signal, up to in_talks_target (a preference, not a quota). 3. Then the rest by signal count.
    Ties: more signals, later stage, more recent contact, then name, so identical inputs always give identical output.
    """
    info = {v.deal: deal_signals(v, today, in_today, critical_ahead) for v in eligible}
    recency = lambda v: -(v.last_contact.toordinal()) if v.last_contact else 0
    ranked = sorted(eligible, key=lambda v: (-len(info[v.deal][0]), -len(info[v.deal][1]), -(v.rank or 0), recency(v), v.deal))
    chosen: dict[str, str] = {}

    def take(v: DealView, reason: str) -> None:
        if v.deal not in chosen and len(chosen) < slots:
            chosen[v.deal] = reason

    for v in ranked:
        if info[v.deal][0]:
            take(v, "time-critical: " + "; ".join(info[v.deal][0]))
    talks = sum(1 for d in chosen if next(v for v in eligible if v.deal == d).rank == IN_TALKS_RANK)
    for v in ranked:
        if talks >= in_talks_target or len(chosen) >= slots:
            break
        if v.rank == IN_TALKS_RANK and v.deal not in chosen and info[v.deal][1]:
            take(v, f"In Talks preference (target {in_talks_target}): " + "; ".join(info[v.deal][1]))
            talks += 1
    for v in ranked:
        if info[v.deal][1] or v.rank != 1:
            take(v, "ranked by signals: " + ("; ".join(info[v.deal][1]) or "no signal beyond recent activity"))

    selected = [v for v in ranked if v.deal in chosen]
    order = {v.deal: n for n, v in enumerate(selected, start=1)}
    records = []
    for v in ranked:
        crit, sig = info[v.deal]
        if v.deal in chosen:
            reason = chosen[v.deal]
        elif not sig and not crit:
            reason = "no evidence-backed signal (recent activity alone is not enough)"
        else:
            reason = f"outranked: all {slots} slots taken by deals with time-critical items or more signals"
        records.append(SelectionRecord(v.deal, v.stage, v.deal in chosen, order.get(v.deal), crit, sig, reason))
    return selected, records


CHANGE_LABELS = {"buying_signal": "Buying signal", "blocker": "New blocker", "objection": "Objection",
                 "need": "Customer need", "commitment_owed": "New commitment (we owe)", "progress": "Progress made",
                 "customer_request": "Customer request", "proposed": "We proposed", "next_step": "Next step noted",
                 "they_owe": "Customer committed"}


def describe_change(type_: str, owner_side: str | None, kind: str | None, title_only: bool, source: str,
                    text: str, title: str | None, event: date | None, speaker: str | None = None) -> tuple[str, str]:
    """(category, display text) for a new development, from structure and evidence rather than wording.

    If the quote is only a title (a calendar event with no description, an email subject), report the recorded
    fact itself and do not repeat the extracted interpretation. A "buying signal" quoted from Bianca's own email is
    Bianca describing its own progress, not the customer's intent, so it is labelled as progress.
    """
    if title_only:
        what = "Meeting" if source == "calendar" else "Email"
        return "recorded", f"{what} on {_date(event)}: {title}" if event else f"{what}: {title}"
    if type_ in ("commitment", "next_step"):
        if owner_side == "customer":
            cat = "they_owe"
        elif kind == "owed":
            cat = "commitment_owed"
        else:
            cat = kind or "next_step"
    elif type_ == "buying_signal" and speaker == "bianca":
        cat = "progress"
    else:
        cat = type_
    return cat, f"{CHANGE_LABELS.get(cat, cat)}: {first_sentence(text, 140)}"


def change_group(rank: int | None) -> int:
    """0: prospects and pilots, 1: customers, 2: early or paused."""
    return 0 if rank in (2, 3, 4) else 1 if rank == CUSTOMER_RANK else 2


def select_changes(changes: list[ChangeRow]) -> list[ChangeRow]:
    """Keep input order inside each group; prospects and pilots first, with a reserved share for customers."""
    out, used = [], {0: 0, 1: 0, 2: 0}
    for c in sorted(changes, key=lambda c: (change_group(c.rank), c.kind != "new")):  # new before late in each group
        g = change_group(c.rank)
        if used[g] < CHANGE_QUOTA[g]:
            out.append(c)
            used[g] += 1
    return out


# ---------------------------------------------------------------- assembly (pure)

@dataclass
class Backlog:
    count: int = 0
    deals: int = 0
    oldest: date | None = None
    top: tuple[str, int] | None = None
    delegated: int = 0
    exceptions: list[Item] = field(default_factory=list)


@dataclass
class Section:
    code: str
    name: str
    short: str
    today: list[TodayRow]
    highlights: list[tuple[DealView, Move, Note | None]]
    also_active: list[str]
    customers: list[tuple[DealView, Move]]
    meetings: list[tuple[Meeting, str]]
    followups: list[Move]
    followup_deals: list[str]
    more_followups: int
    unclear: list[DealView]
    dormant: int
    backlog: Backlog
    hidden: int = 0
    all_items: list[Item] = field(default_factory=list)   # everything routed here, for the payload
    audit: list[SelectionRecord] = field(default_factory=list)


def build_section(code: str, name: str, short: str, views: dict[str, DealView], items: list[Item],
                  meetings: list[tuple[Meeting, str]], today: date, display: dict[str, str],
                  hidden: int = 0, critical_ahead: int = 3, in_talks_target: int = 2) -> Section:
    mine: dict[str, DealView] = {}

    def view(deal: str) -> DealView:
        if deal not in mine:
            src = views[deal]
            own = src.owner == code  # profile notes and unanswered email belong to the deal owner
            mine[deal] = DealView(src.deal, src.stage, src.rank, src.owner, src.last_contact,
                                  blocker=src.blocker if own else None, recommendation=src.recommendation if own else None,
                                  summary=src.summary if own else None, unanswered=src.unanswered if own else None,
                                  developments=src.developments if own else 0,
                                  recommendations=src.recommendations if own else [])
        return mine[deal]

    routed = [i for i in items if i.route == code]
    for i in routed:
        (view(i.deal).delegated if i.basis == "delegated" else view(i.deal).items).append(i)
    for deal, v in views.items():
        if v.owner == code:
            view(deal)

    used: set[str] = set()
    today_rows: list[TodayRow] = []
    owner_label = lambda i: display.get(code, code) + (" (named)" if i.basis == "named" else " (deal owner)")
    for v in mine.values():
        for i in v.items:
            if i.kind == "owed" and i.bucket == "today":
                late = (today - i.due).days
                when = "due tomorrow" if late < 0 else "due today" if late == 0 else f"due {_date(i.due)}, {late}d late"
                today_rows.append(TodayRow(v.deal, v.stage, "Due now", i.text, when, owner_label(i), i.link, i.id))
                used.add(i.id)
        if v.unanswered and reply_label(v.unanswered) == "reply_needed":
            today_rows.append(TodayRow(v.deal, v.stage, "Reply needed", reply_text(v.unanswered),
                                       f"received {v.unanswered.days}d ago", display.get(code, code) + " (deal owner)",
                                       v.unanswered.link))
    today_rows.sort(key=lambda r: (r.tag != "Due now", r.when))
    in_today = {r.deal for r in today_rows}

    # Deals to move: prospects and pilots this founder owns, active or with something pressing.
    def pressing(v: DealView) -> bool:  # a dated promise or a reply keeps a quiet deal visible; nothing else does
        return v.deal in in_today or any(i.kind == "owed" and i.bucket in ("overdue", "week") for i in v.items)

    owned = [v for v in mine.values() if v.owner == code]
    eligible = [v for v in owned if v.rank in PROSPECT_RANKS and
                (deal_status(v, today) == "active" or (v.rank != 1 and pressing(v)) or (v.rank == 1 and v.deal in in_today))]
    selected, audit = select_deals(eligible, today, in_today, critical_ahead, in_talks_target)
    for v in sorted(owned, key=lambda v: v.deal):  # the audit also explains prospects that were not eligible at all
        if v.rank in PROSPECT_RANKS and v not in eligible:
            status = deal_status(v, today)
            audit.append(SelectionRecord(v.deal, v.stage, False, None, [], [],
                                         f"not eligible: status {status}" + (f", last record {_date(v.last_contact)}"
                                                                             if v.last_contact else "")))
    recency = lambda v: -(v.last_contact.toordinal()) if v.last_contact else 0
    highlights = []
    for v in selected:
        m = primary_move(v, today, used)
        if m.item_id:
            used.add(m.item_id)
        highlights.append((v, m, situation(v)))
    shown = {v.deal for v, _, _ in highlights}

    customers = []
    for v in sorted((v for v in owned if v.rank == CUSTOMER_RANK), key=lambda v: (v.deal not in in_today, recency(v), v.deal)):
        if len(customers) < MAX_CUSTOMERS:
            m = primary_move(v, today, used)
            if m.item_id:
                used.add(m.item_id)
            customers.append((v, m))

    # Follow-ups that may be needed (not today): unanswered without a request or older, recent overdue promises,
    # our proposals with no reply, customer requests on deals not shown above.
    follow: list[Move] = []
    for v, m, _ in highlights:  # when the move addresses the blocker instead, the nearest promise stays visible
        dated = sorted((i for i in v.items if i.id not in used and i.kind == "owed" and i.bucket in ("overdue", "week")),
                       key=lambda i: i.due)
        if m.tag != "We owe" and dated:
            i = dated[0]
            follow.append(Move("We owe", f"{v.deal}: {i.text}", f"due {_date(i.due)}", i.link, i.id))
    for v in mine.values():
        if v.deal in shown:
            continue
        if v.unanswered and reply_label(v.unanswered) == "follow_up":
            follow.append(Move("Follow up", f"{v.deal}: {reply_text(v.unanswered)}",
                               f"email {v.unanswered.days}d ago", v.unanswered.link))
        for i in v.items:
            if i.id in used:
                continue
            if (i.kind == "owed" and i.bucket == "overdue") or (i.kind == "customer_request" and i.bucket == "undated"):
                follow.append(Move("We owe" if i.kind == "owed" else "Customer asked", f"{v.deal}: {i.text}",
                                   f"due {_date(i.due)}" if i.due else f"recorded {_date(i.recorded)}", i.link, i.id))
    follow_shown = follow[:MAX_FOLLOWUPS]
    used |= {m.item_id for m in follow_shown if m.item_id}

    # Status to confirm: owned prospects/pilots not shown whose last record is 15-45 days old (or missing).
    unclear = sorted((v for v in owned if v.rank in PROSPECT_RANKS and v.deal not in shown
                      and deal_status(v, today) == "unclear"), key=lambda v: (-(v.rank or 0), v.deal))
    dormant = sum(1 for v in owned if v.rank in PROSPECT_RANKS and v.deal not in shown and deal_status(v, today) == "dormant")

    # Backlog: everything else still open, summarized; individual items only for promised deadlines to active deals.
    rest = [i for v in mine.values() for i in v.items if i.id not in used and i.bucket in ("stale", "old_undated")]
    b = Backlog(len(rest), len({i.deal for i in rest}), min((i.due or i.recorded for i in rest), default=None),
                delegated=sum(len(v.delegated) for v in mine.values()))
    if rest:
        counts: dict[str, int] = {}
        for i in rest:
            counts[i.deal] = counts.get(i.deal, 0) + 1
        b.top = max(counts.items(), key=lambda kv: kv[1])
        b.exceptions = [i for i in sorted(rest, key=lambda i: i.due or date.max)
                        if i.kind == "owed" and i.due and views[i.deal].rank in (2, 3, 4)][:MAX_EXCEPTIONS]

    return Section(code, name, short, today_rows, highlights, [r.deal for r in audit if r.position is None
                                                                and not r.reason.startswith("not eligible")],
                   customers, meetings, follow_shown, [], len(follow) - len(follow_shown), unclear, dormant, b,
                   hidden, routed, audit)


# ---------------------------------------------------------------- rendering

TAG_COLOR = {"Due now": RED, "Reply needed": BLUE, "We owe": INK, "Customer asked": BLUE,
             "AI recommendation": PURPLE, "Follow up": AMBER, "Confirm status": MUTED, "Clarify next action": AMBER}


def _tag(text: str) -> str:
    c = TAG_COLOR.get(text, MUTED)
    return (f'<span style="display:inline-block;padding:0 5px;border:1px solid {c};border-radius:3px;color:{c};'
            f'font:600 10px/16px {FONT};text-transform:uppercase;letter-spacing:.04em;white-space:nowrap">{_e(text)}</span>')


def _muted(s: str) -> str:
    return f'<span style="color:{MUTED};font-size:12px">{s}</span>'


def _table(head: str, rows: list[str]) -> str:
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%">'
            f"<tr>{head}</tr>{''.join(rows)}</table>")


def _src(link: str | None) -> str:
    return _link("source", link) if link else _muted("—")


def render_section(s: Section, today: date, nico_gap: bool) -> str:
    out = [f'<p style="margin:24px 0 0;padding:6px 10px;background:#f2f4f7;border-left:3px solid {BLUE};'
           f'font:600 14px {FONT};color:{INK}">{_e(s.name)}</p>']
    out.append(_h("Needs you today", RED, len(s.today)))
    out.append(_table(_th("Deal", "20%") + _th("What") + _th("When", "15%") + _th("", "8%"),
                      ["<tr>" + _td(f"<b>{_e(r.deal)}</b> {_muted(_e(stage_short(r.stage)))}")
                       + _td(f"{_tag(r.tag)} {_e(first_sentence(r.text))}") + _td(_muted(_e(r.when))) + _td(_src(r.link)) + "</tr>"
                       for r in s.today]) if s.today else _empty("Nothing due now or waiting on your reply."))

    out.append(_h("Deals to move · prospects & pilots", PURPLE, len(s.highlights)))
    if s.highlights:
        rows = []
        for v, m, sit in s.highlights:
            gap = " · Nico's inbox not connected" if nico_gap and v.owner == "NC" else ""
            sit_html = (f"{_e(sit.text)} {_link('evidence', sit.link)}" if sit else _muted("—"))
            rows.append("<tr>" + _td(f"<b>{_e(v.deal)}</b><br>{_muted(_e(stage_short(v.stage)))}", "width:18%")
                        + _td(sit_html, "width:32%")
                        + _td(f"{_tag(m.tag)} {_e(first_sentence(m.text))}<br>{_muted(_e(m.when + gap))}")
                        + _td(_src(m.link), "width:8%") + "</tr>")
        out.append(_table(_th("Deal") + _th("Situation") + _th("Next move") + _th(""), rows))
    else:
        out.append(_empty("No active prospect or pilot needs attention."))
    if s.also_active:
        out.append(f'<p style="margin:6px 0 0;font:12px {FONT};color:{MUTED}">Also active: {_e(", ".join(s.also_active))}</p>')

    if s.customers:
        out.append(_h("Customer accounts", AMBER, len(s.customers)))
        out.append(_table(_th("Account", "20%") + _th("Next move") + _th("", "8%"),
                          ["<tr>" + _td(f"<b>{_e(v.deal)}</b>") + _td(f"{_tag(m.tag)} {_e(first_sentence(m.text))} {_muted(_e(m.when))}")
                           + _td(_src(m.link)) + "</tr>" for v, m in s.customers]))

    out.append(_h("Meetings today & tomorrow", BLUE, len(s.meetings)))
    out.append(meetings_table(s.meetings) if s.meetings else _empty("No customer meetings today or tomorrow."))

    if s.followups:
        out.append(_h("Follow-ups that may be needed", AMBER, len(s.followups) + s.more_followups))
        lines = "".join(f'<p style="margin:3px 0;font:13px/1.4 {FONT};color:{INK}">{_tag(m.tag)} {_e(first_sentence(m.text))} '
                        f'{_muted(_e(m.when))} {_link("source", m.link) if m.link else ""}</p>' for m in s.followups)
        more = (f'<p style="margin:3px 0;font:12px {FONT};color:{MUTED}">+ {s.more_followups} more on record</p>'
                if s.more_followups else "")
        out.append(lines + more)

    notes = []
    if s.unclear:
        names = ", ".join(f"{v.deal} ({_date(v.last_contact)})" for v in s.unclear[:MAX_NAMES])
        if len(s.unclear) > MAX_NAMES:
            names += f" +{len(s.unclear) - MAX_NAMES} more"
        why = " Nico's inbox isn't connected, so activity may be missing." if nico_gap and s.code == "NC" else ""
        notes.append(f"<b>Confirm status:</b> {_e(names)}: last record 2–6 weeks ago.{_e(why)}")
    if s.dormant:
        notes.append(f"{s.dormant} prospect(s) with no record in {UNCLEAR_DAYS}+ days.")
    b = s.backlog
    if b.count:
        top = f", most on {b.top[0]} ({b.top[1]})" if b.top else ""
        notes.append(f"<b>Backlog:</b> {b.count} older open items, {b.deals} deals{_e(top)}, since {_date(b.oldest)}. "
                     f"Kept on record; may be done outside our sources.")
        for i in b.exceptions:
            notes.append(f"&nbsp;&nbsp;• Promised to {_e(i.deal)}: {_e(i.text)} {_muted(f'due {_date(i.due)}')} "
                         f"{_link('source', i.link) if i.link else ''}")
    if b.delegated:
        notes.append(f"{b.delegated} item(s) on your deals name someone else (delegated).")
    if s.hidden:
        notes.append(f"{s.hidden} item(s) hidden: later emails/notes suggest they were done.")
    if notes:
        out.append("".join(f'<p style="margin:6px 0 0;font:12px/1.5 {FONT};color:{MUTED}">{n}</p>' for n in notes))
    return "".join(out)


def change_label(c: ChangeRow) -> str:
    if c.kind == "late" and c.event:
        return f"from {_date(c.event)}, recorded late"
    return c.source


def render(sections: list[Section], ndl_rows: list[TodayRow], changes: list[ChangeRow], today: date,
           active_deals: int, coverage: str, nico_gap: bool, backfill: str | None = None) -> str:
    counts = " · ".join(f"{s.short}: {len(s.today)} today, {len(s.highlights)} deals" for s in sections)
    out = [f'<div style="max-width:760px;margin:0 auto;padding:8px 4px">',
           f'<p style="font:600 18px {FONT};margin:0 0 2px;color:{INK}">Pipeline brief · {_e(f"{today:%a}, {today:%b} {today.day}")}</p>',
           f'<p style="font:13px {FONT};margin:0;color:{MUTED}">{_e(counts)} · {active_deals} deals in the Sheet</p>']
    out += [render_section(s, today, nico_gap) for s in sections]
    if ndl_rows:
        out.append(f'<p style="margin:24px 0 0;padding:6px 10px;background:#f2f4f7;border-left:3px solid {MUTED};'
                   f'font:600 14px {FONT};color:{INK}">Nico de León · urgent only</p>')
        out.append(_table(_th("Deal", "20%") + _th("What") + _th("When", "15%") + _th("", "8%"),
                          ["<tr>" + _td(f"<b>{_e(r.deal)}</b>") + _td(f"{_tag(r.tag)} {_e(r.text)}")
                           + _td(_muted(_e(r.when))) + _td(_src(r.link)) + "</tr>" for r in ndl_rows]))
    out.append(_h("What changed since the last brief", MUTED, len(changes)))
    out.append(_table(_th("Deal", "20%") + _th("What changed (from the source)") + _th("", "8%"),
                      ["<tr>" + _td(f"<b>{_e(c.deal)}</b>") + _td(f"{_e(first_sentence(c.text, 100))} {_muted(_e(change_label(c)))}")
                       + _td(_src(c.link) if c.link or not c.source.startswith("Meeting")
                             else _muted("calendar event")) + "</tr>" for c in changes])
                if changes else _empty("No notable changes."))
    if backfill:
        out.append(f'<p style="margin:6px 0 0;font:12px {FONT};color:{MUTED}">{_e(backfill)}</p>')
    out.append(f'<p style="font:11px {FONT};color:#98a2b3;margin-top:28px">{_e(coverage)} <b>We owe</b> = promise '
               f"or deadline in a source · <b>AI recommendation</b> = suggested, not promised.</p></div>")
    return "".join(out)


def subject(sections: list[Section], today: date) -> str:
    bits = [f"{s.short} {len(s.today)} today, {len(s.highlights)} deals" for s in sections]
    return f"Bianca pipeline · {today:%a}, {today:%b} {today.day} · " + " · ".join(bits)


def plain_text(sections: list[Section], today: date) -> str:
    lines = [f"Pipeline brief · {today:%a}, {today:%b} {today.day}"]
    for s in sections:
        lines.append(f"\n{s.name.upper()}\nNeeds you today ({len(s.today)})")
        lines += [f"• [{r.tag}] {r.deal} — {r.text} ({r.when})" for r in s.today]
        lines.append(f"Deals to move ({len(s.highlights)})")
        lines += [f"• {v.deal} ({stage_short(v.stage)}): [{m.tag}] {m.text} ({m.when})" for v, m, _ in s.highlights]
        lines += [f"• {v.deal} (customer): [{m.tag}] {m.text}" for v, m in s.customers]
        lines += [f"• {m.at:%a %H:%M} {m.title} — {p}" for m, p in s.meetings]
        lines += [f"• [{m.tag}] {m.text} ({m.when})" for m in s.followups]
    return "\n".join(lines)


def to_payload(sections: list[Section], ndl_rows: list[TodayRow], changes: list[ChangeRow]) -> dict:
    """Everything routed to each founder is kept here, including what the email only summarizes."""
    def item(i: Item) -> dict:
        return {"id": i.id, "deal": i.deal, "text": i.text, "due": i.due, "bucket": i.bucket, "kind": i.kind,
                "basis": i.basis, "delegated_to": i.delegated_to, "source": i.source}

    def move(m: Move) -> dict:
        return {"tag": m.tag, "text": m.text, "when": m.when, "item_id": m.item_id}
    return {"version": "v2", "sections": [{
        "owner": s.code,
        "today": [{"deal": r.deal, "tag": r.tag, "text": r.text, "when": r.when, "item_id": r.item_id} for r in s.today],
        "highlights": [{"deal": v.deal, "stage": v.stage, "situation": sit.text if sit else None, "move": move(m)}
                       for v, m, sit in s.highlights],
        "selection_audit": [{"deal": r.deal, "stage": r.stage, "selected": r.selected, "position": r.position,
                             "time_critical": r.critical, "signals": r.signals, "reason": r.reason} for r in s.audit],
        "also_active": s.also_active,
        "customers": [{"deal": v.deal, "move": move(m)} for v, m in s.customers],
        "followups": [move(m) for m in s.followups], "more_followups": s.more_followups,
        "unclear": [v.deal for v in s.unclear], "dormant": s.dormant,
        "backlog": {"count": s.backlog.count, "deals": s.backlog.deals, "oldest": s.backlog.oldest,
                    "exceptions": [item(i) for i in s.backlog.exceptions]},
        "meetings": [{"at": m.at.isoformat(), "title": m.title, "deal": m.deal, "prep": p} for m, p in s.meetings],
        "all_items": [item(i) for i in s.all_items],
    } for s in sections], "ndl": [{"deal": r.deal, "text": r.text, "when": r.when} for r in ndl_rows],
        "changes": [{"deal": c.deal, "text": c.text, "kind": c.kind, "event": c.event} for c in changes]}


def recorded_on(ts: datetime | None, tz) -> date:
    return ts.astimezone(tz).date() if ts else date.min
