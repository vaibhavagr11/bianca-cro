import json
from types import SimpleNamespace

import anthropic
import pytest

from app.brief_text import TABLE_TEXT_LIMIT, TableText, prepare_table_text, read_table_text


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


def test_service_error_uses_fallback_or_a_word_boundary_cut_so_the_brief_still_sends():
    field = TableText("What", LONG_TEXT, ("Nov 5",), FALLBACK)
    no_fallback = TableText("Situation", LONG_TEXT)
    client = StubClaude(error=anthropic.AnthropicError("Provider failure"))
    display = prepare_table_text([field, no_fallback], client, "test-model")
    assert display(field) == FALLBACK
    cut = display(no_fallback)
    assert len(cut) <= TABLE_TEXT_LIMIT and LONG_TEXT.startswith(cut.rstrip("…").split(". ")[0])


def test_oversized_input_is_not_sent_or_sliced():
    field = TableText("Situation", LONG_TEXT * 100, fallback=FALLBACK)
    client = StubClaude()
    assert prepare_table_text([field], client, "test-model")(field) == FALLBACK
    assert not client.requests


@pytest.mark.parametrize("fallback", [
    None, " ", "See the email.", "See the email before Nov 5…", LONG_TEXT, LONG_TEXT[:TABLE_TEXT_LIMIT - 5],
])
def test_local_resolution_ignores_an_invalid_fallback_and_cuts_keeping_required_values(fallback):
    field = TableText("What", LONG_TEXT, ("Nov 5",), fallback)
    shown = read_table_text(field)
    assert shown != fallback and len(shown) <= TABLE_TEXT_LIMIT and "Nov 5" in shown


# ---- finding 13 (11/09 evals): "CCU Chile is in '2." - a stage number is not the end of a sentence

STAGE_SITUATION = ("CCU Chile is in '2. In Talks' after two calls with the commercial team, who asked for a proposal "
                   "covering the three northern regions and a pilot timeline before the quarter ends. Next call pending.")


@pytest.mark.parametrize(("text", "expected"), [
    ("CCU Chile is in '2. In Talks' with no project yet. Next call pending.",
     ["CCU Chile is in '2. In Talks' with no project yet.", "Next call pending."]),
    ("Altavista Wines is at stage 1. Opening. Nico sent an invite.",
     ["Altavista Wines is at stage 1. Opening.", "Nico sent an invite."]),
    ("We met in 2025. Next step is a demo.", ["We met in 2025.", "Next step is a demo."]),
    ("Sales grew 3.5 percent, e.g. in the north. J. Smith at bianca-ai.com agreed. Done!",
     ["Sales grew 3.5 percent, e.g. in the north.", "J. Smith at bianca-ai.com agreed.", "Done!"]),
    ('He said "We are done. Call me." Then he left.', ['He said "We are done. Call me."', "Then he left."]),
    ("The buyer's team is ready. Next.", ["The buyer's team is ready.", "Next."]),
    ("Sr. Pérez pidió la propuesta. Falta el precio.", ["Sr. Pérez pidió la propuesta.", "Falta el precio."]),
])
def test_sentences_do_not_end_at_stage_numbers_decimals_abbreviations_initials_or_inside_quotes(text, expected):
    from app.brief_text import split_sentences
    assert split_sentences(text) == expected


def test_long_situation_is_never_cut_at_the_stage_number_mid_quote_or_with_an_ellipsis():
    assert len(STAGE_SITUATION) > TABLE_TEXT_LIMIT
    shown = read_table_text(TableText("Situation", STAGE_SITUATION))
    assert shown == "CCU Chile is in '2. In Talks' after two calls with the commercial team."
    no_breaks = "The buyer said the team is still validating store data against its own exports " * 3
    cut = read_table_text(TableText("Situation", no_breaks))
    assert len(cut) <= TABLE_TEXT_LIMIT and cut.endswith("(cut short)") and "…" not in cut
    quoted = ("The deal is in '2. In Talks' and the buyer wrote 'we cannot commit to any of the dates you proposed for "
              "the regional rollout until finance signs off on next year's budget and the new head of sales starts'.")
    cut = read_table_text(TableText("Situation", quoted))
    assert len(cut) <= TABLE_TEXT_LIMIT and "…" not in cut and not cut.endswith("'2.")
    assert cut.count("'") % 2 == 0     # the quote that was opened is closed or left out


@pytest.mark.parametrize("rewrite", ["CCU Chile is in '2.", "CCU Chile is in '2. In Talks' after two calls."])
def test_model_rewrite_that_stops_at_the_stage_number_or_mid_sentence_is_rejected(rewrite):
    field = TableText("Situation", STAGE_SITUATION)
    display = prepare_table_text([field], StubClaude([{"id": "1", "text": rewrite}]), "test-model")
    assert display(field) == "CCU Chile is in '2. In Talks' after two calls with the commercial team."
