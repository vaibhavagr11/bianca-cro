"""Re-apply the current redaction rules to stored Gmail records (after the rules change).

Updates subject, body, content_hash and any stored evidence quote. Classifications are kept.
Usage (from backend/): uv run python -m scripts.reredact
"""

from psycopg.types.json import Jsonb

from app import db
from app.gmail import CleanMessage, redact_secrets


def main() -> None:
    changed = 0
    with db.connect() as conn:
        rows = conn.execute(
            "select id, external_id, thread_id, occurred_at::text, subject, participants, body, classification"
            " from source_records where source = 'gmail'"
        ).fetchall()
        for rid, ext, thread, occurred, subject, participants, body, classification in rows:
            new_subject = redact_secrets(subject) if subject else subject
            new_body = redact_secrets(body or "")
            new_class = classification
            if classification and classification.get("evidence_quote"):
                new_class = {**classification, "evidence_quote": redact_secrets(classification["evidence_quote"])}
            if (new_subject, new_body, new_class) == (subject, body or "", classification):
                continue
            new_hash = CleanMessage(ext, thread, occurred, new_subject, participants, new_body).content_hash
            conn.execute(
                "update source_records set subject = %s, body = %s, content_hash = %s, classification = %s"
                " where id = %s",
                (new_subject, new_body, new_hash, Jsonb(new_class) if new_class else None, rid),
            )
            changed += 1
        conn.commit()
    print(f"re-redacted {changed} of {len(rows)} records")


if __name__ == "__main__":
    main()
