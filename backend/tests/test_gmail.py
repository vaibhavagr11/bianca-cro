from app.gmail import REDACTED, process, redact_secrets, skip_reason, strip_quoted

INTERNAL = {"bianca-ai.com", "kellogg.northwestern.edu"}


def addr(*emails):
    return {"value": [{"address": e, "name": ""} for e in emails]}


def msg(id="m1", frm="buyer@acme.com", to=("ramiro.godfrid@bianca-ai.com",), cc=(), subject="Proposal",
        text="Hi Ramiro, can you send pricing?", labels=("INBOX",)):
    return {"id": id, "threadId": "t1", "labelIds": list(labels), "subject": subject,
            "date": "2026-10-21T18:57:35.000Z", "from": addr(frm), "to": addr(*to), "cc": addr(*cc), "text": text}


# --- skip_reason --------------------------------------------------------------

def test_external_thread_passes():
    assert skip_reason(msg(), INTERNAL, set()) is None


def test_internal_only_is_skipped():
    m = msg(frm="ronald.roland@bianca-ai.com")
    assert skip_reason(m, INTERNAL, set()) == "internal_only"


def test_kellogg_team_counts_as_internal():
    m = msg(frm="eren.baysal@kellogg.northwestern.edu", cc=("nicolas.casaux@bianca-ai.com",))
    assert skip_reason(m, INTERNAL, set()) == "internal_only"


def test_one_external_participant_keeps_thread():
    m = msg(frm="ramiro.godfrid@bianca-ai.com", to=("eren.baysal@kellogg.northwestern.edu",), cc=("cfo@acme.com",))
    assert skip_reason(m, INTERNAL, set()) is None


def test_calendar_notification_is_skipped():
    m = msg(frm="gus@gmail.com", subject="Accepted: Gus <> Ramiro @ Thu Oct 22, 2026 12pm - 1pm (GMT-3)")
    assert skip_reason(m, INTERNAL, set()) == "automated"


def test_noreply_sender_is_skipped():
    assert skip_reason(msg(frm="no-reply@stripe.com"), INTERNAL, set()) == "automated"


def test_excluded_label_is_skipped():
    m = msg(labels=("INBOX", "Label_42"))
    assert skip_reason(m, INTERNAL, {"Label_42"}) == "excluded_label"


def test_promotions_label_is_skipped():
    assert skip_reason(msg(labels=("CATEGORY_PROMOTIONS",)), INTERNAL, set()) == "system_label"


def test_empty_body_is_skipped():
    assert skip_reason(msg(text="  "), INTERNAL, set()) == "empty"


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
    text = "key: https://password.link/mef2pZU8/#UnlrVFBh and sk-abcdefghijklmnopqrstuv"
    out = redact_secrets(text)
    assert "password.link" not in out and "sk-abc" not in out
    assert out.count(REDACTED) == 2


def test_redaction_leaves_normal_text():
    assert redact_secrets("Let's meet Thursday at 4pm ART") == "Let's meet Thursday at 4pm ART"


# --- process ------------------------------------------------------------------

def test_process_counts_skips_and_dedupes():
    batch = [
        msg(id="a"),
        msg(id="a"),
        msg(id="b", frm="ronald.roland@bianca-ai.com"),
        msg(id="c", frm="no-reply@x.com"),
        {"labelIds": []},
    ]
    kept, skipped = process(batch, INTERNAL, set())
    assert [m.external_id for m in kept] == ["a"]
    assert skipped == {"duplicate_or_no_id": 2, "internal_only": 1, "automated": 1}


def test_process_output_is_clean_and_hash_stable():
    [m1], _ = process([msg(text="Price list?\n\nOn Mon, X <x@y.com> wrote:\n> old")], INTERNAL, set())
    [m2], _ = process([msg(text="Price list?\n\nOn Mon, X <x@y.com> wrote:\n> old")], INTERNAL, set())
    assert m1.body == "Price list?"
    assert m1.participants["from"] == [{"address": "buyer@acme.com", "name": ""}]
    assert m1.content_hash == m2.content_hash
