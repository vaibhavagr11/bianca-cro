from contextlib import nullcontext
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import brief_api, pipeline_freshness, review_api
from app.main import app


def test_pipeline_receipt_requires_latest_successful_complete_sync_today(monkeypatch):
    sync_id = uuid4()

    class Connection:
        row = None

        def execute(self, sql, params):
            assert "s.source = 'pipeline'" in sql
            assert "s.started_at desc" in sql
            assert params == ("workspace",)
            return self

        def fetchone(self):
            return self.row

    conn = Connection()
    monkeypatch.setattr(pipeline_freshness, "_workspace_id", lambda _: "workspace")
    monkeypatch.setattr(pipeline_freshness.db, "connect", lambda: nullcontext(conn))
    for row in (
        None, (sync_id, "failed", True, True), (sync_id, "running", True, None),
        (sync_id, "succeeded", False, True), (sync_id, "succeeded", True, False),
        (uuid4(), "succeeded", True, True),
    ):
        conn.row = row
        with pytest.raises(HTTPException) as err:
            pipeline_freshness.require_fresh_pipeline(sync_id)
        assert err.value.status_code == 409

    conn.row = (sync_id, "succeeded", True, True)
    pipeline_freshness.require_fresh_pipeline(sync_id)


@pytest.mark.parametrize("path,module", [
    ("/brief/build", brief_api), ("/review/daily", review_api), ("/process/publish", review_api),
])
def test_scheduled_publisher_rejects_stale_pipeline_before_building(monkeypatch, path, module):
    sync_id = uuid4()
    monkeypatch.setattr(review_api.settings, "review_enabled", True)

    def reject(receipt):
        assert receipt == sync_id
        raise HTTPException(409, "Current Pipeline sync required")

    monkeypatch.setattr(module, "require_fresh_pipeline", reject)
    monkeypatch.setattr(brief_api, "build_brief_v1", lambda *args: pytest.fail("Must not build a brief"))
    monkeypatch.setattr(review_api, "_daily_ready", lambda: pytest.fail("Must not start a review build"))
    response = TestClient(app).post(path, params={"pipeline_sync_run_id": str(sync_id)},
                                    headers={"X-Ingest-Token": "test-token"})
    assert response.status_code == 409
    assert response.json()["detail"] == "Current Pipeline sync required"
