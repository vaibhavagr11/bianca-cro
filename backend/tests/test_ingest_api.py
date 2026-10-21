from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
BODY = {"rows": [{"Empresa": "Acme"}]}


def test_ingest_rejects_missing_token():
    assert client.post("/ingest/pipeline", json=BODY).status_code == 401


def test_ingest_rejects_wrong_token():
    resp = client.post("/ingest/pipeline", json=BODY, headers={"X-Ingest-Token": "nope"})
    assert resp.status_code == 401


def test_ingest_rejects_malformed_body():
    resp = client.post("/ingest/pipeline", json={"data": []}, headers={"X-Ingest-Token": "test-token"})
    assert resp.status_code == 422
