"""Run the POC locally.

    uv run scripts/dev.py            # embedded Postgres in ./.pgdata (no install needed)
    uv run scripts/dev.py --reset    # drop and re-create schema + seed data
    DATABASE_URL=postgresql://... uv run scripts/dev.py   # use an existing Postgres

Then open http://localhost:8000
"""

import argparse
import os
import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from billing_api import migrate  # noqa: E402
from billing_api.main import create_app  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="drop and re-create the schema with seed data")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        import pgserver  # dev dependency

        server = pgserver.get_server(ROOT / ".pgdata", cleanup_mode="stop")
        dsn = server.get_uri()
        print(f"Embedded PostgreSQL: {dsn}")

    if migrate.apply(dsn, reset=args.reset):
        print("Schema and seed data applied")

    uvicorn.run(create_app(dsn), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
