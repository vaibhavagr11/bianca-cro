from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
import html
import json

import pytest
from fastapi.testclient import TestClient

from app import brief_api, brief_v2
from app.brief_verification import Verdict
from app.main import app


NOW = datetime(2026, 11, 5, 14, tzinfo=UTC)
SALES = "deal_progression"
DELIVERY = "delivery"
SALES_ASK = "Send pricing for the paid rollout"


def deal(key: str) -> dict:
    return {"id": f"{key}-opportunity", "company_id": key, "name": key,
            "stage": "2. In Talks", "stage_rank": 2, "owner_code": "RG", "removed_at": None}


def email(key: str, company: str, body: str, *, days: int = 2, outgoing: bool = False,
          thread: str | None = None) -> dict:
    ours = {"address": "ramiro@bianca.example", "name": "Ramiro"}
    theirs = {"address": f"buyer@{company}.example", "name": "Buyer"}
    at = NOW - timedelta(days=days)
    expectation = ({"state": "expected", "latest_message_id": key, "latest_content_hash": f"h-{key}",
                    "evidence_message_id": key, "evidence_content_hash": f"h-{key}"} if outgoing else None)
    return {"id": key, "thread_id": thread or key, "company_id": company, "subject": key,
            "body": body, "source": "gmail", "occurred_at": at, "created_at": at, "meta": {},
            "participants": {"from": [ours if outgoing else theirs], "to": [theirs if outgoing else ours]},
            "external_id": key, "content_hash": f"h-{key}", "unclassified": False, "reply_expectation": expectation}


def insight(key: str, company: str, text: str, scope: str | None, *, type_: str = "commitment",
            origin: dict | None = None, quote: str | None = None, owner_side: str | None = "bianca",
            dated: bool = True) -> dict:
    at = origin["occurred_at"] if origin else NOW - timedelta(days=2)
    return {"id": key, "opportunity_id": f"{company}-opportunity", "company_id": company, "type": type_,
            "text": text, "quote": quote or text, "owner_side": owner_side, "owner_name": None,
            "due_date": NOW.date() if dated else None, "status": "open", "action_scope": scope,
            "source": origin["source"] if origin else "notes", "unit_id": origin["thread_id"] if origin else key,
            "origin_record_id": origin["id"] if origin else key, "source_record_ids": [origin["id"] if origin else key],
            "occurred_at": at, "created_at": at, "meta": {}, "last_extracted_at": at,
            "origin_occurred_at": at if origin else None, "updated_at": at}


class Result(list):
    def fetchone(self):
        return self[0] if self else None

    def fetchall(self):
        return list(self)


class BriefData:
    def __init__(self, deals, messages, insights, profiles, *, meetings=(), changes=()):
        self.deals = deals
        self.messages = messages
        self.insights = insights
        self.profiles = profiles
        self.meetings = meetings
        self.changes = changes

    def execute(self, query, params=()):
        if "from workspaces" in query:
            return Result([{"timezone": "UTC", "internal_domains": ["bianca.example"]}])
        if "from team_members" in query:
            return Result([{"owner_code": "RG", "full_name": "Ramiro Godfrid", "email": "ramiro@bianca.example",
                            "receives_brief": True}])
        if "from briefs" in query:
            return Result([{"created_at": NOW - timedelta(hours=6)}])
        if "from opportunity_profiles" in query:
            return Result(self.profiles)
        if "from opportunities o" in query:
            return Result(self.deals)
        if "from insights i" in query:
            return Result() if "count(*)" in query else Result(self.insights)
        if "from opportunity_changes" in query:
            return Result(self.changes)
        if "source = 'calendar'" in query:
            cancelled = "and meta->>'status' = 'cancelled'" in query
            return Result(m for m in self.meetings if (m["meta"]["status"] == "cancelled") == cancelled)
        if "from sync_runs" in query:
            return Result()
        if "max(occurred_at) as last" in query:
            return Result([{"company_id": d["company_id"], "last": NOW - timedelta(days=2)} for d in self.deals])
        if "created_at >" in query or "occurred_at > %s" in query:
            return Result(m for m in self.messages if m["occurred_at"] > params[1])
        if "from source_records" in query and "source = 'gmail'" in query:
            if "distinct on (thread_id)" in query:
                latest = {}
                for message in sorted(self.messages, key=lambda m: m["occurred_at"]):
                    latest[message["thread_id"]] = message
                return Result(latest.values())
            latest_first = "occurred_at desc" in query
            return Result(sorted(self.messages, key=lambda m: (m["thread_id"], -m["occurred_at"].timestamp()
                                                                 if latest_first else m["occurred_at"].timestamp())))
        if "from source_records" in query and "id = any" in query:
            return Result([m for m in self.messages if m["id"] in params[0]])
        raise AssertionError(f"Unexpected query: {query}")


@pytest.fixture
def brief_client(monkeypatch):
    monkeypatch.setattr(brief_api.timeline, "utcnow", lambda: NOW)
    monkeypatch.setattr(brief_api.actions, "latest", lambda *_: {})
    monkeypatch.setattr(brief_api, "_workspace_id", lambda _: "workspace")
    monkeypatch.setattr(brief_api, "_claude", lambda: None)

    def unavailable_plan(*_):
        raise ValueError("Simulate the planner being unavailable")

    monkeypatch.setattr(brief_api.brief, "plan_for", unavailable_plan)

    def client_for(data):
        monkeypatch.setattr(brief_api.db, "connect", lambda: nullcontext(data))
        return TestClient(app)

    return client_for


@pytest.fixture
def scoped_brief(brief_client):
    rollout_ask = "Can you send pricing for the paid rollout by November 5?"
    dashboard_ask = "Please fix the sales dashboard export."
    inbound = email("mixed-email", "mixed", f"{rollout_ask} {dashboard_ask}")
    old_quote = "Could you send the renewal proposal?"
    old = email("old-email", "repeat", old_quote, days=6, thread="repeat-thread")
    repeat = email("quoted-support-email", "repeat", f"Could you fix the export? Earlier: {old_quote}",
                   thread="repeat-thread")
    outgoing = email("outbound-sales-ask", "sales-wait", "Can you approve the proposal?", days=8, outgoing=True)
    support = email("outbound-support-ask", "support-wait", "Can you retry the report?", days=8, outgoing=True)
    later_support = email("later-support-ask", "sales-wait", "Can you retry the export?", outgoing=True)
    stale = email("unscoped-support-email", "unscoped", "Could you add another dashboard?", days=2)
    messages = [inbound, old, repeat, outgoing, support, later_support, stale]
    insights = [
        insight("pricing", "mixed", "Share the pricing deck", SALES),
        insight("pilot", "mixed", "Send the pilot terms", SALES),
        insight("dashboard", "mixed", "Fix the sales dashboard export", DELIVERY, type_="next_step",
                origin=inbound, quote=dashboard_ask),
        insight("incoming", "mixed", SALES_ASK, SALES, type_="next_step", origin=inbound,
                quote=rollout_ask),
        insight("old-ask", "repeat", "Send the renewal proposal", SALES, type_="next_step",
                origin=old, quote=old_quote, dated=False),
        insight("outgoing", "sales-wait", "Customer to approve the proposal", SALES, type_="next_step",
                origin=outgoing, quote=outgoing["body"], owner_side="customer", dated=False),
        insight("outgoing-support", "support-wait", "Customer to retry the report", DELIVERY, type_="next_step",
                origin=support, quote=support["body"], owner_side="customer", dated=False),
        insight("later-support", "sales-wait", "Customer to retry the export", DELIVERY, type_="next_step",
                origin=later_support, quote=later_support["body"], owner_side="customer", dated=False),
        insight("legacy", "unscoped", "Add another dashboard", None, type_="next_step", origin=stale),
        insight("budget", "renewal", "Buyer confirmed the renewal budget", SALES, type_="buying_signal",
                owner_side=None, dated=False),
    ]
    profiles = [
        {"opportunity_id": "mixed-opportunity", "generated_at": NOW, "profiler_version": "profile-v3",
         "profile": {"refs": {"I1": "dashboard"}, "recommended_actions": [
             {"action": "Refactor the analytics exporter", "why": "Support request", "urgency": "high",
              "evidence": ["I1"], "action_scope": SALES}
         ]}},
        {"opportunity_id": "renewal-opportunity", "generated_at": NOW, "profiler_version": "profile-v3",
         "profile": {"refs": {"I1": "budget"}, "recommended_actions": [
             {"action": "Confirm the renewal decision date", "why": "The buyer has budget", "urgency": "high",
              "evidence": ["I1"], "action_scope": SALES},
             {"action": "Repair the data connector", "why": "An existing customer needs help", "urgency": "high",
              "evidence": ["I1"], "action_scope": DELIVERY},
         ]}},
    ]
    deals = [deal(key) for key in ("repeat", "sales-wait", "support-wait", "unscoped")]
    deals.append({**deal("mixed"), "stage": "4. Pilot", "stage_rank": 4})
    deals.append({**deal("renewal"), "stage": "5. Working Client", "stage_rank": 5})
    return brief_client(BriefData(deals, messages, insights, profiles))


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_brief_requires_sales_scope_for_stored_actions_and_exact_email_asks(scoped_brief, version):
    response = scoped_brief.post(f"/brief/build?dry_run=true&version={version}",
                                 headers={"X-Ingest-Token": "test-token"})
    assert response.status_code == 200
    result = response.json()
    html = result["html"]
    assert "Share the pricing deck" in html
    assert "Send the pilot terms" in html
    assert SALES_ASK in html
    assert "Confirm the renewal decision date" not in html
    assert "Fix the sales dashboard export" not in html
    assert "Refactor the analytics exporter" not in html
    assert "Repair the data connector" not in html
    assert "Add another dashboard" in html       # extracted before scopes existed: shown until re-extracted
    assert "quoted-support-email" not in html
    # (the not-yet-scoped "Add another dashboard" row now shows, linking to its own email)

    if version == "v2":
        founder = result["payload"]["sections"][0]
        due_now = {row["text"] for row in founder["today"] if row["tag"] == "Due now"}
        assert due_now == {"Share the pricing deck", "Send the pilot terms"}
        assert any(row["tag"] == "Reply needed" and row["text"] == SALES_ASK for row in founder["today"])
        assert [row["thread_id"] for row in founder["waiting"]] == ["outbound-sales-ask"]
        assert "Customer to approve the proposal" in html
    else:
        ask_row, = [row for row in html.split("</tr>") if SALES_ASK in row]
        assert f"{NOW:%b} {NOW.day}" not in ask_row


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_brief_excludes_working_clients_after_sheet_status_changes(brief_client, version):
    working = deal("Existing Account") | {"stage": "4. Pilot", "stage_rank": 4}
    pilot = deal("Sales Pilot") | {"stage": "4. Pilot", "stage_rank": 4}
    work_text = "Send the operational delivery report"
    pilot_text = "Send the pilot acceptance terms"
    insights = [
        insight("working-promise", working["company_id"], work_text, SALES),
        insight("pilot-promise", pilot["company_id"], pilot_text, SALES),
    ]

    def calendar(title, company, *, rules_company=None, status="confirmed"):
        return {"id": title, "subject": title, "company_id": company, "rules_company_id": rules_company,
                "occurred_at": NOW + timedelta(hours=2),
                "participants": {"attendees": [{"address": "ramiro@bianca.example"}]}, "meta": {"status": status}}

    meetings = [
        calendar("Delivery check-in", working["company_id"]),
        calendar("Rescheduled implementation workshop", None, rules_company=working["company_id"]),
        calendar("Onboarding review called off", None, rules_company=working["company_id"], status="cancelled"),
        calendar("Pilot scoping call", pilot["company_id"]),
        calendar("Partner discussion", None),
    ]
    messages = [email("Operations agenda", working["company_id"], "We have an update for the operations team.", days=0)]
    changes = [{"company_id": pilot["company_id"], "name": pilot["name"], "owner_code": "RG",
                "field": "stage", "old_value": "3. Proposal", "new_value": "4. Pilot", "sync_run_id": "sync"}]
    data = BriefData([working, pilot], messages, insights, [], meetings=meetings, changes=changes)
    client = brief_client(data)

    def build():
        response = client.post(f"/brief/build?dry_run=true&version={version}", headers={"X-Ingest-Token": "test-token"})
        assert response.status_code == 200
        return response.json()

    before = build()
    for text in (working["name"], work_text, "Delivery check-in", "Rescheduled implementation workshop",
                 "Operations agenda", pilot_text, "Pilot scoping call", "Partner discussion"):
        assert text in before["html"]
    if version == "v1":
        assert "Onboarding review called off" in before["html"]

    working.update(stage=" 5.  WORKING CLIENTS ", stage_rank=5)
    changes.insert(0, {"company_id": working["company_id"], "name": working["name"], "owner_code": "RG",
                       "field": "stage", "old_value": "4. Pilot", "new_value": working["stage"],
                       "sync_run_id": "next-sync"})

    after = build()
    output = json.dumps(after)
    for text in (working["name"], work_text, "Delivery check-in", "Rescheduled implementation workshop",
                 "Onboarding review called off", "Operations agenda", "WORKING CLIENTS", "working-promise"):
        assert text not in output
    for text in (pilot_text, "Pilot scoping call", "Partner discussion"):
        assert text in after["html"] and text in after["text"]
    assert "3. Proposal" in after["html"]


@pytest.mark.parametrize("verification", ["off", "shadow"])
@pytest.mark.parametrize("customer_commitment", [False, True], ids=["original-ask", "customer-commitment"])
def test_waiting_on_a_reply_excludes_the_same_action_across_founder_sections(
        monkeypatch, brief_client, verification, customer_commitment):
    company = {**deal("buyer"), "owner_code": "NC"}
    quote = "I'll confirm approval." if customer_commitment else "Can you approve the proposal?"
    waiting_action = "Customer to approve the proposal"
    duplicate_move = "Ask again for proposal approval"
    other_move = "Send the revised pricing sheet"
    other_quote = "The pricing sheet needs to be revised."
    original = email("original", "buyer", f"{quote} {other_quote}", days=11, outgoing=not customer_commitment,
                     thread="proposal")
    followup = "Thanks, I'll look out for it." if customer_commitment else "Checking in on this."
    latest = email("latest", "buyer", followup, days=8, outgoing=True, thread="proposal")
    latest["reply_expectation"].update(evidence_message_id=original["external_id"],
                                       evidence_content_hash=original["content_hash"],
                                       evidence_quote=quote, response=waiting_action)
    insights = [
        insight("approval", "buyer", waiting_action, SALES, type_="commitment" if customer_commitment else "next_step",
                origin=original, quote=quote, owner_side="customer", dated=False),
        insight("pricing", "buyer", other_move, SALES, type_="need", origin=original, quote=other_quote,
                owner_side=None, dated=False),
    ]
    profiles = [{"opportunity_id": company["id"], "generated_at": NOW, "profile": {
        "refs": {"I1": "approval", "I2": "pricing"},
        "recommended_actions": [
            {"action": duplicate_move, "evidence": ["I1"], "action_scope": SALES},
            {"action": other_move, "evidence": ["I1", "I2"], "action_scope": SALES},
        ],
    }}]
    checked = []

    class Gate:
        as_of = NOW
        audit = {}

        def __call__(self, actions):
            checked.extend(actions)
            return {a.key: Verdict("waiting" if a.kind == "waiting" else "still_owed", "fixture") for a in actions}

        def ready_fields(self):
            return {}

        def finalize(self, conn, response):
            pass

    monkeypatch.setattr(brief_api, "create_gate", lambda *_: Gate())
    client = brief_client(BriefData([company], [original, latest], insights, profiles))
    response = client.post(f"/brief/build?dry_run=true&version=v2&verification={verification}",
                           headers={"X-Ingest-Token": "test-token"})
    assert response.status_code == 200
    result = response.json()
    sections = {s["owner"]: s for s in result["payload"]["sections"]}
    assert [h["move"]["text"] for h in sections["NC"]["highlights"]] == [other_move]
    assert [(w["thread_id"], w["action"]) for w in sections["RG"]["waiting"]] == [("proposal", waiting_action)]
    for body in (result["html"], result["text"]):
        assert body.count(waiting_action) == 1
        assert duplicate_move not in body
        assert other_move in body
    if verification == "shadow":
        assert {a.kind for a in checked} == {"waiting", "recommendation"}
        assert all(a.text != duplicate_move for a in checked)


def test_waiting_row_beyond_display_cap_stays_out_of_highlights_and_followups(monkeypatch, brief_client):
    monkeypatch.setattr(brief_v2, "MAX_WAITING", 1)
    deals, messages, insights, profiles = [], [], [], []
    for name, age in (("visible", 7), ("capped", 8)):
        company = deal(name)
        quote = f"Can you approve the {name} proposal?"
        action = f"Customer to approve the {name} proposal"
        ask = email(f"{name}-ask", name, quote, days=age, outgoing=True)
        ask["reply_expectation"].update(evidence_quote=quote, response=action)
        deals.append(company)
        messages.append(ask)
        insights.append(insight(f"{name}-approval", name, action, SALES, type_="next_step", origin=ask, quote=quote,
                                owner_side="customer", dated=False))
        profiles.append({"opportunity_id": company["id"], "generated_at": NOW, "profile": {
            "refs": {"I1": f"{name}-approval"},
            "recommended_actions": [
                {"action": f"Follow up on the {name} proposal approval", "evidence": ["I1"], "action_scope": SALES},
            ],
        }})
    client = brief_client(BriefData(deals, messages, insights, profiles))
    response = client.post("/brief/build?dry_run=true&version=v2", headers={"X-Ingest-Token": "test-token"})
    assert response.status_code == 200
    result = response.json()
    founder = result["payload"]["sections"][0]
    assert [w["deal"] for w in founder["waiting"]] == ["visible"]
    assert founder["more_waiting"] == 1
    assert not founder["highlights"]
    assert not founder["followups"]
    for body in (result["html"], result["text"]):
        assert "Customer to approve the visible proposal" in body
        assert "Customer to approve the capped proposal" not in body
        assert "Follow up on the capped proposal approval" not in body


@pytest.mark.parametrize("with_next_move", [False, True], ids=["follow-up-only", "with-next-move"])
@pytest.mark.parametrize("tagged", [False, True], ids=["legacy-profile", "tagged-profile"])
def test_profile_followup_stays_out_of_deals_to_move(brief_client, with_next_move, tagged):
    company = deal("Prospect Co")
    company_id = company["company_id"]
    followup = ("See whether the buyer has an answer on last week's proposal." if tagged else
                "Check in with the buyer on the proposal we sent last week.")
    next_move = "Send a revised proposal with the requested pricing options."
    refs = {"I1": "sent-proposal"}
    recommendations = [{"action": followup, "evidence": ["I1"], "action_scope": SALES}]
    if tagged:
        recommendations[0]["followup_only"] = True
    insights = [insight("sent-proposal", company_id, "The buyer received the proposal last week.", SALES,
                        type_="buying_signal", dated=False)]
    if with_next_move:
        refs["I2"] = "pricing-options"
        insights.append(insight("pricing-options", company_id, "The buyer asked to compare pricing options.", SALES,
                                type_="need", dated=False))
        recommendations.append({"action": next_move, "evidence": ["I2"], "action_scope": SALES})
    profiles = [{"opportunity_id": company["id"], "generated_at": NOW, "profile": {
        "refs": refs, "recommended_actions": recommendations,
    }}]
    client = brief_client(BriefData([company], [], insights, profiles))
    response = client.post("/brief/build?dry_run=true&version=v2", headers={"X-Ingest-Token": "test-token"})
    assert response.status_code == 200
    result = response.json()
    founder = result["payload"]["sections"][0]
    assert [h["move"]["text"] for h in founder["highlights"]] == ([next_move] if with_next_move else [])
    assert len(founder["followups"]) == 1
    assert founder["followups"][0]["tag"] == "Follow up"
    assert followup in founder["followups"][0]["text"]
    assert not founder["waiting"]
    for body, wording in ((result["html"], html.escape(followup)), (result["text"], followup)):
        assert body.count(wording) == 1
        assert body.index("Follow-ups that may be needed") < body.index(wording)
        assert (next_move in body) == with_next_move
