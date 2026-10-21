import os

# Tests never touch the real database; give Settings dummy values before app import.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:1/test")
os.environ.setdefault("INGEST_TOKEN", "test-token")
