"""Morning brief v2: compact, deal-level, owner-checked. Pure rules and rendering; brief_api gathers the data.

Per founder, in reading order: needs you today (time-critical only), deals to move (one line per prospect or
pilot: situation, one next move, date, evidence), waiting on their reply, meetings, follow-ups that may be
needed, deals whose status to confirm, and a one-line backlog summary. Shared: what changed (from new,
quote-checked insights) and Nico de León's urgent items.
"""

import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

from app.brief import (
    AMBER, BLUE, FONT, INK, MUTED, RED, Meeting, VerificationNotes, _e, _empty, _h, _link, _td, _th,
    meeting_text_fields, meeting_text_rows, meetings_table, stage_short, verification_html, verification_text,
)
from app.brief_followups import is_followup_only, is_waiting_only
from app.brief_text import (
    WAITING_TEXT_KIND, TableText, _valid_shortening, first_or_blank, read_table_text, split_sentences,
)
from app.pipeline import is_paused

ACTIVE_DAYS = 14            # last recorded interaction within this = active
UNCLEAR_DAYS = 45           # 15..45 days = status unclear (confirm); older = dormant
REPLY_DAYS = 7              # customer email this recent with an extracted sales ask = reply needed
PROPOSAL_FOLLOWUP_DAYS = (7, 30)   # our proposal with no reply after this many days = follow-up may be needed
UNDATED_WINDOW_DAYS = 45
MAX_HIGHLIGHTS, MAX_FOLLOWUPS, MAX_EXCEPTIONS, MAX_NAMES = 6, 3, 1, 3
MAX_WAITING = 6
MAX_TABLE_ROWS = 7            # founders (11/12): 4 to 7 things per table; more only makes them stop reading
IN_TALKS_RANK = 2
REQUEST_FRESH_DAYS = 21       # a customer request this recent still counts as outstanding
PROSPECT_RANKS, CUSTOMER_RANK = (1, 2, 3, 4), 5
CHANGE_TYPES = {"buying_signal": "Buying signal", "blocker": "New blocker", "objection": "Objection",
                "commitment": "New commitment", "next_step": "Next step noted", "need": "New need"}
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
    replied: date | None = None     # we wrote back in its email thread on this day, after it was asked


@dataclass
class Note:
    """A profile statement (blocker, recommendation, summary): verified only if its evidence still resolves."""
    text: str
    why: str | None = None
    verified: bool = False
    basis: str | None = None
    link: str | None = None
    evidence: frozenset = frozenset()  # ids of the insights it cites (resolved)
    source: str = ""                # newest cited evidence, e.g. "Notes · Oct 20"
    ref: str | None = None          # stable id of a suggestion, so a founder can dismiss it
    followup_only: bool = False
    on: date | None = None          # day of the newest evidence it cites
    replied: date | None = None     # we wrote in every email thread it cites on or before this day, after `on`


@dataclass
class Unanswered:
    subject: str
    days: int
    link: str | None
    has_request: bool
    action: str | None = None       # what the latest customer message asks of us, if extraction found it
    source: str = ""                # "Email · Oct 6": the customer's latest message
    thread_id: str | None = None
    item_id: str | None = None     # the extracted ask also eligible for deal-level selection

    @property
    def completion_ref(self) -> str | None:
        if self.item_id:
            return f"insight:{self.item_id}"
        return f"thread:{self.thread_id}" if self.thread_id else None


@dataclass(frozen=True)
class SheetDue:
    """A Next step bullet the founders dated in the Sheet with "(complete by <date>)"."""
    text: str
    due: date
    ref: str                        # stable id of this bullet and date, so "Done" can close it
    item_id: str | None = None      # the open extracted item that is the same step, if there is one


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
    last_outbound: date | None = None   # our latest email to someone outside Bianca on this deal
    last_reply: date | None = None      # the customer's latest real email on this deal
    unanswered_emails: int = 0          # our emails in a row since their last reply
    sheet_steps: list[SheetDue] = field(default_factory=list)  # dated Next step bullets from the Sheet
    held: int = 0                   # extracted items held back until the date the founder set for them in the Sheet
    sheet_next: str = ""            # first line of the Sheet's Next step cell, for context lines


@dataclass
class Move:
    """The one next move shown for a deal, with what kind of thing it is."""
    tag: str
    text: str
    when: str                       # deadline or last recorded interaction
    link: str | None
    item_id: str | None = None
    source: str = ""                # where it comes from and when, e.g. "Email · Oct 6"
    done: str | None = None         # what "Mark as complete" closes: insight:<id> | thread:<id> | rec:<ref>
    deal: str | None = None


@dataclass
class TodayRow:
    deal: str
    stage: str | None
    tag: str                        # Due now | Sheet date | Reply needed
    text: str
    when: str
    owner: str
    link: str | None
    item_id: str | None = None
    source: str = ""
    done: str | None = None
    verify: bool = True             # False: a founder's own dated note in the Sheet; mail cannot confirm or close it
    late: int = 0                   # days past its date (0 due today, -1 due tomorrow); 0 for a reply


@dataclass
class ChangeRow:
    deal: str
    rank: int | None
    text: str
    source: str
    link: str | None
    kind: str = "new"               # new | late | sheet
    event: date | None = None       # when it happened (from the source, not when we stored it)
    title_only: bool = False        # only the subject/title is established, not what was agreed
    from_customer: bool = False     # the customer wrote it: a reply is the first thing a founder wants to see


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

    owed: an extracted commitment with a deadline, or a promise still open on Bianca's side.
    progress: a "commitment" the profile reads as done on our side (it cites it only in an open item owned by the
    customer, e.g. "validate our fix"), or that it no longer lists as open although Bianca wrote it.
    customer_request: asked for by the customer. proposed: written in Bianca's own email, the next move is theirs.
    next_step: a step or question without an explicit promise.

    profile_side is how the current, evidence-checked profile lists the same insight among open commitments:
    "bianca", "customer", "none" (not listed), or None when there is no current profile to ask.
    """
    if type_ == "commitment":
        if due is not None or profile_side in (None, "bianca"):
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


def reply_label(u: Unanswered) -> str | None:
    if not u.action:
        return None
    return "reply_needed" if u.days <= REPLY_DAYS and u.has_request else "follow_up"


def reply_text(u: Unanswered) -> str:
    """What to do about an unanswered customer email: the extracted request if there is one, otherwise a neutral
    instruction. Never an action invented from the subject line."""
    instruction = "Review the customer's email and decide the response needed"
    if u.action:
        return u.action
    return f"{instruction} ({u.subject})." if u.subject else f"{instruction}."


# Mail no person wrote: out-of-office replies, bounces, meeting-notes bots and no-reply notifications. They never
# mean a customer is waiting on us, and an out-of-office does not mean the customer answered.
AUTO_SUBJECT_RE = re.compile(
    r"^\s*(automatic reply|auto[- ]?reply|autoreply|auto[- ]?response|out of (the )?office|ooo\b|"
    r"respuesta autom[aá]tica|resposta autom[aá]tica|r[ée]ponse automatique|fuera de (la )?oficina|"
    r"abwesenheitsnotiz|undeliverable|delivery status notification|mail delivery (failed|subsystem))", re.I)
AUTO_SENDER_RE = re.compile(
    r"(no-?reply|do-?not-?reply|^mailer-daemon@|^postmaster@|^bounces?[+@-]|^gemini-notes@|^calendar-notification@"
    r"|@([^@]*\.)?notifications?\.)", re.I)
# A help-desk ticket update ("[Acme Help Center] Request #48213", "Ticket #991 updated") is a system notice from
# whoever runs the ticket, usually a vendor. It is never the customer answering us.
# A desk's own label in brackets is enough on its own. A bare ticket number is not: a buyer's "Solicitud N° 4521 -
# alta de proveedor" or our "Re: Purchase request #7731" is a person writing, so the number counts only when the
# sender is a help-desk host or a support mailbox.
TICKET_SUBJECT_RE = re.compile(
    r"\[[^\]]{0,80}\b(help ?cent(?:er|re)|help ?desk|service ?desk|support (?:cent(?:er|re)|portal|team)|"
    r"centro de ayuda|mesa de ayuda|soporte)\b[^\]]{0,40}\]", re.I)
TICKET_NUMBER_RE = re.compile(r"\b(?:request|ticket|solicitud|incidente?)\s*(?:#|n[°ºo]\.?\s*)\s*\d{2,}", re.I)
SUPPORT_MAILBOX_RE = re.compile(
    r"^(?:support|soporte|help|ayuda|help-?desk|service-?desk|mesa-?de-?ayuda|tickets?|customer-?(?:care|service|success)"
    r"|atencion(?:alcliente)?)(?:[+._-][^@]*)?@", re.I)
# Hosts that send mail for another company's support desk. An address there is never a customer's own domain.
HELPDESK_HOSTS = ("zendesk.com", "freshdesk.com", "helpscout.net", "helpscoutapp.com", "intercom-mail.com",
                  "intercom.io", "atlassian.net", "service-now.com", "servicenow.com", "zohodesk.com", "hubspot.com",
                  "salesforce.com", "kayako.com", "gorgias.com", "frontapp.com")
WAITING_DAYS = (7, 60)      # whole elapsed days since our latest real outbound message


def is_automated(senders: list[str], subject: str | None) -> bool:
    subject = subject or ""
    return (bool(AUTO_SUBJECT_RE.match(subject)) or bool(TICKET_SUBJECT_RE.search(subject))
            or any(AUTO_SENDER_RE.search(a or "") for a in senders)
            or (bool(TICKET_NUMBER_RE.search(subject))
                and any(helpdesk_host(a) or SUPPORT_MAILBOX_RE.match(a or "") for a in senders)))


def helpdesk_host(address: str | None) -> bool:
    host = (address or "").rsplit("@", 1)[-1].lower()
    return any(host == h or host.endswith("." + h) for h in HELPDESK_HOSTS)


def reply_origin(senders: list[str], own_domains: set[str] | None, own_contacts: set[str]) -> str | None:
    """None when the customer wrote; otherwise the sender's domain, so the row names it instead of the customer.

    own_domains are the deal company's own email domains (None when we have learned none yet: then only a help-desk
    host is ruled out). own_contacts are addresses already tied to that company, for people on free-mail domains.
    """
    for address in senders:
        address = (address or "").lower()
        host = address.rsplit("@", 1)[-1]
        if helpdesk_host(address):
            return host
        if address in own_contacts or own_domains is None or host in own_domains:
            continue
        return host
    return None


@dataclass(frozen=True)
class Cadence:
    """How often to nudge while a customer stays silent, and when to suggest Paused instead (see config)."""
    every: int = 7                  # days between "follow up this week" nudges for one deal
    pause_after: int = 5            # our emails in a row with no reply before suggesting Paused
    briefed: frozenset = frozenset()  # recent days a brief was sent, so a nudge shows once per mark

    def due(self, last_sent: date, today: date) -> bool:
        """True on the first brief on or after each `every`-day mark since our last email, and only then.

        A mark that falls on a day with no brief (a weekend) is shown on the next brief day, not skipped.
        """
        days = (today - last_sent).days
        if days < self.every:
            return False
        mark = last_sent + timedelta(days=days - days % self.every)
        return not any(mark <= sent < today for sent in self.briefed)


def elapsed(days: int) -> str:
    """"9 days", "2 weeks", "5 weeks": whole weeks once it is two or more."""
    if days < 14:
        return f"{days} day{'' if days == 1 else 's'}"
    return f"{days // 7} weeks"


def nudge_text(days: int) -> str:
    return f"{elapsed(days)} since your last email - follow up this week"


def pause_text(emails: int) -> str:
    return f"{emails} emails with no reply - consider moving to Paused"


@dataclass
class Waiting:
    """A thread where we wrote last and are still expecting the customer's response."""
    deal: str
    stage: str | None
    to: str                         # who we wrote to
    sent: date                      # our latest email
    days: int
    nudges: int                     # our emails in a row since their last real reply
    replied_before: bool            # they had answered earlier in this thread
    auto_reply: bool                # an out-of-office came back after our latest email
    link: str | None
    source: str = ""
    thread_id: str | None = None
    owner: str | None = None        # whose section it goes to: who sent it, else the deal owner
    rank: int | None = None
    action: str | None = None      # the specific response we are waiting on
    evidence: frozenset[str] = frozenset()  # insight IDs for the specific ask
    nudge: bool = False             # today is this deal's weekly "follow up this week" day
    pause_after: int | None = 5     # our unanswered emails in a row before the row suggests Paused; None: never
                                    # (the deal is already Paused, or time-based nudges are switched off)


def waiting_on_them(thread: list[dict], internal: set[str], now: datetime) -> dict | None:
    """The latest real message is our 7–60 day old outbound with a verified open reply expectation.

    Each message: {"at": datetime, "from": [address], "to": [{"address", "name"}], "cc": [...], "automated": bool},
    oldest first. Returns the latest message, the message supporting the reply expectation, and waiting details.
    """
    dom = lambda a: (a or "").rsplit("@", 1)[-1].lower()
    ours = lambda m: bool(m["from"]) and all(dom(a) in internal for a in m["from"])
    real = [m for m in thread if not m["automated"]]
    if not real or not ours(real[-1]):
        return None
    last = real[-1]
    days = (now - last["at"]).days
    if not WAITING_DAYS[0] <= days <= WAITING_DAYS[1]:
        return None
    people = [p for p in (last.get("to") or []) + (last.get("cc") or [])
              if dom(p.get("address")) not in internal and not AUTO_SENDER_RE.search(p.get("address") or "")]
    if not people:
        return None                 # an internal-only thread: nobody outside to wait for
    expectation = last.get("reply_expectation")
    if not isinstance(expectation, dict) or expectation.get("state") != "expected":
        return None
    if (not last.get("external_id") or not last.get("content_hash")
            or expectation.get("latest_message_id") != last["external_id"]
            or expectation.get("latest_content_hash") != last["content_hash"]):
        return None
    evidence_id, evidence_hash = expectation.get("evidence_message_id"), expectation.get("evidence_content_hash")
    if not evidence_id or not evidence_hash:
        return None
    evidence = next((m for m in real if m.get("external_id") == evidence_id
                     and m.get("content_hash") == evidence_hash), None)
    if evidence is None:
        return None
    nudges = 0
    for m in reversed(real):
        if not ours(m):
            break
        nudges += 1
    names = [(p.get("name") or "").strip() or p["address"] for p in people[:2]]
    to = ", ".join(names) + (f" +{len(people) - 2}" if len(people) > 2 else "")
    return {"last": last, "evidence": evidence, "evidence_outbound": ours(evidence),
            "to": to, "days": days, "nudges": nudges,
            "replied_before": any(not ours(m) for m in real),
            "auto_reply": any(m["automated"] and m["at"] > last["at"] for m in thread)}


def waiting_left_out(waiting: list[Waiting], code: str, shown: list[Waiting]) -> list[str]:
    """The deals of this founder's waits that pick_waiting did not show, in the same order."""
    mine = sorted((w for w in waiting if w.owner == code), key=lambda w: (w.rank not in PROSPECT_RANKS, w.days, w.deal))
    return [w.deal for w in mine if all(w is not s for s in shown)]


def pick_waiting(waiting: list[Waiting], code: str, limit: int = MAX_TABLE_ROWS) -> tuple[list[Waiting], int]:
    """This founder's waits: prospects and pilots first, then the most recent silence (still worth a nudge)."""
    mine = sorted((w for w in waiting if w.owner == code), key=lambda w: (w.rank not in PROSPECT_RANKS, w.days, w.deal))
    shown = min(MAX_WAITING, limit)
    return mine[:shown], max(0, len(mine) - shown)


def waiting_text(w: Waiting) -> str:
    """Legacy completion-preview wording; table display uses the full action in _waiting_text()."""
    action = f" · {first_sentence(w.action)}" if w.action else ""
    return f"To {w.to} · {_waiting_status(w)}{action}"


def _waiting_status(w: Waiting) -> str:
    """How the silence stands, then what to do about it: Paused after too many emails, else the weekly nudge."""
    # The Paused suggestion follows the weekly cadence and takes the nudge's place on that day; on other days the
    # row only states the silence.
    suggest_pause = w.nudge and w.pause_after is not None and w.nudges >= w.pause_after
    if suggest_pause:
        bits = [pause_text(w.nudges)]
    elif w.replied_before:
        bits = [f"{w.nudges} emails since their last reply" if w.nudges > 1 else "no reply to our last email"]
    else:
        bits = [f"no reply to our {w.nudges} emails" if w.nudges > 1 else "no reply yet"]
    if w.auto_reply:
        bits.append("out-of-office received")
    if w.nudge and not suggest_pause:
        bits.append(nudge_text(w.days))
    return ", ".join(bits)


def first_sentence(text: str, limit: int = 95) -> str:
    parts = split_sentences(text) or [""]    # blank text stays blank, as before 11/17
    s = parts[0] if len(parts[0]) >= 30 or len(parts) == 1 else f"{parts[0]} {parts[1]}"  # "Call Ale." alone says too little
    return s if len(s) <= limit else s[:limit].rsplit(" ", 1)[0] + "…"


def _date(d: date) -> str:
    return f"{d:%b} {d.day}"


SHEET_SOURCE = "Sheet next step"
MAX_LEFT_NAMES = 8            # deals named on a "+ N more" line before "and N more"


def sort_today(rows: list[TodayRow]) -> list[TodayRow]:
    """What cannot wait first: a customer waiting on our reply, then what is due today (then tomorrow), then what
    is overdue by its own date, most recent first, then late Sheet-date rows, most recent first. Stable otherwise.

    "Needs you today" is never cut (founders, 11/17: missing an email or a deadline is the worst failure), so this
    order only decides what a founder reads first.
    """
    def rank(r: TodayRow) -> tuple:
        if r.tag == "Reply needed":
            return 0, 0, 0
        if r.late <= 0:
            return 0, 1, -r.late
        return (2 if r.tag == "Sheet date" else 1), r.late, 0
    return sorted(rows, key=rank)


def left_out_names(names) -> str:
    """The deals a capped table does not show, each once, in order: at most MAX_LEFT_NAMES, then "and N more"."""
    unique = list(dict.fromkeys(n for n in names if n))
    more = f" and {len(unique) - MAX_LEFT_NAMES} more" if len(unique) > MAX_LEFT_NAMES else ""
    return ", ".join(unique[:MAX_LEFT_NAMES]) + more


def more_line(count: int, what: str, names) -> str:
    """"+ 3 more in the Sheet waiting on a reply: Andina Chile, Arca Peru, Chifles" - the same in HTML and text."""
    listed = left_out_names(names)
    return f"+ {count} more {what}" + (f": {listed}" if listed else "")


def sheet_rows(v: DealView, today: date, overdue_days: int, owner: str) -> tuple[list[TodayRow], list[SheetDue], list[SheetDue]]:
    """(rows for "Needs you today", steps not yet due, steps too long past) for one deal's dated Sheet bullets.

    A dated bullet shows on its date and stays, marked late, for overdue_days. Before its date it is not shown;
    long after it, it becomes a reminder to update the Sheet rather than a daily row. If the same step also exists
    as an extracted item, the row closes that item (so "Done" and the pre-send check work) and is the only row
    shown for it.
    """
    rows, later, stale = [], [], []
    for step in v.sheet_steps:
        late = (today - step.due).days
        if late < 0:
            later.append(step)
        elif late > overdue_days:
            stale.append(step)
        else:
            when = "due today" if late == 0 else f"due {_date(step.due)}, {late}d late"
            rows.append(TodayRow(v.deal, v.stage, "Sheet date", step.text, when, owner, None, step.item_id,
                                 SHEET_SOURCE, f"insight:{step.item_id}" if step.item_id else f"rec:{step.ref}",
                                 verify=step.item_id is not None, late=late))
    return rows, later, stale


def addresses(evidence: frozenset, blocker: Note) -> bool:
    """A move addresses a blocker when it rests on the same source evidence (shared cited insights)."""
    return bool(evidence & blocker.evidence)


def _still_open(when: str, replied: date | None) -> str:
    """Mark a row whose email thread we have answered since: likely handled, but only a person can say."""
    return f"{when} · you wrote back {_date(replied)} - still open?" if replied else when


def item_move(tag: str, text: str, when: str, i: Item) -> Move:
    return Move(tag, text, _still_open(when, i.replied), i.link, i.id, i.source, f"insight:{i.id}", i.deal)


def email_move(tag: str, text: str, when: str, u: Unanswered, deal: str | None = None) -> Move:
    return Move(tag, text, when, u.link, u.item_id, u.source, u.completion_ref, deal)


def note_move(tag: str, text: str, when: str, n: Note, link: str | None, deal: str | None = None) -> Move:
    return Move(tag, text, _still_open(when, n.replied), link, None, n.source, f"rec:{n.ref}" if n.ref else None, deal)


def recorded_move(i: Item, text: str) -> Move:
    if i.kind == "owed":
        return item_move("We owe", text, f"due {_date(i.due)}" if i.due else f"recorded {_date(i.recorded)}", i)
    if i.kind == "customer_request":
        return item_move("Customer asked", text,
                         f"requested by {_date(i.due)}" if i.due else f"asked {_date(i.recorded)}", i)
    return item_move("Next step noted", text,
                     f"date mentioned {_date(i.due)}" if i.due else f"recorded {_date(i.recorded)}", i)


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
        for i in sorted((i for i in open_items if i.kind in ("owed", "customer_request", "next_step") and i.id in b.evidence),
                        key=lambda i: i.due or date.max):
            return recorded_move(i, i.text)
        for r in d.recommendations:
            if r.verified and addresses(r.evidence, b):
                return note_move("AI recommendation", r.text, _last(d, today), r, r.link)
        if d.last_reply and b.on and d.last_reply > b.on:
            # The customer has written since this blocker was recorded; the profile was not rebuilt because nothing
            # new was extracted from the reply, so do not present the old blocker as untouched.
            return Move("Clarify next action", f"The customer replied on {_date(d.last_reply)}, after this blocker "
                        f"was recorded - read the reply and check whether it still stands",
                        _last(d, today), b.link, None, b.source)
        return Move("Clarify next action", "No recorded action or suggestion addresses this blocker yet",
                    _last(d, today), b.link, None, b.source)
    for i in sorted((i for i in open_items if i.kind == "owed" and i.bucket in ("overdue", "week")),
                    key=lambda i: i.due or date.max):
        late = (today - i.due).days
        return item_move("We owe", i.text, f"due {_date(i.due)}" + (f", {late}d late" if late > 0 else ""), i)
    for kind in ("customer_request", "next_step"):
        for i in sorted((i for i in open_items if i.kind == kind and i.bucket not in ("stale", "old_undated")),
                        key=lambda i: (i.due or date.max, -i.recorded.toordinal())):
            return recorded_move(i, i.text)
    if d.recommendation and d.recommendation.verified:
        return note_move("AI recommendation", d.recommendation.text, _last(d, today), d.recommendation,
                         d.recommendation.link)
    return Move("Confirm status", "Check where this deal stands", _last(d, today), None)


def _last(d: DealView, today: date) -> str:
    return f"last recorded {_date(d.last_contact)} ({(today - d.last_contact).days}d)" if d.last_contact else "no record"


def situation(d: DealView) -> Note | None:
    """The verified blocker or summary, with full wording for the display pass."""
    for n in (d.blocker, d.summary):
        if n and n.verified:
            return n
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
    if any(i.kind == "customer_request" and i.bucket not in ("stale", "old_undated")
           and (today - i.recorded).days <= REQUEST_FRESH_DAYS
           for i in v.items):
        signals.append("outstanding customer request")
    if v.blocker and v.blocker.verified:
        signals.append("verified blocker")
    if v.developments:
        signals.append(f"{v.developments} new or late-recorded development(s)")
    if v.unanswered and reply_label(v.unanswered) == "reply_needed":
        signals.append(f"customer email waiting {v.unanswered.days}d")
    if v.recommendation and v.recommendation.verified:
        signals.append("next milestone: verified recommendation")
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
        if owner_side == "customer" and type_ == "commitment":
            cat = "they_owe"
        elif kind == "owed":
            cat = "commitment_owed"
        else:
            cat = kind or "next_step"
    elif type_ == "buying_signal" and speaker == "bianca":
        cat = "progress"
    else:
        cat = type_
    return cat, f"{CHANGE_LABELS.get(cat, cat)}: {text}"


def undated_note(count: int, deals: list[str], from_notes: bool = True) -> str:
    """Say what the undated developments are, so the count is not a riddle (11/09 evals, finding 19).

    They almost always come from meeting-notes sections with no date in their heading: the day is a guess, so they
    are never listed as new. The deals are named so a founder can check the notes doc.
    """
    names = ", ".join(deals[:MAX_NAMES]) + (f" +{len(deals) - MAX_NAMES} more" if len(deals) > MAX_NAMES else "")
    if not from_notes:
        return f"Also not shown as new: {count} item(s) whose source has no reliable date ({names})."
    return (f"Also not shown as new: {count} item(s) from meeting notes whose heading has no date ({names}). "
            f"Add the meeting date to the heading in the notes doc to have them listed.")


def change_group(rank: int | None) -> int:
    """0: prospects and pilots, 1: customers, 2: early or paused."""
    return 0 if rank in (2, 3, 4) else 1 if rank == CUSTOMER_RANK else 2


def select_changes(changes: list[ChangeRow], limit: int = MAX_TABLE_ROWS) -> list[ChangeRow]:
    """The first `limit` changes: In Talks, Proposal and Pilot deals first, then customers, then Opening and Paused.

    Inside each group: what happened since the last brief before what was recorded late, and what the customer
    wrote before the rest, so a customer's reply is not pushed out by our own notes. The caller prints how many
    were left out. (Until 11/17 this kept 3 + 2 + 0 rows per group, which dropped replies on busy days and every
    change on an Opening deal.)
    """
    ranked = sorted(changes, key=lambda c: (change_group(c.rank), c.kind != "new", not c.from_customer))
    return ranked[:limit]


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
    waiting: list[Waiting] = field(default_factory=list)  # we wrote last, no answer yet
    more_waiting: int = 0
    verification: VerificationNotes | None = None
    sheet_later: list[tuple[str, date]] = field(default_factory=list)   # (deal, date) of dated Sheet steps not yet due
    sheet_stale: list[tuple[str, date]] = field(default_factory=list)   # (deal, date) of Sheet dates long past
    held: int = 0                   # extracted items waiting for the Sheet date a founder gave them
    more_today: int = 0             # always 0 since 11/17: Needs you today shows every row (key kept for callers)
    left: dict[str, list[str]] = field(default_factory=dict)   # table -> deals its "+ N more" line names
    more_meetings: int = 0
    resurfaced: list[DealView] = field(default_factory=list)   # today's small batch of quiet In Talks deals
    quiet_in_talks: int = 0         # all quiet In Talks deals this founder owns, shown today or not


def cap_tables(section: Section, limit: int = MAX_TABLE_ROWS) -> Section:
    """Keep Meetings to `limit` rows and name what was left out. "Needs you today" is never cut: every row shows.

    Run this after the pre-send check, so the rows that survive it fill the table. The other capped tables (Deals
    to move, Waiting, Follow-ups, What changed) are capped where they are built; each prints a "+ N more" line
    naming the deals it left out.
    """
    section.more_meetings += max(0, len(section.meetings) - limit)
    section.left["meetings"] = [m.deal or m.title for m, _ in section.meetings[limit:]]
    section.meetings = section.meetings[:limit]
    return section


def _without_waiting_action(view: DealView, waiting: Waiting) -> DealView:
    action = norm(waiting.action).rstrip(".!?")

    def same_action(text: str) -> bool:
        return bool(action) and norm(text).rstrip(".!?") == action

    def repeats_ask(note: Note) -> bool:
        shared = note.evidence & waiting.evidence
        if shared and note.evidence <= waiting.evidence:
            return True
        # Equal wording can describe different responsibilities when both sources are known.
        return same_action(note.text) and (bool(shared) or not note.evidence or not waiting.evidence)

    items = [item for item in view.items if item.id not in waiting.evidence
             and not (not waiting.evidence and item.kind == "proposed" and waiting.link
                      and item.link == waiting.link and same_action(item.text))]
    recommendations = [note for note in view.recommendations if not repeats_ask(note)]
    preferred = view.recommendation
    if preferred and repeats_ask(preferred):
        preferred = next(iter(recommendations), None)
    return replace(view, items=items,
                   blocker=None if view.blocker and repeats_ask(view.blocker) else view.blocker,
                   summary=None if view.summary and repeats_ask(view.summary) else view.summary,
                   recommendation=preferred, recommendations=recommendations)


_TOPIC = re.compile(r"\b(?:on|about|regarding|re:|sobre|acerca de)\s+(?P<topic>[^.!?;]{3,70})[.!?]?\s*$", re.I)


def _nudge(view: DealView, today: date, cadence: Cadence, about: str, basis: date | None, tag_since: str,
           force: bool = False) -> tuple[str, str] | None:
    """(text, when) for a deal's one time-based follow-up reminder today, or None when none is due.

    Instead of repeating a vague "keep following up", say how long it has been and what to do: after
    cadence.pause_after of our emails with no reply, suggest Paused; otherwise "follow up this week", shown once
    per cadence.every days. No reminder while the customer wrote last (the next move is ours, not a nudge).
    """
    if is_paused(view.stage):
        return None                 # a Paused deal is parked on purpose: no reminder, and nothing to move to Paused
    if view.last_reply and view.last_outbound and view.last_reply > view.last_outbound:
        return None
    if view.unanswered_emails >= cadence.pause_after:
        # Once per cadence.every days since our last email, on the day the nudge would have shown, and instead of it.
        if not view.last_outbound or not (force or cadence.due(view.last_outbound, today)):
            return None
        return f"{view.deal}: {pause_text(view.unanswered_emails)}.", f"last email {_date(view.last_outbound)}"
    if basis is None or basis == date.min or not (force or cadence.due(basis, today)):
        return None                 # date.min: the source has no date at all, so "N weeks since" cannot be said
    days = (today - basis).days
    if about.startswith(":"):       # a proposal of ours: keep what was proposed
        tail = about.rstrip(". ") + "."
    else:                           # a nudge-only text: keep only its topic, if it names one
        topic = _TOPIC.search(about)
        tail = f" (re: {topic['topic'].strip()})." if topic else "."
    return f"{view.deal}: {elapsed(days)} since {tag_since} - follow up this week{tail}", f"{tag_since} {_date(basis)}"


def _split_followups(view: DealView, today: date, used: set[str], cadence: Cadence | None = Cadence(),
                     has_wait: bool = False) -> tuple[DealView, list[Move], bool]:
    """Collect reminder actions and leave independent next moves available for deal selection.

    Returns the view without its reminder material, the reminders to print today, and whether the deal has
    reminder material at all (it may be between two weekly nudges, with nothing to print today).

    Nudge-only items and suggestions ("keep following up", "check in") and waiting statements ("wait for Franco's
    reply", a blocker that says "no reply since Oct 21") are never printed as written or used as a deal's next
    move: what is with the customer belongs to Waiting, not to Deals to move. A deal gets
    at most one time-based reminder ("3 weeks since your last email - follow up this week"), on its weekly day,
    and none when a Waiting row for the deal already carries it (has_wait). A Paused deal gets none at all.

    cadence=None switches the time-based reminders off (BRIEF_FOLLOWUP_NUDGES=false): reminders are then printed as
    written and waiting statements are left alone, exactly as before 11/17.
    """
    timed = cadence is not None
    followups: list[Move] = []
    followup_items: set[str] = set()
    unanswered = view.unanswered
    if unanswered and reply_label(unanswered) == "follow_up":
        if unanswered.item_id not in used:
            followups.append(email_move("Follow up", f"{view.deal}: {reply_text(unanswered)}",
                                        f"email {unanswered.days}d ago", unanswered, view.deal))
        if unanswered.item_id:
            followup_items.add(unanswered.item_id)
        unanswered = None

    nudges: list[tuple[Move, str, date | None, str]] = []   # (what closes it, its wording, since when, since what)
    proposals = [i for i in view.items if i.kind == "proposed" and i.id not in used
                 and PROPOSAL_FOLLOWUP_DAYS[0] <= (today - i.recorded).days <= PROPOSAL_FOLLOWUP_DAYS[1]
                 and (view.last_contact or i.recorded) <= i.recorded]
    if proposals:
        proposal = max(proposals, key=lambda i: i.recorded)
        if timed:
            nudges.append((item_move("Follow up", "", "", proposal), f": {proposal.text}",
                           proposal.recorded, "we proposed this"))
        else:
            followups.append(item_move("Follow up", f"{view.deal}: Check whether to follow up on our proposal. "
                                       f"{proposal.text}", f"proposed {_date(proposal.recorded)}", proposal))
        followup_items.update(i.id for i in proposals)

    for i in view.items:
        if (i.kind not in ("owed", "customer_request", "next_step") or i.id in followup_items
                or not (is_followup_only(i.text) or (timed and is_waiting_only(i.text)))):
            continue
        followup_items.add(i.id)
        if i.id not in used and i.bucket not in ("stale", "old_undated"):
            if timed:
                nudges.append((item_move("Follow up", "", "", i), i.text, view.last_outbound or i.recorded,
                               "your last email" if view.last_outbound else "this was noted"))
            else:
                followups.append(item_move("Follow up", f"{view.deal}: {i.text}", f"recorded {_date(i.recorded)}", i))

    recommendations = list(view.recommendations)
    if view.recommendation and view.recommendation not in recommendations:
        recommendations.insert(0, view.recommendation)
    deal_moves = []
    reminder_texts = {norm(i.text).rstrip(".!?") for i in view.items if i.id in followup_items}
    if view.unanswered and unanswered is None:
        reminder_texts.add(norm(reply_text(view.unanswered)).rstrip(".!?"))
    for rec in recommendations:
        if not rec.verified:
            continue
        repeats_reminder = bool(rec.evidence & followup_items) and norm(rec.text).rstrip(".!?") in reminder_texts
        if (rec.followup_only or is_followup_only(rec.text) or (timed and is_waiting_only(rec.text))
                or repeats_reminder):
            if rec.evidence and rec.evidence <= followup_items:
                pass                    # the item it rests on is already the reminder
            elif timed:
                nudges.append((note_move("Follow up", "", "", rec, rec.link, view.deal), rec.text,
                               view.last_outbound or view.last_contact,
                               "your last email" if view.last_outbound else "the last contact on record"))
            else:
                followups.append(note_move("Follow up", f"{view.deal}: {rec.text}", _last(view, today),
                                           rec, rec.link, view.deal))
        else:
            deal_moves.append(rec)

    # A blocker that only says "no reply since ..." is the waiting state itself, not a situation with a next move.
    blocker = view.blocker
    if timed and blocker and blocker.verified and is_waiting_only(blocker.text):
        nudges.append((Move("Follow up", "", "", blocker.link, None, blocker.source, None, view.deal), "",
                       view.last_outbound or view.last_contact,
                       "your last email" if view.last_outbound else "the last contact on record"))
        blocker = None

    if timed and nudges and not has_wait:
        target, about, basis, since = nudges[0]
        worded = _nudge(view, today, cadence, about, basis, since)
        if worded:
            followups.append(replace(target, text=worded[0], when=worded[1], deal=view.deal))

    return (replace(view, items=[i for i in view.items if i.id not in followup_items], unanswered=unanswered,
                    blocker=blocker, recommendation=next(iter(deal_moves), None), recommendations=deal_moves),
            followups, bool(followups or nudges))


def build_section(code: str, name: str, short: str, views: dict[str, DealView], items: list[Item],
                  meetings: list[tuple[Meeting, str]], today: date, display: dict[str, str],
                  hidden: int = 0, critical_ahead: int = 3, in_talks_target: int = 2, *,
                  waiting: list[Waiting] | None = None, more_waiting: int = 0, left_waiting: list[str] | None = None,
                  waiting_candidates: list[Waiting] | None = None, sheet_overdue_days: int = 14,
                  cadence: Cadence | None = Cadence(), max_rows: int = MAX_TABLE_ROWS, resurface: int = 0,
                  resurfaced_recently: frozenset[str] = frozenset()) -> Section:
    mine: dict[str, DealView] = {}

    def view(deal: str) -> DealView:
        if deal not in mine:
            src = views[deal]
            own = src.owner == code  # profile notes and unanswered email belong to the deal owner
            mine[deal] = DealView(src.deal, src.stage, src.rank, src.owner, src.last_contact,
                                  blocker=src.blocker if own else None, recommendation=src.recommendation if own else None,
                                  summary=src.summary if own else None, unanswered=src.unanswered if own else None,
                                  developments=src.developments if own else 0,
                                  recommendations=src.recommendations if own else [],
                                  sheet_steps=src.sheet_steps if own else [], held=src.held if own else 0,
                                  sheet_next=src.sheet_next, last_outbound=src.last_outbound,
                                  last_reply=src.last_reply, unanswered_emails=src.unanswered_emails)
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
                today_rows.append(TodayRow(v.deal, v.stage, "Due now", i.text, when, owner_label(i), i.link, i.id,
                                           i.source, f"insight:{i.id}", late=late))
                used.add(i.id)
        if v.unanswered and reply_label(v.unanswered) == "reply_needed":
            u = v.unanswered
            today_rows.append(TodayRow(v.deal, v.stage, "Reply needed", reply_text(u), f"received {u.days}d ago",
                                       display.get(code, code) + " (deal owner)", u.link, u.item_id, u.source,
                                       u.completion_ref))
            if u.item_id:
                used.add(u.item_id)
    sheet_later: list[tuple[str, date]] = []
    sheet_stale: list[tuple[str, date]] = []
    for v in mine.values():         # the dates the founders set themselves in the Sheet's Next step
        rows, later, stale = sheet_rows(v, today, sheet_overdue_days, display.get(code, code) + " (Sheet)")
        today_rows += rows
        used.update(r.item_id for r in rows if r.item_id)
        sheet_later += [(v.deal, step.due) for step in later]
        sheet_stale += [(v.deal, step.due) for step in stale]
    today_rows = sort_today(today_rows)
    in_today = {r.deal for r in today_rows}

    # Deals to move: prospects and pilots this founder owns, active or with something pressing.
    def pressing(v: DealView) -> bool:  # a dated promise or a reply keeps a quiet deal visible; nothing else does
        return v.deal in in_today or any(i.kind == "owed" and i.bucket in ("overdue", "week") for i in v.items)

    owned = [v for v in mine.values() if v.owner == code]
    eligible = [v for v in owned if v.rank in PROSPECT_RANKS and
                (deal_status(v, today) == "active" or (v.rank != 1 and pressing(v)) or (v.rank == 1 and v.deal in in_today))]
    waiting = waiting or []
    waits_by_deal: dict[str, list[Waiting]] = {}
    for w in waiting if waiting_candidates is None else waiting_candidates:
        waits_by_deal.setdefault(w.deal, []).append(w)

    action_views = {}
    reminders = {}
    quiet_reminders: set[str] = set()   # deals whose only material is a reminder, even on a day none is printed
    for deal, v in mine.items():
        for w in waits_by_deal.get(deal, []):
            v = _without_waiting_action(v, w)
        action_views[deal], reminders[deal], pending = _split_followups(v, today, used, cadence,
                                                                        has_wait=bool(waits_by_deal.get(deal)))
        if pending:
            quiet_reminders.add(deal)

    highlight_views = []
    followup_only = set()
    dated_only = set()
    today_only = set()
    sheet_dated = {r.deal for r in today_rows if r.tag == "Sheet date"} | {deal for deal, _ in sheet_later}
    primary_moves = {}
    for original in eligible:
        v = action_views[original.deal]
        move = primary_move(v, today, used)
        has_reminder = bool(waits_by_deal.get(v.deal) or reminders[v.deal] or v.deal in quiet_reminders)
        proposal_only = move.tag == "Confirm status" and any(i.kind == "proposed" for i in original.items)
        # A generic fallback would give a follow-up-only deal a highlight slot.
        if proposal_only or (has_reminder and move.tag in ("Confirm status", "Clarify next action")):
            followup_only.add(v.deal)
            continue
        # The founder already dated this deal's next step in the Sheet: no "check where this stands" on top of it.
        if move.tag == "Confirm status" and v.deal in sheet_dated:
            dated_only.add(v.deal)
            continue
        # A deal whose one open thing is already in Needs you today has nothing left for a second row.
        if move.tag == "Confirm status" and v.deal in in_today:
            today_only.add(v.deal)
            continue
        primary_moves[v.deal] = move
        highlight_views.append(v)

    selected, audit = select_deals(highlight_views, today, in_today, critical_ahead, in_talks_target,
                                   slots=min(MAX_HIGHLIGHTS, max_rows))
    for v in sorted(owned, key=lambda v: v.deal):  # the audit also explains prospects that were not eligible at all
        if v.deal in followup_only:
            audit.append(SelectionRecord(v.deal, v.stage, False, None, [], [],
                                         "not eligible: waiting or follow-up with no separate next move"))
        elif v.deal in dated_only:
            audit.append(SelectionRecord(v.deal, v.stage, False, None, [], [],
                                         "not eligible: its next step is dated in the Sheet and nothing else is open"))
        elif v.deal in today_only:
            audit.append(SelectionRecord(v.deal, v.stage, False, None, [], [],
                                         "not eligible: already in Needs you today and nothing else is open"))
        elif v.rank in PROSPECT_RANKS and v not in eligible:
            status = deal_status(v, today)
            audit.append(SelectionRecord(v.deal, v.stage, False, None, [], [],
                                         f"not eligible: status {status}" + (f", last record {_date(v.last_contact)}"
                                                                             if v.last_contact else "")))
    highlights = []
    for v in selected:
        m = primary_moves[v.deal]
        if m.item_id:
            used.add(m.item_id)
        highlights.append((v, m, situation(v)))
    shown = {v.deal for v, _, _ in highlights}

    # Follow-ups stay here even when the deal also has a separate highlighted move.
    follow: list[Move] = []
    for v, m, _ in highlights:  # when the move addresses the blocker instead, the nearest promise stays visible
        dated = sorted((i for i in v.items if i.id not in used and i.kind == "owed" and i.bucket in ("overdue", "week")),
                       key=lambda i: i.due)
        if m.tag != "We owe" and dated:
            i = dated[0]
            follow.append(item_move("We owe", f"{v.deal}: {i.text}", f"due {_date(i.due)}", i))
    for v in action_views.values():
        follow.extend(reminders[v.deal])
        if v.deal in shown:
            continue
        for i in v.items:
            if i.id in used:
                continue
            if (i.kind == "owed" and i.bucket == "overdue") or (
                    i.kind in ("customer_request", "next_step") and i.bucket not in ("stale", "old_undated")):
                if not v.unanswered or i.id != v.unanswered.item_id:
                    follow.append(recorded_move(i, f"{v.deal}: {i.text}"))
    follow_shown = follow[:min(MAX_FOLLOWUPS, max_rows)]
    used |= {m.item_id for m in follow_shown if m.item_id}

    # Status to confirm: owned prospects/pilots not shown whose last record is 15-45 days old (or missing).
    unclear = sorted((v for v in owned if v.rank in PROSPECT_RANKS and v.deal not in shown and v.deal not in sheet_dated
                      and deal_status(v, today) == "unclear"), key=lambda v: (-(v.rank or 0), v.deal))
    dormant = sum(1 for v in owned if v.rank in PROSPECT_RANKS and v.deal not in shown and v.deal not in sheet_dated
                  and deal_status(v, today) == "dormant")

    # Quiet In Talks deals come back in small batches (founders, 11/12: "if it is In Talks it should appear
    # somewhere, but not 70 emails in one day"): owned, In Talks, no recorded contact in over two weeks (or none
    # at all), and not already in this section. Longest-untouched first; a deal listed in the last cooldown
    # period waits its turn, so the batch moves through the list instead of repeating.
    busy = (shown | in_today | sheet_dated | set(waits_by_deal)
            | {m.deal or m.text.split(":", 1)[0] for m in follow_shown}
            | {m.deal for m, _ in meetings if m.deal})     # meeting it today or tomorrow is not "quiet"
    quiet = sorted((v for v in owned if v.rank == IN_TALKS_RANK and v.deal not in busy
                    and deal_status(v, today) != "active"), key=lambda v: (v.last_contact or date.min, v.deal))
    resurfaced = [v for v in quiet if v.deal not in resurfaced_recently][:max(0, resurface)]
    unclear = [v for v in unclear if v not in resurfaced]
    dormant -= sum(1 for v in resurfaced if deal_status(v, today) == "dormant")   # named above, not counted again

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
                   meetings, follow_shown, [], len(follow) - len(follow_shown), unclear, dormant, b,
                   hidden, routed, audit, waiting=waiting, more_waiting=more_waiting,
                   sheet_later=sorted(sheet_later, key=lambda pair: pair[1]),
                   sheet_stale=sorted(sheet_stale, key=lambda pair: pair[1]),
                   held=sum(v.held for v in mine.values()), resurfaced=resurfaced,
                   quiet_in_talks=len(quiet) if resurface > 0 else 0,
                   left={"waiting": list(left_waiting or []),
                         "followups": [m.deal or m.text.split(":", 1)[0] for m in follow[len(follow_shown):]]})


# ---------------------------------------------------------------- meeting prep (pure)

RESPONSES = {"accepted": "accepted", "declined": "declined", "tentative": "tentative", "needsAction": "not yet answered"}
PREP_LABELS = {"AI recommendation": "Suggested", "Follow up": "Follow up"}


def _guest_line(guests: list[dict]) -> str:
    """Who from outside Bianca is on the invite and whether they said yes."""
    if not guests:
        return ""
    name = lambda g: g.get("name") or g["address"]
    if all(g.get("response") == "declined" for g in guests):
        who = name(guests[0]) + (f" +{len(guests) - 1}" if len(guests) > 1 else "")
        return f"{who} declined the invite - confirm it is still on"
    lead = next((g for g in guests if g.get("response") == "accepted"), guests[0])
    answer = RESPONSES.get(lead.get("response") or "")
    return f"With {name(lead)}" + (f" ({answer})" if answer else "") + (f" +{len(guests) - 1}" if len(guests) > 1 else "")


def meeting_prep(meeting: Meeting, section: "Section", view: DealView | None, today: date, *, checked: bool = False) -> str:
    """One prep line per meeting, from what the brief already knows. Never blank for a meeting tied to a deal.

    Order: who is coming, then the most pressing thing on record for the deal - a row this brief already shows
    for it (Needs you today, Deals to move, Waiting, Follow-ups), else the deal's open ask, blocker or suggestion,
    else its summary, else when we were last in touch. With checked=True (a verified build) only rows that passed
    the pre-send check and plain facts are used. A meeting with no pipeline deal says so and names the guest.
    """
    guests = _guest_line(meeting.guests)
    if not meeting.deal:
        return ". ".join(x for x in (guests, "No pipeline deal matched - check who this is with") if x) + "."
    deal, context = meeting.deal, ""
    for row in section.today:
        if row.deal == deal:
            context = f"{row.tag}: {row.text}"
            break
    if not context:
        for v, move, note in section.highlights:
            if v.deal != deal:
                continue
            if move.tag in ("Confirm status", "Clarify next action"):
                context = f"Open issue: {note.text}" if note else ""
            else:
                context = f"{PREP_LABELS.get(move.tag, move.tag)}: {move.text}"
            break
    if not context:
        wait = next((w for w in section.waiting if w.deal == deal), None)
        if wait:
            context = f"Waiting on their reply since {_date(wait.sent)}" + (f": {wait.action}" if wait.action else "")
    if not context:
        follow = next((m for m in section.followups if (m.deal or m.text.split(":", 1)[0]) == deal), None)
        if follow:
            context = follow.text.split(":", 1)[-1].strip() if follow.text.startswith(f"{deal}:") else follow.text
    if not context and view is not None:
        if view.unanswered and view.unanswered.action and not checked:
            context = f"Customer asked: {view.unanswered.action}"
        elif view.blocker and view.blocker.verified:
            context = f"Open issue: {view.blocker.text}"
        elif view.recommendation and view.recommendation.verified and not checked:
            context = f"Suggested: {view.recommendation.text}"
        elif view.sheet_next:
            context = f"Sheet next step: {view.sheet_next}"
        elif view.summary and view.summary.verified:
            context = first_or_blank(view.summary.text)
    if not context:
        last = view.last_contact if view else None
        context = (f"No open item on record - last contact {_date(last)}" if last
                   else "No open item or earlier contact on record for this deal")
    matched = f" (deal matched by guest domain {meeting.matched_by})" if meeting.matched_by else ""
    context = context.rstrip()
    lead = f"{guests}. " if guests else ""
    return f"{lead}{context}{'' if context.endswith(('.', '!', '?')) else '.'}{matched}"


# ---------------------------------------------------------------- rendering

TAG_COLOR = {"Due now": RED, "Sheet date": RED, "Reply needed": BLUE, "We owe": INK, "Customer asked": BLUE,
             "Next step noted": BLUE, "AI recommendation": PURPLE, "Follow up": AMBER, "Confirm status": MUTED,
             "Clarify next action": AMBER}

# sRGB background/foreground pairs sampled from the pipeline Sheet. Each pair has at least 4.5:1 contrast.
STAGE_BADGES = {
    "paused": ("Paused", "#ffcfc8", "#b10202"),
    "opening": ("Opening", "#bfe1f6", "#0953a8"),
    "in talks": ("In Talks", "#ffe59f", "#473821"),
    "proposal": ("Proposal", "#e6cff3", "#5b3286"),
    "pilot": ("Pilot", "#d4edbb", "#13734b"),
    "working client": ("Working Client", "#13734b", "#d4edbb"),
}
UNKNOWN_STAGE_COLORS = ("#e5e7eb", "#344054")


def _stage_badge(stage: str | None) -> str:
    label = " ".join(stage_short((stage or "").strip()).split())
    key = label.casefold()
    label, background, foreground = STAGE_BADGES.get(key, (label, *UNKNOWN_STAGE_COLORS))
    suffix = key.replace(" ", "-") if key in STAGE_BADGES else "unknown"
    return (f'<span class="deal-stage deal-stage--{suffix}" style="display:inline-block;margin-top:4px;'
            f'padding:2px 6px;border-radius:4px;background-color:{background};color:{foreground};'
            f'font:600 12px/1.4 {FONT};white-space:nowrap">{_e(label)}</span>')


def _deal_cell(company: str, stage: str | None) -> str:
    return f"<b>{_e(company)}</b><br>{_stage_badge(stage)}"


def _stage_css() -> str:
    """Keep both badge colors together in clients supporting dark CSS; inline styles are the fallback.

    The sender accepts an HTML fragment, so do not opt the rest of the email into dark mode with head metadata.
    Clients that force their own color changes still need inbox checks.
    """
    colors = [(key.replace(" ", "-"), background, foreground) for key, (_, background, foreground) in STAGE_BADGES.items()]
    colors.append(("unknown", *UNKNOWN_STAGE_COLORS))

    def rule(selector: str, background: str, foreground: str) -> str:
        return f"{selector}{{background-color:{background}!important;color:{foreground}!important}}"

    dark = "".join(rule(f".bianca-brief .deal-stage--{key}", background, foreground)
                   for key, background, foreground in colors)
    outlook = "".join(rule(f"[data-ogsc] .bianca-brief .deal-stage--{key},[data-ogsb] .bianca-brief .deal-stage--{key}",
                           background, foreground) for key, background, foreground in colors)
    return f"<style>@media (prefers-color-scheme:dark){{{dark}}}{outlook}</style>"


def _tag(text: str) -> str:
    c = TAG_COLOR.get(text, MUTED)
    return (f'<span style="display:inline-block;padding:0 5px;border:1px solid {c};border-radius:3px;color:{c};'
            f'font:600 10px/16px {FONT};text-transform:uppercase;letter-spacing:.04em;white-space:nowrap">{_e(text)}</span>')


def _muted(s: str) -> str:
    return f'<span style="color:{MUTED};font-size:12px">{s}</span>'


def _table(head: str, rows: list[str]) -> str:
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%">'
            f"<tr>{head}</tr>{''.join(rows)}</table>")


def _left_out(text: str) -> str:
    """The line under a table that says how many rows it does not show."""
    return f'<p style="margin:6px 0 0;font:12px {FONT};color:{MUTED}">{_e(text)}</p>'


def _src(source: str | None) -> str:
    """Where a row comes from and when, as plain text: "Email · Oct 6", "Notes · Sep 28"."""
    return f'<span style="white-space:nowrap">{_muted(_e(source))}</span>' if source else _muted("—")


def _done(ref: str | None, action_url) -> str:
    """The "Mark as complete" button: a link to a confirm page (email can't post forms)."""
    if not (ref and action_url):
        return ""
    return (f'<a href="{_e(action_url(ref))}" style="display:inline-block;padding:2px 7px;border:1px solid #d0d5dd;'
            f'border-radius:4px;font:12px {FONT};color:#344054;text-decoration:none;white-space:nowrap">✓ Done</a>')


def _what_text(row: TodayRow) -> TableText:
    if row.tag == "Reply needed" and row.text.startswith("Review the customer's email and decide the response needed"):
        return TableText("What (reply needed, email subject only)", row.text,
                         required=("Review the customer's email",))
    return TableText(f"What ({row.tag})", row.text)


def _move_text(move: Move) -> TableText:
    required = ("profile not verified",) if move.text.endswith("(profile not verified)") else ()
    return TableText(f"Next move ({move.tag})", move.text, required=required)


def _compact_waiting_status(w: Waiting) -> str:
    """Keep the silence, count, OOO and today's recommendation. The Waiting column already carries elapsed days."""
    count = str(w.nudges)
    # Corrupt/unbounded counters should not consume the entire cell; never cut a number in half.
    if len(count) > 12:
        count = "multiple"
    if w.replied_before:
        status = f"{count} emails since their last reply" if w.nudges > 1 else "no reply to our last email"
    else:
        status = f"no reply to our {count} emails" if w.nudges > 1 else "no reply yet"
    bits = [status]
    if w.auto_reply:
        bits.append("out-of-office received")
    if w.nudge:
        pause = w.pause_after is not None and w.nudges >= w.pause_after
        bits.append("consider Paused" if pause else "follow up this week")
    return "; ".join(bits)


def _waiting_text(w: Waiting) -> TableText:
    """Display-only input: no early sentence cut, and a complete fallback for every optional-model outcome.

    Source action, evidence and completion labels are never replaced with the display result. Fallbacks try
    whole source wording first, then whole leading sentences with an explicit omission cue. If necessary the
    deal column identifies the customer and the original email carries the full recipient/request details.
    """
    text = f"To {w.to} · {_waiting_status(w)}" + (f" · {w.action}" if w.action else "")
    status = _compact_waiting_status(w)
    field = TableText(WAITING_TEXT_KIND, text, required=(status,))
    action = " ".join((w.action or "").split())
    requests = [action] if action else [""]
    if action:
        sentences = split_sentences(action)
        # Never select a later sentence out of its qualifying context, or build quadratic combinations
        # from an unexpectedly large source. A first-sentence excerpt always discloses the omission.
        if len(sentences) > 1 and sentences[0].rstrip("\"'”’)]").endswith((".", "!", "?")):
            requests.append(f"{sentences[0]} More in email.")
        requests.append("Request details in email.")
    for request in requests:
        for recipient in (f"To {w.to}", "Customer"):
            candidate = f"{recipient} · {status}" + (f" · {request}" if request else "")
            if not candidate.rstrip("\"'”’)]").endswith((".", "!", "?")):
                candidate += "."
            if _valid_shortening(field, candidate):
                return replace(field, fallback=candidate)
    # The structured status is bounded independently of free-form recipient/action text. This last route
    # also handles an unmatched quote or bracket in a source identity without discarding the waiting facts.
    fallback = f"{status}." + (" Request details in email." if action else "")
    return replace(field, fallback=fallback)


def _waiting_source_url(w: Waiting) -> str | None:
    """Only expose an existing, usable HTTPS source link; never invent one from a completion reference."""
    if not w.link or any(char.isspace() or ord(char) < 32 for char in w.link):
        return None
    try:
        parsed = urlsplit(w.link)
        host = parsed.hostname or ""
        if parsed.scheme == "https" and host.strip(".") and not parsed.username and not parsed.password:
            parsed.port  # validate an explicit port; malformed ports raise ValueError
            return w.link
    except ValueError:
        pass
    return None


def _waiting_source(w: Waiting) -> str:
    source = _src(w.source or "Email")
    url = _waiting_source_url(w)
    return f'<a href="{_e(url)}">{source}</a>' if url else source


def _change_text(change: ChangeRow) -> TableText:
    kind = "What changed (recorded subject or title only)" if change.title_only else "What changed"
    return TableText(kind, change.text)


def table_text_fields(sections: list[Section], ndl_rows: list[TodayRow], changes: list[ChangeRow]):
    """Narrative fields from the same rows that appear in the v2 tables."""
    for section in sections:
        yield from map(_what_text, section.today)
        for _, move, situation in section.highlights:
            if situation:
                yield TableText("Situation", situation.text)
            yield _move_text(move)
        yield from map(_move_text, section.followups)
        yield from map(_waiting_text, section.waiting)
        yield from meeting_text_fields(section.meetings)
    yield from map(_what_text, ndl_rows)
    yield from map(_change_text, changes)


def render_section(s: Section, today: date, nico_gap: bool, action_url=None, *, display=read_table_text) -> str:
    done_th = _th("", "9%") if action_url else ""
    done_td = (lambda ref: _td(_done(ref, action_url))) if action_url else (lambda ref: "")
    out = [f'<p style="margin:24px 0 0;padding:6px 10px;background:#f2f4f7;border-left:3px solid {BLUE};'
           f'font:600 14px {FONT};color:{INK}">{_e(s.name)}</p>']
    out.append(_h("Needs you today", RED, len(s.today) + s.more_today))
    out.append(_table(_th("Deal", "20%") + _th("What") + _th("When", "13%") + _th("Source", "12%") + done_th,
                      ["<tr>" + _td(_deal_cell(r.deal, r.stage))
                       + _td(f"{_tag(r.tag)} {_e(display(_what_text(r)))}") + _td(_muted(_e(r.when))) + _td(_src(r.source))
                       + done_td(r.done) + "</tr>"
                       for r in s.today]) if s.today else _empty("Nothing due now or waiting on your reply."))

    out.append(_h("Deals to Move", PURPLE))
    if s.highlights:
        rows = []
        for v, m, sit in s.highlights:
            gap = " · Nico's inbox not connected" if nico_gap and v.owner == "NC" else ""
            sit_html = _e(display(TableText("Situation", sit.text))) if sit else _muted("—")
            rows.append("<tr>" + _td(_deal_cell(v.deal, v.stage), "width:16%")
                        + _td(sit_html, "width:30%")
                        + _td(f"{_tag(m.tag)} {_e(display(_move_text(m)))}<br>{_muted(_e(m.when + gap))}")
                        + _td(_src(m.source), "width:12%") + done_td(m.done) + "</tr>")
        out.append(_table(_th("Deal") + _th("Situation") + _th("Next move") + _th("Source") + done_th, rows))
    else:
        out.append(_empty("No active prospect or pilot needs attention."))
    if s.also_active:
        out.append(_left_out(more_line(len(s.also_active), "active in the Sheet", s.also_active)))

    if s.waiting:
        out.append(_h("Waiting on their reply", AMBER, len(s.waiting) + s.more_waiting))
        out.append(_table(_th("Deal", "20%") + _th(WAITING_TEXT_KIND) + _th("Waiting", "13%") + _th("Source", "12%") + done_th,
                          ["<tr>" + _td(_deal_cell(w.deal, w.stage)) + _td(_e(display(_waiting_text(w))))
                           + _td(_muted(_e(f"{w.days}d since {_date(w.sent)}"))) + _td(_waiting_source(w))
                           + done_td(f"thread:{w.thread_id}" if w.thread_id else None) + "</tr>" for w in s.waiting]))
        if s.more_waiting:
            out.append(_left_out(more_line(s.more_waiting, "in the Sheet waiting on a reply", s.left.get("waiting", []))))

    out.append(_h("Meetings today & tomorrow", BLUE, len(s.meetings) + s.more_meetings))
    out.append(meetings_table(s.meetings, display=display) if s.meetings else _empty("No customer meetings today or tomorrow."))
    if s.more_meetings:
        out.append(_left_out(more_line(s.more_meetings, "on your calendar", s.left.get("meetings", []))))

    if s.followups:
        out.append(_h("Follow-ups that may be needed", AMBER, len(s.followups) + s.more_followups))
        lines = "".join(f'<p style="margin:3px 0;font:13px/1.4 {FONT};color:{INK}">{_tag(m.tag)} {_e(display(_move_text(m)))} '
                        f'{_muted(_e(m.when + (" · " + m.source if m.source else "")))} {_done(m.done, action_url)}</p>'
                        for m in s.followups)
        more = (f'<p style="margin:3px 0;font:12px {FONT};color:{MUTED}">'
                f'{_e(more_line(s.more_followups, "on record", s.left.get("followups", [])))}</p>'
                if s.more_followups else "")
        out.append(lines + more)

    if s.resurfaced:
        out.append(_h(f"Quiet In Talks deals to revisit ({len(s.resurfaced)} of {s.quiet_in_talks})", MUTED))
        out.append("".join(f'<p style="margin:3px 0;font:13px/1.4 {FONT};color:{INK}"><b>{_e(v.deal)}</b> '
                           f'{_muted(_e(quiet_line(v, today)))}</p>' for v in s.resurfaced))
        out.append(_left_out(RESURFACE_NOTE))

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
        label = "older items on record" if s.verification is not None else "older open items"
        notes.append(f"<b>Backlog:</b> {b.count} {label}, {b.deals} deals{_e(top)}, since {_date(b.oldest)}. "
                     f"Kept on record; may be done outside our sources.")
        for i in b.exceptions:
            notes.append(f"&nbsp;&nbsp;• Promised to {_e(i.deal)}: {_e(i.text)} "
                         f"{_muted(_e(f'due {_date(i.due)}' + (' · ' + i.source if i.source else '')))}")
    if b.delegated:
        notes.append(f"{b.delegated} item(s) on your deals name someone else (delegated).")
    if s.hidden:
        notes.append(f"{s.hidden} item(s) hidden: later emails/notes suggest they were done.")
    notes += [(f"<b>{_e(label)}</b> " if label else "") + _e(text) for label, text in sheet_footers(s, today)]
    if notes:
        out.append("".join(f'<p style="margin:6px 0 0;font:12px/1.5 {FONT};color:{MUTED}">{n}</p>' for n in notes))
    return "".join(out) + verification_html(s.verification)


def sheet_footers(s: Section, today: date) -> list[tuple[str, str]]:
    """(label, text) for the lines about dated Sheet steps that are not rows today; the same in HTML and text."""
    out = []
    if s.sheet_stale:
        names = ", ".join(f"{deal} ({_long_date(due, today)})" for deal, due in s.sheet_stale[:MAX_NAMES])
        more = f" +{len(s.sheet_stale) - MAX_NAMES} more" if len(s.sheet_stale) > MAX_NAMES else ""
        out.append(("Sheet dates long past:", f"{names}{more} - update the Next step in the Sheet or move the deal "
                                              f"to Paused."))
    if s.sheet_later:
        deal, due = s.sheet_later[0]
        held = f" ({s.held} matching item(s) held back until then)" if s.held else ""
        out.append(("", f"{len(s.sheet_later)} dated Sheet step(s) not due yet{held}; next: {deal}, "
                        f"{_long_date(due, today)}. Each comes back in Needs you today on its date."))
    return out


RESURFACE_NOTE = ("A few each day, longest-untouched first. For each: email them, set a date in the Sheet's Next step, "
                  "or move the deal to Paused.")


def quiet_line(v: DealView, today: date) -> str:
    """What we know about a quiet deal: how long since the last recorded contact, and the Sheet's own next step."""
    last = (f"last contact {_date(v.last_contact)} ({elapsed((today - v.last_contact).days)} ago)" if v.last_contact
            else "no email, meeting or note on record")
    return last + (f" · Sheet next step: {first_sentence(v.sheet_next, 110)}" if v.sheet_next else "")


def _long_date(d: date, today: date) -> str:
    return _date(d) if d.year == today.year else f"{_date(d)}, {d.year}"


def change_label(c: ChangeRow) -> str:
    if c.kind == "late" and c.event:
        return f"from {_date(c.event)}, recorded late"
    return c.source


def completable(sections: list[Section], ndl_rows: list[TodayRow]) -> dict[str, tuple[str, str]]:
    """Every row that gets a "Mark as complete" button: its target -> (deal, what the row says)."""
    out: dict[str, tuple[str, str]] = {}
    for s in sections:
        rows = [(r.done, r.deal, r.text) for r in s.today]
        rows += [(m.done, v.deal, m.text) for v, m, _ in s.highlights]
        rows += [(m.done, m.text.split(":", 1)[0], m.text.split(":", 1)[-1].strip()) for m in s.followups]
        rows += [(f"thread:{w.thread_id}" if w.thread_id else None, w.deal, f"Waiting on their reply: {waiting_text(w)}")
                 for w in s.waiting]
        for ref, deal, text in rows:
            if ref:
                out.setdefault(ref, (deal, text))
    for r in ndl_rows:
        if r.done:
            out.setdefault(r.done, (r.deal, r.text))
    return out


def render(sections: list[Section], ndl_rows: list[TodayRow], changes: list[ChangeRow], today: date,
           active_deals: int, coverage: str, nico_gap: bool, backfill: str | None = None, action_url=None,
           ndl_review: VerificationNotes | None = None, *, display=read_table_text, more_changes: int = 0,
           left_changes=()) -> str:
    """action_url(ref) -> link for a row's "Mark as complete" button; None leaves the buttons out."""
    counts = " · ".join(f"{s.short}: {len(s.today) + s.more_today} today, {len(s.highlights)} deals" for s in sections)
    out = [_stage_css(), f'<div class="bianca-brief" style="max-width:760px;margin:0 auto;padding:8px 4px">',
           f'<p style="font:600 18px {FONT};margin:0 0 2px;color:{INK}">Pipeline brief · {_e(f"{today:%a}, {today:%b} {today.day}")}</p>',
           f'<p style="font:13px {FONT};margin:0;color:{MUTED}">{_e(counts)} · {active_deals} deals in the Sheet</p>']
    out += [render_section(s, today, nico_gap, action_url, display=display) for s in sections]
    if ndl_rows or (ndl_review and (ndl_review.waiting or ndl_review.unclear)):
        out.append(f'<p style="margin:24px 0 0;padding:6px 10px;background:#f2f4f7;border-left:3px solid {MUTED};'
                   f'font:600 14px {FONT};color:{INK}">Nico de León · urgent only</p>')
        if ndl_rows:
            out.append(_table(_th("Deal", "20%") + _th("What") + _th("When", "13%") + _th("Source", "12%")
                              + (_th("", "9%") if action_url else ""),
                              ["<tr>" + _td(_deal_cell(r.deal, r.stage)) + _td(f"{_tag(r.tag)} {_e(display(_what_text(r)))}")
                               + _td(_muted(_e(r.when))) + _td(_src(r.source))
                               + (_td(_done(r.done, action_url)) if action_url else "") + "</tr>" for r in ndl_rows]))
        out.append(verification_html(ndl_review))
    out.append(_h("What changed since the last brief", MUTED, len(changes) + more_changes))
    out.append(_table(_th("Deal", "20%") + _th("What changed (from the source)") + _th("Source", "12%"),
                      ["<tr>" + _td(f"<b>{_e(c.deal)}</b>")
                       + _td(f"{_e(display(_change_text(c)))} {_muted(_e(change_label(c)))}")
                       + _td(_src(f"{c.source} · {_date(c.event)}" if c.event else c.source)) + "</tr>" for c in changes])
                if changes else _empty("No notable changes."))
    if more_changes:
        out.append(_left_out(more_line(more_changes, "changes on record, not shown", left_changes)))
    if backfill:
        out.append(f'<p style="margin:6px 0 0;font:12px {FONT};color:{MUTED}">{_e(backfill)}</p>')
    out.append(f'<p style="font:11px {FONT};color:#98a2b3;margin-top:28px">{_e(coverage)} <b>We owe</b> = recorded '
               f"agreement · <b>AI recommendation</b> = suggested next move.</p></div>")
    return "".join(out)


def subject(sections: list[Section], today: date) -> str:
    bits = [f"{s.short} {len(s.today) + s.more_today} today, {len(s.highlights)} deals" for s in sections]
    return f"Bianca pipeline · {today:%a}, {today:%b} {today.day} · " + " · ".join(bits)


def plain_text(sections: list[Section], today: date, ndl_review: VerificationNotes | None = None, *,
               display=read_table_text) -> str:
    lines = [f"Pipeline brief · {today:%a}, {today:%b} {today.day}"]
    for s in sections:
        lines.append(f"\n{s.name.upper()}\nNeeds you today ({len(s.today) + s.more_today})")
        lines += [f"• [{r.tag}] {r.deal} — {display(_what_text(r))} ({r.when})" for r in s.today]
        lines.append("Deals to Move")
        lines += [f"• {v.deal} ({stage_short(v.stage)}): [{m.tag}] {display(_move_text(m))} ({m.when})"
                  for v, m, _ in s.highlights]
        if s.also_active:
            lines.append(more_line(len(s.also_active), "active in the Sheet", s.also_active))
        if s.waiting:
            lines.append(f"Waiting on their reply ({len(s.waiting) + s.more_waiting})")
            for w in s.waiting:
                lines.append(f"• {w.deal}: {display(_waiting_text(w))} ({w.days}d since {_date(w.sent)})")
                if url := _waiting_source_url(w):
                    lines.append(f"  Source: {w.source or 'Email'} — {url}")
            if s.more_waiting:
                lines.append(more_line(s.more_waiting, "in the Sheet waiting on a reply", s.left.get("waiting", [])))
        if s.meetings:
            lines.append("Meetings today & tomorrow")
            lines += [f"• {m.at:%a %H:%M} {title} — {prep}" for m, title, prep in meeting_text_rows(s.meetings, display)]
            if s.more_meetings:
                lines.append(more_line(s.more_meetings, "on your calendar", s.left.get("meetings", [])))
        if s.followups:
            lines.append(f"Follow-ups that may be needed ({len(s.followups) + s.more_followups})")
            lines += [f"• [{m.tag}] {display(_move_text(m))} ({m.when})" for m in s.followups]
            if s.more_followups:
                lines.append(more_line(s.more_followups, "on record", s.left.get("followups", [])))
        if s.resurfaced:
            lines.append(f"Quiet In Talks deals to revisit ({len(s.resurfaced)} of {s.quiet_in_talks})")
            lines += [f"• {v.deal} - {quiet_line(v, today)}" for v in s.resurfaced]
            lines.append(RESURFACE_NOTE)
        lines += [f"{label} {text}".strip() for label, text in sheet_footers(s, today)]
        lines.extend(verification_text(s.verification))
    if ndl_review and (ndl_review.waiting or ndl_review.unclear):
        lines += ["\nNICO DE LEÓN", *verification_text(ndl_review)]
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
        "today": [{"deal": r.deal, "tag": r.tag, "text": r.text, "when": r.when, "item_id": r.item_id,
                   "source": r.source} for r in s.today],
        "more_today": s.more_today, "more_meetings": s.more_meetings,
        "resurfaced": [{"deal": v.deal, "last_contact": v.last_contact} for v in s.resurfaced],
        "quiet_in_talks": s.quiet_in_talks,
        "sheet_later": [{"deal": deal, "due": due} for deal, due in s.sheet_later],
        "sheet_stale": [{"deal": deal, "due": due} for deal, due in s.sheet_stale],
        "highlights": [{"deal": v.deal, "stage": v.stage, "situation": sit.text if sit else None, "move": move(m)}
                       for v, m, sit in s.highlights],
        "selection_audit": [{"deal": r.deal, "stage": r.stage, "selected": r.selected, "position": r.position,
                             "time_critical": r.critical, "signals": r.signals, "reason": r.reason} for r in s.audit],
        "also_active": s.also_active,
        "customers": [],  # retain the response key for callers of v2
        "followups": [move(m) for m in s.followups], "more_followups": s.more_followups,
        "waiting": [{"deal": w.deal, "to": w.to, "sent": w.sent, "days": w.days, "nudges": w.nudges,
                     "replied_before": w.replied_before, "auto_reply": w.auto_reply, "thread_id": w.thread_id,
                     "action": w.action}
                    for w in s.waiting], "more_waiting": s.more_waiting,
        "unclear": [v.deal for v in s.unclear], "dormant": s.dormant,
        "backlog": {"count": s.backlog.count, "deals": s.backlog.deals, "oldest": s.backlog.oldest,
                    "exceptions": [item(i) for i in s.backlog.exceptions]},
        "meetings": [{"at": m.at.isoformat(), "title": m.title, "deal": m.deal, "prep": p} for m, p in s.meetings],
        "all_items": [item(i) for i in s.all_items],
        **({"verification": s.verification.sections()} if s.verification is not None else {}),
    } for s in sections], "ndl": [{"deal": r.deal, "text": r.text, "when": r.when} for r in ndl_rows],
        "changes": [{"deal": c.deal, "text": c.text, "kind": c.kind, "event": c.event} for c in changes]}


def recorded_on(ts: datetime | None, tz) -> date:
    return ts.astimezone(tz).date() if ts else date.min
