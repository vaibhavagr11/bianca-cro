"""Review semantics, stable evidence, batching and durable external-write recovery. No live I/O."""

import copy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import review_service as review
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


def proposal(**overrides):
    return {"id": PROPOSAL, "workspace_id": WORKSPACE, "company_id": COMPANY, "opportunity_id": OPPORTUNITY,
            "company_name": "Pernod Ricard", "owner_code": "NDL", "stage": "4. Pilot", "country": "ARG",
            "bullets": ["Send the pilot checklist."], "sources": [], "source_versions": [review.source_version(record())],
            "snapshot_last_progress": "Pilot terms agreed.", "snapshot_next_step": "Ask for the signed agreement.",
            "revision": 1, "status": "pending", "created_at": NOW, "updated_at": NOW,
            "removed_at": None, **overrides}


def test_source_identity_survives_reextraction_and_mailbox_aliases_but_not_real_calendar_changes():
    original = record(source="calendar", meta={"status": "confirmed"})
    version = review.source_version(original)
    assert review.source_version({**original, "id": "replacement-id", "updated_at": NOW,
                                  "extracted_version": "extract-v4", "meta": {"status": "confirmed", "mailboxes": ["RG", "NC"]}}) == version
    assert review.source_version({**original, "meta": {"status": "cancelled"}}) != version
    assert review.source_version({**original, "content_hash": "changed-body"}) != version


def test_existing_insight_wording_owner_and_due_date_are_preserved_and_quoted():
    bullets, sources = review.proposal_content([record()], [insight()])
    assert bullets == ["We owe: Send the pilot checklist. (owner: Nicolás de León) — due 2026-11-05"]
    assert sources[0]["quote"] == insight()["quote"]
    assert sources[0]["date"] == "2026-11-02"
    assert sources[0]["recordedAt"] == NOW.isoformat()


def test_old_thread_context_is_not_repeated_when_only_a_new_message_needs_review():
    new_message = record(body="Thanks, that works.")
    bullets, _ = review.proposal_content([new_message], [insight(source_record_ids=["older-record", RECORD])])
    assert len(bullets) == 1
    assert bullets[0].startswith("Commercial email recorded")
    assert "We owe" not in bullets[0]


def test_calendar_title_only_cannot_become_a_buying_signal_or_completed_meeting():
    meeting = record(source="calendar", body="", subject="Pilot purchase approved", occurred_at="2026-11-05T12:00:00Z")
    bullets, _ = review.proposal_content([meeting], [insight(type="buying_signal", quote="Pilot purchase approved",
                                                           text="Customer approved the purchase.")])
    assert bullets == ["Meeting scheduled (2026-11-05): Pilot purchase approved. Review the agenda and confirm any next actions."]
    assert "Buying signal:" not in bullets[0]


def test_cancelled_calendar_record_has_explicit_factual_fallback():
    bullets, sources = review.proposal_content([record(source="calendar", meta={"status": "cancelled"})], [])
    assert bullets[0].startswith("Meeting cancelled (2026-11-02)")
    assert sources[0]["type"] == "Calendar"


def test_completed_or_unverifiable_insights_are_not_presented_as_open_actions():
    for change in ({"status": "done"}, {"status": "possibly_done"}, {"quote": "This sentence is invented."}):
        bullets, _ = review.proposal_content([record()], [insight(**change)])
        assert all(not b.startswith("We owe:") for b in bullets)


def test_duplicate_insights_do_not_duplicate_bullets_and_no_four_deal_selector_is_used():
    bullets, _ = review.proposal_content([record()], [insight(), insight(id="new-insight-uuid")])
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
            return Result([copy.deepcopy(p)] if str(p["workspace_id"]) == str(params[0]) and str(p["id"]) == params[1] else [])
        if q.startswith("select * from source_records"):
            return Result([self.store.source_record])
        if q.startswith("select * from review_decisions"):
            return Result([copy.deepcopy(i) for i in self.data["intents"] if i["idempotency_key"] == params[1]])
        if q.startswith("insert into review_decisions"):
            keys = ["workspace_id", "proposal_id", "actor", "action", "revision", "idempotency_key", "request_hash",
                    "bullets", "before_cells", "after_cells", "edited", "status"]
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
                p.update(status="approved", bullets=params[0].obj, decided_by=params[1], decided_at=NOW)
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
        self.calls = []
        self.uncertain = False
        self.conflict_on_write = False

    def read_company(self, company):
        self.store.events.append("sheet-read")
        return SimpleNamespace(last_progress=self.last_progress, next_step=self.next_step)

    def apply_update(self, company, old_last, old_next, new_last, new_next):
        self.store.events.append("sheet-write")
        # Approval must be durably recorded before leaving the process for Google.
        saved = self.store.data["intents"][-1]
        assert saved["status"] == "applying"
        assert saved["before_cells"] == {"lastProgress": old_last, "nextStep": old_next}
        assert saved["after_cells"] == {"lastProgress": new_last, "nextStep": new_next}
        self.calls.append((company, old_last, old_next, new_last, new_next))
        if self.conflict_on_write:
            raise SheetConflict("changed")
        current, before, after = (self.last_progress, self.next_step), (old_last, old_next), (new_last, new_next)
        if current == after:
            return self.read_company(company)
        if current != before:
            raise SheetConflict("changed")
        self.last_progress, self.next_step = after
        if self.uncertain:
            self.uncertain = False
            raise SheetWriteUncertain("write response lost")
        return self.read_company(company)


@pytest.fixture
def service(monkeypatch):
    store = Store()
    sheets = FakeSheets(store)
    monkeypatch.setattr(review.db, "connect", store.connect)
    monkeypatch.setattr(review, "SheetsClient", lambda: sheets)
    monkeypatch.setattr(review, "_mirror_approval", lambda *args: True)
    return store, sheets


def approve(bullets=None, **kwargs):
    return review.decide(WORKSPACE, PROPOSAL, "NDL", "approve", 1, "approval-key-1", bullets=bullets, **kwargs)


def test_approval_replaces_last_progress_exactly_and_updates_only_after_confirmed_write(service):
    store, sheets = service
    old_last, old_next = sheets.last_progress, sheets.next_step
    result = approve()
    assert sheets.last_progress == old_next
    assert sheets.last_progress != old_last
    assert sheets.next_step == "- Send the pilot checklist."
    assert store.data["proposal"]["status"] == "approved"
    assert result["activity"]["actor"] == "NDL"
    assert result["activity"]["before"] == {"lastProgress": old_last, "nextStep": old_next}
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


def test_ignore_does_not_open_sheets_or_mutate_cells_and_consumes_source_versions(service, monkeypatch):
    store, sheets = service
    monkeypatch.setattr(review, "SheetsClient", lambda: pytest.fail("Ignore must not touch Google Sheets"))
    result = review.decide(WORKSPACE, PROPOSAL, "RG", "ignore", 1, "ignored-key-1")
    assert result["status"] == "ignored"
    assert store.data["proposal"]["status"] == "ignored"
    assert result["activity"]["before"] == result["activity"]["after"]
    assert store.data["receipts"]
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
    with pytest.raises(review.ReviewConflict):
        review.decide(WORKSPACE, PROPOSAL, "NC", "approve", 1, "different-key", bullets=["Edited next action."])
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


def test_missing_credentials_map_to_unavailable_and_completed_retry_needs_no_google(service, monkeypatch):
    _, _ = service
    result = approve()
    def unavailable():
        raise review.SheetError("missing credentials")
    monkeypatch.setattr(review, "SheetsClient", unavailable)
    assert approve() == result


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
                       "country": "ARG", "last_progress": "Old progress", "next_step": "Old next step"}
                      for i in range(count)]
        self.proposals = {}
        self.receipts = []
        self.records_enabled = True

    def execute(self, sql, params=()):
        q = " ".join(sql.split())
        if "pg_advisory_xact_lock" in q:
            return Result([])
        if q.startswith("select o.id as opportunity_id"):
            return Result(copy.deepcopy(self.deals))
        if q.startswith("select company_id, source"):
            return Result(self.receipts)
        if q.startswith("select * from review_proposals"):
            item = self.proposals.get(params[1])
            return Result([copy.deepcopy(item)] if item else [])
        if q.startswith("select * from source_records"):
            return Result([record(external_id=params[1])] if self.records_enabled else [])
        if q.startswith("select * from insights"):
            return Result([insight()])
        if q.startswith("insert into review_proposals"):
            keys = ["workspace_id", "company_id", "opportunity_id", "company_name", "owner_code", "stage", "country",
                    "bullets", "sources", "source_versions", "snapshot_last_progress", "snapshot_next_step"]
            item = {k: copy.deepcopy(v.obj if hasattr(v, "obj") else v) for k, v in zip(keys, params)}
            item.update(id="proposal-" + params[1], status="pending", revision=1)
            self.proposals[params[1]] = item
            return Result([])
        if q.startswith("update review_proposals"):
            if "status = 'superseded'" in q:
                item = next(p for p in self.proposals.values() if p["id"] == params[0])
                item["status"] = "superseded"
                return Result([])
            keys = ["company_name", "owner_code", "stage", "country", "bullets", "sources", "source_versions",
                    "snapshot_last_progress", "snapshot_next_step"]
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


def test_pending_card_revision_tracks_changed_sheet_baseline_without_losing_new_sources(monkeypatch):
    monkeypatch.setattr(review, "settings", SimpleNamespace(review_build_batch=20))
    conn = BuildConnection(1)
    review.build_proposals(conn, WORKSPACE)
    conn.deals[0]["next_step"] = "A fresh manually edited baseline"
    result = review.build_proposals(conn, WORKSPACE)
    assert result["updated"] == 1
    assert conn.proposals["company-0"]["revision"] == 2
    assert conn.proposals["company-0"]["snapshot_next_step"] == conn.deals[0]["next_step"]


def test_applying_card_is_never_rebuilt_while_external_result_is_unknown(monkeypatch):
    monkeypatch.setattr(review, "settings", SimpleNamespace(review_build_batch=20))
    conn = BuildConnection(1)
    review.build_proposals(conn, WORKSPACE)
    before = copy.deepcopy(conn.proposals["company-0"])
    conn.proposals["company-0"]["status"] = "applying"
    assert review.build_proposals(conn, WORKSPACE)["applying"] == 1
    assert conn.proposals["company-0"]["revision"] == before["revision"]


def test_reclassified_or_relinked_sources_withdraw_stale_pending_card_without_consuming_them(monkeypatch):
    monkeypatch.setattr(review, "settings", SimpleNamespace(review_build_batch=20))
    conn = BuildConnection(1)
    review.build_proposals(conn, WORKSPACE)
    conn.records_enabled = False
    review.build_proposals(conn, WORKSPACE)
    assert conn.proposals["company-0"]["status"] == "superseded"
    assert not conn.receipts


def test_mirror_refresh_recomputes_pipeline_hash_and_only_updates_reviewed_cells():
    class MirrorConnection:
        def __init__(self):
            self.writes = []
        def execute(self, sql, params=()):
            if sql.startswith("select o.*"):
                return Result([{"id": OPPORTUNITY, "name": "Pernod Ricard", "country": "ARG", "owner_code": "NDL",
                                "stage": "4. Pilot", "primary_buyer": "Commercial", "contacts_text": "Tomás",
                                "last_progress": "Old progress", "next_step": "Old next step", "removed_at": None}])
            self.writes.append((sql, params))
            return Result([{"id": "sync-run"}]) if "returning id" in sql else Result([])
    conn = MirrorConnection()
    assert review._mirror_approval(conn, proposal(), {"lastProgress": "Old progress", "nextStep": "Old next step"},
                                   {"lastProgress": "Old next step", "nextStep": "- Confirm launch."}) is True
    sql, values = conn.writes[0]
    assert sql.startswith("update opportunities set last_progress")
    assert values[:2] == ("Old next step", "- Confirm launch.")
    assert len(values[2]) == 64
    assert any("opportunity_changes" in query for query, _ in conn.writes)


def test_mirror_refresh_never_overwrites_a_newer_pipeline_sync():
    class ChangedMirror:
        def execute(self, sql, params=()):
            assert sql.startswith("select o.*"), "No UPDATE may run when the cached row already changed."
            return Result([{"last_progress": "Newer progress", "next_step": "A newer synced next step", "removed_at": None}])
    assert review._mirror_approval(ChangedMirror(), proposal(),
                                  {"lastProgress": "Old progress", "nextStep": "Old next step"},
                                  {"lastProgress": "Old next step", "nextStep": "- Confirm launch."}) is False


def test_removed_company_cannot_be_approved(service):
    store, sheets = service
    store.data["proposal"]["removed_at"] = NOW
    with pytest.raises(review.ReviewConflict, match="no longer"):
        approve()
    assert not sheets.calls
    assert not store.data["intents"]
