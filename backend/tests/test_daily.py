from contextlib import contextmanager
from datetime import UTC, date, datetime

from fastapi.testclient import TestClient

from app import daily
from app.daily import take
from app.main import app


def test_take_splits_batches_per_stage():
    assert take(["a", "b", "c", "d"], "reconcile") == (["a", "b", "c"], ["d"])
    assert take(["a", "b", "c"], "profile") == (["a", "b"], ["c"])
    assert take([], "profile") == ([], [])


def test_daily_requires_token():
    assert TestClient(app).post("/process/daily").status_code == 401


def test_daily_refreshes_deals_from_all_extraction_batches(monkeypatch):
    run = {"id": "run", "for_date": date(2026, 11, 8), "stage": "extract", "queue": [], "touched": [],
           "steps": 0, "started_at": datetime(2026, 11, 8, tzinfo=UTC)}
    batches = iter([
        {"remaining_units": 1, "touched_opportunity_ids": ["updated"]},
        {"remaining_units": 0, "touched_opportunity_ids": ["empty"]},
    ])

    class Rows(list):
        def fetchone(self):
            return self[0]

    class Connection:
        def commit(self):
            pass

        def execute(self, sql, params=()):
            if sql.startswith("select timezone"):
                return Rows([{"timezone": "UTC"}])
            if sql.startswith("insert into daily_runs"):
                return Rows([dict(run)])
            if sql.startswith("select exists"):
                return Rows([{"exists": False}])
            if sql.startswith("select id::text"):
                return Rows([{"id": "updated"}, {"id": "empty"}, {"id": "committed"}])
            if sql.startswith("select distinct opportunity_id"):
                assert "updated_at >=" in sql
                return Rows([("committed",)])
            assert sql.startswith("update daily_runs set stage")
            stage, queue, touched = params[:3]
            run.update(stage=stage, queue=queue, touched=touched, steps=run["steps"] + 1)
            return Rows([dict(run)])

    @contextmanager
    def connect():
        yield Connection()

    monkeypatch.setattr(daily.db, "connect", connect)
    monkeypatch.setattr(daily.timeline, "utcnow", lambda: run["started_at"])
    monkeypatch.setattr(daily, "_workspace_id", lambda conn: "test-workspace")
    monkeypatch.setattr(daily.process, "extract_insights", lambda *args, **kwargs: next(batches))

    assert daily.daily_step(x_ingest_token="test-token")["stage"] == "extract"
    assert run["touched"] == ["updated"]
    assert daily.daily_step(x_ingest_token="test-token")["stage"] == "reconcile"
    assert run["queue"] == ["committed", "empty", "updated"]
