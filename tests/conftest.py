import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from fastapi.testclient import TestClient

from billing_api import migrate
from billing_api.main import create_app

TEMPLATE_DB = "billing_config_template"


def with_database(dsn: str, name: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit(parts._replace(path=f"/{name}"))


@pytest.fixture(scope="session")
def server_dsn(tmp_path_factory):
    """TEST_DATABASE_URL (a server where we may CREATE DATABASE) or an embedded Postgres."""
    dsn = os.environ.get("TEST_DATABASE_URL")
    server = None
    if not dsn:
        import pgserver

        server = pgserver.get_server(tmp_path_factory.mktemp("pg"), cleanup_mode="stop")
        dsn = server.get_uri()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {TEMPLATE_DB}")
        conn.execute(f"CREATE DATABASE {TEMPLATE_DB}")
    migrate.apply(with_database(dsn, TEMPLATE_DB))
    yield dsn


@pytest.fixture
def db_dsn(server_dsn):
    """A fresh, seeded database per test (cloned from the template)."""
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(server_dsn, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name} TEMPLATE {TEMPLATE_DB}")
    yield with_database(server_dsn, name)
    with psycopg.connect(server_dsn, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture
def client(db_dsn):
    with TestClient(create_app(db_dsn)) as tc:
        yield tc


def as_role(role: str) -> dict[str, str]:
    return {"X-User": f"{role.lower()}@openborder.com", "X-Role": role}


EDITOR = as_role("EDITOR")
PUBLISHER = as_role("PUBLISHER")
ADMIN = as_role("ADMIN")
VIEWER = as_role("VIEWER")
