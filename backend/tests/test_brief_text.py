import json
from types import SimpleNamespace

import anthropic
import pytest

from app.brief_text import TABLE_TEXT_LIMIT, BriefTextReview, TableText, prepare_table_text, read_table_text


LONG_TEXT = "Ask the customer to confirm the meeting and provide the documents before Nov 5. " * 4
FALLBACK = "Review the original email before Nov 5."


class StubClaude:
    def __init__(self, rows=(), *, error=None):
        self.rows = rows
        self.error = error
        self.messages = self
        self.requests = []
        self.options = []

    def with_options(self, **options):
        self.options.append(options)
        return self

    def parse(self, **request):
        self.requests.append(request)
        if self.error:
            raise self.error
        response = request["output_format"](summaries=self.rows)
        return SimpleNamespace(parsed_output=response, stop_reason="end_turn")


@pytest.mark.parametrize("length", [TABLE_TEXT_LIMIT - 1, TABLE_TEXT_LIMIT])
def test_short_text_is_preserved_without_model_request(length):
    original = "É" * (length - 4) + " … &"
    field = TableText("What", original)
    client = StubClaude()
    display = prepare_table_text([field], client, "test-model")
    assert display(field) == original
    assert not client.requests and not client.options


def test_only_long_fields_are_sent_in_one_batch_and_duplicates_are_reused():
    long_field = TableText("What", LONG_TEXT, ("Nov 5",), FALLBACK)
    other = TableText("Situation", "The client is still validating the data. " * 5)
    short_field = TableText("What", "Confirm the call.")
    client = StubClaude([
        {"id": "2", "text": "The client is still validating the data."},
        {"id": "1", "text": "Confirm the meeting and ask for documents by Nov 5."},
    ])
    display = prepare_table_text(iter([long_field, short_field, other, long_field]), client, "test-model")
    assert len(client.requests) == 1
    assert client.options == [{"timeout": 10, "max_retries": 0}]
    data = [json.loads(line) for line in client.requests[0]["messages"][0]["content"].splitlines()]
    assert data == [
        {"id": "1", "kind": "What", "text": LONG_TEXT, "required": ["Nov 5"]},
        {"id": "2", "kind": "Situation", "text": other.text, "required": []},
    ]
    assert display(long_field) == "Confirm the meeting and ask for documents by Nov 5."
    assert display(other) == "The client is still validating the data."
    assert display(short_field) == short_field.text


@pytest.mark.parametrize("text", [
    " ",
    "Confirm the meeting before Nov 50.",
    "Confirm the meeting before Nov 5…",
    "Confirm the meeting before Nov 5...",
    "Confirm the meeting before Nov 5 and",
    "Confirm the meeting before Nov 5. " * 8,
    "Ask the customer to confirm the meeting and provide the documents before Nov 5",
])
def test_invalid_summaries_use_only_valid_explicit_fallback(text):
    field = TableText("What", LONG_TEXT, ("Nov 5",), FALLBACK)
    client = StubClaude([{"id": "1", "text": text}])
    display = prepare_table_text([field], client, "test-model")
    assert display(field) == FALLBACK


def test_160_character_summary_fits_and_161_does_not():
    field = TableText("Meeting title", "L" * (TABLE_TEXT_LIMIT + 1), fallback="See the meeting invitation.")
    for length in (TABLE_TEXT_LIMIT, TABLE_TEXT_LIMIT + 1):
        text = "Q" * (length - 1) + "."
        client = StubClaude([{"id": "1", "text": text}])
        display = prepare_table_text([field], client, "test-model")
        assert display(field) == (text if length == TABLE_TEXT_LIMIT else field.fallback)


@pytest.mark.parametrize("ids", [(), ("2",), ("1", "1"), ("1", "2")])
def test_missing_unknown_and_duplicate_ids_use_fallback(ids):
    field = TableText("What", LONG_TEXT, ("Nov 5",), FALLBACK)
    client = StubClaude([{"id": key, "text": "Confirm the meeting and ask for documents by Nov 5."} for key in ids])
    assert prepare_table_text([field], client, "test-model")(field) == FALLBACK


def test_service_error_uses_fallback_or_reports_only_the_field_type():
    field = TableText("What", LONG_TEXT, ("Nov 5",), FALLBACK)
    needs_review = TableText("Situation", LONG_TEXT)
    client = StubClaude(error=anthropic.AnthropicError("Provider failure"))
    display = prepare_table_text([field, needs_review], client, "test-model")
    assert display(field) == FALLBACK
    with pytest.raises(BriefTextReview) as exc:
        display(needs_review)
    assert exc.value.kind == "Situation"
    assert str(exc.value) == "Daily Brief table text needs review (Situation)."


def test_oversized_input_is_not_sent_or_sliced():
    field = TableText("Situation", LONG_TEXT * 100, fallback=FALLBACK)
    client = StubClaude()
    assert prepare_table_text([field], client, "test-model")(field) == FALLBACK
    assert not client.requests


@pytest.mark.parametrize("fallback", [
    None, " ", "See the email.", "See the email before Nov 5…", LONG_TEXT, LONG_TEXT[:TABLE_TEXT_LIMIT - 5],
])
def test_local_resolution_rejects_missing_or_invalid_fallback(fallback):
    field = TableText("What", LONG_TEXT, ("Nov 5",), fallback)
    with pytest.raises(BriefTextReview, match="What"):
        read_table_text(field)
