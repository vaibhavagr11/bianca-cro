"""Waiting descriptions must fit without clipping source prose or changing Done targets."""

import copy
import html
import json
from dataclasses import replace
from datetime import date
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import Mock

import anthropic
import pytest
from pydantic import SecretStr

from app import actions, brief_text, brief_v2
from app.brief_text import TABLE_TEXT_LIMIT, TableText, prepare_table_text, read_table_text
from app.config import settings


TODAY = date(2026, 11, 19)
REQUEST = ("Please share the final store list, including names and addresses for every location, "
           "so we can prepare the new pilot.")
LONG_REQUEST = ("Ana should confirm the plan by Friday after reviewing the detailed regional assumptions "
                "with procurement, finance, legal, and the local operations team. "
                "The date remains provisional until their review finishes.")


def waiting(**changes):
    row = brief_v2.Waiting("Example", "2. In Talks", "Ana", TODAY, 15, 1, False, False,
                           "https://mail.google.com/mail/u/0/#inbox/thread-123",
                           source="Email · Nov 19", thread_id="thread-123", owner="NC",
                           action=LONG_REQUEST, evidence=frozenset({"insight-123"}))
    return replace(row, **changes)


def section(row):
    return brief_v2.Section(
        code="NC", name="Nico Casaux", short="Nico", today=[], highlights=[], also_active=[], meetings=[],
        followups=[], followup_deals=[], more_followups=0, unclear=[], dormant=0,
        backlog=brief_v2.Backlog(), waiting=[row],
    )


def sentence(length):
    """A complete sentence with an exact length, including a long quoted filename."""
    prefix, suffix = 'Please send the file named "', '".'
    assert length > len(prefix + suffix)
    return prefix + "x" * (length - len(prefix + suffix)) + suffix


def model(rows=(), error=None):
    client = Mock()
    parse = client.with_options.return_value.messages.parse

    def respond(**request):
        if error:
            raise error
        return SimpleNamespace(stop_reason="end_turn", parsed_output=request["output_format"](summaries=rows))

    parse.side_effect = respond
    return client, parse


def assert_complete_fallback(text):
    assert 0 < len(text) <= TABLE_TEXT_LIMIT
    assert "…" not in text and "..." not in text
    assert text.endswith((".", "!", "?"))


def test_waiting_action_longer_than_95_is_not_preclipped():
    assert len(REQUEST) == 117
    row = waiting(action=REQUEST)
    original = copy.deepcopy(row)
    legacy_preview = brief_v2.waiting_text(row)
    field = brief_v2._waiting_text(row)
    client, parse = model()

    assert field.kind == "Who / Status"
    assert field.text == f"To Ana · no reply yet · {REQUEST}"
    assert len(field.text) <= TABLE_TEXT_LIMIT
    assert prepare_table_text([field], client, "test-model")(field) == field.text
    parse.assert_not_called()
    assert "…" in legacy_preview
    assert brief_v2.waiting_text(row) == legacy_preview
    assert row == original


@pytest.mark.parametrize("length", [94, 95, 96])
def test_action_at_the_old_cutoff_keeps_every_character(length):
    source = sentence(length)
    field = brief_v2._waiting_text(waiting(action=source))
    assert read_table_text(field) == f"To Ana · no reply yet · {source}"


@pytest.mark.parametrize("length", [159, 160, 161])
def test_the_limit_applies_to_the_whole_cell_including_separators(length):
    prefix = "To Ana · no reply yet · "
    source = sentence(length - len(prefix))
    field = brief_v2._waiting_text(waiting(action=source))
    client, parse = model()
    displayed = prepare_table_text([field], client, "test-model")(field)

    assert field.text == prefix + source and len(field.text) == length
    if length <= TABLE_TEXT_LIMIT:
        assert displayed == field.text
        parse.assert_not_called()
    else:
        assert_complete_fallback(displayed)
        parse.assert_called_once()
        request = json.loads(parse.call_args.kwargs["messages"][0]["content"])
        assert request["text"] == field.text


def test_long_waiting_action_reaches_model_intact_and_valid_rewrite_is_used():
    row = waiting()
    field = brief_v2._waiting_text(row)
    summary = f"To Ana · {field.required[0]} · Confirm the plan by Friday; the date remains provisional."
    assert len(summary) <= TABLE_TEXT_LIMIT
    client, parse = model([{"id": "1", "text": summary}])
    display = prepare_table_text([field], client, "test-model")
    displayed = display(field)

    assert displayed == summary
    request = json.loads(parse.call_args.kwargs["messages"][0]["content"])
    assert LONG_REQUEST in request["text"]
    assert "…" not in request["text"]
    assert row.action == LONG_REQUEST
    sections = [section(row)]
    rendered = brief_v2.render(sections, [], [], TODAY, 1, "Sources", False, display=display)
    assert summary in TableCells(rendered).cells
    assert summary in brief_v2.plain_text(sections, TODAY, display=display)


@pytest.mark.parametrize("length", [160, 161])
def test_model_output_is_checked_at_the_160_character_boundary(length):
    field = brief_v2._waiting_text(waiting())
    prefix = f"To Ana · {field.required[0]} · "
    summary = prefix + sentence(length - len(prefix))
    client, _ = model([{"id": "1", "text": summary}])
    shown = prepare_table_text([field], client, "test-model")(field)
    assert shown == (summary if length == TABLE_TEXT_LIMIT else read_table_text(field))
    assert_complete_fallback(shown)


@pytest.mark.parametrize("fallback", [None, "Partial request…", "Unclosed (request.", "Long " * 50])
def test_malformed_waiting_fallback_never_reaches_the_generic_cutter(fallback, monkeypatch):
    field = replace(brief_v2._waiting_text(waiting()), fallback=fallback)
    monkeypatch.setattr(brief_text, "cut_table_text", lambda _: pytest.fail("Waiting must never use clipping"))
    assert_complete_fallback(read_table_text(field))


@pytest.mark.parametrize("bad", [
    "To Ana · {status} · Confirm the plan by",
    "To Ana · {status} · Confirm the plan by.",
    "To Ana · {status} · Wait for.",
    "To Ana · {status} · Confirm the plan…",
    "To Ana · {status} · Confirm the plan...",
    'To Ana · {status} · Confirm the "plan.',
    "To Ana · {status} · Confirm the plan (by Friday.",
    "Ana already answered and approved the plan.",
    "To Ana · {status} · " + "Confirm the plan. " * 12,
])
def test_invalid_waiting_rewrites_use_complete_bounded_fallback(bad, monkeypatch):
    field = brief_v2._waiting_text(waiting())
    client, _ = model([{"id": "1", "text": bad.format(status=field.required[0])}])
    monkeypatch.setattr(brief_text, "cut_table_text", lambda _: pytest.fail("Waiting must never use clipping"))
    shown = prepare_table_text([field], client, "test-model")(field)
    assert shown == read_table_text(field)
    assert_complete_fallback(shown)


@pytest.mark.parametrize("mode", ["verified", "missing", "failure", "overflow", "oversized"])
def test_all_non_model_paths_avoid_sentence_clipping(mode, monkeypatch):
    row = waiting(action=LONG_REQUEST * (200 if mode == "oversized" else 1))
    field = brief_v2._waiting_text(row)
    client, parse = model(error=anthropic.AnthropicError("Unavailable") if mode == "failure" else None)
    fields = [field]
    if mode == "overflow":
        fields = [TableText("What", f"{i}: " + LONG_REQUEST) for i in range(brief_text.MAX_BATCH_FIELDS)] + fields
    monkeypatch.setattr(brief_text, "cut_table_text", lambda _: pytest.fail("Waiting must never use clipping"))
    shown = prepare_table_text(fields, None if mode == "verified" else client, "test-model")(field)
    assert_complete_fallback(shown)
    assert shown == read_table_text(field)
    assert "email" in shown.casefold()
    if mode in ("verified", "oversized"):
        parse.assert_not_called()


def test_fallback_keeps_a_complete_first_request_and_marks_remaining_context():
    request = "Please confirm Friday. " + LONG_REQUEST
    field = brief_v2._waiting_text(waiting(action=request))
    shown = read_table_text(field)
    assert "Please confirm Friday." in shown
    assert "More in email." in shown
    assert_complete_fallback(shown)


@pytest.mark.parametrize("replied,nudges,nudge,auto_reply,pause_after", [
    (False, 4, False, True, 5),
    (True, 3, True, True, 5),
    (True, 5, True, True, 5),
    (True, 8, True, False, None),
])
def test_fallback_keeps_reply_history_ooo_and_cadence_facts(replied, nudges, nudge, auto_reply, pause_after):
    row = waiting(replied_before=replied, nudges=nudges, nudge=nudge,
                  auto_reply=auto_reply, pause_after=pause_after)
    field = brief_v2._waiting_text(row)
    shown = read_table_text(field)
    assert_complete_fallback(shown)
    assert str(nudges) in shown
    if auto_reply:
        assert "out-of-office" in shown
    if nudge and pause_after is not None and nudges >= pause_after:
        assert "Paused" in shown and "follow up" not in shown
    elif nudge:
        assert "follow up this week" in shown
    elif replied:
        assert "last reply" in shown


@pytest.mark.parametrize("action", [None, "", LONG_REQUEST])
def test_very_long_identity_is_never_sliced(action):
    identity = "Alexandria " * 24 + "van Example"
    row = waiting(to=identity, action=action, auto_reply=True, nudges=4)
    before = copy.deepcopy(row)
    shown = read_table_text(brief_v2._waiting_text(row))
    assert_complete_fallback(shown)
    assert "Customer" in shown
    assert "Alexandria" not in shown
    assert "out-of-office" in shown and "4" in shown
    assert row == before


class TableCells(HTMLParser):
    def __init__(self, document):
        super().__init__(convert_charrefs=True)
        self.cells = []
        self.current = None
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        if tag == "td":
            self.current = []

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == "td" and self.current is not None:
            self.cells.append("".join(self.current))
            self.current = None


def test_rendered_wording_sources_payload_evidence_and_signed_done_reference(monkeypatch):
    row = waiting(to="Ana & José", action="Send R&D's <draft> by Friday.",
                  link="https://mail.google.com/mail/u/0/#inbox/thread-123?x=1&y=2")
    original = copy.deepcopy(row)
    sections = [section(row)]
    payload_before = brief_v2.to_payload(sections, [], [])
    labels_before = brief_v2.completable(sections, [])
    monkeypatch.setattr(settings, "brief_action_secret", SecretStr("synthetic-test-secret"))
    tokens = {}

    def action_url(ref):
        tokens[ref] = actions.sign(ref, *labels_before[ref])
        return f"https://example.test/done?t={tokens[ref]}"

    field = brief_v2._waiting_text(row)
    display = prepare_table_text([field], None, "test-model")
    shown = display(field)
    rendered = brief_v2.render(sections, [], [], TODAY, 1, "Sources", False,
                               action_url=action_url, display=display)
    plain = brief_v2.plain_text(sections, TODAY, display=display)

    assert shown in TableCells(rendered).cells
    assert len(shown) <= TABLE_TEXT_LIMIT and shown in plain
    assert "&lt;draft&gt;" in rendered and "R&amp;D" in rendered
    assert f'href="{html.escape(row.link, quote=True)}"' in rendered
    assert row.link in plain
    assert ">Who / Status</th>" in rendered
    assert brief_v2.to_payload(sections, [], []) == payload_before
    assert payload_before["sections"][0]["waiting"][0]["action"] == row.action
    assert brief_v2.completable(sections, []) == labels_before
    assert row == original
    assert set(tokens) == {"thread:thread-123"}
    decoded = actions.verify(tokens["thread:thread-123"])
    assert decoded["kind"] == "thread" and decoded["ref"] == "thread-123"
    assert decoded["text"] == labels_before["thread:thread-123"][1][:120]


def test_long_source_keeps_the_existing_completion_preview_contract(monkeypatch):
    row = waiting()
    sections = [section(row)]
    labels = brief_v2.completable(sections, [])
    original_preview = f"Waiting on their reply: {brief_v2.waiting_text(row)}"
    assert len(original_preview) > 120
    field = brief_v2._waiting_text(row)
    read_table_text(field)
    assert labels["thread:thread-123"] == (row.deal, original_preview)
    assert brief_v2.completable(sections, []) == labels
    monkeypatch.setattr(settings, "brief_action_secret", SecretStr("synthetic-test-secret"))
    decoded = actions.verify(actions.sign("thread:thread-123", *labels["thread:thread-123"]))
    assert decoded["ref"] == "thread-123" and decoded["kind"] == "thread"
    assert decoded["text"] == original_preview[:120]


@pytest.mark.parametrize("link", [
    None, "", "javascript:alert(1)", "http://example.test/email",
    "https://example.test:bad/email", "https://example.test:99999/email", "https://.",
])
def test_missing_or_unsafe_source_link_does_not_create_an_anchor(link):
    row = waiting(link=link)
    sections = [section(row)]
    rendered = brief_v2.render(sections, [], [], TODAY, 1, "Sources", False)
    plain = brief_v2.plain_text(sections, TODAY)
    shown = read_table_text(brief_v2._waiting_text(row))
    assert_complete_fallback(shown)
    assert "Email · Nov 19" in html.unescape(rendered)
    assert 'href="javascript:' not in rendered and 'href="http:' not in rendered
    if link:
        assert link not in plain
