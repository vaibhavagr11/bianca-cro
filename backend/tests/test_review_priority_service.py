"""Selection persistence, deferred evidence, bounded work and approval replay. No external I/O."""

import copy
from types import SimpleNamespace

import pytest

from app import review_selection as selection
from app import review_service as review
from test_review_service import BuildConnection, Result, WORKSPACE, NOW, insight, record


class PriorityConnection(BuildConnection):
    def __init__(self, count=1):
        super().__init__(count)
        self.cache = {}
        self.candidate_receipts = []

    def execute(self, sql, params=()):
        q = " ".join(sql.split())
        if q == "select to_regclass('review_selection_cache') as found":
            return Result([{"found": "review_selection_cache"}])
        if q.startswith("select exists(select 1 from review_candidate_receipts"):
            return Result([{"started": bool(self.candidate_receipts) or any(
                p.get("selection_snapshot") and p["status"] in ("pending", "applying")
                for p in self.proposals.values())}])
        if q.startswith("select candidate_key from review_candidate_receipts"):
            return Result([r for r in self.candidate_receipts if r["company_id"] == params[1]])
        if q.startswith("select input_fingerprint, snapshot from review_selection_cache"):
            return Result([copy.deepcopy(self.cache[params[1]])] if params[1] in self.cache else [])
        if q.startswith("insert into review_selection_cache"):
            self.cache[params[1]] = {"input_fingerprint": params[2], "snapshot": copy.deepcopy(params[3].obj)}
            return Result([])
        if q.startswith("insert into review_candidate_receipts"):
            self.candidate_receipts.append(dict(zip(("workspace_id", "company_id", "candidate_key", "proposal_id"), params)))
            return Result([])
        if q.startswith("select * from source_records") and "for share" in q:
            return Result(copy.deepcopy(self.records[params[1]]))
        if q.startswith("insert into review_proposals") and "selection_snapshot" in q:
            result = super().execute(sql, params[:-1])
            self.proposals[params[1]]["selection_snapshot"] = copy.deepcopy(params[-1].obj)
            return result
        if q.startswith("update review_proposals") and "selection_snapshot = %s" in q:
            result = super().execute(sql, params[:-2] + (params[-1],))
            item = next(p for p in self.proposals.values() if p["id"] == params[-1])
            item["selection_snapshot"] = copy.deepcopy(params[-2].obj)
            return result
        return super().execute(sql, params)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(review, "settings", SimpleNamespace(review_build_batch=20, review_priority_selection_enabled=True,
                                                            review_max_next_steps=1, review_max_context=1))


def selector(monkeypatch, chooser=lambda keys: keys):
    calls = []

    def choose(content, deal, as_of):
        calls.append(deal["company_name"])
        keys = chooser(selection.candidate_keys(content))
        metadata = {"version": selection.VERSION, "fingerprint": selection.fingerprint(content, deal, as_of), **keys}
        return selection.apply_selection(content, metadata), metadata

    monkeypatch.setattr(review, "_select_priorities", choose)
    return calls


def same_source_candidates(conn):
    texts = ["Send the signed contract to procurement.", "Confirm the purchase evaluation criteria with the buyer.",
             "Deliver the revised expansion pricing to the sponsor.", "Resolve the security approval before contracting.",
             "Confirm the agreed renewal decision date."]
    source = conn.records["company-0"][0]
    source["body"] = " ".join(texts)
    conn.insights = [insight(id=f"insight-{n}", text=text, quote=text, action_scope="deal_progression",
                             owner_name=None, due_date=None, origin_record_id=source["id"])
                     for n, text in enumerate(texts)]
    return texts


def test_more_than_three_important_items_survive_even_when_legacy_limits_are_one(enabled, monkeypatch):
    conn = PriorityConnection()
    same_source_candidates(conn)
    calls = selector(monkeypatch)
    assert review.build_proposals(conn, WORKSPACE)["created"] == 1
    assert len(conn.proposals["company-0"]["next_step_bullets"]) == 5
    assert len(calls) == 1


def test_cached_selection_reuses_identical_input_and_approval_never_calls_model(enabled, monkeypatch):
    conn = PriorityConnection()
    calls = selector(monkeypatch)
    review.build_proposals(conn, WORKSPACE)
    assert review.build_proposals(conn, WORKSPACE, force=True)["unchanged"] == 1
    assert len(calls) == 1
    item = conn.proposals["company-0"]
    monkeypatch.setattr(review, "_select_priorities", lambda *args: pytest.fail("Approval called the model"))
    assert review._require_current_content(conn, WORKSPACE, item)
    item["current_stage"] = "3. Proposal"
    with pytest.raises(review.ReviewConflict, match="sales evidence changed"):
        review._require_current_content(conn, WORKSPACE, item)


def test_same_source_deferred_action_is_not_consumed_or_approved(enabled, monkeypatch):
    conn = PriorityConnection()
    same_source_candidates(conn)
    calls = selector(monkeypatch, lambda keys: {"nextStep": keys["nextStep"][:1], "clientContext": []})
    review.build_proposals(conn, WORKSPACE)
    item = conn.proposals["company-0"]
    assert len(item["next_step_bullets"]) == 1
    details = review._proposal_data(item)["supportingDetails"]
    assert len(details["nextStep"]) == 4
    selected_key = item["selection_snapshot"]["selection"]["nextStep"][0]
    review._consume_sources(conn, item)
    assert [r["candidate_key"] for r in conn.candidate_receipts] == [selected_key]
    assert conn.receipts == []  # The shared source must remain available.
    item["status"] = "approved"
    review.build_proposals(conn, WORKSPACE, force=True)
    new = conn.proposals["company-0"]
    assert selected_key not in new["selection_snapshot"]["selection"]["nextStep"]
    assert len(new["selection_snapshot"]["candidates"][0]) == 4
    assert len(calls) == 2


def test_empty_selections_are_cached_and_batched_without_starving_later_companies(enabled, monkeypatch):
    review.settings.review_selection_batch = 1  # Independent of the 20-card write batch.
    conn = PriorityConnection(3)
    calls = selector(monkeypatch, lambda keys: {"nextStep": [], "clientContext": []})
    first = review.build_proposals(conn, WORKSPACE)
    assert first["remaining"] == 2 and not first["done"]
    second = review.build_proposals(conn, WORKSPACE)
    assert second["remaining"] == 1 and not second["done"]
    last = review.build_proposals(conn, WORKSPACE)
    assert last["done"] and last["created"] == 0
    assert calls == ["Company 00", "Company 01", "Company 02"]
    assert not conn.proposals and len(conn.cache) == 3


def test_no_candidates_need_no_model_and_existing_source_receipts_still_apply(enabled, monkeypatch):
    conn = PriorityConnection()
    source = conn.records["company-0"][0]
    conn.receipts = [{"company_id": "company-0", **review.source_version(source)}]
    calls = selector(monkeypatch)
    assert review.build_proposals(conn, WORKSPACE)["done"]
    assert not calls and not conn.proposals


def test_elapsed_budget_commits_completed_selection_and_defers_next_model_call(enabled, monkeypatch):
    review.settings.review_selection_batch = 20
    conn = PriorityConnection(2)
    clock = [0]
    monkeypatch.setattr(review, "monotonic", lambda: clock[0])
    calls = selector(monkeypatch)
    original = review._select_priorities

    def slow(*args):
        result = original(*args)
        clock[0] += 61
        return result

    monkeypatch.setattr(review, "_select_priorities", slow)
    first = review.build_proposals(conn, WORKSPACE)
    assert first["created"] == 1 and first["remaining"] == 1 and not first["done"]
    assert len(conn.cache) == 1 and len(calls) == 1
    second = review.build_proposals(conn, WORKSPACE)
    assert second["done"] and second["created"] == 1 and len(calls) == 2


def test_model_failure_never_publishes_unfiltered_fallback(enabled, monkeypatch):
    conn = PriorityConnection()

    def fail(*args):
        raise review.ReviewUnavailable("Priority selection could not be verified.")

    monkeypatch.setattr(review, "_select_priorities", fail)
    with pytest.raises(review.ReviewUnavailable):
        review.build_proposals(conn, WORKSPACE)
    assert not conn.proposals and not conn.cache and not conn.receipts


def test_changed_deferred_evidence_invalidates_selection_before_approval(enabled, monkeypatch):
    conn = PriorityConnection()
    same_source_candidates(conn)
    selector(monkeypatch, lambda keys: {"nextStep": keys["nextStep"][:1], "clientContext": []})
    review.build_proposals(conn, WORKSPACE)
    item = conn.proposals["company-0"]
    chosen = item["next_step_bullets"][0]
    deferred = next(i for i in conn.insights if i["text"] not in chosen)
    deferred["status"] = "done"
    with pytest.raises(review.ReviewConflict, match="sales evidence changed"):
        review._require_current_content(conn, WORKSPACE, item)
    assert not conn.candidate_receipts


def test_enabled_selection_requires_legacy_pending_cards_to_be_rebuilt(enabled, monkeypatch):
    conn = PriorityConnection()
    calls = selector(monkeypatch)
    review.build_proposals(conn, WORKSPACE)
    item = conn.proposals["company-0"]
    item.pop("selection_snapshot")
    with pytest.raises(review.ReviewConflict, match="predates priority selection"):
        review._require_current_content(conn, WORKSPACE, item)
    assert len(calls) == 1


@pytest.mark.parametrize("decided", [False, True])
def test_disabling_selector_after_use_refuses_rebuild_without_consuming_deferred_items(enabled, monkeypatch, decided):
    conn = PriorityConnection()
    same_source_candidates(conn)
    selector(monkeypatch, lambda keys: {"nextStep": keys["nextStep"][:1], "clientContext": []})
    review.build_proposals(conn, WORKSPACE)
    item = conn.proposals["company-0"]
    if decided:
        review._consume_sources(conn, item)
        item["status"] = "approved"
    before = copy.deepcopy((conn.proposals, conn.receipts, conn.candidate_receipts))
    review.settings.review_priority_selection_enabled = False
    with pytest.raises(review.ReviewUnavailable, match="Re-enable priority selection"):
        review.build_proposals(conn, WORKSPACE, force=True)
    assert (conn.proposals, conn.receipts, conn.candidate_receipts) == before


def test_disabled_mode_still_builds_before_migration(enabled):
    review.settings.review_priority_selection_enabled = False
    conn = BuildConnection(1)
    assert review.build_proposals(conn, WORKSPACE)["created"] == 1
