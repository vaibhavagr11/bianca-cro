import psycopg

from app.config import settings


def connect() -> psycopg.Connection:
    # One connection per call is fine at this stage; we add a pool when traffic needs it.
    return psycopg.connect(settings.database_url.get_secret_value(), connect_timeout=5)
