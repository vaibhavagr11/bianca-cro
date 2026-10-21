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


# --- parse_rows ---------------------------------------------------------------

from app.pipeline import (  # noqa: E402
    ExistingDeal,
    PipelineValidationError,
    SyncRefused,
    parse_rows,
    plan_sync,
)


def sheet_row(company, stage="1. Opening", owner="NC", next_step=None, **extra):
    row = {"Empresa": company, "Owner": owner, "Status": stage, "Country": "ARG", "Primary buyer": None,
           "Nombre contacto (Rol)": None, "Last progress": None, "Next step": next_step}
    row.update(extra)
    return row


def test_parse_rows_maps_columns_and_ignores_unknown():
    [row] = parse_rows([sheet_row("Acme Foods", stage="2. In Talks", next_step="Send proposal", Extra="x")])
    assert row.company == "Acme Foods"
    assert row.normalized_name == "acme foods"
    assert row.fields["stage"] == "2. In Talks"
    assert row.fields["next_step"] == "Send proposal"
    assert row.fields["owner_code"] == "NC"


def test_parse_rows_trims_and_blanks_become_none():
    [row] = parse_rows([sheet_row("  Acme  ", next_step="   ")])
    assert row.company == "Acme"
    assert row.fields["next_step"] is None


def test_parse_rows_skips_blank_company():
    rows = parse_rows([sheet_row("Acme"), sheet_row(""), sheet_row(None)])
    assert [r.company for r in rows] == ["Acme"]


def test_parse_rows_rejects_duplicates():
    with pytest.raises(PipelineValidationError, match="duplicate companies: acme"):
        parse_rows([sheet_row("Acme"), sheet_row("ACME ")])


def test_parse_rows_rejects_missing_company_column():
    with pytest.raises(PipelineValidationError, match="Empresa"):
        parse_rows([{"Company": "Acme"}])


def test_parse_rows_rejects_empty_payload():
    with pytest.raises(PipelineValidationError):
        parse_rows([])


def test_row_hash_changes_only_when_tracked_field_changes():
    a = parse_rows([sheet_row("Acme", stage="1. Opening")])[0]
    b = parse_rows([sheet_row("Acme", stage="1. Opening", Extra="ignored")])[0]
    c = parse_rows([sheet_row("Acme", stage="2. In Talks")])[0]
    assert a.row_hash == b.row_hash
    assert a.row_hash != c.row_hash


# --- plan_sync ----------------------------------------------------------------

def existing_from(row, opp_id, removed=False):
    return ExistingDeal(opp_id, row.normalized_name, dict(row.fields), row.row_hash, removed)


def test_plan_sync_first_run_creates_everything():
    rows = parse_rows([sheet_row("Acme"), sheet_row("Globex")])
    plan = plan_sync([], rows)
    assert [r.company for r in plan.creates] == ["Acme", "Globex"]
    assert not plan.updates and not plan.unchanged and not plan.removals


def test_plan_sync_unchanged_row_is_skipped():
    row = parse_rows([sheet_row("Acme")])[0]
    plan = plan_sync([existing_from(row, "o1")], [row])
    assert plan.unchanged == ["o1"]
    assert not plan.creates and not plan.updates


def test_plan_sync_logs_each_changed_field():
    old = parse_rows([sheet_row("Acme", stage="2. In Talks", next_step="Call")])[0]
    new = parse_rows([sheet_row("Acme", stage="3. Proposal", next_step="Send contract")])[0]
    plan = plan_sync([existing_from(old, "o1")], [new])
    [(opp_id, _, changes, restored)] = plan.updates
    assert opp_id == "o1"
    assert not restored
    assert changes == [("stage", "2. In Talks", "3. Proposal"), ("next_step", "Call", "Send contract")]


def test_plan_sync_marks_missing_rows_removed():
    rows = parse_rows([sheet_row(n) for n in ["A", "B", "C"]])
    existing = [existing_from(r, f"o{i}") for i, r in enumerate(rows)]
    plan = plan_sync(existing, rows[:2])
    assert [d.opportunity_id for d in plan.removals] == ["o2"]


def test_plan_sync_already_removed_and_still_missing_is_ignored():
    rows = parse_rows([sheet_row(n) for n in ["A", "B"]])
    existing = [existing_from(rows[0], "o0"), existing_from(rows[1], "o1", removed=True)]
    plan = plan_sync(existing, rows[:1])
    assert plan.removals == []
    assert plan.unchanged == ["o0"]


def test_plan_sync_restores_returning_row():
    row = parse_rows([sheet_row("Acme")])[0]
    plan = plan_sync([existing_from(row, "o1", removed=True)], [row])
    [(opp_id, _, changes, restored)] = plan.updates
    assert opp_id == "o1" and restored and changes == []


def test_plan_sync_refuses_mass_removal():
    rows = parse_rows([sheet_row(n) for n in ["A", "B", "C", "D"]])
    existing = [existing_from(r, f"o{i}") for i, r in enumerate(rows)]
    with pytest.raises(SyncRefused, match="3 of 4"):
        plan_sync(existing, rows[:1])


def test_plan_sync_allows_removing_exactly_half():
    rows = parse_rows([sheet_row(n) for n in ["A", "B", "C", "D"]])
    existing = [existing_from(r, f"o{i}") for i, r in enumerate(rows)]
    assert len(plan_sync(existing, rows[:2]).removals) == 2
