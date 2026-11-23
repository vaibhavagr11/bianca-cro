"""Review semantics, stable evidence, batching and durable external-write recovery. No live I/O."""

import copy
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest

from app import review_service as review
from app.review_content import bullet_id
from app.review_sheets import SheetConflict, SheetWriteUncertain


NOW = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
WORKSPACE = "11111111-1111-1111-1111-111111111111"
PROPOSAL = "22222222-2222-2222-2222-222222222222"
COMPANY = "33333333-3333-3333-3333-333333333333"
OPPORTUNITY = "44444444-4444-4444-4444-444444444444"
RECORD = "55555555-5555-5555-5555-555555555555"


def record(**overrides):
    return {"id": RECORD, "source": "gmail", "external_id": "message-1", "thread_id": "thread-1",
            "content_hash": "body-hash", "occurred_at": NOW, "created_at": NOW, "subject": "Pilot next steps",
            "body": "We will send the pilot checklist by Thursday.", "meta": {},
            "classification": {"is_commercial": True, "evidence_quote": "send the pilot checklist"}, **overrides}


def insight(**overrides):
    return {"id": "ephemeral-insight-1", "source_record_ids": [RECORD], "type": "commitment",
            "text": "Send the pilot checklist.", "quote": "We will send the pilot checklist by Thursday.",
            "owner_side": "bianca", "owner_name": "Nicolás de León", "due_date": "2026-11-05",
            "occurred_at": NOW, "status": "open", **overrides}


def insight_for_record(material):
    if material["body"] == record()["body"]:
        return insight(source_record_ids=[material["id"]], occurred_at=material["occurred_at"])
    return insight(id=f"insight-{material['id']}", source_record_ids=[material["id"]],
                   origin_record_id=material["id"], action_scope="deal_progression", text=material["body"],
                   quote=material["body"], occurred_at=material["occurred_at"], owner_name=None, due_date=None)


def proposal(**overrides):
    version = review.source_version(record())
    next_steps, context, sources, lineage = review.proposal_content([record()], [insight()])
    value = {"id": PROPOSAL, "workspace_id": WORKSPACE, "company_id": COMPANY, "opportunity_id": OPPORTUNITY,
             "company_name": "Pernod Ricard", "owner_code": "NDL", "stage": "4. Pilot", "country": "ARG",
             "bullets": next_steps + context, "next_step_bullets": next_steps,
             "client_context_bullets": context, "routing_version": 1, "snapshot_client_context": "", "sources": sources,
             "source_versions": [version], "observed_source_versions": [version], "lineage_version": 1,
             "bullet_lineage": lineage,
             "snapshot_last_progress": "Pilot terms agreed.", "snapshot_next_step": "Ask for the signed agreement.",
             "revision": 1, "status": "pending", "created_at": NOW, "updated_at": NOW, "removed_at": None,
             "current_stage": "4. Pilot", "current_company_name": "Pernod Ricard"}
    value.update(overrides)
    return value


def test_source_identity_survives_reextraction_and_mailbox_aliases_but_not_real_calendar_changes():
    original = record(source="calendar", meta={"status": "confirmed"})
    version = review.source_version(original)
    assert review.source_version({**original, "id": "replacement-id", "updated_at": NOW,
                                  "extracted_version": "extract-v4", "meta": {"status": "confirmed", "mailboxes": ["RG", "NC"]}}) == version
    assert review.source_version({**original, "meta": {"status": "cancelled"}}) != version
    assert review.source_version({**original, "content_hash": "changed-body"}) != version


def test_existing_insight_wording_owner_and_due_date_are_preserved_and_quoted():
    bullets, _, sources, _ = review.proposal_content([record()], [insight()])
    assert bullets == ["We owe: Send the pilot checklist. (owner: Nicolás de León) — due 2026-11-05"]
    assert sources[0]["quote"] == insight()["quote"]
    assert sources[0]["date"] == "2026-11-02"
    assert sources[0]["recordedAt"] == NOW.isoformat()


def test_old_thread_context_is_not_repeated_when_only_a_new_message_needs_review():
    new_message = record(body="Thanks, that works.")
    bullets, _, _, _ = review.proposal_content([new_message], [insight(source_record_ids=["older-record", RECORD])])
    assert not bullets


def test_calendar_title_only_cannot_become_a_buying_signal_or_completed_meeting():
    meeting = record(source="calendar", body="", subject="Pilot purchase approved", occurred_at="2026-11-05T12:00:00Z")
    bullets, context, _, _ = review.proposal_content([meeting], [insight(type="buying_signal", quote="Pilot purchase approved",
                                                                    text="Customer approved the purchase.")])
    assert not bullets and not context


def test_cancelled_calendar_record_does_not_create_a_sheet_update():
    bullets, _, sources, _ = review.proposal_content([record(source="calendar", meta={"status": "cancelled"})], [])
    assert not bullets and not sources


def test_completed_or_unverifiable_insights_are_not_presented_as_open_actions():
    for change in ({"status": "done"}, {"status": "possibly_done"}, {"quote": "This sentence is invented."}):
        bullets, _, _, _ = review.proposal_content([record()], [insight(**change)])
        assert all(not b.startswith("We owe:") for b in bullets)


def test_duplicate_insights_do_not_duplicate_bullets_and_no_four_deal_selector_is_used():
    bullets, _, _, _ = review.proposal_content([record()], [insight(), insight(id="new-insight-uuid")])
    assert len(bullets) == 1


def test_bullets_are_literal_lines_and_empty_or_oversized_updates_fail_before_io():
    assert review.clean_bullets(["• Share the checklist.", "=HYPERLINK(\"example\")"]) == [
        "Share the checklist.", '=HYPERLINK("example")']
    for invalid in ([], [" "], [None], ["one\ntwo"], ["x" * 49_001]):
        with pytest.raises(ValueError):
            review.clean_bullets(invalid)


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class Store:
    def __init__(self, item=None):
        self.data = {"proposal": item or proposal(), "intents": [], "receipts": []}
        self.events = []
        self.locked = True
        self.fail_finalize = False
        self.source_record = record()
        self.insights = [insight()]

    def connect(self):
        return DecisionConnection(self)


class DecisionConnection:
    """A transactional in-memory boundary; only the service's SQL writes mutate it."""
    def __init__(self, store):
        self.store, self.data = store, copy.deepcopy(store.data)

    def __enter__(self):
        return self

    def __exit__(self, typ, value, tb):
        if typ:
            self.rollback()
        else:
            self.commit()

    def commit(self):
        self.store.events.append("commit")
        self.store.data = copy.deepcopy(self.data)

    def rollback(self):
        self.store.events.append("rollback")
        self.data = copy.deepcopy(self.store.data)

    def execute(self, sql, params=()):
        q = " ".join(sql.split())
        p = self.data["proposal"]
        if "pg_try_advisory_lock" in q:
            return Result([{"locked": self.store.locked}])
        if q.startswith("select p.*"):
            row = {**p, "current_stage": p.get("current_stage", p["stage"]),
                   "current_company_name": p.get("current_company_name", p["company_name"])}
            return Result([copy.deepcopy(row)] if str(p["workspace_id"]) == str(params[0]) and str(p["id"]) == params[1] else [])
        if q.startswith("select * from source_records"):
            return Result([self.store.source_record])
        if q.startswith("select * from insights"):
            return Result([i for i in self.store.insights if set(i["source_record_ids"]) & set(params[-1])])
        if q.startswith("select * from review_decisions"):
            return Result([copy.deepcopy(i) for i in self.data["intents"] if i["idempotency_key"] == params[1]])
        if q.startswith("insert into review_decisions"):
            keys = ["workspace_id", "proposal_id", "actor", "action", "revision", "idempotency_key", "request_hash",
                    "bullets", "before_cells", "after_cells", "edited", "status",
                    "next_step_bullets", "client_context_bullets", "routing_version", "sheet_target", "bullet_lineage", "lineage_version"]
            item = {key: copy.deepcopy(value.obj if hasattr(value, "obj") else value) for key, value in zip(keys, params)}
            item.update(id=f"decision-{len(self.data['intents']) + 1}", created_at=NOW, completed_at=None, error=None, write_attempts=0)
            self.data["intents"].append(item)
            return Result([copy.deepcopy(item)])
        if q.startswith("update review_decisions"):
            item = next(i for i in self.data["intents"] if i["id"] == params[-1])
            if "status = 'succeeded'" in q:
                if self.store.fail_finalize:
                    raise OSError("simulated database failure after sheet write")
                item.update(status="succeeded", error=None, completed_at=NOW, mirror_refreshed=params[0])
            elif "status = 'conflict'" in q:
                item.update(status="conflict", error=params[0], completed_at=NOW)
            elif "set completed_at" in q:
                item["completed_at"] = NOW
            elif "set error" in q:
                item["error"] = params[0]
            elif "set write_attempts" in q:
                item["write_attempts"] += 1
            return Result([copy.deepcopy(item)])
        if q.startswith("update review_proposals"):
            if "status = 'applying'" in q:
                p["status"] = "applying"
            if "set status = 'pending'" in q:
                p["status"] = "pending"
            if "status = 'ignored'" in q:
                p.update(status="ignored", decided_by=params[0], decided_at=NOW)
            if "status = 'approved'" in q:
                p.update(status="approved", bullets=params[0].obj, next_step_bullets=params[1].obj,
                         client_context_bullets=params[2].obj, bullet_lineage=params[3].obj, lineage_version=params[4],
                         decided_by=params[5], decided_at=NOW)
            return Result([])
        if q.startswith("insert into review_source_receipts"):
            self.data["receipts"].append(params)
            return Result([])
        raise AssertionError("Unmodeled service query: " + q)


class FakeSheets:
    def __init__(self, store):
        self.store = store
        self.last_progress = store.data["proposal"]["snapshot_last_progress"]
        self.next_step = store.data["proposal"]["snapshot_next_step"]
        self.client_context = store.data["proposal"]["snapshot_client_context"]
        self.calls = []
        self.uncertain = False
        self.conflict_on_write = False
        self.status = "4. Pilot"
        self.flip_on_apply = False

    def read_company(self, company):
        self.store.events.append("sheet-read")
        return SimpleNamespace(last_progress=self.last_progress, next_step=self.next_step, client_context=self.client_context,
                               status=self.status)

    def read_company_statuses(self):
        self.store.events.append("status-read")
        return {"pernod ricard": self.status}

    def apply_update(self, company, old_last, old_next, new_last, new_next, *, expected_client_context, new_client_context,
                     allow_write=True, recovering=False):
        current, before, after = ((self.last_progress, self.next_step, self.client_context),
                                  (old_last, old_next, expected_client_context), (new_last, new_next, new_client_context))
        if self.flip_on_apply:
            self.status = "5. Working client"
        if not allow_write or review.is_working_client(self.status):
            if recovering and current == after:
                return self.read_company(company)
            raise review.SheetWorkingClient("outside sales")
        self.store.events.append("sheet-write")
        # Approval must be durably recorded before leaving the process for Google.
        saved = self.store.data["intents"][-1]
        assert saved["status"] == "applying"
        assert saved["before_cells"] == {"lastProgress": old_last, "nextStep": old_next, "clientContext": expected_client_context}
        assert saved["after_cells"] == {"lastProgress": new_last, "nextStep": new_next, "clientContext": new_client_context}
        self.calls.append((company, old_last, old_next, new_last, new_next, expected_client_context, new_client_context))
        if self.conflict_on_write:
            raise SheetConflict("changed")
        if current == after:
            return self.read_company(company)
        if current != before:
            raise SheetConflict("changed")
        self.last_progress, self.next_step, self.client_context = after
        if self.uncertain:
            self.uncertain = False
            raise SheetWriteUncertain("write response lost")
        return self.read_company(company)


@pytest.fixture
def service(monkeypatch):
    store = Store()
    sheets = FakeSheets(store)
    monkeypatch.setattr(review, "settings", SimpleNamespace(
        review_spreadsheet_id="14lXRsWoEY63tOsz4SwggiItgTNPFWI4KDVTLay3MLDw",
        review_sheet_tab="Pipeline", review_sheet_id=1869050409, review_client_context_mode="append", review_build_batch=20))
    monkeypatch.setattr(review.db, "connect", store.connect)
    monkeypatch.setattr(review, "SheetsClient", lambda: sheets)
    monkeypatch.setattr(review, "_mirror_approval", lambda *args: True)
    return store, sheets


def approve(bullets=None, **kwargs):
    return review.decide(WORKSPACE, PROPOSAL, "NDL", "approve", 1, "approval-key-1", next_step_bullets=bullets, **kwargs)


@pytest.mark.parametrize("actor", ["NDL", "MG"])
def test_approval_replaces_last_progress_exactly_and_updates_only_after_confirmed_write(service, actor):
    store, sheets = service
    old_last, old_next = sheets.last_progress, sheets.next_step
    result = review.decide(WORKSPACE, PROPOSAL, actor, "approve", 1, "approval-key-1")
    assert sheets.last_progress == old_next
    assert sheets.last_progress != old_last
    assert sheets.next_step == "- " + proposal()["next_step_bullets"][0]
    assert store.data["proposal"]["status"] == "approved"
    assert result["activity"]["actor"] == actor
    assert result["activity"]["before"] == {"lastProgress": old_last, "nextStep": old_next, "clientContext": ""}
    assert len(store.data["receipts"]) == 1
    assert store.events.index("commit", 1) < store.events.index("sheet-write")


def test_edited_approval_is_audited_and_duplicate_key_does_not_rotate_twice(service):
    store, sheets = service
    result = approve(["Send the revised scope.", "Confirm the kickoff."])
    assert result["activity"]["edited"] is True
    assert approve(["Send the revised scope.", "Confirm the kickoff."])["activity"] == result["activity"]
    assert len(sheets.calls) == 1
    with pytest.raises(review.ReviewConflict):
        approve(["A different decision using the old key."])


@pytest.mark.parametrize("timing,deadline,edited", [
    ("by Nov 21", " (complete by Nov 21, 2026)", False),
    ("next Friday", " (complete by Nov 6, 2026)", False),
    ("by Nov 21", " (complete by Nov 22, 2026)", True),
    ("by Nov 21", "", True),
])
def test_approved_deadline_preserves_provenance_only_for_the_automatic_format(service, timing, deadline, edited):
    store, sheets = service
    action = f"The coordinator should following up with Morgan {timing}, since the team is waiting for a response."
    store.source_record = record(body=action)
    store.insights = [insight(type="next_step", text=action, quote=action, action_scope="deal_progression",
                              owner_name=None, due_date=None, origin_record_id=RECORD)]
    next_steps, context, sources, lineage = review.proposal_content([store.source_record], store.insights)
    version = review.source_version(store.source_record)
    store.data["proposal"].update(
        bullets=next_steps + context, next_step_bullets=next_steps, client_context_bullets=context,
        sources=sources, source_versions=[version], observed_source_versions=[version], bullet_lineage=lineage,
    )
    approved_bullet = (
        "Next step: The coordinator should keep following up with Morgan, since the team is waiting for a response."
        + deadline
    )

    result = approve([approved_bullet])

    assert sheets.next_step == "- " + approved_bullet
    assert result["activity"]["edited"] is edited
    assert result["activity"]["bulletLineage"]["nextStep"][0] == {
        "bulletId": bullet_id("nextStep", approved_bullet),
        "text": approved_bullet,
        "origin": "founder_edit" if edited else "source",
        "sourceIds": [] if edited else lineage["nextStep"][0]["sourceIds"],
    }


def test_ignore_only_reads_live_status_and_consumes_source_versions(service):
    store, sheets = service
    result = review.decide(WORKSPACE, PROPOSAL, "RG", "ignore", 1, "ignored-key-1")
    assert result["status"] == "ignored"
    assert store.data["proposal"]["status"] == "ignored"
    assert result["activity"]["before"] == result["activity"]["after"]
    assert store.data["receipts"]
    assert "status-read" in store.events and "sheet-read" not in store.events
    assert not sheets.calls


def test_empty_old_next_step_replaces_last_progress_with_an_empty_cell(service):
    store, sheets = service
    store.data["proposal"]["snapshot_next_step"] = sheets.next_step = ""
    approve()
    assert sheets.last_progress == ""


def test_live_whitespace_is_preserved_in_rotation_while_matching_ingest_normalization(service):
    _, sheets = service
    sheets.next_step = "  Ask for the signed agreement.\n"
    approve()
    assert sheets.last_progress == "  Ask for the signed agreement.\n"


def test_changed_sheet_or_stale_revision_never_creates_a_write_intent(service):
    store, sheets = service
    sheets.next_step = "A colleague already changed this."
    with pytest.raises(review.ReviewConflict, match="Sheet row changed"):
        approve()
    assert not sheets.calls
    assert not store.data["intents"]
    store.data["proposal"]["revision"] = 2
    with pytest.raises(review.ReviewConflict, match="newer update"):
        approve()


def test_changed_source_material_blocks_a_stale_card_even_before_the_next_builder_run(service):
    store, sheets = service
    store.source_record = record(content_hash="corrected-message")
    with pytest.raises(review.ReviewConflict, match="source material changed"):
        approve()
    assert not sheets.calls
    assert not store.data["intents"]


@pytest.mark.parametrize("edited", [False, True])
def test_delivery_reclassification_blocks_a_stale_card_before_a_sheet_read(service, edited):
    store, sheets = service
    store.insights[0]["action_scope"] = "delivery"
    with pytest.raises(review.ReviewConflict, match="sales evidence changed"):
        approve(["Edited next step."] if edited else None)
    assert "sheet-read" not in store.events
    assert not sheets.calls and not store.data["intents"] and not store.data["receipts"]


def test_uncertain_write_retains_fixed_intent_and_same_key_retry_recovers_without_rotation(service):
    store, sheets = service
    old_next = sheets.next_step
    sheets.uncertain = True
    with pytest.raises(review.ReviewUnavailable):
        approve(["Edited next action."])
    assert store.data["proposal"]["status"] == "applying"
    assert store.data["intents"][0]["status"] == "applying"
    assert not store.data["receipts"]
    assert sheets.last_progress == old_next
    store.insights[0]["action_scope"] = "delivery"
    with pytest.raises(review.ReviewConflict):
        review.decide(WORKSPACE, PROPOSAL, "NC", "approve", 1, "different-key", next_step_bullets=["Edited next action."])
    result = approve(["Edited next action."])
    assert result["status"] == "approved"
    assert sheets.last_progress == old_next
    assert sheets.next_step == "- Edited next action."
    assert sheets.calls[0] == sheets.calls[1]


def test_database_failure_after_google_success_leaves_recoverable_intent(service):
    store, sheets = service
    store.fail_finalize = True
    with pytest.raises(review.ReviewUnavailable, match="needs confirmation"):
        approve()
    assert store.data["proposal"]["status"] == "applying"
    assert store.data["intents"][0]["status"] == "applying"
    after = (sheets.last_progress, sheets.next_step)
    store.fail_finalize = False
    assert approve()["status"] == "approved"
    assert (sheets.last_progress, sheets.next_step) == after


def test_uncertain_write_followed_by_a_human_edit_remains_reserved_for_reconciliation(service):
    store, sheets = service
    sheets.uncertain = True
    with pytest.raises(review.ReviewUnavailable):
        approve()
    sheets.next_step = "A later human edit that must be preserved."
    with pytest.raises(review.ReviewUnavailable, match="reconcile"):
        approve()
    assert store.data["proposal"]["status"] == "applying"
    assert store.data["intents"][0]["status"] == "applying"
    assert sheets.next_step == "A later human edit that must be preserved."
    assert not store.data["receipts"]


def test_completed_retry_cannot_resurface_a_company_if_live_status_is_unavailable(service, monkeypatch):
    store, sheets = service
    approve()
    def unavailable():
        raise review.SheetError("missing credentials")
    monkeypatch.setattr(review, "SheetsClient", unavailable)
    with pytest.raises(review.ReviewUnavailable, match="could not be read"):
        approve()
    assert store.data["intents"][0]["status"] == "succeeded"
    assert len(sheets.calls) == 1


def test_missing_credentials_before_first_approval_does_not_save_an_intent(service, monkeypatch):
    store, _ = service
    def unavailable():
        raise review.SheetError("missing credentials")
    monkeypatch.setattr(review, "SheetsClient", unavailable)
    with pytest.raises(review.ReviewUnavailable, match="could not be read"):
        approve()
    assert store.data["proposal"]["status"] == "pending"
    assert not store.data["intents"]


def test_conflict_between_review_and_write_keeps_card_pending_and_does_not_consume_sources(service):
    store, sheets = service
    sheets.conflict_on_write = True
    with pytest.raises(review.ReviewConflict):
        approve()
    assert store.data["proposal"]["status"] == "pending"
    assert store.data["intents"][0]["status"] == "conflict"
    assert not store.data["receipts"]


def test_lock_prevents_simultaneous_founder_write_and_wrong_workspace_cannot_read_proposal(service):
    store, sheets = service
    store.locked = False
    with pytest.raises(review.ReviewConflict, match="already being processed"):
        approve()
    assert not sheets.calls
    store.locked = True
    with pytest.raises(review.ReviewNotFound):
        review.decide("different-workspace", PROPOSAL, "NDL", "approve", 1, "approval-key-1")


class BuildConnection:
    def __init__(self, count):
        self.deals = [{"opportunity_id": f"opportunity-{i}", "company_id": f"company-{i}",
                       "company_name": f"Company {i:02}", "owner_code": "NDL", "stage": "2. In Talks",
                       "country": "ARG", "last_progress": "Old progress", "next_step": "Old next step", "client_context": ""}
                      for i in range(count)]
        self.proposals, self.receipts, self.exclusions = {}, [], []
        self.records = {deal["company_id"]: [record(external_id=deal["company_id"], company_id=deal["company_id"])]
                        for deal in self.deals}
        self.unlinked_records = []
        self.records_enabled = True
        self.queue_state = None
        self.now = NOW
        self.timezone = "America/Argentina/Buenos_Aires"
        self.insights = None

    def execute(self, sql, params=()):
        q = " ".join(sql.split())
        if q == "select to_regclass('review_selection_cache') as found":
            return Result([{"found": None}])
        if "pg_advisory_xact_lock" in q:
            return Result([])
        if q.startswith("select now()"):
            return Result([{"now": self.now, "timezone": self.timezone}])
        if q.startswith("select * from review_queue_state"):
            return Result([copy.deepcopy(self.queue_state)] if self.queue_state else [])
        if q.startswith("insert into review_queue_state"):
            self.queue_state = dict(zip(["workspace_id", "initialized_at", "bootstrap_cutoff", "cycle_for_date", "cycle_started_at"], params))
            self.queue_state.update(bootstrap_completed=False, cycle_completed_at=None, last_completed_on=None)
            return Result([copy.deepcopy(self.queue_state)])
        if q.startswith("update review_queue_state"):
            if "cycle_completed_at = now()" in q:
                self.queue_state.update(cycle_completed_at=self.now, last_completed_on=self.queue_state["cycle_for_date"], bootstrap_completed=True)
            else:
                self.queue_state.update(cycle_for_date=params[0], cycle_started_at=params[1], cycle_completed_at=None)
            return Result([copy.deepcopy(self.queue_state)])
        if q.startswith("insert into review_bootstrap_exclusions"):
            self.exclusions.extend({"workspace_id": params[0], **row} for row in params[1].obj)
            return Result([])
        if q.startswith("select source, external_id, version from review_bootstrap_exclusions"):
            return Result(copy.deepcopy(self.exclusions))
        if q.startswith("select o.id as opportunity_id"):
            return Result(copy.deepcopy(self.deals))
        if q.startswith("select company_id, source"):
            return Result(self.receipts)
        if q.startswith("select * from review_proposals"):
            item = self.proposals.get(params[1])
            return Result([copy.deepcopy(item)] if item and item["status"] in ("pending", "applying") else [])
        if q.startswith("select * from source_records") or q.startswith("select source, external_id, content_hash"):
            material = ([r for values in self.records.values() for r in values] + self.unlinked_records
                        if len(params) == 2 else self.records.get(params[1], []) if self.records_enabled else [])
            return Result(copy.deepcopy([r for r in material if review._instant(r["created_at"]) <= params[-1]]))
        if q.startswith("select * from insights"):
            insights = (self.insights if self.insights is not None else
                        [insight_for_record(r) for r in self.records[params[1]]])
            return Result(copy.deepcopy([i for i in insights if set(i["source_record_ids"]) & set(params[-1])]))
        if q.startswith("insert into review_proposals"):
            keys = ["workspace_id", "company_id", "opportunity_id", "company_name", "owner_code", "stage", "country",
                    "bullets", "sources", "source_versions", "snapshot_last_progress", "snapshot_next_step",
                    "snapshot_client_context", "next_step_bullets", "client_context_bullets", "routing_version",
                    "observed_source_versions", "bullet_lineage", "lineage_version"]
            item = {k: copy.deepcopy(v.obj if hasattr(v, "obj") else v) for k, v in zip(keys, params)}
            item.update(id="proposal-" + params[1], status="pending", revision=1)
            self.proposals[params[1]] = item
            return Result([])
        if q.startswith("update review_proposals"):
            if "status = 'superseded'" in q:
                item = next(p for p in self.proposals.values() if p["id"] == params[0])
                item["status"] = "superseded"
                item["revision"] += 1
                return Result([])
            keys = ["company_name", "owner_code", "stage", "country", "bullets", "sources", "source_versions",
                    "snapshot_last_progress", "snapshot_next_step", "snapshot_client_context",
                    "next_step_bullets", "client_context_bullets", "routing_version", "observed_source_versions", "bullet_lineage", "lineage_version"]
            item = next(p for p in self.proposals.values() if p["id"] == params[-1])
            item.update({k: copy.deepcopy(v.obj if hasattr(v, "obj") else v) for k, v in zip(keys, params)})
            item["revision"] += 1
            return Result([])
        raise AssertionError("Unmodeled builder query: " + q)


def test_builder_covers_all_companies_in_batches_and_unchanged_reruns_do_not_duplicate(monkeypatch):
    monkeypatch.setattr(review, "settings", SimpleNamespace(review_build_batch=20))
    conn = BuildConnection(23)
    first = review.build_proposals(conn, WORKSPACE)
    assert first["created"] == 20 and first["remaining"] == 3 and first["done"] is False
    second = review.build_proposals(conn, WORKSPACE)
    assert second["created"] == 3 and second["remaining"] == 0 and second["done"] is True
    assert len(conn.proposals) == 23
    assert all(p["owner_code"] == "NDL" for p in conn.proposals.values())
    assert review.build_proposals(conn, WORKSPACE)["created"] == 0
    assert all(p["revision"] == 1 for p in conn.proposals.values())


def test_builder_does_not_recreate_ignored_or_approved_source_versions(monkeypatch):
    monkeypatch.setattr(review, "settings", SimpleNamespace(review_build_batch=20))
    conn = BuildConnection(1)
    version = review.source_version(record(external_id="company-0"))
    conn.receipts = [{"company_id": "company-0", **version}]
    assert review.build_proposals(conn, WORKSPACE)["created"] == 0
    assert not conn.proposals


def test_builder_withdraws_working_client_pending_cards_without_consuming_evidence(monkeypatch):
    monkeypatch.setattr(review, "settings", SimpleNamespace(review_build_batch=20))
    conn = BuildConnection(3)
    conn.deals[1]["stage"] = "5. Working client"
    assert review.build_proposals(conn, WORKSPACE)["created"] == 2
    assert "company-1" not in conn.proposals
    conn.deals[0]["stage"] = "5. Working client"
    assert review.build_proposals(conn, WORKSPACE, force=True)["updated"] == 1
    assert conn.proposals["company-0"]["status"] == "superseded"
    assert conn.proposals["company-2"]["status"] == "pending"
    assert not conn.receipts
    conn.deals[0]["stage"] = "4. Pilot"
    assert review.build_proposals(conn, WORKSPACE, force=True)["created"] == 1
    assert conn.proposals["company-0"]["sources"]


def test_pending_card_revision_tracks_changed_sheet_baseline_without_losing_new_sources(monkeypatch):
    monkeypatch.setattr(review, "settings", SimpleNamespace(review_build_batch=20))
    conn = BuildConnection(1)
    review.build_proposals(conn, WORKSPACE)
    conn.deals[0]["next_step"] = "A fresh manually edited baseline"
    result = review.build_proposals(conn, WORKSPACE, force=True)
    assert result["updated"] == 1
    assert conn.proposals["company-0"]["revision"] == 2
    assert conn.proposals["company-0"]["snapshot_next_step"] == conn.deals[0]["next_step"]


def test_applying_card_is_never_rebuilt_while_external_result_is_unknown(monkeypatch):
    monkeypatch.setattr(review, "settings", SimpleNamespace(review_build_batch=20))
    conn = BuildConnection(1)
    review.build_proposals(conn, WORKSPACE)
    before = copy.deepcopy(conn.proposals["company-0"])
    conn.proposals["company-0"]["status"] = "applying"
    assert review.build_proposals(conn, WORKSPACE, force=True)["applying"] == 1
    assert conn.proposals["company-0"]["revision"] == before["revision"]


@pytest.mark.parametrize("scope_changed", [False, True])
def test_no_current_sales_evidence_withdraws_a_card_without_consuming_its_sources(monkeypatch, scope_changed):
    monkeypatch.setattr(review, "settings", SimpleNamespace(review_build_batch=20))
    conn = BuildConnection(1)
    review.build_proposals(conn, WORKSPACE)
    if scope_changed:
        conn.insights = [insight(action_scope="out_of_scope")]
    else:
        conn.records_enabled = False
    assert review.build_proposals(conn, WORKSPACE, force=True)["updated"] == 1
    assert conn.proposals["company-0"]["status"] == "superseded"
    assert conn.proposals["company-0"]["revision"] == 2
    assert not conn.receipts


def test_mirror_refresh_recomputes_pipeline_hash_and_only_updates_reviewed_cells():
    class MirrorConnection:
        def __init__(self):
            self.writes = []
        def execute(self, sql, params=()):
            if sql.startswith("select o.*"):
                return Result([{"id": OPPORTUNITY, "name": "Pernod Ricard", "country": "ARG", "owner_code": "NDL",
                                "stage": "4. Pilot", "primary_buyer": "Commercial", "contacts_text": "Tomás",
                                "last_progress": "Old progress", "next_step": "Old next step", "client_context": "", "removed_at": None}])
            self.writes.append((sql, params))
            return Result([{"id": "sync-run"}]) if "returning id" in sql else Result([])
    conn = MirrorConnection()
    assert review._mirror_approval(conn, proposal(), {"lastProgress": "Old progress", "nextStep": "Old next step", "clientContext": ""},
                                   {"lastProgress": "Old next step", "nextStep": "- Confirm launch.", "clientContext": ""}) is True
    sql, values = conn.writes[0]
    assert sql.startswith("update opportunities set last_progress")
    assert values[:2] == ("Old next step", "- Confirm launch.")
    assert values[2] is None
    assert len(values[3]) == 64
    assert any("opportunity_changes" in query for query, _ in conn.writes)


def test_mirror_refresh_never_overwrites_a_newer_pipeline_sync():
    class ChangedMirror:
        def execute(self, sql, params=()):
            assert sql.startswith("select o.*"), "No UPDATE may run when the cached row already changed."
            return Result([{"last_progress": "Newer progress", "next_step": "A newer synced next step", "removed_at": None}])
    assert review._mirror_approval(ChangedMirror(), proposal(),
                                  {"lastProgress": "Old progress", "nextStep": "Old next step", "clientContext": ""},
                                  {"lastProgress": "Old next step", "nextStep": "- Confirm launch.", "clientContext": ""}) is False


def test_removed_company_cannot_be_approved(service):
    store, sheets = service
    store.data["proposal"]["removed_at"] = NOW
    with pytest.raises(review.ReviewConflict, match="no longer"):
        approve()
    assert not sheets.calls
    assert not store.data["intents"]


@pytest.mark.parametrize("action", ["approve", "ignore"])
@pytest.mark.parametrize("status_source", ["synced", "live"])
def test_working_client_stale_decision_is_rejected_before_intent_or_write(service, action, status_source):
    store, sheets = service
    if status_source == "synced":
        store.data["proposal"]["current_stage"] = "5. Working client"
    else:
        sheets.status = "5. Working client"
    with pytest.raises(review.ReviewConflict, match="outside the sales pipeline"):
        review.decide(WORKSPACE, PROPOSAL, "RG", action, 1, "stale-decision")
    assert not (sheets.calls or store.data["intents"] or store.data["receipts"])


def test_working_client_status_flip_between_prepare_and_apply_stops_the_write(service):
    store, sheets = service
    sheets.flip_on_apply = True
    with pytest.raises(review.ReviewConflict, match="outside the sales pipeline"):
        approve()
    assert store.data["intents"][0]["status"] == "conflict"
    assert not sheets.calls and not store.data["receipts"]


@pytest.mark.parametrize("confirmed", [False, True])
def test_working_client_approval_recovery_never_writes_or_surfaces_data(service, confirmed):
    store, sheets = service
    sheets.uncertain = True
    with pytest.raises(review.ReviewUnavailable):
        approve()
    if not confirmed:
        sheets.last_progress = store.data["proposal"]["snapshot_last_progress"]
        sheets.next_step = store.data["proposal"]["snapshot_next_step"]
    sheets.status = "5. Working client"
    with pytest.raises(review.ReviewConflict, match="outside the sales pipeline"):
        approve()
    assert len(sheets.calls) == 1
    assert store.data["intents"][0]["status"] == ("succeeded" if confirmed else "applying")
    assert bool(store.data["receipts"]) is confirmed


def test_completed_approval_retry_cannot_resurface_working_client(service):
    _, sheets = service
    approve()
    sheets.status = "5. Working client"
    with pytest.raises(review.ReviewConflict, match="outside the sales pipeline"):
        approve()
    assert len(sheets.calls) == 1


def test_insight_categories_route_to_separate_columns_without_inference_from_text():
    text = "The customer requires reporting. They asked about pricing. Security review is a concern. Legal approval blocks the rollout. Send the checklist. We will share data."
    material = record(body=text)
    facts = [
        insight(type="need", text="We owe: reporting capability is needed.", quote="The customer requires reporting.", owner_side=None, owner_name=None, due_date=None),
        insight(type="buying_signal", text="Pricing discussion is underway.", quote="They asked about pricing.", owner_side=None, owner_name=None, due_date=None),
        insight(type="objection", text="Security review is a concern.", quote="Security review is a concern.", owner_side=None, owner_name=None, due_date=None),
        insight(type="blocker", text="Customer need: legal approval is missing.", quote="Legal approval blocks the rollout.", owner_side=None, owner_name=None, due_date=None),
        insight(type="next_step", text="Send the checklist.", quote="Send the checklist.", owner_side="bianca", owner_name=None, due_date=None),
        insight(type="commitment", text="Share data.", quote="We will share data.", owner_side="customer", owner_name=None, due_date=None),
    ]
    next_steps, context, sources, lineage = review.proposal_content([material], facts)
    assert len(next_steps) == len(context) == 3
    assert "Blocker: Customer need: legal approval is missing." in next_steps
    assert "Customer need: We owe: reporting capability is needed." in context
    assert any(x.startswith("Buying signal:") for x in context)
    assert any(x.startswith("Objection:") for x in context)
    assert "Next step: Send the checklist." in next_steps
    assert "Customer committed: Share data." in next_steps
    assert len(sources) == 1
    assert len(lineage["nextStep"]) == len(lineage["clientContext"]) == 3


def test_context_only_approval_preserves_both_action_cells_and_appends_context(service):
    store, sheets = service
    sheets.client_context = store.data["proposal"]["snapshot_client_context"] = "Original customer context.\n"
    before = (sheets.last_progress, sheets.next_step)
    result = approve([], client_context_bullets=["Customer need: expand reporting."])
    assert (sheets.last_progress, sheets.next_step) == before
    assert sheets.client_context == "Original customer context.\n- Customer need: expand reporting."
    assert result["activity"]["before"]["clientContext"] == "Original customer context.\n"
    assert result["activity"]["after"]["clientContext"] == sheets.client_context
    assert store.data["intents"][0]["next_step_bullets"] == []
    assert store.data["intents"][0]["client_context_bullets"] == ["Customer need: expand reporting."]


def test_action_only_approval_preserves_existing_client_context(service):
    store, sheets = service
    sheets.client_context = store.data["proposal"]["snapshot_client_context"] = "  Existing client context.\n"
    approve(["Confirm launch readiness."], client_context_bullets=[])
    assert sheets.client_context == "  Existing client context.\n"
    assert sheets.next_step == "- Confirm launch readiness."


def test_context_append_preserves_exact_existing_text_and_deduplicates_bullet_markers():
    existing = "  • Customer need: Daily reports.  \r\n* Buying signal: PRICING requested.\n"
    assert review._append_context(existing, ["customer need: daily reports.", "Buying signal: Pricing requested.",
                                              "Objection: Security review.", " objection:   security review. "]) == (
        existing + "- Objection: Security review.")
    assert review._append_context("- Same context", ["same context"]) == "- Same context"
    assert review._append_context("", ["One context", "one context"]) == "- One context"


def test_mixed_approval_audits_both_destinations_and_deduplicates_context_on_uncertain_retry(service):
    store, sheets = service
    sheets.client_context = store.data["proposal"]["snapshot_client_context"] = "- Original need"
    old_next = sheets.next_step
    sheets.uncertain = True
    with pytest.raises(review.ReviewUnavailable):
        approve(["Send scope."], client_context_bullets=["Original need", "Buying signal: Pilot requested."])
    after = (sheets.last_progress, sheets.next_step, sheets.client_context)
    assert after == (old_next, "- Send scope.", "- Original need\n- Buying signal: Pilot requested.")
    assert store.data["intents"][0]["sheet_target"] == {
        "spreadsheet_id": review.settings.review_spreadsheet_id,
        "sheet_tab": "Pipeline", "sheet_id": 1869050409}
    assert approve(["Send scope."], client_context_bullets=["Original need", "Buying signal: Pilot requested."])["status"] == "approved"
    assert (sheets.last_progress, sheets.next_step, sheets.client_context) == after
    assert sheets.calls[0] == sheets.calls[1]
    assert store.data["intents"][0]["write_attempts"] == 2


def test_context_payload_changes_cannot_reuse_a_durable_key(service):
    store, sheets = service
    approve([], client_context_bullets=["Customer need: Forecasting."])
    with pytest.raises(review.ReviewConflict, match="different decision"):
        approve([], client_context_bullets=["Customer need: Distribution."])
    assert len(sheets.calls) == 1


def test_changed_sheet_configuration_blocks_recovery_before_any_google_call(service, monkeypatch):
    store, sheets = service
    sheets.uncertain = True
    with pytest.raises(review.ReviewUnavailable):
        approve(["Send scope."], client_context_bullets=["Customer need: Reporting."])
    calls_before = len(store.events)
    monkeypatch.setattr(review.settings, "review_spreadsheet_id", "another-target")
    with pytest.raises(review.ReviewConflict, match="different Sheet configuration"):
        approve(["Send scope."], client_context_bullets=["Customer need: Reporting."])
    assert all(event not in ("sheet-read", "sheet-write") for event in store.events[calls_before:])
    assert store.data["proposal"]["status"] == "applying"


def test_legacy_pending_and_applying_proposals_never_write_or_consume_sources(service):
    store, sheets = service
    for status in ("pending", "applying"):
        store.data["proposal"].update(routing_version=0, status=status)
        with pytest.raises(review.ReviewConflict, match="predates column routing"):
            approve(["An action."])
        with pytest.raises(review.ReviewConflict, match="predates column routing"):
            review.decide(WORKSPACE, PROPOSAL, "RG", "ignore", 1, "legacy-ignore-key")
    assert not store.data["intents"]
    assert not store.data["receipts"]
    assert not sheets.calls
    assert "sheet-read" not in store.events


def test_builder_rebuilds_legacy_pending_cards_from_typed_sources_and_captures_context(monkeypatch):
    monkeypatch.setattr(review, "settings", SimpleNamespace(review_build_batch=20))
    conn = BuildConnection(1)
    review.build_proposals(conn, WORKSPACE)
    old = conn.proposals["company-0"]
    old.update(routing_version=0, next_step_bullets=[], client_context_bullets=[])
    conn.deals[0]["client_context"] = "Current synced context"
    assert review.build_proposals(conn, WORKSPACE, force=True)["updated"] == 1
    result = conn.proposals["company-0"]
    assert result["routing_version"] == 1
    assert result["next_step_bullets"] == ["We owe: Send the pilot checklist. (owner: Nicolás de León) — due 2026-11-05"]
    assert result["snapshot_client_context"] == "Current synced context"
    assert result["revision"] == 2


def test_empty_or_oversized_context_decisions_fail_before_intent_and_write(service):
    store, sheets = service
    with pytest.raises(ValueError, match="at least one"):
        approve([], client_context_bullets=[])
    sheets.client_context = store.data["proposal"]["snapshot_client_context"] = "x" * 49_999
    with pytest.raises(ValueError, match="resulting Client Context"):
        approve([], client_context_bullets=["New context"])
    assert not store.data["intents"]
    assert not sheets.calls


def test_unchanged_large_context_does_not_block_action_only_approval(service):
    store, sheets = service
    sheets.client_context = store.data["proposal"]["snapshot_client_context"] = "x" * 49_000
    approve(["An action."], client_context_bullets=[])
    assert sheets.client_context == "x" * 49_000


def test_changed_client_context_is_a_stale_baseline_even_when_action_cells_match(service):
    store, sheets = service
    sheets.client_context = "A newer human context edit"
    with pytest.raises(review.ReviewConflict, match="Sheet row changed"):
        approve(["An action."], client_context_bullets=[])
    assert not store.data["intents"]
    assert not sheets.calls


@pytest.mark.parametrize("sheet_id", [None, -1, True, "1869050409"])
def test_unpinned_sheet_target_blocks_new_approvals_before_google_or_intent(service, monkeypatch, sheet_id):
    store, sheets = service
    monkeypatch.setattr(review.settings, "review_sheet_id", sheet_id)
    with pytest.raises(review.ReviewUnavailable, match="numeric REVIEW_SHEET_ID"):
        approve(["An action."])
    assert not store.data["intents"]
    assert "sheet-read" not in store.events
    assert not sheets.calls


def test_workspace_recovery_exposes_exact_split_payload_and_not_legacy_or_redirected_retry(service, monkeypatch):
    store, sheets = service
    action = "Send the checklist before next Friday."
    store.source_record = record(body=action)
    store.insights = [insight_for_record(store.source_record)]
    next_steps, context, sources, lineage = review.proposal_content([store.source_record], store.insights)
    store.data["proposal"].update(
        bullets=next_steps + context, next_step_bullets=next_steps, client_context_bullets=context,
        sources=sources, bullet_lineage=lineage,
    )

    class WorkspaceConnection:
        def execute(self, sql, params=()):
            if sql.startswith("select p.*"):
                return Result([copy.deepcopy(store.data["proposal"])] if OPPORTUNITY in params[1] else [])
            if sql.startswith("select source, external_id, content_hash, occurred_at, subject, meta from source_records"):
                assert params[0] == WORKSPACE and {str(identifier) for identifier in params[1]} == {RECORD}
                return Result([store.source_record])
            if sql.startswith("select idempotency_key"):
                return Result([copy.deepcopy(store.data["intents"][0])])
            if sql.startswith("select o.*"):
                item = store.data["proposal"]
                name = "pernod ricard"
                if name not in params[1] and (item["removed_at"] is None or name in params[2]):
                    return Result([])
                return Result([{"id": OPPORTUNITY, "name": item["company_name"], "stage": item["current_stage"],
                                "normalized_name": name, "owner_code": "NDL", "last_progress": "Done",
                                "next_step": "Later", "removed_at": item["removed_at"], "approved": 0}])
            if sql.startswith("select d.*"):
                return Result([
                    {**copy.deepcopy(intent), "company_name": store.data["proposal"]["company_name"]}
                    for intent in store.data["intents"] if OPPORTUNITY in params[1]
                ])
            raise AssertionError(sql)

    pending = review.workspace_view(WorkspaceConnection(), WORKSPACE)["proposals"][0]
    assert pending["nextStepBullets"] == store.data["proposal"]["next_step_bullets"]
    assert pending["reviewNextStepBullets"] == [
        "We owe: Send the checklist. (complete by Nov 6, 2026)"
    ]
    assert pending["bulletLineage"] == store.data["proposal"]["bullet_lineage"]
    assert not sheets.calls
    # H-10: the view says whether a card is only waiting on the customer; a card built earlier cannot say.
    assert pending["waiting"] is False
    ours = copy.deepcopy(store.data["proposal"]["bullet_lineage"])
    for side, expected in (("customer", True), ("blocker", True), ("unassigned", False)):
        store.data["proposal"]["bullet_lineage"]["nextStep"][0]["side"] = side
        assert review.workspace_view(WorkspaceConnection(), WORKSPACE)["proposals"][0]["waiting"] is expected
    del store.data["proposal"]["bullet_lineage"]["nextStep"][0]["side"]
    assert "waiting" not in review.workspace_view(WorkspaceConnection(), WORKSPACE)["proposals"][0]
    store.data["proposal"]["bullet_lineage"] = ours

    store.source_record["meta"]["date_inferred"] = True
    estimated = review.workspace_view(WorkspaceConnection(), WORKSPACE)["proposals"][0]
    assert "reviewNextStepBullets" not in estimated
    store.source_record["meta"].clear()

    edited_action = "Send the revised proposal by Nov 21, 2026."
    sheets.uncertain = True
    with pytest.raises(review.ReviewUnavailable):
        approve([edited_action], client_context_bullets=["Customer need: Updated context."])

    view = review.workspace_view(WorkspaceConnection(), WORKSPACE)
    proposal_view = view["proposals"][0]
    assert proposal_view["retry"] == {"idempotency_key": "approval-key-1", "revision": 1,
                                       "next_step_bullets": [edited_action],
                                       "client_context_bullets": ["Customer need: Updated context."]}
    assert proposal_view["nextStepBullets"] == [edited_action]
    assert proposal_view["clientContextBullets"] == ["Customer need: Updated context."]
    # H-16: a saved Keep + add approval tells the page its mode, so the retry keeps the same request identity.
    saved = store.data["intents"][0]
    replace_hash = saved["request_hash"]
    saved["request_hash"] = review._request_hash(PROPOSAL, "approve", 1, saved["next_step_bullets"],
                                                  saved["client_context_bullets"], "append")
    assert review.workspace_view(WorkspaceConnection(), WORKSPACE)["proposals"][0]["retry"]["next_step_mode"] == "append"
    saved["request_hash"] = replace_hash
    assert "reviewNextStepBullets" not in proposal_view
    assert "bullets" not in proposal_view
    assert view["routingVersion"] == 1
    assert view["clientContextMode"] == "append"
    assert view["sheetUrl"] == (
        "https://docs.google.com/spreadsheets/d/14lXRsWoEY63tOsz4SwggiItgTNPFWI4KDVTLay3MLDw/edit"
        "?gid=1869050409#gid=1869050409"
    )
    with monkeypatch.context() as config:
        config.setattr(review.settings, "review_spreadsheet_id", "production-sheet")
        config.setattr(review.settings, "review_sheet_id", 0)
        assert review.workspace_view(WorkspaceConnection(), WORKSPACE)["sheetUrl"] == (
            "https://docs.google.com/spreadsheets/d/production-sheet/edit?gid=0#gid=0"
        )
        config.setattr(review.settings, "review_sheet_id", None)
        assert review.workspace_view(WorkspaceConnection(), WORKSPACE)["sheetUrl"] is None
    store.data["intents"][0]["sheet_target"]["sheet_id"] = 999
    redirected = review.workspace_view(WorkspaceConnection(), WORKSPACE)["proposals"][0]
    assert "retry" not in redirected
    assert "different Sheet configuration" in redirected["error"]
    store.data["proposal"]["routing_version"] = 0
    legacy = review.workspace_view(WorkspaceConnection(), WORKSPACE)["proposals"][0]
    assert "retry" not in legacy
    assert "predates column routing" in legacy["error"]
    assert len(view["rows"]) == len(view["activity"]) == 1
    sheets.status = "5. Working client"
    excluded = review.workspace_view(WorkspaceConnection(), WORKSPACE)
    assert excluded["proposals"] == excluded["rows"] == excluded["activity"] == []
    store.data["proposal"].update(current_stage="4. Pilot", removed_at=NOW)
    sheets.read_company_statuses = lambda: {}
    removed = review.workspace_view(WorkspaceConnection(), WORKSPACE)
    assert len(removed["activity"]) == 1 and removed["proposals"] == removed["rows"] == []
    sheets.read_company_statuses = lambda: {"pernod ricard": None}
    ambiguous = review.workspace_view(WorkspaceConnection(), WORKSPACE)
    assert ambiguous["proposals"] == ambiguous["rows"] == ambiguous["activity"] == []
    sheets.status = "4. Pilot"
    store.data["proposal"]["current_stage"] = "5. Working client"
    excluded = review.workspace_view(WorkspaceConnection(), WORKSPACE)
    assert excluded["proposals"] == excluded["rows"] == excluded["activity"] == []


def test_context_mirror_updates_tracked_value_and_logs_its_change():
    class ContextMirror:
        def __init__(self):
            self.writes = []
        def execute(self, sql, params=()):
            if sql.startswith("select o.*"):
                return Result([{"id": OPPORTUNITY, "name": "Pernod Ricard", "country": "ARG", "owner_code": "NDL",
                                "stage": "4. Pilot", "primary_buyer": "Commercial", "contacts_text": "Tomás",
                                "last_progress": "Old progress", "next_step": "Old next step", "client_context": "Old context",
                                "removed_at": None}])
            self.writes.append((sql, params))
            return Result([{"id": "sync-run"}]) if "returning id" in sql else Result([])
    conn = ContextMirror()
    before = {"lastProgress": "Old progress", "nextStep": "Old next step", "clientContext": "Old context"}
    after = {**before, "clientContext": "Old context\n- New context"}
    assert review._mirror_approval(conn, proposal(), before, after)
    assert conn.writes[0][1][:3] == ("Old progress", "Old next step", "Old context\n- New context")
    changes = [params for sql, params in conn.writes if "insert into opportunity_changes" in sql]
    assert len(changes) == 1
    assert changes[0][3:] == ("client_context", "Old context", "Old context\n- New context")


def test_explicit_replace_configuration_still_preserves_action_cells(service, monkeypatch):
    store, sheets = service
    sheets.client_context = store.data["proposal"]["snapshot_client_context"] = "Old context"
    monkeypatch.setattr(review.settings, "review_client_context_mode", "replace")
    before = (sheets.last_progress, sheets.next_step)
    approve([], client_context_bullets=["New context"])
    assert sheets.client_context == "- New context"
    assert (sheets.last_progress, sheets.next_step) == before


def dated_record(number, occurred, *, created=NOW, **overrides):
    return record(id=f"66666666-6666-6666-6666-{number:012d}", external_id=f"dated-{number}",
                  occurred_at=occurred, created_at=created, body=f"New commercial item {number}.",
                  content_hash=f"content-{number}", **overrides)


def test_initial_window_is_inclusive_14_days_and_excludes_future_and_undated_versions(monkeypatch):
    monkeypatch.setattr(review, "settings", SimpleNamespace(review_build_batch=20))
    conn = BuildConnection(1)
    cutoff = NOW - timedelta(days=14)
    conn.records["company-0"] = [dated_record(1, cutoff - timedelta(microseconds=1)),
                                  dated_record(2, cutoff), dated_record(3, NOW),
                                  dated_record(4, NOW + timedelta(microseconds=1)), dated_record(5, None)]
    result = review.build_proposals(conn, WORKSPACE)
    assert result["bootstrap"] == {"initializedAt": NOW.isoformat(), "cutoff": cutoff.isoformat(),
                                    "days": 14, "completed": True}
    assert {s["externalId"] for s in conn.proposals["company-0"]["sources"]} == {"dated-2", "dated-3"}
    assert {e["external_id"] for e in conn.exclusions} == {"dated-1", "dated-4", "dated-5"}
    assert not conn.receipts, "Bootstrap exclusions must never be fabricated founder decisions."


def test_initial_exclusions_survive_restart_and_do_not_reappear_on_later_daily_runs(monkeypatch):
    monkeypatch.setattr(review, "settings", SimpleNamespace(review_build_batch=20))
    conn = BuildConnection(1)
    conn.records["company-0"] = [dated_record(1, NOW - timedelta(days=90))]
    assert review.build_proposals(conn, WORKSPACE)["created"] == 0
    resumed = copy.deepcopy(conn)  # All persisted state survives a different process instance.
    resumed.now = NOW + timedelta(days=20)
    assert review.build_proposals(resumed, WORKSPACE)["created"] == 0
    assert not resumed.proposals
    assert resumed.queue_state["bootstrap_cutoff"] == NOW - timedelta(days=14)
    assert len(resumed.exclusions) == 1


def test_unclassified_unlinked_initial_old_sources_remain_excluded_when_linked_later(monkeypatch):
    monkeypatch.setattr(review, "settings", SimpleNamespace(review_build_batch=20))
    conn = BuildConnection(1)
    conn.records["company-0"] = []
    unlinked = dated_record(1, NOW - timedelta(days=60), status="failed", company_id=None)
    conn.unlinked_records = [unlinked]
    review.build_proposals(conn, WORKSPACE)
    conn.now += timedelta(days=1)
    conn.records["company-0"] = [{**unlinked, "company_id": "company-0", "status": "classified"}]
    conn.unlinked_records = []
    assert review.build_proposals(conn, WORKSPACE)["created"] == 0
    assert not conn.proposals


def test_same_day_is_skipped_but_new_information_is_collected_the_next_day(monkeypatch):
    monkeypatch.setattr(review, "settings", SimpleNamespace(review_build_batch=20))
    conn = BuildConnection(1)
    review.build_proposals(conn, WORKSPACE)
    before = copy.deepcopy(conn.proposals["company-0"])
    arrived = NOW + timedelta(hours=1)
    conn.records["company-0"].append(dated_record(9, arrived, created=arrived))
    conn.now = arrived
    assert review.build_proposals(conn, WORKSPACE)["skipped"] is True
    assert conn.proposals["company-0"] == before
    conn.now = NOW + timedelta(days=1)
    assert review.build_proposals(conn, WORKSPACE)["updated"] == 1
    assert len(conn.proposals["company-0"]["next_step_bullets"]) == 2


def test_daily_gate_uses_workspace_calendar_day_not_utc_date(monkeypatch):
    monkeypatch.setattr(review, "settings", SimpleNamespace(review_build_batch=20))
    conn = BuildConnection(1)
    conn.now = NOW.replace(hour=23)
    assert review.build_proposals(conn, WORKSPACE)["forDate"] == "2026-11-02"
    conn.now = (NOW + timedelta(days=1)).replace(hour=1)
    assert review.build_proposals(conn, WORKSPACE)["skipped"] is True
    conn.now = (NOW + timedelta(days=1)).replace(hour=3)
    result = review.build_proposals(conn, WORKSPACE)
    assert result["skipped"] is False and result["forDate"] == "2026-11-03"


def test_bootstrap_batches_keep_the_original_boundary_when_resumed_after_midnight(monkeypatch):
    monkeypatch.setattr(review, "settings", SimpleNamespace(review_build_batch=20))
    conn = BuildConnection(23)
    first = review.build_proposals(conn, WORKSPACE)
    assert not first["done"] and not first["bootstrap"]["completed"]
    anchor = conn.queue_state["initialized_at"]
    conn.now += timedelta(days=1)
    second = review.build_proposals(conn, WORKSPACE)
    assert second["created"] == 3 and second["done"]
    assert second["forDate"] == "2026-11-02"
    assert conn.queue_state["initialized_at"] == anchor
    assert conn.queue_state["last_completed_on"].isoformat() == "2026-11-02"
    assert review.build_proposals(conn, WORKSPACE)["forDate"] == "2026-11-03"


def test_new_late_arriving_old_items_are_allowed_after_the_initial_import(monkeypatch):
    monkeypatch.setattr(review, "settings", SimpleNamespace(review_build_batch=20))
    conn = BuildConnection(1)
    conn.records["company-0"] = []
    review.build_proposals(conn, WORKSPACE)
    conn.now += timedelta(days=1)
    conn.records["company-0"] = [dated_record(9, NOW - timedelta(days=120), created=conn.now)]
    result = review.build_proposals(conn, WORKSPACE)
    assert result["created"] == 1
    assert conn.proposals["company-0"]["sources"][0]["externalId"] == "dated-9"


def test_real_content_change_to_an_excluded_initial_record_is_new_information(monkeypatch):
    monkeypatch.setattr(review, "settings", SimpleNamespace(review_build_batch=20))
    conn = BuildConnection(1)
    old = dated_record(1, NOW - timedelta(days=90))
    conn.records["company-0"] = [old]
    review.build_proposals(conn, WORKSPACE)
    conn.now += timedelta(days=1)
    conn.records["company-0"] = [{**old, "body": "A newly corrected commercial note.", "content_hash": "new-real-content"}]
    assert review.build_proposals(conn, WORKSPACE)["created"] == 1
    current = conn.proposals["company-0"]["source_versions"][0]
    assert current != {key: conn.exclusions[0][key] for key in ("source", "external_id", "version")}


def test_still_qualified_pending_bullets_survive_more_than_14_days_and_grow(monkeypatch):
    monkeypatch.setattr(review, "settings", SimpleNamespace(review_build_batch=20))
    conn = BuildConnection(1)
    review.build_proposals(conn, WORKSPACE)
    old = copy.deepcopy(conn.proposals["company-0"])
    conn.now += timedelta(days=30)
    conn.records["company-0"].append(dated_record(9, conn.now, created=conn.now))
    assert review.build_proposals(conn, WORKSPACE)["updated"] == 1
    pending = conn.proposals["company-0"]
    assert set(old["next_step_bullets"]) < set(pending["next_step_bullets"])
    assert pending["next_step_bullets"][0] == "We owe: New commercial item 9."  # newest first
    assert len(pending["next_step_bullets"]) == len(old["next_step_bullets"]) + 1
    assert old["bullet_lineage"]["nextStep"][0] in pending["bullet_lineage"]["nextStep"]
    assert review.build_proposals(conn, WORKSPACE)["skipped"] is True


def test_force_rebuild_adds_newly_extracted_insights_without_resetting_initial_history_policy(monkeypatch):
    monkeypatch.setattr(review, "settings", SimpleNamespace(review_build_batch=20))
    conn = BuildConnection(1)
    conn.insights = []
    conn.records["company-0"].append(dated_record(1, NOW - timedelta(days=90)))
    assert review.build_proposals(conn, WORKSPACE)["created"] == 0
    conn.insights = [insight()]
    conn.now += timedelta(hours=1)
    assert review.build_proposals(conn, WORKSPACE, force=True)["created"] == 1
    current = conn.proposals["company-0"]
    assert any(text.startswith("We owe:") for text in current["next_step_bullets"])
    assert len(conn.exclusions) == 1
    assert all(source["externalId"] != "dated-1" for source in current["sources"])
    assert conn.queue_state["initialized_at"] == NOW


def test_changed_source_version_replaces_unsupported_lineage_and_approval_consumes_only_current_version(monkeypatch):
    monkeypatch.setattr(review, "settings", SimpleNamespace(review_build_batch=20))
    conn = BuildConnection(1)
    review.build_proposals(conn, WORKSPACE)
    original = copy.deepcopy(conn.proposals["company-0"]["sources"][0])
    conn.now += timedelta(days=1)
    conn.records["company-0"][0].update(body="The pilot scope has been corrected.", content_hash="new-scope")
    review.build_proposals(conn, WORKSPACE)
    pending = conn.proposals["company-0"]
    assert len(pending["sources"]) == len(pending["source_versions"]) == 1
    assert len(pending["observed_source_versions"]) == 1
    assert pending["sources"][0]["id"] != original["id"]
    item = {**copy.deepcopy(pending), "id": PROPOSAL, "workspace_id": WORKSPACE,
            "company_id": COMPANY, "opportunity_id": OPPORTUNITY, "removed_at": None}
    store = Store(item)
    store.source_record = conn.records["company-0"][0]
    store.insights = [insight_for_record(store.source_record)]
    sheets = FakeSheets(store)
    monkeypatch.setattr(review, "settings", SimpleNamespace(
        review_spreadsheet_id="test-sheet", review_sheet_tab="Pipeline", review_sheet_id=1869050409,
        review_client_context_mode="append", review_build_batch=20))
    monkeypatch.setattr(review.db, "connect", store.connect)
    monkeypatch.setattr(review, "SheetsClient", lambda: sheets)
    monkeypatch.setattr(review, "_mirror_approval", lambda *args: True)
    result = review.decide(WORKSPACE, PROPOSAL, "RG", "approve", pending["revision"], "accumulated-key-1")
    assert result["status"] == "approved"
    assert len(store.data["receipts"]) == 1
    assert result["activity"]["bulletLineage"]["nextStep"] == [
        {**node, "origin": "source"} for node in pending["bullet_lineage"]["nextStep"]]


def test_rebuild_prunes_delivery_on_unchanged_source_and_keeps_sales_and_unscoped(monkeypatch):
    monkeypatch.setattr(review, "settings", SimpleNamespace(review_build_batch=20))
    conn = BuildConnection(1)
    conn.insights = [insight(action_scope="deal_progression"),
                     insight(text="Discuss expansion.", action_scope=None),
                     insight(text="Fix the report.", action_scope=None)]
    review.build_proposals(conn, WORKSPACE)
    old = copy.deepcopy(conn.proposals["company-0"])
    assert len(old["next_step_bullets"]) == 3
    conn.insights[-1]["action_scope"] = "delivery"
    conn.now += timedelta(days=1)
    assert review.build_proposals(conn, WORKSPACE)["updated"] == 1
    pending = conn.proposals["company-0"]
    assert pending["next_step_bullets"] == [bullet for bullet in old["next_step_bullets"] if "Fix the report" not in bullet]
    assert pending["sources"] == old["sources"]
    assert pending["source_versions"] == old["source_versions"]
    assert pending["revision"] == 2 and pending["status"] == "pending"
    assert not conn.receipts


def test_legacy_pending_lineage_is_rebuilt_or_withdrawn_by_initial_window(monkeypatch):
    monkeypatch.setattr(review, "settings", SimpleNamespace(review_build_batch=20))
    conn = BuildConnection(1)
    conn.proposals["company-0"] = proposal(id="legacy-card", company_id="company-0", lineage_version=0)
    conn.records["company-0"] = [dated_record(1, NOW - timedelta(days=90))]
    review.build_proposals(conn, WORKSPACE)
    assert conn.proposals["company-0"]["status"] == "superseded"
    assert len(conn.exclusions) == 1


def test_corrupt_lineage_can_be_rebuilt_from_current_sources_without_fabricating_edges(monkeypatch):
    monkeypatch.setattr(review, "settings", SimpleNamespace(review_build_batch=20))
    conn = BuildConnection(1)
    review.build_proposals(conn, WORKSPACE)
    conn.proposals["company-0"]["bullet_lineage"]["nextStep"][0]["sourceIds"] = ["unknown-source"]
    assert review.build_proposals(conn, WORKSPACE, force=True)["updated"] == 1
    current = conn.proposals["company-0"]
    assert review.validate_lineage(current["next_step_bullets"], current["client_context_bullets"],
                                   current["sources"], current["bullet_lineage"])


def test_pending_without_lineage_cannot_create_a_new_decision(service):
    store, sheets = service
    store.data["proposal"]["lineage_version"] = 0
    with pytest.raises(review.ReviewConflict, match="predates source lineage"):
        approve()
    with pytest.raises(review.ReviewConflict, match="predates source lineage"):
        review.decide(WORKSPACE, PROPOSAL, "RG", "ignore", 1, "legacy-lineage-ignore")
    assert not store.data["intents"] and "sheet-read" not in store.events


def test_pre_lineage_typed_applying_intent_remains_recoverable_without_reassigning_sources(service):
    store, sheets = service
    sheets.uncertain = True
    with pytest.raises(review.ReviewUnavailable):
        approve()
    store.data["proposal"].update(lineage_version=0, bullet_lineage={"nextStep": [], "clientContext": []})
    store.data["intents"][0].update(lineage_version=0, bullet_lineage={"nextStep": [], "clientContext": []})
    before = (sheets.last_progress, sheets.next_step)
    assert approve()["status"] == "approved"
    assert (sheets.last_progress, sheets.next_step) == before
    assert store.data["proposal"]["lineage_version"] == 0


def test_edited_text_has_no_false_source_edges_in_the_durable_decision_audit(service):
    store, sheets = service
    result = approve(["A founder-authored action."], client_context_bullets=["Founder-authored customer context."])
    lineage = result["activity"]["bulletLineage"]
    assert result["activity"]["lineageVersion"] == 1
    for destination in ("nextStep", "clientContext"):
        assert lineage[destination][0]["sourceIds"] == []
        assert lineage[destination][0]["origin"] == "founder_edit"
    assert store.data["intents"][0]["bullet_lineage"] == lineage


def test_card_limit_keeps_the_newest_step_and_a_decision_retires_the_older_sources(monkeypatch):
    monkeypatch.setattr(review, "settings", SimpleNamespace(review_build_batch=20, review_max_next_steps=1))
    conn = BuildConnection(1)
    review.build_proposals(conn, WORKSPACE)
    conn.now += timedelta(days=3)
    newer = dated_record(9, conn.now, created=conn.now)
    conn.records["company-0"].append(newer)
    assert review.build_proposals(conn, WORKSPACE)["updated"] == 1
    pending = conn.proposals["company-0"]
    assert pending["next_step_bullets"] == ["We owe: New commercial item 9."]
    assert [source["externalId"] for source in pending["sources"]] == ["dated-9"]
    # Both versions are recorded on the card, so approving or ignoring it also retires the older email.
    assert {version["external_id"] for version in pending["source_versions"]} == {"company-0", "dated-9"}
    assert pending["observed_source_versions"] == pending["source_versions"]
    conn.receipts = [{"company_id": "company-0", **version} for version in pending["source_versions"]]
    conn.proposals.clear()
    conn.now += timedelta(days=1)
    assert review.build_proposals(conn, WORKSPACE)["created"] == 0


class StatusConnection:
    def __init__(self, stage="1. Opening", removed=None):
        self.row = {"id": OPPORTUNITY, "name": "Diageo", "country": "MEX", "stage": stage, "removed_at": removed,
                    "owner_code": "RG", "primary_buyer": None, "contacts_text": None, "last_progress": None,
                    "next_step": "- last push", "client_context": None, "row_hash": "old-hash"}
        self.changes, self.runs, self.committed = [], [], False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def commit(self):
        self.committed = True

    def execute(self, sql, params=()):
        q = " ".join(sql.split())
        if q.startswith("select o.*, c.name"):
            return Result([dict(self.row)] if params == (WORKSPACE, OPPORTUNITY) else [])
        if q.startswith("update opportunities set stage"):
            self.row["stage"], self.row["row_hash"] = params[0], params[1]
            return Result([])
        if q.startswith("insert into sync_runs"):
            self.runs.append(params[1])
            return Result([{"id": "run-1"}])
        if q.startswith("insert into opportunity_changes"):
            self.changes.append(params[3:])
            return Result([])
        raise AssertionError(q)


class StatusSheets:
    statuses = {"diageo": "1. Opening", "campari": "4. Pilot", "bees": "0. Paused", "delta": "5. Working client"}
    calls = []
    error = None

    def read_company_statuses(self):
        return dict(self.statuses)

    def set_status(self, company, expected, new):
        if self.error:
            raise self.error
        type(self).calls.append((company, expected, new))
        return SimpleNamespace(status=new)


@pytest.fixture
def status_service(monkeypatch):
    conn = StatusConnection()
    StatusSheets.calls, StatusSheets.error = [], None
    monkeypatch.setattr(review.db, "connect", lambda: conn)
    monkeypatch.setattr(review, "SheetsClient", StatusSheets)
    return conn


@pytest.mark.parametrize("actor", ["RG", "MG"])
def test_status_change_writes_the_sheet_then_mirrors_the_stage_with_history(status_service, actor):
    result = review.change_status(WORKSPACE, OPPORTUNITY, actor, "1. Opening", " 0. Paused ")
    assert StatusSheets.calls == [("Diageo", "1. Opening", "0. Paused")]
    assert result["status"] == "0. Paused" and result["company"] == "Diageo" and result["inSalesPipeline"] is True
    assert status_service.row["stage"] == "0. Paused" and status_service.row["row_hash"] != "old-hash"
    assert status_service.changes == [("1. Opening", "0. Paused")]
    assert status_service.runs == [f"sheet_review_status:{actor}"] and status_service.committed


def test_status_change_allows_only_statuses_already_in_the_sheet_and_valid_founders(status_service):
    for bad in ("9. Won", "", "paused"):
        with pytest.raises(ValueError):
            review.change_status(WORKSPACE, OPPORTUNITY, "RG", "1. Opening", bad)
    with pytest.raises(ValueError):
        review.change_status(WORKSPACE, OPPORTUNITY, "someone", "1. Opening", "0. Paused")
    with pytest.raises(review.ReviewNotFound):
        review.change_status(WORKSPACE, PROPOSAL, "RG", "1. Opening", "0. Paused")
    assert not StatusSheets.calls and not status_service.changes


def test_status_change_conflict_or_outage_leaves_the_mirror_untouched(status_service):
    StatusSheets.error = SheetConflict("changed")
    with pytest.raises(review.ReviewConflict):
        review.change_status(WORKSPACE, OPPORTUNITY, "RG", "1. Opening", "0. Paused")
    StatusSheets.error = SheetWriteUncertain("unverified")
    with pytest.raises(review.ReviewUnavailable):
        review.change_status(WORKSPACE, OPPORTUNITY, "RG", "1. Opening", "0. Paused")
    assert status_service.row["stage"] == "1. Opening" and not status_service.changes


def test_status_change_does_not_overwrite_a_mirror_that_a_newer_sync_already_moved(status_service):
    status_service.row["stage"] = "2. In Talks"
    result = review.change_status(WORKSPACE, OPPORTUNITY, "NC", "1. Opening", "0. Paused")
    assert result["mirror_refreshed"] is False
    assert status_service.row["stage"] == "2. In Talks" and not status_service.changes


def test_moving_a_deal_to_working_client_reports_that_it_left_the_sales_pipeline(status_service):
    result = review.change_status(WORKSPACE, OPPORTUNITY, "RG", "1. Opening", "5. Working client")
    assert result["inSalesPipeline"] is False
    assert review.status_options({"a": "4. Pilot", "b": None, "c": " 4. Pilot ", "d": "0. Paused", "e": ""}) == [
        "0. Paused", "4. Pilot"]


# ---- H-16: Keep current bullets + add these (next_step_mode="append")

def test_append_mode_leaves_last_progress_alone_and_adds_below_the_existing_next_step(service):
    store, sheets = service
    sheets.next_step = store.data["proposal"]["snapshot_next_step"] = "  -pusheado 2-nov\r\n* Ask for the signed agreement.  "
    old_last, old_next = sheets.last_progress, sheets.next_step
    result = approve(["ask for the  signed agreement.", "Send the revised scope."], next_step_mode="append")
    assert sheets.last_progress == old_last
    assert sheets.next_step == old_next + "\n- Send the revised scope."
    assert result["activity"]["before"] == {"lastProgress": old_last, "nextStep": old_next, "clientContext": ""}
    assert result["activity"]["after"] == {"lastProgress": old_last, "nextStep": sheets.next_step, "clientContext": ""}
    # The same durable intent and write path as a replace: one planned three-cell write, sources consumed.
    assert sheets.calls == [("Pernod Ricard", old_last, old_next, old_last, sheets.next_step, "", "")]
    assert store.data["intents"][0]["next_step_bullets"] == ["ask for the  signed agreement.", "Send the revised scope."]
    assert store.data["proposal"]["status"] == "approved" and len(store.data["receipts"]) == 1
    assert store.events.index("commit", 1) < store.events.index("sheet-write")


def test_append_mode_on_an_empty_next_step_writes_the_bullets_without_rotating(service):
    store, sheets = service
    sheets.next_step = store.data["proposal"]["snapshot_next_step"] = ""
    approve(["Send scope.", "Book the review."], next_step_mode="append", client_context_bullets=["Customer need: Reporting."])
    assert sheets.last_progress == "Pilot terms agreed."
    assert sheets.next_step == "- Send scope.\n- Book the review."
    assert sheets.client_context == "- Customer need: Reporting."


def test_default_mode_is_still_replace_and_the_mode_is_part_of_the_request_identity(service):
    store, sheets = service
    assert review._request_hash(PROPOSAL, "approve", 1, ["Send scope."], []) == review._request_hash(
        PROPOSAL, "approve", 1, ["Send scope."], [], "replace")
    assert review._request_hash(PROPOSAL, "approve", 1, ["Send scope."], [], "append") != review._request_hash(
        PROPOSAL, "approve", 1, ["Send scope."], [])
    old_next = sheets.next_step
    result = approve(["Send scope."], next_step_mode="append")
    assert sheets.next_step == old_next + "\n- Send scope."
    # The same key cannot be reused to turn the saved append into a replace, or the reverse.
    with pytest.raises(review.ReviewConflict, match="different decision"):
        approve(["Send scope."])
    assert approve(["Send scope."], next_step_mode="append")["activity"] == result["activity"]
    assert len(sheets.calls) == 1
    for invalid in ("merge", "", None):
        with pytest.raises(ValueError):
            approve(["Send scope."], next_step_mode=invalid)
    with pytest.raises(ValueError):
        review.decide(WORKSPACE, PROPOSAL, "NDL", "ignore", 1, "ignore-key-1", next_step_mode="append")


def test_uncertain_append_is_retried_from_the_saved_plan_and_never_appends_twice(service):
    store, sheets = service
    old_last, old_next = sheets.last_progress, sheets.next_step
    sheets.uncertain = True
    with pytest.raises(review.ReviewUnavailable):
        approve(["Send scope."], next_step_mode="append")
    assert store.data["proposal"]["status"] == "applying"
    intent = store.data["intents"][0]
    assert review._saved_next_step_mode(intent) == "append"
    with pytest.raises(review.ReviewConflict, match="different decision"):
        approve(["Send scope."])  # A retry that lost the mode is refused, never applied as a replace.
    assert approve(["Send scope."], next_step_mode="append")["status"] == "approved"
    assert (sheets.last_progress, sheets.next_step) == (old_last, old_next + "\n- Send scope.")
    assert sheets.calls[0] == sheets.calls[1] and intent["after_cells"]["lastProgress"] == old_last


def test_append_of_steps_already_in_the_cell_changes_nothing_but_still_closes_the_card(service):
    store, sheets = service
    before = (sheets.last_progress, sheets.next_step, sheets.client_context)
    result = approve(["Ask for the signed agreement."], next_step_mode="append")
    assert (sheets.last_progress, sheets.next_step, sheets.client_context) == before
    assert result["status"] == "approved" and result["activity"]["before"] == result["activity"]["after"]


def test_append_checks_the_live_row_first_and_an_oversized_result_is_refused_before_any_intent(service):
    store, sheets = service
    sheets.next_step = "Edited by a founder in the Sheet."
    with pytest.raises(review.ReviewConflict, match="Sheet row changed"):
        approve(["Send scope."], next_step_mode="append")
    sheets.next_step = store.data["proposal"]["snapshot_next_step"] = "x" * 49_990
    with pytest.raises(ValueError, match="exceeds one Sheet cell"):
        approve(["Send the revised scope."], next_step_mode="append")
    assert not store.data["intents"] and not sheets.calls


def test_saved_replace_approvals_are_recognised_as_replace():
    intent = {"proposal_id": PROPOSAL, "revision": 3, "next_step_bullets": ["A"], "client_context_bullets": [],
              "request_hash": review._request_hash(PROPOSAL, "approve", 3, ["A"], [])}
    assert review._saved_next_step_mode(intent) == "replace"
    assert review._saved_next_step_mode({**intent, "request_hash": "unknown"}) == "replace"


# ---- H-06: an emailed ask already answered in its thread is not suggested, at build and at approval alike

ASK_RECORD = "66666666-6666-6666-6666-666666666666"
REPLY_RECORD = "77777777-7777-7777-7777-777777777777"


def customer_email(**overrides):
    values = {"id": ASK_RECORD, "external_id": "message-ask", "thread_id": "thread-ask", "company_id": COMPANY,
              "body": "Does Wednesday morning work for the meeting? We also need the store list.",
              "participants": {"from": [{"address": "ana@redbull.com"}], "to": [{"address": "nico@bianca-ai.com"}]}}
    return record(**{**values, **overrides})


def our_reply(**overrides):
    values = {"id": REPLY_RECORD, "external_id": "message-reply", "thread_id": "thread-ask", "company_id": COMPANY,
              "body": "Wednesday 11 AM works for us.", "occurred_at": NOW + timedelta(hours=2),
              "participants": {"from": [{"address": "nico@bianca-ai.com"}], "to": [{"address": "ana@redbull.com"}]}}
    return record(**{**values, **overrides})


def email_asks():
    base = dict(source="gmail", type="next_step", owner_side="bianca", owner_name=None, due_date=None,
                source_record_ids=[ASK_RECORD], origin_record_id=ASK_RECORD, action_scope="deal_progression")
    return [insight(id="ask-meeting", text="Confirm a time on Wednesday morning.",
                    quote="Does Wednesday morning work for the meeting?", **base),
            insight(id="ask-stores", text="Send the store list.", quote="We also need the store list.",
                    **{**base, "type": "commitment"})]


class Mail:
    """What the answered-ask check reads: the company's mail, later insights, brief clicks, the cycle start."""
    def __init__(self, messages, *, cycle_started_at=NOW + timedelta(days=1), clicks=(), insights=(), has_clicks_table=True):
        self.messages, self.cycle_started_at, self.clicks = list(messages), cycle_started_at, list(clicks)
        self.insights, self.has_clicks_table, self.queries = list(insights), has_clicks_table, []

    def answer(self, q, params):
        if q.startswith("select cycle_started_at from review_queue_state"):
            return Result([{"cycle_started_at": self.cycle_started_at}])
        if q.startswith("select id, thread_id, occurred_at, subject, participants from source_records"):
            assert "created_at <= %s" in q and "status = 'classified'" in q and "is_commercial" in q
            workspace, company, cutoff, _, origins = params
            threads = {m["thread_id"] for m in self.messages if m["id"] in origins}
            found = [m for m in self.messages if m["thread_id"] in threads and m.get("company_id", company) == company
                     and review._instant(m["created_at"]) <= cutoff]
            self.queries.append(("mail", cutoff))
            return Result(copy.deepcopy(sorted(found, key=lambda m: m["occurred_at"])))
        if q.startswith("select internal_domains from workspaces"):
            return Result([{"internal_domains": ["bianca-ai.com"]}])
        if q.startswith("select origin_record_id, action_scope from insights"):
            return Result([i for i in self.insights if i["origin_record_id"] in params[1]])
        if q.startswith("select to_regclass('brief_actions')"):
            return Result([{"found": "brief_actions" if self.has_clicks_table else None}])
        if q.startswith("select distinct on (ref) ref, action, created_at from brief_actions"):
            latest = {}
            for click in sorted(self.clicks, key=lambda c: c["created_at"]):
                if click["ref"] in params[1] and click["created_at"] <= params[2]:
                    latest[click["ref"]] = click
            return Result(list(latest.values()))
        return None


class MailBuild(BuildConnection):
    def __init__(self, mail_data):
        super().__init__(1)
        self.mail = mail_data

    def execute(self, sql, params=()):
        return self.mail.answer(" ".join(sql.split()), params) or super().execute(sql, params)


def build_settings(monkeypatch):
    monkeypatch.setattr(review, "settings", SimpleNamespace(review_build_batch=20, project_team_domains=["kellogg.northwestern.edu"]))


def ask_build(messages, **mail_options):
    conn = MailBuild(Mail(messages, **mail_options))
    conn.records = {"company-0": [customer_email(company_id="company-0")]}
    conn.insights = email_asks()
    return conn


def test_builder_drops_an_emailed_ask_bianca_already_answered_and_keeps_the_rest_of_the_card(monkeypatch):
    build_settings(monkeypatch)
    unanswered = ask_build([customer_email(company_id="company-0")])
    review.build_proposals(unanswered, WORKSPACE)
    assert unanswered.proposals["company-0"]["next_step_bullets"] == [
        "Next step: Confirm a time on Wednesday morning.", "We owe: Send the store list."]
    answered = ask_build([customer_email(company_id="company-0"), our_reply(company_id="company-0")])
    review.build_proposals(answered, WORKSPACE)
    card = answered.proposals["company-0"]
    # The reply answers the question; the promised store list is a deliverable and stays.
    assert card["next_step_bullets"] == ["We owe: Send the store list."]
    assert review.validate_lineage(card["next_step_bullets"], card["client_context_bullets"], card["sources"], card["bullet_lineage"])
    # The build judges from what was stored when its cycle started.
    assert answered.mail.queries == [("mail", answered.queue_state["cycle_started_at"])]


def test_builder_makes_no_card_when_the_only_suggestion_was_already_answered(monkeypatch):
    build_settings(monkeypatch)
    conn = ask_build([customer_email(company_id="company-0"), our_reply(company_id="company-0")])
    conn.insights = email_asks()[:1]
    assert review.build_proposals(conn, WORKSPACE)["created"] == 0 and not conn.proposals
    # A later build withdraws a pending card whose only step has since been answered.
    pending = ask_build([customer_email(company_id="company-0")])
    pending.insights = email_asks()[:1]
    review.build_proposals(pending, WORKSPACE)
    pending.mail.messages.append(our_reply(company_id="company-0"))
    pending.now = NOW + timedelta(days=1, hours=1)
    assert review.build_proposals(pending, WORKSPACE)["updated"] == 1
    assert pending.proposals["company-0"]["status"] == "superseded" and not pending.receipts


def test_reply_stored_after_the_cycle_started_waits_for_the_next_build(monkeypatch):
    build_settings(monkeypatch)
    late = our_reply(company_id="company-0", created_at=NOW + timedelta(hours=6))
    conn = ask_build([customer_email(company_id="company-0"), late])
    review.build_proposals(conn, WORKSPACE)
    assert len(conn.proposals["company-0"]["next_step_bullets"]) == 2
    conn.now = NOW + timedelta(days=1)
    review.build_proposals(conn, WORKSPACE)
    assert conn.proposals["company-0"]["next_step_bullets"] == ["We owe: Send the store list."]


def test_brief_done_click_on_the_thread_answers_the_ask_and_an_undo_reopens_it(monkeypatch):
    build_settings(monkeypatch)
    done = {"ref": "thread-ask", "action": "done", "created_at": NOW + timedelta(hours=1)}
    conn = ask_build([customer_email(company_id="company-0")], clicks=[done])
    review.build_proposals(conn, WORKSPACE)
    # A click made after this cycle started is picked up by the next build, like any newer evidence.
    assert len(conn.proposals["company-0"]["next_step_bullets"]) == 2
    conn.now = NOW + timedelta(days=1)
    review.build_proposals(conn, WORKSPACE)
    assert conn.proposals["company-0"]["next_step_bullets"] == ["We owe: Send the store list."]
    undone = ask_build([customer_email(company_id="company-0")],
                       clicks=[done, {"ref": "thread-ask", "action": "undone", "created_at": NOW + timedelta(hours=2)}])
    undone.now = NOW + timedelta(days=1)
    review.build_proposals(undone, WORKSPACE)
    assert len(undone.proposals["company-0"]["next_step_bullets"]) == 2
    no_table = ask_build([customer_email(company_id="company-0")], clicks=[done], has_clicks_table=False)
    no_table.now = NOW + timedelta(days=1)
    review.build_proposals(no_table, WORKSPACE)
    assert len(no_table.proposals["company-0"]["next_step_bullets"]) == 2


def test_a_reply_about_delivery_work_only_or_from_another_company_does_not_answer(monkeypatch):
    build_settings(monkeypatch)
    delivery = [{"origin_record_id": REPLY_RECORD, "action_scope": "delivery"}]
    conn = ask_build([customer_email(company_id="company-0"), our_reply(company_id="company-0")], insights=delivery)
    review.build_proposals(conn, WORKSPACE)
    assert len(conn.proposals["company-0"]["next_step_bullets"]) == 2
    other = ask_build([customer_email(company_id="company-0"), our_reply(company_id="another-company")])
    review.build_proposals(other, WORKSPACE)
    assert len(other.proposals["company-0"]["next_step_bullets"]) == 2


class MailStore(Store):
    """The decision store, plus the mail the answered-ask check reads."""
    def __init__(self, mail_data):
        self.mail = mail_data
        self.source_record = customer_email()
        self.insights = email_asks()
        answered = review.answered_email_asks(self.insights, [m for m in mail_data.messages
                                                              if m["created_at"] <= mail_data.cycle_started_at],
                                              frozenset({"bianca-ai.com"}))
        (steps, context, sources, lineage), versions = review.reviewed_content([self.source_record], self.insights, answered=answered)
        super().__init__(proposal(bullets=steps + context, next_step_bullets=steps, client_context_bullets=context,
                                  sources=sources, bullet_lineage=lineage,
                                  source_versions=[review.source_version(self.source_record)],
                                  observed_source_versions=[review.source_version(self.source_record)]))
        self.source_record, self.insights = customer_email(), email_asks()

    def connect(self):
        store = self

        class Connection(DecisionConnection):
            def execute(self, sql, params=()):
                return store.mail.answer(" ".join(sql.split()), params) or super().execute(sql, params)
        return Connection(self)


@pytest.fixture
def mail_service(monkeypatch):
    def make(mail_data):
        store = MailStore(mail_data)
        sheets = FakeSheets(store)
        monkeypatch.setattr(review, "settings", SimpleNamespace(
            review_spreadsheet_id="14lXRsWoEY63tOsz4SwggiItgTNPFWI4KDVTLay3MLDw", review_sheet_tab="Pipeline",
            review_sheet_id=1869050409, review_client_context_mode="append", review_build_batch=20,
            project_team_domains=["kellogg.northwestern.edu"]))
        monkeypatch.setattr(review.db, "connect", store.connect)
        monkeypatch.setattr(review, "SheetsClient", lambda: sheets)
        monkeypatch.setattr(review, "_mirror_approval", lambda *args: True)
        return store, sheets
    return make


def test_approval_recheck_applies_the_same_answered_rule_as_the_build(mail_service):
    store, sheets = mail_service(Mail([customer_email(), our_reply()], cycle_started_at=NOW + timedelta(days=1)))
    assert store.data["proposal"]["next_step_bullets"] == ["We owe: Send the store list."]
    assert approve()["status"] == "approved"
    assert sheets.next_step == "- We owe: Send the store list."


def test_a_reply_that_arrives_after_the_build_does_not_block_the_card_until_the_next_cycle(mail_service):
    late = our_reply(created_at=NOW + timedelta(days=2))
    store, sheets = mail_service(Mail([customer_email(), late], cycle_started_at=NOW + timedelta(days=1)))
    assert len(store.data["proposal"]["next_step_bullets"]) == 2
    assert approve()["status"] == "approved"  # same cycle start as the build: the late reply is not read yet


def test_card_built_before_a_newer_cycle_saw_the_reply_is_refused_until_rebuilt(mail_service):
    store, sheets = mail_service(Mail([customer_email(), our_reply()], cycle_started_at=NOW - timedelta(days=1)))
    assert len(store.data["proposal"]["next_step_bullets"]) == 2   # built before the reply was stored
    store.mail.cycle_started_at = NOW + timedelta(days=1)          # a newer cycle now counts the reply
    with pytest.raises(review.ReviewConflict, match="sales evidence changed"):
        approve()
    assert not sheets.calls and not store.data["intents"]


# ---- H-11: what one approval costs in Sheet calls (kept as is; see docs/gsheet-updates.md)

def test_one_approval_reads_the_live_row_once_to_plan_and_makes_no_other_sheet_call_before_the_write(service):
    """Plan read (exact live text for the saved before/after values) + apply_update's check, write and read-back.

    Each of the three reads has its own job, so none is removed: the plan needs the Sheet's exact bytes before the
    intent is saved, the check must sit right before the write, and the read-back confirms what was written.
    """
    store, sheets = service
    approve()
    before_write = store.events[:store.events.index("sheet-write")]
    assert before_write.count("sheet-read") == 1 and "status-read" not in store.events
    assert store.events.count("sheet-write") == 1 and len(sheets.calls) == 1
