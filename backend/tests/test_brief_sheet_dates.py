"""B-15 end to end: a dated Sheet Next step comes back in "Needs you today" on its date, once."""

import html
from datetime import timedelta

import pytest

from app import brief_api
from test_brief_sales_scope import (  # noqa: F401
    NOW, SALES, BriefData, PassingGate, brief_client, deal, insight,
)

HEADERS = {"X-Ingest-Token": "test-token"}
TODAY = NOW.date()                                   # Nov 5, 2026


def sheet_deal(name: str, next_step: str, **overrides) -> dict:
    return {**deal(name), "next_step": next_step, **overrides}


def build(client, verification="off"):
    response = client.post(f"/brief/build?dry_run=true&version=v2&verification={verification}", headers=HEADERS)
    assert response.status_code == 200
    return response.json()


def today_rows(result, owner=0):
    return [(r["deal"], r["tag"], r["text"], r["when"], r["source"])
            for r in result["payload"]["sections"][owner]["today"]]


@pytest.mark.parametrize("verification", ["off", "shadow"])
def test_dated_sheet_step_is_due_on_its_date_and_overdue_after_it(monkeypatch, brief_client, verification):
    monkeypatch.setattr(brief_api, "create_gate", lambda *_: PassingGate())
    deals = [
        sheet_deal("Due Today", "- Email Franco the revised deck (complete by Nov 5, 2026)\n- Keep an eye on budget"),
        sheet_deal("Late", "- Next step: Call Ana about pricing (complete by 2 Nov 2026)"),
        sheet_deal("Long Past", "- Send the case study (complete by Oct 1, 2026)"),
        sheet_deal("Months Out", "- Revisit after their planning cycle (complete by Feb 17, 2027)"),
        sheet_deal("On Hold", "- Check back (complete by Nov 5, 2026)", stage="0. Paused", stage_rank=0),
        sheet_deal("Client", "- Renewal call (complete by Nov 5, 2026)", stage="5. Working client", stage_rank=5),
        sheet_deal("Undated", "- Email her Monday"),
    ]
    result = build(brief_client(BriefData(deals, [], [], [])), verification)
    assert today_rows(result) == [                   # what is due today first, then the late one (decision 11/17)
        ("Due Today", "Sheet date", "Email Franco the revised deck", "due today", "Sheet next step"),
        ("Late", "Sheet date", "Call Ana about pricing", "due Nov 2, 3d late", "Sheet next step"),
    ]
    section = result["payload"]["sections"][0]
    assert section["sheet_stale"] == [{"deal": "Long Past", "due": "2026-10-01"}]
    assert section["sheet_later"] == [{"deal": "Months Out", "due": "2027-02-17"}]
    body = html.unescape(result["html"])
    assert "Sheet dates long past:</b> Long Past (Oct 1) - update the Next step in the Sheet" in body
    assert "1 dated Sheet step(s) not due yet; next: Months Out, Feb 17, 2027." in body
    for hidden in ("Check back", "Renewal call", "Email her Monday", "Revisit after their planning cycle"):
        assert hidden not in body and hidden not in result["text"]
    assert "[Sheet date] Due Today — Email Franco the revised deck (due today)" in result["text"]
    # A dated deal gets no "check where this deal stands" row on top, and its Sheet row is not sent for checking.
    if verification == "off":
        assert {h["deal"] for h in section["highlights"]} == {"Long Past", "Undated"}
    noted = [deal for _, rows in section.get("verification", []) for deal, _ in rows]
    assert not {"Due Today", "Late", "Months Out"} & set(noted)


def test_a_date_months_out_brings_the_deal_back_on_that_date(monkeypatch, brief_client):
    """Ramiro's 11/12 question: "if I set a date three months out, will it come back then?" - yes."""
    client = brief_client(BriefData([sheet_deal("Months Out", "- Revisit after planning (complete by Feb 17, 2027)")],
                                    [], [], []))
    assert today_rows(build(client)) == []
    for day, expected in ((103, []), (104, "due today"), (106, "due Feb 17, 2d late"), (119, [])):
        monkeypatch.setattr(brief_api.timeline, "utcnow", lambda day=day: NOW + timedelta(days=day))
        rows = today_rows(build(client))
        assert rows == ([] if expected == [] else
                        [("Months Out", "Sheet date", "Revisit after planning", expected, "Sheet next step")])


@pytest.mark.parametrize("verification", ["off", "shadow"])
def test_sheet_step_and_its_extracted_item_are_one_row_never_two(monkeypatch, brief_client, verification):
    monkeypatch.setattr(brief_api, "create_gate", lambda *_: PassingGate())
    step = "Send the pilot terms"
    deals = [sheet_deal("Pilot Co", f"- We owe: {step}. (owner: Ramiro Godfrid) (complete by Nov 5, 2026)")]
    insights = [insight("terms", "Pilot Co", f"{step}.", SALES)]            # extracted promise, also due today
    result = build(brief_client(BriefData(deals, [], insights, [])), verification)
    [row] = result["payload"]["sections"][0]["today"]
    assert (row["tag"], row["source"], row["item_id"]) == ("Sheet date", "Sheet next step", "terms")
    for body in (html.unescape(result["html"]), result["text"]):
        assert body.count(step) == 1
    founder = result["payload"]["sections"][0]
    assert not founder["highlights"] or all(step not in h["move"]["text"] for h in founder["highlights"])
    assert not founder["followups"]


def test_extracted_item_is_held_until_the_date_the_founder_set_in_the_sheet(brief_client):
    step = "Share the rollout plan"
    other = "Send the signed order form"
    deals = [sheet_deal("Later Co", f"- Next step: {step} (complete by Dec 1, 2026)")]
    insights = [insight("plan", "Later Co", step, SALES, type_="next_step", dated=False),
                insight("form", "Later Co", other, SALES, type_="next_step", dated=False)]
    profiles = [{"opportunity_id": "Later Co-opportunity", "generated_at": NOW, "profile": {
        "refs": {"I1": "plan"}, "recommended_actions": [
            {"action": "Share the rollout plan with the buyer", "evidence": ["I1"], "action_scope": SALES}]}}]
    result = build(brief_client(BriefData(deals, [], insights, profiles)))
    body = html.unescape(result["html"])
    assert step not in body and step not in result["text"]          # not as an item, not as a suggestion
    assert other in body                                            # an unrelated step on the deal still shows
    assert "1 dated Sheet step(s) not due yet (1 matching item(s) held back until then); next: Later Co, Dec 1." in body


@pytest.mark.parametrize("how", ["item marked done", "sheet row marked done", "check suggests done"])
def test_a_closed_step_is_not_raised_again_from_the_sheet(monkeypatch, brief_client, how):
    step = "Send the pilot terms"
    deals = [sheet_deal("Pilot Co", f"- We owe: {step}. (complete by Nov 5, 2026)")]
    insights = []
    if how != "sheet row marked done":
        row = insight("terms", "Pilot Co", f"{step}.", SALES)
        row["status"] = "done" if how == "item marked done" else "possibly_done"
        insights.append(row)
    else:
        from app.brief_sheet_steps import match_key
        ref = brief_api._recommendation_ref("Pilot Co-opportunity", f"sheet step: {match_key(step)} {TODAY.isoformat()}")
        monkeypatch.setattr(brief_api.actions, "latest", lambda *_: {("rec", ref): ("done", NOW)})
    result = build(brief_client(BriefData(deals, [], insights, [])))
    assert today_rows(result) == [] and step not in result["html"]


def test_sheet_row_has_a_done_button_and_ndl_deals_use_the_shared_table(monkeypatch, brief_client):
    monkeypatch.setattr(brief_api.settings, "brief_action_secret", __import__("pydantic").SecretStr("secret"))
    deals = [sheet_deal("Mine", "- Call Franco (complete by Nov 5, 2026)"),
             sheet_deal("Third Founder", "- Send the NDA (complete by Nov 4, 2026)", owner_code="NDL")]
    result = build(brief_client(BriefData(deals, [], [], [])))
    assert today_rows(result) == [("Mine", "Sheet date", "Call Franco", "due today", "Sheet next step")]
    assert result["payload"]["ndl"] == [{"deal": "Third Founder", "text": "Send the NDA", "when": "due Nov 4, 1d late"}]
    assert result["html"].count("✓ Done") == 2
