"""Deadline extraction and display formatting for pending War Room suggestions."""

from datetime import date

import pytest

from app.review_deadlines import normalize_deadline


REFERENCE = date(2026, 11, 15)
SUFFIX = " (complete by Nov 21, 2026)"


@pytest.mark.parametrize("timing", [
    "by Nov 21",
    "at November 21st",
    "on Nov 21, 2026",
    "around Nov 21",
    "by 2026-11-21",
    "before Nov 21",
])
def test_explicit_action_dates_move_to_the_end(timing):
    assert normalize_deadline(f"Next step: Send the proposal {timing}.", REFERENCE) == (
        f"Next step: Send the proposal.{SUFFIX}"
    )


def test_inline_follow_up_deadline_leaves_the_reason_in_the_body():
    source = "Next step: The coordinator should following up with Morgan by Nov 21, since the team is waiting for a response."
    assert normalize_deadline(source, REFERENCE) == (
        "Next step: The coordinator should keep following up with Morgan, since the team is waiting for a response."
        + SUFFIX
    )


@pytest.mark.parametrize("tail", [
    " — requested by 2026-11-21",
    " — due 2026-11-21",
    " (complete around November 21, 2026)",
    SUFFIX,
])
def test_existing_due_date_formats_are_canonicalized(tail):
    assert normalize_deadline(f"Next step: Send the proposal.{tail}") == (
        f"Next step: Send the proposal.{SUFFIX}"
    )


@pytest.mark.parametrize("source", [
    "Next step: Send the proposal. Deadline: Nov 21.",
    "Next step: Send the proposal. The deadline is Nov 21.",
    "Next step: Send the proposal. Due date: Nov 21.",
    "Next step: Send the proposal (deadline: Nov 21).",
])
def test_deadline_labels_are_removed_from_the_body(source):
    assert normalize_deadline(source, REFERENCE) == f"Next step: Send the proposal.{SUFFIX}"


def test_weekday_and_calendar_date_format_as_one_deadline():
    source = "Next step: Send the proposal by Friday, Nov 20."
    assert normalize_deadline(source, REFERENCE, allow_relative=False) == (
        "Next step: Send the proposal. (complete by Nov 20, 2026)"
    )


def test_explicit_deadline_preserves_the_date_in_a_background_clause():
    source = "Next step: Send the proposal before Nov 21, since they emailed us last Friday."
    assert normalize_deadline(source, REFERENCE) == (
        "Next step: Send the proposal, since they emailed us last Friday." + SUFFIX
    )


@pytest.mark.parametrize(("timing", "expected_date"), [
    ("next Friday", "Nov 20, 2026"),
    ("by next Friday", "Nov 20, 2026"),
    ("this Friday", "Nov 20, 2026"),
    ("on or around next Friday", "Nov 20, 2026"),
    ("tomorrow", "Nov 16, 2026"),
    ("by the day after tomorrow", "Nov 17, 2026"),
    ("in 2 weeks", "Nov 29, 2026"),
    ("by end of month", "Nov 30, 2026"),
])
def test_relative_action_deadlines_use_the_reference_date(timing, expected_date):
    source = f"Next step: Send the proposal {timing}."
    assert normalize_deadline(source, REFERENCE) == (
        f"Next step: Send the proposal. (complete by {expected_date})"
    )


def test_next_weekday_is_strictly_after_the_reference_date():
    friday = date(2026, 11, 20)
    assert normalize_deadline("Next step: Send the proposal by next Friday.", friday) == (
        "Next step: Send the proposal. (complete by Nov 27, 2026)"
    )


def test_relative_deadline_preserves_the_time_of_day():
    assert normalize_deadline("Next step: Send the proposal tomorrow at noon.", REFERENCE) == (
        "Next step: Send the proposal at noon. (complete by Nov 16, 2026)"
    )


def test_relative_deadline_and_generated_suffix_resolve_to_one_date():
    source = "Next step: Send the proposal by tomorrow. — requested by 2026-11-16"
    expected = "Next step: Send the proposal. (complete by Nov 16, 2026)"
    assert normalize_deadline(source, REFERENCE) == expected
    assert normalize_deadline(expected, REFERENCE) == expected


def test_repeated_deadline_becomes_one_suffix():
    source = "Next step: Send the proposal by Nov 21. — requested by 2026-11-21"
    expected = f"Next step: Send the proposal.{SUFFIX}"
    assert normalize_deadline(source, REFERENCE) == expected
    assert normalize_deadline(expected, REFERENCE) == expected


def test_yearless_deadline_uses_source_date_across_the_year_boundary():
    assert normalize_deadline("Next step: Send the proposal by Jan 5.", date(2026, 12, 30)) == (
        "Next step: Send the proposal. (complete by Jan 5, 2027)"
    )


@pytest.mark.parametrize("bullet,reference", [
    ("Next step: Send the proposal by Nov 31.", REFERENCE),
    ("Next step: Send the proposal by Nov 21 or Nov 25.", REFERENCE),
    ("Next step: Send the proposal by Friday, Nov 21, 2026.", REFERENCE),
    ("Next step: Send the proposal by Nov 21.", None),
    ("Next step: Send the proposal, since the customer requested it on Nov 21.", REFERENCE),
    ("Next step: Send the proposal by ſept 21.", REFERENCE),
    ("Next step: Send the proposal next Friday.", None),
    ("Next step: Send the proposal by Friday or Monday.", REFERENCE),
    ("Next step: Send the proposal sometime next week.", REFERENCE),
    ("Next step: Send the proposal within 2–3 days.", REFERENCE),
    ("Next step: Follow up since they emailed us last Friday.", REFERENCE),
    ("Next step: Follow up on Friday's feedback.", REFERENCE),
    ("Next step: The customer sent the specs Friday.", REFERENCE),
    ("Next step: Review the proposal sent before Nov 21.", REFERENCE),
])
def test_ambiguous_invalid_and_background_dates_stay_in_the_text(bullet, reference):
    assert normalize_deadline(bullet, reference) == bullet
