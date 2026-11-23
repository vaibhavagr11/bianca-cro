"""Matias can receive both brief versions and see sales work assigned to MG."""

import json

import pytest

from app import brief_api
from test_brief_sales_scope import (  # noqa: F401
    SALES, BriefData, Result, brief_client, deal, email, insight,
)


MATIAS_EMAIL = "matias@bianca.example"
HEADERS = {"X-Ingest-Token": "test-token"}


class MatiasData(BriefData):
    def execute(self, query, params=()):
        rows = super().execute(query, params)
        if "from team_members" in query:
            return Result([*rows, {"owner_code": "MG", "full_name": "Matias Grinberg",
                                  "email": MATIAS_EMAIL, "receives_brief": True}])
        return rows


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_mg_deals_and_subscription_appear_in_both_brief_versions(monkeypatch, brief_client, version):
    monkeypatch.setattr(brief_api.settings, "brief_to", [])
    rendered_sections = []
    render = brief_api.brief.render

    def capture_sections(sections, *args, **kwargs):
        rendered_sections.extend(sections)
        return render(sections, *args, **kwargs)

    monkeypatch.setattr(brief_api.brief, "render", capture_sections)
    data = MatiasData([{**deal("mg-prospect"), "owner_code": "MG"}], [],
                      [insight("mg-terms", "mg-prospect", "Send the paid pilot agreement", SALES)], [])
    response = brief_client(data).post(f"/brief/build?dry_run=true&version={version}", headers=HEADERS)
    assert response.status_code == 200
    result = response.json()
    assert result["to"].split(",") == ["ramiro@bianca.example", MATIAS_EMAIL]
    section_list = result["payload"]["sections"] if version == "v2" else rendered_sections
    sections = {section["owner"]: section for section in section_list}
    assert "Send the paid pilot agreement" in str(sections["MG"])
    assert "Send the paid pilot agreement" not in str([sections["RG"], sections["NC"]])
    assert "Matias Grinberg" in result["html"] and "Send the paid pilot agreement" in result["text"]
    if version == "v2":
        full = {section["owner"]: section for section in result["full_payload"]["sections"]}
        assert "Send the paid pilot agreement" in json.dumps(full["MG"])
        assert "Matias Grinberg" in result["full_html"]


def test_named_matias_action_on_another_owner_deal_routes_to_mg(brief_client):
    action = insight("named-mg", "rg-prospect", "Matias to send the approved commercial terms", SALES)
    action["owner_name"] = "Matias Grinberg"
    data = MatiasData([deal("rg-prospect")], [], [action], [])
    result = brief_client(data).post("/brief/build?dry_run=true&version=v2", headers=HEADERS).json()
    for payload in (result["payload"], result["full_payload"]):
        sections = {section["owner"]: section for section in payload["sections"]}
        assert "Matias to send the approved commercial terms" in json.dumps(sections["MG"]["today"])
        assert "Matias to send the approved commercial terms" not in json.dumps(sections["RG"]["today"])


def test_mg_outgoing_followup_appears_in_waiting_on_them(brief_client):
    message = email("mg-followup", "mg-prospect", "Can you approve the pilot proposal?", days=8, outgoing=True)
    message["participants"]["from"] = [{"address": MATIAS_EMAIL, "name": "Matias Grinberg"}]
    data = MatiasData([{**deal("mg-prospect"), "owner_code": "MG"}], [message], [], [])
    original_execute = data.execute

    def execute(query, params=()):
        if "from workspaces" in query:
            return Result([{"timezone": "UTC", "internal_domains": ["bianca.example"]}])
        return original_execute(query, params)

    data.execute = execute
    result = brief_client(data).post("/brief/build?dry_run=true&version=v2", headers=HEADERS).json()
    for payload in (result["payload"], result["full_payload"]):
        sections = {section["owner"]: section for section in payload["sections"]}
        assert [row["thread_id"] for row in sections["MG"]["waiting"]] == ["mg-followup"]
        assert sections["RG"]["waiting"] == []
