"""Apply pending SQL migrations in order, then optionally run seed files.

Usage (from backend/):
    uv run python -m scripts.migrate           # apply new migrations
    uv run python -m scripts.migrate --seed    # ...and (re)run seeds/*.sql
    uv run python -m scripts.migrate --status  # list applied / pending, change nothing
"""

import argparse
import sys
from pathlib import Path

from app import db

BACKEND_DIR = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = BACKEND_DIR / "migrations"
SEEDS_DIR = BACKEND_DIR / "seeds"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", action="store_true", help="run seeds/*.sql after migrating")
    parser.add_argument("--status", action="store_true", help="show applied/pending and exit")
    args = parser.parse_args()

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))

    with db.connect() as conn:
        conn.execute(
            "create table if not exists schema_migrations ("
            " version text primary key, applied_at timestamptz not null default now())"
        )
        conn.execute("alter table schema_migrations enable row level security")
        conn.commit()
        applied = {row[0] for row in conn.execute("select version from schema_migrations")}
        pending = [f for f in files if f.stem not in applied]
        conn.commit()  # close the read so each migration below gets its own real transaction

        if args.status:
            for f in files:
                print(f"{'applied' if f.stem in applied else 'PENDING'}  {f.name}")
            return 0

        for f in pending:
            # Each migration runs in its own transaction: all of it applies, or none of it.
            with conn.transaction():
                conn.execute(f.read_text())
                conn.execute("insert into schema_migrations (version) values (%s)", (f.stem,))
            print(f"applied  {f.name}")
        if not pending:
            print("no pending migrations")

        if args.seed:
            for f in sorted(SEEDS_DIR.glob("*.sql")):
                with conn.transaction():
                    conn.execute(f.read_text())
                print(f"seeded   {f.name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
