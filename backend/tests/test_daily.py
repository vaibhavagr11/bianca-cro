from fastapi.testclient import TestClient

from app.daily import take
from app.main import app


def test_take_splits_batches_per_stage():
    assert take(["a", "b", "c", "d"], "reconcile") == (["a", "b", "c"], ["d"])
    assert take(["a", "b", "c"], "profile") == (["a", "b"], ["c"])
    assert take([], "profile") == ([], [])


def test_daily_requires_token():
    assert TestClient(app).post("/process/daily").status_code == 401


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
