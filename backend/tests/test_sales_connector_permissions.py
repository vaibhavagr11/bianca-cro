"""Permission tests against a migrated, disposable local Postgres database."""

import os
from secrets import token_hex

import psycopg
from psycopg import errors, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb
import pytest


TEST_DATABASE_URL = os.environ.get("BIANCA_SALES_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="requires a migrated local BIANCA_SALES_TEST_DATABASE_URL"
)


@pytest.fixture
def sales_database():
    connection_parameters = conninfo_to_dict(TEST_DATABASE_URL)
    host = connection_parameters.get("host", "")
    assert host in {"localhost", "127.0.0.1", "::1"} or host.startswith("/"), "Use a local test database"
    assert "," not in host and not connection_parameters.get("hostaddr"), "Use a single local database"

    suffix = token_hex(6)
    login = f"sales_test_{suffix}"
    unbound = f"sales_test_unbound_{suffix}"
    permissive_policy = f"sales_test_permissive_{suffix}"
    password = token_hex(24)
    workspaces = {}
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as admin:
        try:
            with admin.transaction():
                for role_name in (login, unbound):
                    admin.execute(sql.SQL("create role {} login password {}").format(
                        sql.Identifier(role_name), sql.Literal(password),
                    ))
                    admin.execute(sql.SQL("grant bianca_sales_reader to {}").format(sql.Identifier(role_name)))
                admin.execute(sql.SQL("create policy {} on public.workspaces for select to {} using (true)").format(
                    sql.Identifier(permissive_policy), sql.Identifier(login),
                ))

                for label in ("bound", "other"):
                    workspace = admin.execute(
                        "insert into public.workspaces (name, timezone) values (%s, 'UTC') returning id",
                        (f"sales_test_{label}_{suffix}",),
                    ).fetchone()[0]
                    workspaces[label] = workspace
                    company = admin.execute(
                        "insert into public.companies (workspace_id, name, normalized_name)"
                        " values (%s, 'Example', 'example') returning id", (workspace,),
                    ).fetchone()[0]
                    admin.execute(
                        "insert into public.opportunities (workspace_id, company_id, row_hash) values (%s, %s, 'test')",
                        (workspace, company),
                    )
                    admin.execute(
                        "insert into public.contacts (workspace_id, company_id, email) values (%s, %s, 'sales@example.com')",
                        (workspace, company),
                    )
                    admin.execute(
                        "insert into public.company_domains (workspace_id, company_id, domain) values (%s, %s, 'example.com')",
                        (workspace, company),
                    )
                    admin.execute(
                        "insert into public.sync_runs (workspace_id, source) values (%s, 'gmail'), (%s, 'classify')",
                        (workspace, workspace),
                    )
                    records = {}
                    for key, status, classification in (
                        ("sales", "classified", {"category": "prospect", "is_commercial": True}),
                        ("private", "classified", {"category": "personal", "is_commercial": True}),
                        ("investor", "classified", {"category": "investor", "is_commercial": False}),
                        ("unclassified", "pending", None),
                        ("failed", "failed", {"category": "prospect", "is_commercial": True}),
                        ("legacy", "classified", {"category": "prospect"}),
                    ):
                        records[key] = admin.execute(
                            "insert into public.source_records (workspace_id, company_id, source, external_id, thread_id,"
                            " content_hash, body, status, classification)"
                            " values (%s, %s, 'gmail', %s, %s, 'test', 'test body', %s, %s) returning id",
                            (workspace, company, key, key, status, Jsonb(classification)),
                        ).fetchone()[0]
                        admin.execute(
                            "insert into public.gmail_thread_aliases (workspace_id, mailbox, thread_id, canonical_thread_id)"
                            " values (%s, 'RG', %s, %s)", (workspace, f"alias-{key}", key),
                        )

                    for insight, origin, candidates in (
                        ("sales", records["sales"], [records["sales"]]),
                        ("private", records["private"], [records["sales"], records["private"]]),
                        ("legacy-sales", None, [records["sales"]]),
                        ("legacy-mixed", None, [records["sales"], records["private"]]),
                        ("legacy-empty", None, []),
                    ):
                        admin.execute(
                            "insert into public.insights (workspace_id, company_id, source, unit_id, source_record_ids,"
                            " type, text, quote, confidence, extractor_version, origin_record_id)"
                            " values (%s, %s, 'gmail', %s, %s, 'need', %s, 'test body', 'high', 'test', %s)",
                            (workspace, company, insight, candidates, insight, origin),
                        )

                admin.execute(
                    "insert into bianca_sales_private.workspace_bindings (login_role_oid, workspace_id)"
                    " select oid, %s from pg_catalog.pg_roles where rolname = %s",
                    (workspaces["bound"], login),
                )

            yield login, unbound, password, workspaces
        finally:
            with admin.transaction():
                admin.execute("delete from public.workspaces where id = any(%s)", (list(workspaces.values()),))
                admin.execute(sql.SQL("drop policy if exists {} on public.workspaces").format(
                    sql.Identifier(permissive_policy),
                ))
                for role_name in (login, unbound):
                    admin.execute(sql.SQL("drop role if exists {}").format(sql.Identifier(role_name)))


def test_sales_role_can_read_only_bound_workspace_and_sales_records(sales_database):
    login, _, password, workspaces = sales_database
    with psycopg.connect(TEST_DATABASE_URL, user=login, password=password, autocommit=True) as reader:
        assert reader.execute("select id, timezone from public.workspaces").fetchall() == [(workspaces["bound"], "UTC")]
        for table in ("companies", "opportunities", "contacts", "company_domains"):
            assert reader.execute(sql.SQL("select workspace_id from public.{}").format(sql.Identifier(table))).fetchall() == [
                (workspaces["bound"],)
            ]
        assert reader.execute("select workspace_id, source from public.sync_runs").fetchall() == [
            (workspaces["bound"], "gmail")
        ]
        assert reader.execute("select workspace_id, external_id, body from public.source_records").fetchall() == [
            (workspaces["bound"], "sales", "test body")
        ]
        assert reader.execute("select workspace_id, thread_id from public.gmail_thread_aliases").fetchall() == [
            (workspaces["bound"], "alias-sales")
        ]
        assert reader.execute("select workspace_id, text from public.insights order by text").fetchall() == [
            (workspaces["bound"], "legacy-sales"), (workspaces["bound"], "sales"),
        ]


def test_sales_role_cannot_change_its_scope_or_write(sales_database):
    login, unbound, password, workspaces = sales_database
    with psycopg.connect(TEST_DATABASE_URL, user=login, password=password, autocommit=True) as reader:
        reader.execute("select set_config('app.workspace_id', %s, false)", (str(workspaces["other"]),))
        reader.execute("set role bianca_sales_reader")
        assert reader.execute("select session_user, current_user").fetchone() == (login, "bianca_sales_reader")
        assert reader.execute("select id from public.workspaces").fetchall() == [(workspaces["bound"],)]
        for statement in (
            "select * from bianca_sales_private.workspace_bindings",
            "update bianca_sales_private.workspace_bindings set workspace_id = null",
            "select name from public.workspaces",
            "select error from public.sync_runs",
            "select email from public.team_members",
            "update public.source_records set body = 'changed'",
            "delete from public.opportunities",
            "insert into public.companies (workspace_id, name, normalized_name) values (gen_random_uuid(), 'x', 'x')",
        ):
            with pytest.raises(errors.InsufficientPrivilege):
                reader.execute(statement)
        with pytest.raises(errors.InsufficientPrivilege):
            reader.execute(sql.SQL("set session authorization {}").format(sql.Identifier(unbound)))
        reader.execute("set row_security = off")
        with pytest.raises(errors.InsufficientPrivilege):
            reader.execute("select id from public.workspaces")

    with psycopg.connect(TEST_DATABASE_URL, user=unbound, password=password) as reader:
        assert reader.execute("select id from public.workspaces").fetchall() == []
        assert reader.execute("select id from public.source_records").fetchall() == []


def test_connector_queries_run_with_the_restricted_login(sales_database):
    from app import sales_mcp, sales_queries
    from mcp.server.mcpserver.exceptions import ToolError

    login, _, password, workspaces = sales_database
    dsn = make_conninfo(TEST_DATABASE_URL, user=login, password=password)
    feeds = ["gmail:RG", "notes"]
    workspace_id = workspaces["bound"]
    with psycopg.connect(TEST_DATABASE_URL) as admin:
        admin.execute("update public.workspaces set internal_domains = array['bianca.test'] where id = %s",
                      (workspace_id,))
        opportunity_id, company_id = admin.execute(
            "update public.opportunities set stage_rank = 2, stage = '2. In Talks' where workspace_id = %s"
            " returning id, company_id", (workspace_id,),
        ).fetchone()
        email_id = admin.execute(
            "update public.source_records set occurred_at = now() - interval '1 day',"
            " subject = 'Buying interest', body = 'We have budget for the rollout.',"
            " participants = %s, meta = %s where workspace_id = %s and external_id = 'sales' returning id",
            (Jsonb({"from": [{"address": "sales@example.com"}], "to": [{"address": "founder@bianca.test"}]}),
             Jsonb({"mailboxes": ["RG"]}), workspace_id),
        ).fetchone()[0]
        admin.execute(
            "insert into public.source_records (workspace_id, company_id, source, external_id, occurred_at, subject,"
            " content_hash, body, status, classification, meta) values"
            " (%s, %s, 'notes', 'meeting', now() - interval '2 days', 'Meeting', 'notes-hash',"
            " 'Example Buyer: Rollout time is critical.', 'classified', %s, %s)",
            (workspace_id, company_id, Jsonb({"category": "prospect", "is_commercial": True}),
             Jsonb({"doc_id": "fixture-doc", "date_inferred": False})),
        )
        admin.execute(
            "insert into public.insights (workspace_id, opportunity_id, company_id, source, unit_id,"
            " source_record_ids, type, text, quote, confidence, extractor_version, origin_record_id, action_scope)"
            " values (%s, %s, %s, 'gmail', 'budget', %s, 'buying_signal', 'Rollout budget',"
            " 'We have budget for the rollout.', 'high', 'test', %s, 'deal_progression')",
            (workspace_id, opportunity_id, company_id, [email_id], email_id),
        )
        admin.execute(
            "insert into public.sync_runs (workspace_id, source, status, started_at, finished_at,"
            " feed_key, coverage_complete) values"
            " (%s, 'gmail', 'succeeded', now() - interval '1 hour', now() - interval '55 minutes', 'gmail:RG', true),"
            " (%s, 'notes', 'succeeded', now() - interval '10 days', now() - interval '10 days', null, false)",
            (workspace_id, workspace_id),
        )

    result = sales_mcp._read(dsn, feeds, 84, sales_queries.search_pipeline, query="Example")
    assert [deal["company"] for deal in result["opportunities"]] == ["Example"]
    assert result["coverage"]["sources"]["gmail"]["last_run_status"] == "running"
    assert result["coverage"]["feed_receipts"]["gmail:RG"]["state"] == "recent"
    assert result["coverage"]["sources"]["notes"]["sync_freshness"] == "stale"
    assert result["coverage"]["feed_receipts"]["notes"]["state"] == "unverified"

    activity = sales_mcp._read(dsn, feeds, 84, sales_queries.get_account_activity,
                               opportunity_id=opportunity_id, viewer_mailbox="RG")
    assert activity["last_customer_email"]["url"].endswith("#all/alias-sales")
    assert activity["last_meeting_with_dated_notes"]["url"].endswith("/fixture-doc")
    stakeholder = sales_mcp._read(dsn, feeds, 84, sales_queries.find_stakeholder_evidence,
                                  opportunity_id=opportunity_id, stakeholder="Example Buyer")
    assert stakeholder["evidence"][0]["excerpts"][0]["quote"] == "Rollout time is critical."
    ranking = sales_mcp._read(dsn, feeds, 84, sales_queries.rank_warm_prospects)
    assert ranking["prospects"][0]["buying_signals"][0]["record_id"] == str(email_id)

    with pytest.raises(ToolError, match="database access is unavailable"):
        sales_mcp._read(TEST_DATABASE_URL, feeds, 84, sales_queries.search_pipeline)
