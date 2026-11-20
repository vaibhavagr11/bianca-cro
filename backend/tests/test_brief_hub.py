from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
from html import unescape
import re
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from app import brief_api
from app.brief_delivery import DeliveryGate, VerificationPending
from app.brief_verification import Verdict
from test_brief_sales_scope import (  # noqa: F401
    NOW, SALES, BriefData, Result, _meeting, brief_client, deal, email, insight,
)


HEADERS = {"X-Ingest-Token": "test-token"}


def busy_brief():
    names = [f"prospect-{index:02d}" for index in range(9)]
    deals = [deal(name) for name in names]
    insights = []
    for index, name in enumerate(names):
        promise = insight(f"promise-{index}", name, f"Send the commercial proposal for {name}", SALES)
        promise["due_date"] = NOW.date() + timedelta(days=4)
        insights += [promise, insight(f"followup-{index}", name, f"Follow up on proposal approval for {name}", SALES,
                                      type_="next_step", dated=False)]
    waits = [email(f"wait-{index}", name, f"Can you approve the {name} proposal?", days=8, outgoing=True)
             for index, name in enumerate(names)]
    meetings = [_meeting(f"Review with {name}", name, hours=index + 1) for index, name in enumerate(names)]
    changes = [{"name": name, "company_id": name, "field": "stage", "old_value": "Opening",
                "new_value": "In Talks", "sync_run_id": None} for name in names]
    for index in range(4):
        name = f"ndl-{index}"
        deals.append({**deal(name), "owner_code": "NDL"})
        insights.append(insight(f"ndl-promise-{index}", name, f"Send the paid rollout agreement for {name}", SALES))
    return BriefData(deals, waits, insights, [], meetings=meetings, changes=changes)


def test_v2_build_saves_full_rows_and_uses_identical_shared_cells_and_action_links(monkeypatch, brief_client):
    monkeypatch.setattr(brief_api.settings, "brief_followup_nudges", False)
    monkeypatch.setattr(brief_api.settings, "brief_action_secret", SecretStr("fixture-action-secret"))
    monkeypatch.setattr(brief_api.settings, "public_base_url", "https://bianca.example/")
    monkeypatch.setattr(brief_api.actions.time, "time", lambda: NOW.timestamp())
    prepared = []
    prepare = brief_api.prepare_table_text

    def prepare_once(fields, client, model):
        fields = list(fields)
        prepared.append(fields)
        return prepare(fields, client, model)

    monkeypatch.setattr(brief_api, "prepare_table_text", prepare_once)
    data = ArchivedBriefData(busy_brief())
    client = brief_client(data)
    monkeypatch.setattr(brief_api.db, "connect", data.connect)
    response = client.post("/brief/build?version=v2", headers=HEADERS)
    assert response.status_code == 200
    result = response.json()
    founder, full = result["payload"]["sections"][0], result["full_payload"]["sections"][0]
    assert len(founder["highlights"]) == len(founder["waiting"]) == 6
    assert len(founder["followups"]) == 3 and len(founder["meetings"]) == 7
    for section in ("highlights", "waiting", "followups", "meetings"):
        assert len(full[section]) == 9
    assert len(result["payload"]["changes"]) == 7
    assert len(result["full_payload"]["changes"]) == 9
    assert len(result["payload"]["ndl"]) == 3
    assert len(result["full_payload"]["ndl"]) == 4
    assert full["highlights"][:6] == founder["highlights"]
    assert full["waiting"][:6] == founder["waiting"]
    assert full["followups"][:3] == founder["followups"]
    assert full["meetings"][:7] == founder["meetings"]

    brief_id = UUID(result["brief_id"])
    assert result["brief_url"] == f"https://bianca.example/review/app?tab=daily-brief&brief={brief_id}"
    assert result["html"].index("View full Daily Brief in War Room") < result["html"].index("Pipeline brief")
    assert result["brief_url"] in unescape(result["html"])
    assert result["text"].splitlines()[0].endswith(result["brief_url"])
    assert "View full Daily Brief in War Room" not in result["full_html"]
    email_rows = re.findall(r"<tr>.*?</tr>", result["html"])
    full_rows = re.findall(r"<tr>.*?</tr>", result["full_html"])
    assert set(email_rows) <= set(full_rows)
    assert len(prepared) == 1
    assert any(field.text == "Send the commercial proposal for prospect-08" for field in prepared[0])

    tokens = [parse_qs(urlsplit(unescape(link)).query)["t"][0]
              for link in re.findall(r'href="([^"]*/actions/complete\?[^"]+)"', result["full_html"])]
    refs = {f"{target['kind']}:{target['ref']}"
            for token in tokens if (target := brief_api.actions.verify(token, now=NOW.timestamp()))}
    assert {f"insight:promise-{index}" for index in range(9)} <= refs
    assert {f"thread:wait-{index}" for index in range(9)} <= refs


def test_dry_run_keeps_the_full_preview_without_linking_to_an_unsaved_brief(brief_client):
    response = brief_client(busy_brief()).post("/brief/build?dry_run=true&version=v2", headers=HEADERS)
    assert response.status_code == 200
    result = response.json()
    assert result["brief_url"] is None
    assert "View full Daily Brief in War Room" not in result["html"] + result["text"]
    assert result["full_html"]
    assert len(result["full_payload"]["sections"][0]["highlights"]) == 9


def test_full_changes_keep_multiple_qualifying_events_on_the_same_deal(brief_client):
    data = busy_brief()
    name = data.deals[0]["name"]
    for index, text in enumerate(("The buyer approved the rollout budget", "Legal approval is blocking the proposal")):
        message = email(f"new-{index}", name, text)
        message["occurred_at"] = message["created_at"] = NOW - timedelta(hours=1)
        update = insight(f"development-{index}", name, text, SALES, type_="buying_signal" if index == 0 else "blocker",
                         origin=message, owner_side=None, dated=False)
        update["last_extracted_at"] = NOW - timedelta(hours=1)
        data.messages.append(message)
        data.insights.append(update)
    result = brief_client(data).post("/brief/build?dry_run=true&version=v2", headers=HEADERS).json()
    details = [change["text"] for change in result["full_payload"]["changes"]]
    assert any("buyer approved the rollout budget" in text for text in details)
    assert any("Legal approval is blocking the proposal" in text for text in details)


class ArchivedBriefData(BriefData):
    def __init__(self, data):
        self.__dict__.update(data.__dict__)
        self.saved_response = None
        self.archives = []
        self.operations = []
        self.active_transaction = None
        self.fail_archive = False

    @contextmanager
    def connect(self):
        self.active_transaction = uuid4()
        old_response, old_archives = deepcopy(self.saved_response), list(self.archives)
        try:
            yield self
        except Exception:
            self.saved_response, self.archives = old_response, old_archives
            raise
        finally:
            self.active_transaction = None

    def cursor(self, **_):
        return self

    def execute(self, query, params=()):
        if query.startswith("select response from brief_verification_runs"):
            return Result([{"response": self.saved_response}])
        if query.startswith("update brief_verification_runs set response"):
            self.saved_response = deepcopy(params[0].obj)
            self.operations.append((self.active_transaction, "verification"))
            return Result()
        if query.startswith("insert into briefs"):
            if self.fail_archive:
                raise RuntimeError("archive write failed")
            self.archives.append((query, params))
            self.operations.append((self.active_transaction, "archive"))
            return Result()
        return super().execute(query, params)


class ReadyGate(DeliveryGate):
    def __init__(self, data):
        super().__init__("workspace", uuid4(), uuid4(), False, NOW, NOW - timedelta(hours=6), "fixture")
        self.data = data
        self.checked = []

    def _ensure_current(self, _):
        pass

    def __call__(self, actions):
        if self.data.saved_response is not None:
            raise VerificationPending(self.data.saved_response)
        self.checked.append(actions)
        return {action.key: Verdict("waiting" if action.kind == "waiting" else "still_owed", "fixture")
                for action in actions}


def test_verified_snapshot_and_ready_response_share_one_transaction_and_retry_keeps_its_link(monkeypatch, brief_client):
    data = ArchivedBriefData(busy_brief())
    client = brief_client(data)
    gate = ReadyGate(data)
    monkeypatch.setattr(brief_api.settings, "brief_followup_nudges", False)
    monkeypatch.setattr(brief_api, "create_gate", lambda *_: gate)
    monkeypatch.setattr(brief_api.db, "connect", data.connect)
    first = client.post("/brief/build?version=v2&verification=required", headers=HEADERS)
    assert first.status_code == 200
    result = first.json()
    assert result["ready_to_send"] is True
    assert len(gate.checked) == 1
    assert len({action.key for action in gate.checked[0]}) == len(gate.checked[0])
    assert any(action.company_id == "prospect-08" for action in gate.checked[0])
    assert len(data.archives) == 1
    query, params = data.archives[0]
    assert "(id, workspace_id, for_date, subject, html, payload, recipients, full_html, full_payload)" in query
    assert str(params[0]) == result["brief_id"]
    assert params[4] == result["html"]
    assert params[7] == result["full_html"]
    assert params[8].obj == result["full_payload"]
    assert data.saved_response["brief_url"] == result["brief_url"]
    assert data.saved_response["brief_id"] == result["brief_id"]
    assert data.operations[0][0] is not None and data.operations[0][0] == data.operations[1][0]
    assert [operation for _, operation in data.operations] == ["verification", "archive"]

    retry = client.post("/brief/build?version=v2&verification=required", headers=HEADERS)
    assert retry.status_code == 200
    assert retry.json() == result
    assert len(data.archives) == len(gate.checked) == 1


def test_failed_archive_rolls_back_the_ready_response_before_a_retry(monkeypatch, brief_client):
    data = ArchivedBriefData(busy_brief())
    data.fail_archive = True
    client = brief_client(data)
    gate = ReadyGate(data)
    monkeypatch.setattr(brief_api, "create_gate", lambda *_: gate)
    monkeypatch.setattr(brief_api.db, "connect", data.connect)
    with pytest.raises(RuntimeError, match="archive write failed"):
        client.post("/brief/build?version=v2&verification=required", headers=HEADERS)
    assert data.saved_response is None and data.archives == []

    data.fail_archive = False
    response = client.post("/brief/build?version=v2&verification=required", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["ready_to_send"] is True
    assert len(data.archives) == 1
    assert str(data.archives[0][1][0]) == data.saved_response["brief_id"]
