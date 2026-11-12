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


def test_pipeline_without_war_room_keeps_the_original_schema_and_hash(monkeypatch):
    from contextlib import nullcontext
    from hashlib import sha256
    from json import dumps

    from app import ingest

    existing_fields = ["NDL", "4. Pilot", None, None, None, "Progress", "Next"]
    old_hash = sha256(dumps(["Acme", existing_fields], ensure_ascii=False).encode()).hexdigest()
    updates = []

    class Connection:
        def execute(self, sql, params):
            assert "client_context" not in sql
            assert sql.count("%s") == len(params)
            if sql.startswith("select id from workspaces"):
                return self
            if sql.startswith("select o.id,"):
                return self
            if sql.startswith("insert into sync_runs"):
                return self
            if sql.startswith("update opportunities set owner_code"):
                updates.append(params)
            return self

        def fetchone(self):
            return ("test-id",)

        def fetchall(self):
            return [("opp-id", "acme", *existing_fields, old_hash, False)]

        def commit(self):
            pass

        def transaction(self):
            return nullcontext()

        def cursor(self):
            return nullcontext(self)

        def executemany(self, sql, params):
            assert all(row[3] == "next_step" for row in params)

    monkeypatch.setattr(ingest.settings, "review_enabled", False)
    monkeypatch.setattr(ingest.db, "connect", lambda: nullcontext(Connection()))
    row = {"Empresa": "Acme", "Owner": "NDL", "Status": "4. Pilot", "Last progress": "Progress",
           "Next step": "Next", "Client Context": "Ignored when review is disabled"}
    headers = {"X-Ingest-Token": "test-token"}
    preview = client.post("/ingest/pipeline?dry_run=true", json={"rows": [row]}, headers=headers)
    assert preview.status_code == 200
    assert preview.json()["unchanged"] == 1
    assert preview.json()["columns_ignored"] == ["Client Context"]
    row["Next step"] = "New step"
    result = client.post("/ingest/pipeline", json={"rows": [row]}, headers=headers)
    assert result.status_code == 200
    assert result.json()["updated"] == 1
    assert updates[0][:7] == ("NDL", "4. Pilot", 4, None, None, "Progress", "New step")
