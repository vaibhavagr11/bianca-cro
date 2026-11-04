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


def test_pipeline_existing_row_maps_client_context_and_hash_in_correct_positions():
    from app import ingest
    from app.pipeline import TRACKED_FIELDS

    class Result:
        def fetchall(self):
            return [("opp-id", "acme", "NDL", "4. Pilot", "ARG", "Buyer", "Contact", "Progress", "Next", "Customer need", "row-hash", False)]

    class Connection:
        def execute(self, sql, params):
            assert "o.client_context" in sql
            return Result()

    [row] = ingest._load_existing(Connection(), "workspace")
    assert set(row.fields) == set(TRACKED_FIELDS)
    assert row.fields["client_context"] == "Customer need"
    assert row.fields["next_step"] == "Next"
    assert row.row_hash == "row-hash"
    assert row.removed is False


def test_pipeline_database_values_include_client_context_after_next_step():
    from app import ingest
    from app.pipeline import parse_rows

    [row] = parse_rows([{"Empresa": "Acme", "Owner": "NDL", "Status": "4. Pilot",
                         "Last progress": "Progress", "Next step": "Next", "Client Context": "Customer need"}])
    assert ingest._opp_values(row) == ("NDL", "4. Pilot", 4, None, None, "Progress", "Next", "Customer need")
