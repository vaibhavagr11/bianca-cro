from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import brief_api, brief_v2
from app.brief_verification import Verdict
from app.main import app


NOW = datetime(2026, 11, 5, 14, tzinfo=UTC)
SALES = "deal_progression"
DELIVERY = "delivery"
SALES_ASK = "Send the signed pricing terms"


def deal(key: str) -> dict:
    return {"id": f"{key}-opportunity", "company_id": key, "name": key,
            "stage": "2. In Talks", "stage_rank": 2, "owner_code": "RG"}


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
    def __init__(self, deals, messages, insights, profiles):
        self.deals = deals
        self.messages = messages
        self.insights = insights
        self.profiles = profiles

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
        if "from opportunity_changes" in query or "from sync_runs" in query or "source = 'calendar'" in query:
            return Result()
        if "max(occurred_at) as last" in query:
            return Result([{"company_id": d["company_id"], "last": NOW - timedelta(days=2)} for d in self.deals])
        if "created_at >" in query or "occurred_at > %s" in query:
            return Result()  # The fixture has no source records in the current change window.
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


def brief_client(monkeypatch, data):
    monkeypatch.setattr(brief_api.db, "connect", lambda: nullcontext(data))
    monkeypatch.setattr(brief_api.timeline, "utcnow", lambda: NOW)
    monkeypatch.setattr(brief_api.actions, "latest", lambda *_: {})
    monkeypatch.setattr(brief_api, "_workspace_id", lambda _: "workspace")
    monkeypatch.setattr(brief_api, "_claude", lambda: None)
    return TestClient(app)


@pytest.fixture
def scoped_brief(monkeypatch):
    inbound = email("mixed-email", "mixed", f"Can you {SALES_ASK.lower()}? Please fix the sales dashboard.")
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
        insight("dashboard", "mixed", "Fix the sales dashboard", DELIVERY, origin=inbound),
        insight("incoming", "mixed", SALES_ASK, SALES, type_="next_step", origin=inbound,
                quote="Can you send the signed pricing terms?"),
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
    deals = [deal(key) for key in ("mixed", "repeat", "sales-wait", "support-wait", "unscoped")]
    deals.append({**deal("renewal"), "stage": "5. Working Client", "stage_rank": 5})
    def unavailable_plan(*_):
        raise ValueError("Simulate the planner being unavailable")

    monkeypatch.setattr(brief_api.brief, "plan_for", unavailable_plan)
    return brief_client(monkeypatch, BriefData(deals, messages, insights, profiles))


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
    # Customer-account suggestions only appear in v1: master removed v2's Customer accounts section.
    assert ("Confirm the renewal decision date" in html) == (version == "v1")
    assert "Fix the sales dashboard" not in html
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


@pytest.mark.parametrize("verification", ["off", "shadow"])
@pytest.mark.parametrize("customer_commitment", [False, True], ids=["original-ask", "customer-commitment"])
def test_waiting_on_a_reply_excludes_the_same_action_across_founder_sections(monkeypatch, verification,
                                                                           customer_commitment):
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
    client = brief_client(monkeypatch, BriefData([company], [original, latest], insights, profiles))
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


def test_waiting_row_beyond_display_cap_does_not_exclude_its_move(monkeypatch):
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
            "recommended_actions": [{"action": action, "evidence": ["I1"], "action_scope": SALES}],
        }})
    client = brief_client(monkeypatch, BriefData(deals, messages, insights, profiles))
    response = client.post("/brief/build?dry_run=true&version=v2", headers={"X-Ingest-Token": "test-token"})
    assert response.status_code == 200
    founder = response.json()["payload"]["sections"][0]
    assert [w["deal"] for w in founder["waiting"]] == ["visible"]
    assert founder["more_waiting"] == 1
    assert [(h["deal"], h["move"]["text"]) for h in founder["highlights"]] == [
        ("capped", "Customer to approve the capped proposal"),
    ]
