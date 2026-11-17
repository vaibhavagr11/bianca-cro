"""B-15 (11/12 founder session): a "(complete by <date>)" note in the Sheet's Next step is read as a due date."""

from datetime import date

import pytest

from app.brief_sheet_steps import first_bullet, match_key, parse_cell, parse_date, same_step
from app.pipeline import is_paused
from app.review_deadlines import normalize_deadline

TODAY = date(2026, 11, 17)


@pytest.mark.parametrize(("value", "expected"), [
    ("Nov 16, 2026", date(2026, 11, 16)),
    ("November 16, 2026", date(2026, 11, 16)),
    ("16 Nov 2026", date(2026, 11, 16)),
    ("16 de noviembre de 2026", date(2026, 11, 16)),
    ("1 dic 2026", date(2026, 12, 1)),
    ("2026-11-16", date(2026, 11, 16)),
    ("Feb 17, 2027", date(2027, 2, 17)),
    ("Jan 5", date(2027, 1, 5)),            # no year: the nearest one, as the Hub does
    ("Nov 16", date(2026, 11, 16)),
    ("25/11/2026", date(2026, 11, 25)),
    ("03/04/2026", None),                   # March or April: not guessed
    ("Nov 31, 2026", None),                 # the date Ramiro could not save on 11/12
    ("end of November", None),
    ("soon", None),
])
def test_dates_are_read_in_the_formats_founders_use(value, expected):
    assert parse_date(value, TODAY) == expected


def test_the_hubs_own_format_round_trips():
    bullet = normalize_deadline("Next step: Send the proposal by Nov 21", date(2026, 11, 17))
    assert bullet == "Next step: Send the proposal (complete by Nov 21, 2026)"
    [step] = parse_cell(f"- {bullet}", TODAY)
    assert (step.text, step.due) == ("Send the proposal", date(2026, 11, 21))


def test_cell_keeps_only_bullets_with_a_readable_date_and_the_founders_wording():
    cell = ("- We owe: Send the checklist. (owner: Nico) (complete by Nov 6, 2026)\n"
            "- Customer next step: KeHE access for Bianca (complete by 1 Dec 2026).\n"
            "• Email her Monday\n"
            "- Revisit in Q1 (complete by sometime)\n"
            "3) Call Franco (Complete By Feb 17, 2027)")
    steps = parse_cell(cell, TODAY)
    assert [(s.text, s.due) for s in steps] == [
        ("We owe: Send the checklist. (owner: Nico)", date(2026, 11, 6)),
        ("Customer next step: KeHE access for Bianca", date(2026, 12, 1)),
        ("Call Franco", date(2027, 2, 17)),
    ]
    assert parse_cell(None, TODAY) == [] and parse_cell("", TODAY) == []
    assert first_bullet(cell) == "We owe: Send the checklist. (owner: Nico) (complete by Nov 6, 2026)"


@pytest.mark.parametrize(("sheet", "extracted", "same"), [
    ("We owe: Send the checklist. (owner: Nico) (complete by Nov 6, 2026)", "Send the checklist.", True),
    ("Next step: Send the proposal (complete by Nov 21, 2026)", "Send the proposal by Nov 21", True),
    ("We owe: Send the pilot checklist. — due 2026-11-05", "Send the pilot checklist.", True),
    ("Send Patricio the whitespace-analysis overview and pricing (complete by Dec 1, 2026)",
     "Send Patricio the whitespace-analysis overview", True),      # a founder added two words in the Hub
    ("Email her Monday (complete by Nov 16, 2026)", "Send the signed agreement", False),
    ("Call Franco (complete by Nov 16, 2026)", "Call Ana", False),
])
def test_a_sheet_bullet_is_matched_to_the_extracted_item_it_came_from(sheet, extracted, same):
    assert same_step(match_key(sheet), match_key(extracted)) is same


@pytest.mark.parametrize(("stage", "paused"), [("0. Paused", True), (" paused ", True), ("0. On hold", True),
                                              ("2. In Talks", False), (None, False)])
def test_paused_status(stage, paused):
    assert is_paused(stage) is paused
