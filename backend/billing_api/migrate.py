"""Apply db/*.sql in order. POC-grade: replace with the team's migration tool."""

from pathlib import Path

import psycopg

DB_DIR = Path(__file__).resolve().parents[2] / "db"


def apply(dsn: str, *, reset: bool = False, seed: bool = True) -> bool:
    """Create the billing_config schema if missing. Returns True if it was (re)built."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = 'billing_config'"
        ).fetchone()
        if exists and not reset:
            return False
        if exists:
            conn.execute("DROP SCHEMA billing_config CASCADE")
        for path in sorted(DB_DIR.glob("*.sql")):
            if not seed and "seed" in path.name:
                continue
            with conn.transaction():
                conn.execute(path.read_text())
    return True
