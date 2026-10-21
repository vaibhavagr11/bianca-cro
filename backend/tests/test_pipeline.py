import pytest

from app.pipeline import normalize_name, parse_stage


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("FEMSA Arg", "femsa arg"),
        ("  FEMSA   Arg ", "femsa arg"),
        ("Nestlé", "nestle"),
        ("BUHL (Prime, etc)", "buhl (prime, etc)"),
        ("Kimberly-Clark Argentina", "kimberly-clark argentina"),
    ],
)
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("0. Paused", 0),
        ("2. In Talks", 2),
        ("5. Working client", 5),
        ("3) Proposal", 3),
        (" 4. Pilot", 4),
        ("Pilot", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_stage(raw, expected):
    assert parse_stage(raw) == expected
