from contextlib import nullcontext
import sys

import pytest

from scripts import migrate


@pytest.mark.parametrize("sheet_review", [False, True])
def test_sheet_review_migrations_are_opt_in(monkeypatch, tmp_path, sheet_review):
    versions = sorted(migrate.SHEET_REVIEW_MIGRATIONS | {"017_action_scope"})
    for version in versions:
        (tmp_path / f"{version}.sql").write_text("select 1")
    applied = []

    class Connection:
        def execute(self, sql, params=()):
            if sql.startswith("insert into schema_migrations"):
                applied.append(params[0])
            return []

        def commit(self):
            pass

        def transaction(self):
            return nullcontext()

    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", tmp_path)
    monkeypatch.setattr(migrate.db, "connect", lambda: nullcontext(Connection()))
    monkeypatch.setattr(sys, "argv", ["migrate", *(["--sheet-review"] if sheet_review else [])])
    assert migrate.main() == 0
    assert applied == (versions if sheet_review else ["017_action_scope"])


@pytest.mark.parametrize("hub_auth", [False, True])
def test_gibb_migration_is_opt_in_preserves_022_and_defers_021(monkeypatch, tmp_path, hub_auth):
    versions = ["021_review_priority_selection", "022_hub_google_auth", "023_hub_gibb_reviewer"]
    for version in versions:
        (tmp_path / f"{version}.sql").write_text("select 1")
    applied = []

    class Connection:
        def execute(self, sql, params=()):
            if sql.startswith("select version from schema_migrations"):
                return [("022_hub_google_auth",)]
            if sql.startswith("insert into schema_migrations"):
                applied.append(params[0])
            return []

        def commit(self):
            pass

        def transaction(self):
            return nullcontext()

    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", tmp_path)
    monkeypatch.setattr(migrate.db, "connect", lambda: nullcontext(Connection()))
    monkeypatch.setattr(sys, "argv", ["migrate", *(["--hub-auth"] if hub_auth else [])])
    assert migrate.main() == 0
    assert applied == (["023_hub_gibb_reviewer"] if hub_auth else [])
