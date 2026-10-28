import pytest

from app.gmail import REDACTED, process, redact_secrets, skip_reason, strip_quoted

INTERNAL = {"bianca-ai.com"}
PROJECT = {"kellogg.northwestern.edu"}


def addr(*emails):
    return {"value": [{"address": e, "name": ""} for e in emails]}


def msg(id="m1", frm="buyer@acme.com", to=("ramiro.godfrid@bianca-ai.com",), cc=(), subject="Proposal",
        text="Hi Ramiro, can you send pricing?", labels=("INBOX",)):
    return {"id": id, "threadId": "t1", "labelIds": list(labels), "subject": subject,
            "date": "2026-10-21T18:57:35.000Z", "from": addr(frm), "to": addr(*to), "cc": addr(*cc), "text": text}


# --- skip_reason --------------------------------------------------------------

def test_external_thread_passes():
    assert skip_reason(msg(), INTERNAL, PROJECT, set()) is None


def test_bianca_internal_thread_is_kept_for_classifier():
    m = msg(frm="ronald.roland@bianca-ai.com")
    assert skip_reason(m, INTERNAL, PROJECT, set()) is None


def test_project_team_thread_is_skipped():
    m = msg(frm="eren.baysal@kellogg.northwestern.edu", cc=("nicolas.casaux@bianca-ai.com",))
    assert skip_reason(m, INTERNAL, PROJECT, set()) == "project_team"


def test_project_team_only_thread_is_skipped():
    m = msg(frm="eren.baysal@kellogg.northwestern.edu", to=("gibb.anella@kellogg.northwestern.edu",))
    assert skip_reason(m, INTERNAL, PROJECT, set()) == "project_team"


def test_one_external_participant_keeps_thread():
    m = msg(frm="ramiro.godfrid@bianca-ai.com", to=("eren.baysal@kellogg.northwestern.edu",), cc=("cfo@acme.com",))
    assert skip_reason(m, INTERNAL, PROJECT, set()) is None


def test_calendar_notification_is_skipped():
    m = msg(frm="gus@gmail.com", subject="Accepted: Gus <> Ramiro @ Thu Oct 22, 2026 12pm - 1pm (GMT-3)")
    assert skip_reason(m, INTERNAL, PROJECT, set()) == "automated"


def test_noreply_sender_is_skipped():
    assert skip_reason(msg(frm="no-reply@stripe.com"), INTERNAL, PROJECT, set()) == "automated"


def test_excluded_label_is_skipped():
    m = msg(labels=("INBOX", "Label_42"))
    assert skip_reason(m, INTERNAL, PROJECT, {"Label_42"}) == "excluded_label"


def test_promotions_label_is_skipped():
    assert skip_reason(msg(labels=("CATEGORY_PROMOTIONS",)), INTERNAL, PROJECT, set()) == "system_label"


def test_empty_body_is_skipped():
    assert skip_reason(msg(text="  "), INTERNAL, PROJECT, set()) == "empty"


# --- cleaning -----------------------------------------------------------------

def test_strip_quoted_gmail_wrapped_header():
    text = "Thanks, sending tomorrow.\n\nOn Tue, Oct 20, 2026 at 3:53 PM Eren <\neren@x.edu> wrote:\n\n> Hi Nico"
    assert strip_quoted(text) == "Thanks, sending tomorrow."


def test_strip_quoted_spanish():
    text = "Dale, lo vemos.\n\nEl mar, 20 oct 2026 a las 10:00, Juan <juan@x.com> escribió:\n> hola"
    assert strip_quoted(text) == "Dale, lo vemos."


def test_strip_quoted_outlook_separator():
    text = "See attached.\n\n________________________________\nFrom: Someone\nSent: Monday"
    assert strip_quoted(text) == "See attached."


def test_strip_quoted_keeps_text_without_quotes():
    assert strip_quoted("Just one line") == "Just one line"


def test_redacts_secret_sharing_links_and_keys():
    text = "key: https://password.link/AbCd1234/#FakeFragmentXyz and sk-abcdefghijklmnopqrstuv"
    out = redact_secrets(text)
    assert "password.link" not in out and "sk-abc" not in out
    assert out.count(REDACTED) == 2


@pytest.mark.parametrize(
    "text, leaked",
    [
        ("Join by phone passcode: 650806", "650806"),
        ("Passcode: yk9Ey2yM", "yk9Ey2yM"),
        ("Código de acceso: 12ab34", "12ab34"),
        ("https://zoom.us/j/123?pwd=FakePwd123abc&jst=2", "FakePwd123abc"),
        ("https://teams.microsoft.com/meet/123?p=FakeTeamsCode12", "FakeTeamsCode12"),
    ],
)
def test_redacts_meeting_passcodes(text, leaked):
    assert leaked not in redact_secrets(text)


def test_redaction_leaves_normal_text():
    assert redact_secrets("Let's meet Thursday at 4pm ART") == "Let's meet Thursday at 4pm ART"


# --- process ------------------------------------------------------------------

def test_process_counts_skips_and_dedupes():
    batch = [
        msg(id="a"),
        msg(id="a"),
        msg(id="b", frm="eren.baysal@kellogg.northwestern.edu"),
        msg(id="c", frm="no-reply@x.com"),
        {"labelIds": []},
    ]
    kept, skipped = process(batch, INTERNAL, PROJECT, set())
    assert [m.external_id for m in kept] == ["a"]
    assert skipped == {"duplicate_or_no_id": 2, "project_team": 1, "automated": 1}


def test_process_output_is_clean_and_hash_stable():
    [m1], _ = process([msg(text="Price list?\n\nOn Mon, X <x@y.com> wrote:\n> old")], INTERNAL, PROJECT, set())
    [m2], _ = process([msg(text="Price list?\n\nOn Mon, X <x@y.com> wrote:\n> old")], INTERNAL, PROJECT, set())
    assert m1.body == "Price list?"
    assert m1.participants["from"] == [{"address": "buyer@acme.com", "name": ""}]
    assert m1.content_hash == m2.content_hash


# --- bank-detail redaction -----------------------------------------------------



@pytest.mark.parametrize(
    "text, leaked",
    [
        ("CBU: 0170099220000067797370", "0170099220000067797370"),
        ("Alias: mi.cuenta.pagos", "mi.cuenta.pagos"),
        ("Cuenta corriente 123-456789/0", "456789"),
        ("Número de cuenta: 4021-2345-01", "2345"),
        ("IBAN: DE89 3704 0044 0532 0130 00", "3704"),
        ("SWIFT: BOFAUS3NXXX", "BOFAUS3NXXX"),
        ("Routing number 021000021 and account number 000123456789", "021000021"),
        ("Call me at +54 911 3181-7980", "3181"),
    ],
)
def test_redacts_bank_details(text, leaked):
    out = redact_secrets(text)
    assert leaked not in out and REDACTED in out


@pytest.mark.parametrize(
    "text",
    [
        "We received payment for both invoices.",
        "Pilot starts on 2026-10-22 at 4pm, budget $50,000 for 3 months.",
        "Order of 1,200 units across 35 stores.",
    ],
)
def test_bank_redaction_keeps_business_text(text):
    assert redact_secrets(text) == text


# --- multiple inboxes -------------------------------------------------------

from app.gmail import clean, normalize_message_id  # noqa: E402


def test_message_id_is_normalized_and_kept():
    assert normalize_message_id(" <CAF+X@Mail.Gmail.com> ") == "caf+x@mail.gmail.com"
    assert normalize_message_id(None) is None and normalize_message_id("<>") is None
    m = clean(msg(id="g1") | {"messageId": "<ABC@x.com>", "threadId": "t1"})
    assert m.message_id == "abc@x.com"


def test_message_id_does_not_change_the_content_hash():  # existing records stay "unchanged" on re-send
    base = msg(id="g1")
    assert clean(base).content_hash == clean(base | {"messageId": "<a@b>"}).content_hash


def test_gmail_payload_defaults_to_ramiros_inbox():
    from app.ingest import GmailPayload
    assert GmailPayload(messages=[]).mailbox == "RG"
    assert GmailPayload(messages=[], mailbox="NC").mailbox == "NC"
