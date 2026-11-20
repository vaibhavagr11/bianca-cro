"""One brief, built end to end from a made-up pipeline that contains every failure fixed on 11/17/2026.

Each block of the fixture reproduces one finding from the 11/09 evals or one item from the founders' 11/12 session;
the assertions read the email a founder would get. All companies are fictional. Set BRIEF_FIXTURE_HTML to a file
path to also save the rendered email for a human to look at.
"""

import html
import os

import pytest

from app import brief_api
from test_brief_sales_scope import (  # noqa: F401
    DELIVERY, NOW, SALES, BriefData, PassingGate, _meeting, brief_client, deal, email, insight,
)

HEADERS = {"X-Ingest-Token": "test-token"}
STAGE_SITUATION = ("Cerveza Norte is in '2. In Talks' after two calls with the commercial team, who asked for a proposal "
                   "covering the three northern regions and a pilot timeline before the quarter ends. Next call pending.")


def fixture() -> BriefData:
    deals, messages, insights, profiles, meetings = [], [], [], [], []

    # Finding 13: a Situation that mentions the stage number must not be cut at "'2."
    deals.append(deal("Cerveza Norte"))
    insights.append(insight("norte-proposal", "Cerveza Norte", "Send the three-region proposal", SALES,
                            type_="next_step", dated=False))
    profiles.append({"opportunity_id": "Cerveza Norte-opportunity", "generated_at": NOW, "profile": {
        "refs": {"I1": "norte-proposal"}, "summary": {"text": STAGE_SITUATION, "evidence": ["I1"]}}})

    # Findings 11, 5 and 12: prep on every meeting, the guest's domain ties a meeting to its deal, and a meeting
    # called off after the calendar fetch is not listed.
    deals.append({**deal("Freestyle Snacks"), "company_id": "freestyle"})
    ally = {"address": "ally@freestylesnacking.com", "name": "Ally Park", "response": "accepted"}
    meetings.append(_meeting("30 Minute Call (Ramiro Godfrid)", None, [ally], hours=2))
    meetings.append(_meeting("Proposal walkthrough", "Cerveza Norte",
                             [{"address": "lu@cerveza norte.example", "name": "Lu Vega", "response": "accepted"}], hours=4))
    called_off = _meeting("Called-off demo", "Cerveza Norte", hours=5)
    called_off["meta"] = {"status": "cancelled", "cancelled_by": "mail notice"}
    meetings.append(called_off)

    # Finding 15: a vendor's help-desk ticket about a deal is not the customer replying; a real reply is.
    deals.append(deal("Delta Foods"))
    ticket = email("[DataVendor Help Center] Request #48213: store file", "Delta Foods", "Your request was updated.", days=0)
    ticket["participants"]["from"] = [{"address": "support@datavendor.zendesk.com", "name": "DataVendor Support"}]
    messages += [ticket, email("Re: store list", "Delta Foods", "Thanks, received.", days=0)]

    # B-15: a Next step the founder dated in the Sheet is due today; another is three months out.
    deals.append({**deal("Andes Dairy"), "next_step": "- Email Franco the revised deck (complete by Nov 5, 2026)"})
    deals.append({**deal("Long Lead Co"), "next_step": "- Revisit after their planning cycle (complete by Feb 17, 2027)"})

    # B-02 and B-10: what is with the customer stays in Waiting; no "keep following up" row anywhere.
    deals.append(deal("Farma Sur"))
    ask = email("farma-ask", "Farma Sur", "Can you approve the proposal?", days=14, outgoing=True)
    ask["reply_expectation"].update(evidence_quote=ask["body"], response="Customer to approve the proposal")
    messages.append(ask)
    insights += [
        insight("farma-approval", "Farma Sur", "Customer to approve the proposal", SALES, type_="next_step", origin=ask,
                quote=ask["body"], owner_side="customer", dated=False),
        insight("farma-wait", "Farma Sur", "Wait for the buyer's reply on the proposal", SALES, type_="next_step",
                dated=False),
        insight("farma-silence", "Farma Sur", "No reply since Oct 22", SALES, type_="blocker", owner_side=None, dated=False),
    ]
    profiles.append({"opportunity_id": "Farma Sur-opportunity", "generated_at": NOW, "profile": {
        "refs": {"I1": "farma-silence", "I2": "farma-wait"},
        "blockers": [{"text": "No reply since Oct 22", "evidence": ["I1"]}],
        "recommended_actions": [{"action": "Keep following up with the buyer", "evidence": ["I2"], "action_scope": SALES}]}})

    # B-11: five of our emails in a row with no reply.
    deals.append(deal("Silent Wines"))
    messages += [email(f"silent-{n}", "Silent Wines", "Can you confirm the pilot dates?", days=days, outgoing=True,
                       thread="silent-thread") for n, days in enumerate((40, 33, 26, 19, 12))]

    # B-05 / findings 7 and 10: a working client appears nowhere, whatever it has on record.
    client_co = {**deal("Koala Clean"), "stage": "5. Working client", "stage_rank": 5,
                 "next_step": "- Renewal call (complete by Nov 5, 2026)"}
    deals.append(client_co)
    insights.append(insight("client-promise", "Koala Clean", "Send the monthly delivery report", SALES))
    messages += [email("client-wait", "Koala Clean", "Can you confirm the renewal?", days=9, outgoing=True),
                 email("Client question", "Koala Clean", "Could you send the invoice?", days=0)]
    meetings.append(_meeting("Client check-in", "Koala Clean", hours=6))

    # B-07: a Pilot shows sales items only, not product work stored before items were classified.
    deals.append({**deal("Cheese Pilot"), "stage": "4. Pilot", "stage_rank": 4})
    insights += [insight("pilot-product", "Cheese Pilot", "Fix the SKU mapping for the pilot stores", None),
                 insight("pilot-delivery", "Cheese Pilot", "Reset the pilot users' passwords", DELIVERY)]

    # B-09: more candidates than a table holds.
    for n in range(1, 10):
        name = f"Busy Deal {n}"
        deals.append(deal(name))
        insights.append(insight(f"busy-{n}", name, f"Send the signed order form to {name}", SALES))

    # B-12: quiet In Talks deals come back a few at a time.
    quiet = {f"Quiet Deal {n}": days for n, days in enumerate((70, 50, 40, 30), start=1)}
    deals += [{**deal(name), "next_step": "- Reconnect after their reorg"} for name in quiet]

    data = BriefData(deals, messages, insights, profiles, meetings=meetings, domains=[
        {"domain": "delta foods.example", "company_id": "Delta Foods", "evidence_count": 9, "origin": "learned"}])
    data.last_contact = dict(quiet)
    return data


@pytest.fixture
def brief(monkeypatch, brief_client, request):
    monkeypatch.setattr(brief_api, "create_gate", lambda *_: PassingGate())
    verification = getattr(request, "param", "off")
    response = brief_client(fixture()).post(f"/brief/build?dry_run=true&version=v2&verification={verification}",
                                           headers=HEADERS)
    assert response.status_code == 200
    result = response.json()
    result["page"] = html.unescape(result["html"])
    result["founder"] = result["payload"]["sections"][0]
    return result


def test_fixture_brief_reads_right_section_by_section(brief):
    text, page, founder = brief["text"], brief["page"], brief["founder"]

    # Needs you today: the Sheet date is there, and every row shows - this table is never cut (B-15; decision 11/17).
    assert "• [Sheet date] Andes Dairy — Email Franco the revised deck (due today)" in text
    assert "Needs you today (10)" in page and len(founder["today"]) == 10 and founder["more_today"] == 0
    assert "more need you today" not in page and "more need you today" not in text
    assert "Revisit after their planning cycle" not in page
    assert "1 dated Sheet step(s) not due yet; next: Long Lead Co, Feb 17, 2027." in page

    # Deals to move: the Situation survives the stage number (finding 13) and no table is longer than 7 (B-09).
    assert "Cerveza Norte is in '2. In Talks' after two calls with the commercial team." in page
    assert "is in '2.<" not in brief["html"] and "…" not in page
    for table in page.split("<table")[1:]:                 # every table but Needs you today stops at 7 rows
        rows = table.split("</table>")[0]
        assert rows.count("<tr>") - 1 <= (10 if ">Due now<" in rows else 7)

    # Waiting on their reply: the waiting deal is only here, with the weekly nudge; five emails suggest Paused.
    assert "• Farma Sur: To Buyer · no reply yet, 2 weeks since your last email - follow up this week" in text
    assert "• Silent Wines: To Buyer · 5 emails with no reply - consider moving to Paused" in text
    assert "Farma Sur" not in {h["deal"] for h in founder["highlights"]} | {r["deal"] for r in founder["today"]}
    assert page.count("Farma Sur") == 1
    for vague in ("Keep following up", "Wait for the buyer", "No reply since"):
        assert vague not in page and vague not in text

    # Meetings: every meeting has prep, the guest's domain found the deal, the called-off one is gone.
    prep = {m["title"]: (m["deal"], m["prep"]) for m in founder["meetings"]}
    assert prep["30 Minute Call (Ramiro Godfrid)"] == (
        "Freestyle Snacks", "With Ally Park (accepted). No open item on record - last contact Nov 3. "
                            "(deal matched by guest domain freestylesnacking.com)")
    assert prep["Proposal walkthrough"] == (
        "Cerveza Norte", "With Lu Vega (accepted). Next step noted: Send the three-region proposal.")
    assert "Called-off demo" not in page and all(line for _, line in prep.values())

    # What changed: the vendor ticket is not a customer reply; the customer's own email is.
    changes = [c["text"] for c in brief["payload"]["changes"]]
    assert "Customer replied (nothing new extracted): Re: store list" in changes
    assert not any("Help Center" in c for c in changes) and "Help Center" not in page

    # Quiet In Talks deals: three today, longest-untouched first, labelled.
    assert [r["deal"] for r in founder["resurfaced"]] == ["Quiet Deal 1", "Quiet Deal 2", "Quiet Deal 3"]
    assert "Quiet In Talks deals to revisit (3 of 4)" in page
    assert "with no record in 45+ days" not in page          # the two oldest are named in the batch, not counted again

    # Working client and pilot product work: nowhere in the email or the payload.
    everything = page + text + str(brief["payload"])
    for gone in ("Koala Clean", "Renewal call", "monthly delivery report", "Client check-in", "Client question",
                 "Fix the SKU mapping", "Reset the pilot users"):
        assert gone not in everything

    if path := os.environ.get("BRIEF_FIXTURE_HTML"):
        with open(path, "w", encoding="utf-8") as out:
            out.write("<!doctype html><html><head><meta charset=\"utf-8\"><title>Daily brief fixture - 11/17/2026"
                      "</title></head><body style=\"background:#fff\">" + brief["html"] + "</body></html>")


@pytest.mark.parametrize("brief", ["shadow"], indirect=True)
def test_fixture_brief_holds_under_the_pre_send_check(brief):
    text, page, founder = brief["text"], brief["page"], brief["founder"]
    assert "• [Sheet date] Andes Dairy — Email Franco the revised deck (due today)" in text   # not sent for checking
    assert len(founder["today"]) == 10 and founder["more_today"] == 0
    assert "• Silent Wines: To Buyer · 5 emails with no reply - consider moving to Paused" in text
    assert "Farma Sur" not in {h["deal"] for h in founder["highlights"]}
    assert all(m["prep"] for m in founder["meetings"]) and "Called-off demo" not in page
    assert "Koala Clean" not in page + text and "Keep following up" not in page
    assert "Cerveza Norte is in '2. In Talks' after two calls with the commercial team." in page


def test_the_same_data_builds_the_same_brief_twice(monkeypatch, brief_client):
    """Finding 17: with no model in the loop, wording does not move between two builds of the same data."""
    first = brief_client(fixture()).post("/brief/build?dry_run=true&version=v2", headers=HEADERS).json()
    second = brief_client(fixture()).post("/brief/build?dry_run=true&version=v2", headers=HEADERS).json()
    assert first["html"] == second["html"] and first["text"] == second["text"]
