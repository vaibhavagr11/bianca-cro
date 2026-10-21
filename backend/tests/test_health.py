from fastapi.testclient import TestClient

from app import db
from app.main import app

client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_db_unreachable_returns_503(monkeypatch):
    def boom():
        raise OSError("connection refused")

    monkeypatch.setattr(db, "connect", boom)
    resp = client.get("/health/db")
    assert resp.status_code == 503
    assert resp.json() == {"detail": "database unreachable"}
