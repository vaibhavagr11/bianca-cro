from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app import brief_delivery
from app.ingest import CalendarPayload, GmailPayload, _start_sync

NOW = datetime(2026, 11, 5, 14, tzinfo=timezone.utc)


class Cursor:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    def cursor(self, **kwargs):
        return self

    def execute(self, sql, params):
        self.calls.append((sql, params))
        self.result = next(self.results)
        return self

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.result


def receipt(feed, **changes):
    return {"id": uuid4(), "feed_key": feed, "status": "succeeded", "coverage_complete": True,
            "started_at": NOW - timedelta(minutes=2), "finished_at": NOW - timedelta(minutes=1)} | changes


@pytest.fixture(autouse=True)
def feeds(monkeypatch):
    monkeypatch.setattr(brief_delivery.timeline, "utcnow", lambda: NOW)
    monkeypatch.setattr(brief_delivery.settings, "brief_verification_feeds", ["gmail:RG", "gmail:NC"])


def test_receipt_requires_all_mailboxes_and_sent_coverage():
    rg = receipt("gmail:RG")
    with pytest.raises(HTTPException, match="missing, incomplete"):
        brief_delivery.validate_sync_batch(Cursor([[rg]]), uuid4(), str(uuid4()))
    with pytest.raises(HTTPException, match="missing, incomplete"):
        brief_delivery.validate_sync_batch(
            Cursor([[rg, receipt("gmail:NC", coverage_complete=False)]]), uuid4(), str(uuid4()),
        )
    with pytest.raises(HTTPException, match="missing, incomplete"):
        brief_delivery.validate_sync_batch(
            Cursor([[rg, receipt("gmail:NC", started_at=NOW - timedelta(days=1))]]), uuid4(), str(uuid4()),
        )


def test_sync_batch_accepts_complete_receipts_and_rejects_superseded_batch():
    both = [receipt("gmail:RG"), receipt("gmail:NC", finished_at=NOW)]
    assert brief_delivery.validate_sync_batch(Cursor([both, {"found": False}]), uuid4(), str(uuid4())) == NOW
    with pytest.raises(HTTPException, match="superseded"):
        brief_delivery.validate_sync_batch(Cursor([both, {"found": True}]), uuid4(), str(uuid4()))


def test_processing_requires_successful_stage_and_no_pending_activity():
    cursor = Cursor([{"ready": False}])
    with pytest.raises(HTTPException, match="not finished successfully"):
        brief_delivery._check_processing(cursor, uuid4(), str(uuid4()), NOW)
    assert "stage = 'done'" in cursor.calls[0][0]
    assert "status = 'pending'" in cursor.calls[0][0]


def test_mail_receipts_track_mailbox_and_require_explicit_sent_coverage():
    uid, batch_id = uuid4(), uuid4()
    cursor = Cursor([(uid,), (uid,)])
    mail = GmailPayload(messages=[], mailbox="nc", sync_batch_id=batch_id, coverage_complete=True)
    assert _start_sync(cursor, uid, "gmail", mail, 0, mailbox=mail.mailbox,
                       includes_sent=mail.includes_sent) == uid
    assert cursor.calls[-1][1][-3:] == (batch_id, "gmail:NC", False)
    mail.includes_sent = True
    _start_sync(cursor, uid, "gmail", mail, 0, mailbox=mail.mailbox, includes_sent=mail.includes_sent)
    assert cursor.calls[-1][1][-3:] == (batch_id, "gmail:NC", True)
    with pytest.raises(HTTPException, match="mailbox is required"):
        _start_sync(cursor, uid, "calendar", CalendarPayload(events=[], sync_batch_id=batch_id), 0)


def test_stale_verification_cannot_release_an_email(monkeypatch):
    gate = brief_delivery.DeliveryGate(uuid4(), uuid4(), uuid4(), False, NOW, NOW, "original")
    monkeypatch.setattr(brief_delivery, "validate_sync_batch", lambda *args: NOW)
    monkeypatch.setattr(brief_delivery, "_check_processing", lambda *args: None)
    monkeypatch.setattr(brief_delivery, "_snapshot", lambda *args: "changed")
    with pytest.raises(brief_delivery.VerificationPending) as exc:
        gate.finalize(Cursor([{"response": None}]), {"html": "would have been sent"})
    assert exc.value.response["ready_to_send"] is False
    assert "html" not in exc.value.response


def test_preview_and_send_modes_report_distinct_readiness():
    common = (uuid4(), uuid4(), uuid4())
    assert not brief_delivery.DeliveryGate(*common, True, NOW, NOW, "x").ready_fields()["ready_to_send"]
    assert brief_delivery.DeliveryGate(*common, False, NOW, NOW, "x").ready_fields()["ready_to_send"]
