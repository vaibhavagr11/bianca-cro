from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import pytest

from app import sales_history
from test_sales_connector_permissions import TEST_DATABASE_URL, sales_database  # noqa: F401

NOW = datetime(2026, 11, 11, 15, tzinfo=UTC)
requires_database = pytest.mark.skipif(not TEST_DATABASE_URL, reason="requires a migrated local test database")


@pytest.fixture
def history_case(sales_database):
    login, _, password, workspaces = sales_database
    workspace = workspaces["bound"]
    with psycopg.connect(TEST_DATABASE_URL) as admin:
        admin.execute("update public.workspaces set internal_domains = array['bianca.test'] where id = %s", (workspace,))
    with psycopg.connect(TEST_DATABASE_URL, user=login, password=password, row_factory=dict_row) as reader:
        yield reader, workspace


def _deal(admin, workspace, name, stage):
    company, = admin.execute(
        "insert into public.companies (workspace_id, name, normalized_name) values (%s, %s, %s) returning id",
        (workspace, name, name.lower()),
    ).fetchone()
    opportunity, = admin.execute(
        "insert into public.opportunities (workspace_id, company_id, stage, stage_rank, owner_code, row_hash)"
        " values (%s, %s, %s, %s, 'RG', 'history-test') returning id",
        (workspace, company, f"{stage}. Fixture", stage),
    ).fetchone()
    admin.execute(
        "insert into public.contacts (workspace_id, company_id, name, email) values (%s, %s, 'Ana Buyer', %s)",
        (workspace, company, f"ana@{company}.test"),
    )
    return company, opportunity


def _run(admin, workspace, at, *, source="pipeline", attested=False):
    return admin.execute(
        "insert into public.sync_runs (workspace_id, source, status, started_at, finished_at, coverage_complete)"
        " values (%s, %s, 'succeeded', %s, %s, %s) returning id", (workspace, source, at, at, attested),
    ).fetchone()[0]


def _change(admin, workspace, opportunity, sync, at, field, old, new):
    admin.execute(
        "insert into public.opportunity_changes (workspace_id, opportunity_id, sync_run_id, field, old_value, new_value,"
        " detected_at) values (%s, %s, %s, %s, %s, %s, %s)", (workspace, opportunity, sync, field, old, new, at),
    )


def _mail(admin, workspace, company, at, *, subject="Buyer reply", body="Direct evidence"):
    key = uuid4().hex
    return admin.execute(
        "insert into public.source_records (workspace_id, company_id, source, external_id, thread_id, occurred_at,"
        " subject, participants, meta, body, content_hash, status, classification)"
        " values (%s, %s, 'gmail', %s, %s, %s, %s, %s, %s, %s, 'fixture', 'classified', %s) returning id",
        (workspace, company, key, key, at, subject,
         Jsonb({"from": [{"address": f"ana@{company}.test"}], "to": [{"address": "founder@bianca.test"}]}),
         Jsonb({"mailboxes": ["RG"]}), body, Jsonb({"is_commercial": True, "category": "prospect"})),
    ).fetchone()[0]


def _blocker(admin, workspace, company, opportunity, record, quote):
    admin.execute(
        "insert into public.insights (workspace_id, company_id, opportunity_id, source, unit_id, source_record_ids,"
        " type, text, quote, confidence, extractor_version, origin_record_id, action_scope)"
        " values (%s, %s, %s, 'gmail', %s, %s, 'blocker', %s, %s, 'high', 'fixture', %s, 'deal_progression')",
        (workspace, company, opportunity, str(record), [record], quote, quote, record),
    )


def test_calendar_month_window_clamps_to_end_of_month():
    assert sales_history._month_start(date(2024, 3, 31), 1) == date(2024, 2, 29)
    assert sales_history._month_start(date(2026, 1, 31), 2) == date(2025, 11, 30)


@requires_database
def test_digest_distinguishes_first_import_from_later_changes(history_case):
    reader, workspace = history_case
    with psycopg.connect(TEST_DATABASE_URL) as admin:
        _, opportunity = _deal(admin, workspace, "History Account", 2)
        initial = NOW - timedelta(days=8)
        _change(admin, workspace, opportunity, _run(admin, workspace, initial), initial, "created", None, "History Account")
        changed = NOW - timedelta(days=1)
        _change(admin, workspace, opportunity, _run(admin, workspace, changed), changed, "stage", "1. Opening", "2. In Talks")
    first = sales_history.get_deal_change_digest(reader, workspace, opportunity, months=1, limit=1, as_of=NOW)
    second = sales_history.get_deal_change_digest(reader, workspace, opportunity, months=1, offset=1, as_of=NOW)
    assert first["period"]["start"] == "2026-10-11"
    assert first["total_matches"] == 2 and first["next_offset"] == 1
    assert first["history_coverage"]["starts_after_requested_date"]
    assert first["changes"][0]["old_value"] == "1. Opening"
    assert second["changes"][0]["event"] == "initial_import" and second["next_offset"] is None


@requires_database
def test_stalls_include_recent_open_blockers_and_exclude_unknown_silence(history_case):
    reader, workspace = history_case
    with psycopg.connect(TEST_DATABASE_URL) as admin:
        blocked_company, blocked = _deal(admin, workspace, "Recently blocked", 2)
        quiet_company, quiet = _deal(admin, workspace, "Quiet proposal", 3)
        _, unknown = _deal(admin, workspace, "Unknown activity", 1)
        blocker_text = "We cannot move forward until Legal approves the proposal."
        record = _mail(admin, workspace, blocked_company, NOW - timedelta(days=1), body=blocker_text)
        _blocker(admin, workspace, blocked_company, blocked, record, blocker_text)
        old_mail = _mail(admin, workspace, quiet_company, NOW - timedelta(days=35))
        _mail(admin, workspace, quiet_company, NOW - timedelta(days=1), subject="Automatic reply: Buyer reply")
        _blocker(admin, workspace, quiet_company, quiet, old_mail, "This quote never appeared in the source.")
        entered = NOW - timedelta(days=40)
        _change(admin, workspace, quiet, _run(admin, workspace, entered), entered, "stage", "2. In Talks", "3. Proposal")

    result = sales_history.find_stalled_deals(reader, workspace, quiet_days=30, viewer_mailbox="RG", as_of=NOW)
    assert result["stalled_count"] == 2 and result["open_blocker_count"] == result["quiet_count"] == 1
    blocked_row, quiet_row = result["deals"]
    assert blocked_row["opportunity_id"] == str(blocked)
    assert blocked_row["stall_reasons"] == ["open_blocker"] and blocked_row["touch_status"] == "recent"
    assert blocked_row["open_blocker"]["quote"] == blocker_text
    assert quiet_row["stall_reasons"] == ["quiet"] and quiet_row["open_blocker"] is None
    assert quiet_row["last_buyer_touch"]["record_id"] == str(old_mail)
    assert quiet_row["observed_stage_entry_at"] == entered.isoformat()
    assert [row["opportunity_id"] for row in result["unclassified_touch_deals"]] == [str(unknown)]


@requires_database
def test_compare_counts_prospect_stage_at_touch_and_distinct_accounts(history_case):
    reader, workspace = history_case
    with psycopg.connect(TEST_DATABASE_URL) as admin:
        company, opportunity = _deal(admin, workspace, "Now a pilot", 4)
        unknown_company, _ = _deal(admin, workspace, "Current prospect with no history", 3)
        initial = NOW - timedelta(days=65)
        _change(admin, workspace, opportunity, _run(admin, workspace, initial), initial, "created", None, "Now a pilot")
        staged = NOW - timedelta(days=50)
        _change(admin, workspace, opportunity, _run(admin, workspace, staged), staged, "stage", "1. Opening", "2. In Talks")
        promoted = NOW - timedelta(days=7)
        same_sync = _run(admin, workspace, promoted)
        for _ in range(2):
            _change(admin, workspace, opportunity, same_sync, promoted, "stage", "2. In Talks", "4. Pilot")
        _mail(admin, workspace, company, NOW - timedelta(days=30))
        for days in (9, 8, 6):
            _mail(admin, workspace, company, NOW - timedelta(days=days))
        _mail(admin, workspace, unknown_company, NOW - timedelta(days=5))
        _run(admin, workspace, NOW - timedelta(days=9), source="gmail", attested=True)

    result = sales_history.compare_pipeline_periods(
        reader, workspace, date(2026, 11, 1), date(2026, 11, 9), date(2026, 10, 1), date(2026, 10, 31), as_of=NOW,
    )
    current, prior = result["current"], result["prior"]
    assert (current["days"], prior["days"]) == (9, 31)
    assert current["metrics"]["prospect_advances"] == current["metrics"]["distinct_advancing_prospects"] == 1
    assert current["metrics"]["distinct_accounts_with_buyer_touch"] == 2
    assert current["metrics"]["distinct_prospect_accounts_with_buyer_touch"] == 1
    assert current["metrics"]["distinct_accounts_with_buyer_touch_unknown_stage"] == 1
    assert prior["metrics"]["distinct_prospect_accounts_with_buyer_touch"] == 1
    assert current["source_coverage"]["gmail"]["verified_receipts"] == 1
    assert prior["source_coverage"]["gmail"]["coverage_status"] == "no_successful_sync_recorded"
