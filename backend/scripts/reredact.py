"""Re-apply the current redaction rules to stored records of every source (after the rules change).

Updates subject, body, content_hash and any stored evidence quote. Classifications are kept.
Calendar hashes are left as they are: the next calendar sync re-stores changed events with the new hash.
Usage (from backend/): uv run python -m scripts.reredact
"""

import hashlib

from psycopg.types.json import Jsonb

from app import db
from app.gmail import CleanMessage, redact_secrets


def new_hash(source, ext, thread, occurred, subject, participants, body, old_hash):
    if source == "gmail":
        return CleanMessage(ext, thread, occurred, subject, participants, body).content_hash
    if source == "notes":
        return hashlib.sha256(body.encode()).hexdigest()  # same as ingest_notes
    return old_hash


def main() -> None:
    changed: dict[str, int] = {}
    with db.connect() as conn:
        rows = conn.execute(
            "select id, source, external_id, thread_id, occurred_at::text, subject, participants, body,"
            " classification, content_hash from source_records where body is not null or subject is not null"
        ).fetchall()
        for rid, source, ext, thread, occurred, subject, participants, body, classification, old_hash in rows:
            new_subject = redact_secrets(subject) if subject else subject
            new_body = redact_secrets(body) if body is not None else None
            new_class = classification
            if classification and classification.get("evidence_quote"):
                new_class = {**classification, "evidence_quote": redact_secrets(classification["evidence_quote"])}
            if (new_subject, new_body, new_class) == (subject, body, classification):
                continue
            content_hash = new_hash(source, ext, thread, occurred, new_subject, participants, new_body or "", old_hash)
            conn.execute(
                "update source_records set subject = %s, body = %s, content_hash = %s, classification = %s"
                " where id = %s",
                (new_subject, new_body, content_hash, Jsonb(new_class) if new_class else None, rid),
            )
            changed[source] = changed.get(source, 0) + 1
        conn.commit()
    print(f"re-redacted {changed} of {len(rows)} records")


if __name__ == "__main__":
    main()
