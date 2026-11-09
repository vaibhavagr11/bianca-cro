import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import brief_verification as verification

START = datetime(2026, 10, 28, 13, tzinfo=timezone.utc)
CUSTOMER = frozenset({"buyer@customer.test"})
BIANCA = frozenset({"founder@bianca.test"})
ORIGIN = verification.Activity("origin", "gmail", "company", "original-thread", START,
                               "Pricing request", "Can you send pricing for the pilot?", CUSTOMER, BIANCA, "inbound")
ACTION = verification.VerificationAction("price", "commitment", "Send pricing for the pilot", "company", ORIGIN.id, START)


def activity(id, text, *, thread="different-thread", at=START + timedelta(hours=1), direction="outbound"):
    return verification.Activity(id, "gmail", "company", thread, at, "", text,
                                 BIANCA if direction == "outbound" else CUSTOMER,
                                 CUSTOMER if direction == "outbound" else BIANCA, direction)


def review(action, records, client=None, budget=50_000, origin=ORIGIN):
    prepared = verification.PreparedCheck((action,), {origin.id: origin}, {action.key: tuple(records)})
    return verification.verify(prepared, client, "fake-model", token_budget=budget)


class FakeClaude:
    def __init__(self, decide):
        self.messages = self
        self.decide = decide
        self.calls = []

    def parse(self, *, messages, **kwargs):
        payload = json.loads(messages[0]["content"])
        self.calls.append(payload)
        verdicts = self.decide(payload)
        return SimpleNamespace(parsed_output=verification.ModelDecisions(verdicts=verdicts),
                               usage=SimpleNamespace(input_tokens=350, output_tokens=50))


def model_verdict(state, record=None, quote=None):
    return {"action": "A1", "state": state, "record": record, "quote": quote}


def test_same_thread_acknowledgement_clears_reply_but_not_delivery():
    ack = activity("ack", "Thanks for the pricing request. I'll send it tomorrow.", thread=ORIGIN.thread_id)
    reply = replace(ACTION, kind="reply", text="Reply to the customer’s email")

    report = review(reply, [ack])
    assert report.verdicts[reply.key] == verification.Verdict("handled", "direct_same_thread_reply", ("ack",))
    assert report.budget_used == 0

    client = FakeClaude(lambda payload: [model_verdict("still_owed")])
    assert review(ACTION, [ack], client).verdicts[ACTION.key].state == "still_owed"


def test_same_thread_unrelated_outbound_does_not_clear_reply_by_rule():
    unrelated = activity("unrelated", "Sending the quarterly survey to all customers.", thread=ORIGIN.thread_id)
    reply = replace(ACTION, kind="reply", text="Reply to the customer's email")
    assert review(reply, [unrelated]).verdicts[reply.key].state == "unclear"


def test_cross_thread_completion_requires_a_specific_cited_passage():
    later = activity("followup", "Here is the pricing for your pilot: $500 per month.")
    client = FakeClaude(lambda payload: [model_verdict("handled", "R1", "pricing for your pilot: $500 per month.")])
    report = review(ACTION, [later], client)
    assert client.calls[0]["actions"][0]["eligible"] == ["R1"]
    assert report.verdicts[ACTION.key] == verification.Verdict("handled", "supported_by_later_activity", ("followup",), "model")
    assert (report.input_tokens, report.output_tokens, report.budget_used) == (350, 50, 400)


@pytest.mark.parametrize("direction,quote,at", [
    ("outbound", "Made up completion", START + timedelta(hours=1)),
    ("outbound", "Here is the pricing", START),
    ("calendar", "Here is the pricing", START + timedelta(hours=1)),
    ("automated", "Here is the pricing", START + timedelta(hours=1)),
])
def test_rejects_false_quotes_wrong_source_and_same_timestamp(direction, quote, at):
    later = activity("followup", "Here is the pricing for the pilot", at=at, direction=direction)
    client = FakeClaude(lambda payload: [model_verdict("handled", "R1", quote)])
    assert review(ACTION, [later], client).verdicts[ACTION.key].reason == "invalid_model_citation"


def test_missing_origin_and_budget_never_appear_as_open_without_review():
    pending = activity("maybe", "Would you like us to continue?")
    client = FakeClaude(lambda payload: pytest.fail("should not use the model"))
    assert review(replace(ACTION, origin_record_id=None), [], client).verdicts[ACTION.key].reason == "missing_or_ambiguous_origin"
    assert review(ACTION, [pending], client, budget=200).verdicts[ACTION.key].reason == "token_budget"
    assert review(ACTION, [], client).verdicts[ACTION.key].state == "still_owed"


def test_widens_cross_thread_search_when_first_pass_is_unclear():
    proof = activity("proof", "Our proposal is attached as promised.")
    other = [activity(f"mail-{i}", "I will review pilot pricing.", at=START + timedelta(hours=i)) for i in range(2, 6)]

    def decide(payload):
        for row in payload["records"]:
            if "Our proposal is attached" in row["text"]:
                return [model_verdict("handled", row["id"], "Our proposal is attached as promised.")]
        return [model_verdict("unclear")]

    client = FakeClaude(decide)
    report = review(ACTION, [proof, *other], client)
    assert len(client.calls) == 2
    assert report.verdicts[ACTION.key].evidence_ids == ("proof",)
    assert report.budget_used == 800


def test_truncated_proof_does_not_close_an_action():
    passage = "Pricing for your pilot is $500. "
    record = activity("proof", passage + "Unrelated context. " * 260)
    client = FakeClaude(lambda payload: [model_verdict("handled", "R1", passage.strip())])
    report = review(ACTION, [record], client)
    assert len(client.calls) == 2
    assert report.verdicts[ACTION.key].state == "unclear"


def test_unreviewed_newer_message_prevents_closure_on_old_evidence():
    proof = activity("proof", "Pilot pricing is $500 per month.", thread=ORIGIN.thread_id)
    other = [activity(f"mail-{i}", "Pricing for your pilot", at=START + timedelta(hours=i), thread=ORIGIN.thread_id)
             for i in range(2, 12)]

    def decide(payload):
        for row in payload["records"]:
            if "$500" in row["text"]:
                return [model_verdict("handled", row["id"], "Pilot pricing is $500 per month.")]
        return [model_verdict("still_owed")]

    client = FakeClaude(decide)
    assert review(ACTION, [proof, *other], client).verdicts[ACTION.key].state == "unclear"
    assert len(client.calls) == 2


def test_waiting_remains_waiting_if_newer_messages_are_about_something_else():
    sent = activity("sent", "Can you confirm pricing by Thursday?", thread=ORIGIN.thread_id)
    unrelated = activity("other", "Thanks for the webinar invite", direction="inbound", at=sent.occurred_at + timedelta(hours=1))
    action = replace(ACTION, kind="waiting", origin_record_id=sent.id, origin_at=sent.occurred_at)
    client = FakeClaude(lambda payload: [model_verdict("waiting")])
    assert review(action, [unrelated], client, origin=sent).verdicts[ACTION.key].state == "waiting"


def test_load_uses_original_cutoff_and_considers_exact_contacts_and_canonical_threads():
    def row(activity):
        return {"id": activity.id, "source": "gmail", "company_id": activity.company_id, "thread_id": activity.thread_id,
                "occurred_at": activity.occurred_at, "subject": activity.subject, "body": activity.body, "meta": {},
                "participants": {"from": [{"address": addr} for addr in activity.senders],
                                 "to": [{"address": addr} for addr in activity.recipients]}}

    same = replace(activity("reply", "Thanks for the pricing request", thread=ORIGIN.thread_id), company_id="mislinked")
    cross = replace(activity("sent", "Attached are the pilot rates"), company_id=None)
    auto = replace(activity("auto", "Away until Monday", direction="inbound"), subject="Out of Office")

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def execute(self, sql, params):
            if "internal_domains" in sql:
                return self
            if "and id = any" in sql:
                return [row(ORIGIN)]
            if "from contacts" in sql:
                return [{"email": "owner@customer.test"}]
            assert "workspace_id = %s" in sql and "occurred_at > %s" in sql and "lower(p->>'address')" in sql
            assert "source = 'gmail' and (thread_id = any" in sql
            assert params == ("workspace", START, START + timedelta(days=1), "company", [ORIGIN.thread_id],
                              ["buyer@customer.test", "owner@customer.test"])
            return [row(same), row(cross), row(auto)]

        def fetchone(self):
            return {"internal_domains": ["bianca.test"]}

    conn = SimpleNamespace(cursor=lambda **_: Cursor())
    prepared = verification.load(conn, "workspace", [ACTION], as_of=START + timedelta(days=1))
    assert [record.id for record in prepared.candidates[ACTION.key]] == ["reply", "sent"]


def test_an_email_to_another_contact_at_the_company_is_not_a_reply():
    reply = replace(ACTION, kind="reply", text="Reply to the customer's email (Pricing request)")
    colleague = replace(activity("invoice", "Attached is invoice INV-21 for October."),
                        recipients=frozenset({"accounts@customer.test"}))
    client = FakeClaude(lambda payload: [model_verdict("handled", "R1", "Attached is invoice INV-21 for October.")])
    assert review(reply, [colleague], client).verdicts[reply.key].reason == "invalid_model_citation"

    answer = activity("answer", "Pricing for the pilot is $500 per month.")   # to the person who asked
    client = FakeClaude(lambda payload: [model_verdict("handled", "R1", "Pricing for the pilot is $500 per month.")])
    assert review(reply, [answer], client).verdicts[reply.key].state == "handled"
