from fastapi.testclient import TestClient

from app.daily import take
from app.main import app


def test_take_splits_batches_per_stage():
    assert take(["a", "b", "c", "d"], "reconcile") == (["a", "b", "c"], ["d"])
    assert take(["a", "b", "c"], "profile") == (["a", "b"], ["c"])
    assert take([], "profile") == ([], [])


def test_daily_requires_token():
    assert TestClient(app).post("/process/daily").status_code == 401
