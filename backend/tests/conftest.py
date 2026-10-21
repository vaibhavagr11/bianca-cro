import os

# Tests never touch the real database; give Settings a dummy value before app import.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:1/test")
