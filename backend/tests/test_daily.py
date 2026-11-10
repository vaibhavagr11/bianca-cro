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
           "steps": 0, "started_at": datetime(2026, 11, 8, tzinfo=UTC), "sync_batch_id": None}
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
            if "pg_try_advisory_lock" in sql or "pg_advisory_unlock" in sql:
                return Rows([(True,)])
            if sql.startswith("select timezone"):
                return Rows([{"timezone": "UTC"}])
            if sql.startswith("insert into daily_runs"):
                return Rows([dict(run)])
            if sql.startswith("select exists"):
                return Rows([{"exists": False}])
            if sql.startswith("select id::text"):
                return Rows([{"id": "updated"}, {"id": "empty"}, {"id": "committed"}])
            if sql.startswith("select distinct opportunity_id"):
                assert "last_extracted_at >=" in sql
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


def test_attested_daily_run_defers_completion_to_the_verified_brief(monkeypatch):
    from datetime import date, datetime, timedelta, timezone
    from uuid import UUID

    from app import brief_delivery, daily

    batch_id = UUID(int=1)
    started = datetime.now(timezone.utc)
    run = {"id": "daily", "sync_batch_id": batch_id, "started_at": started, "stage": "extract",
           "queue": [], "touched": [], "steps": 2, "for_date": date(2026, 10, 7)}

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def commit(self):
            pass

        def execute(self, sql, params):
            if sql.startswith("select timezone"):
                return Result({"timezone": "UTC"})
            if sql.startswith("insert into daily_runs"):
                assert "do nothing" in sql
                return Result(None)
            if sql.startswith("select * from daily_runs"):
                return Result(run)
            if sql.startswith("select exists"):
                return Result({"exists": False})
            if sql.startswith("select id::text"):
                return [{"id": "deal"}]
            if sql.startswith("select distinct opportunity_id"):
                assert "last_extracted_at" in sql
                return [("deal",)]
            if sql.startswith("update daily_runs"):
                stage, queue, touched, *_ = params
                return Result({**run, "stage": stage, "queue": queue, "touched": touched, "steps": 3})
            raise AssertionError(sql)

    class Result:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return self.value

    monkeypatch.setattr(daily.db, "connect", Conn)
    monkeypatch.setattr(brief_delivery, "validate_sync_batch", lambda *_: started - timedelta(minutes=1))
    monkeypatch.setattr(daily.process, "extract_insights", lambda *_, **__: {"remaining_units": 0})
    assert daily._daily_step("workspace", "token", batch_id)["stage"] == "profile"

    run["sync_batch_id"] = None
    assert daily._daily_step("workspace", "token", None)["stage"] == "reconcile"
