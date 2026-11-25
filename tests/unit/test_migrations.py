"""Tests for the alembic migrations, on a real PostgreSQL server.

Run when SOURCERYKIT_TEST_POSTGRES_URL points at a server where the user may create databases,
e.g. postgresql://postgres:bench@localhost:55432/postgres; skipped otherwise.
"""

import asyncio
import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from sourcerykit.cli.upgrade import _run_migrations
from sourcerykit.config import get_settings
from sourcerykit.db import _engine
from sourcerykit.db._schema import ensure_schema

SERVER = os.getenv("SOURCERYKIT_TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(not SERVER, reason="set SOURCERYKIT_TEST_POSTGRES_URL to run")


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A new, empty database, set as SOURCERYKIT_POSTGRES_URL; dropped afterwards."""
    name = f"sourcerykit_test_{uuid.uuid4().hex[:12]}"
    admin = make_url(SERVER).set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = make_url(admin).set(database=name).render_as_string(hide_password=False)
    monkeypatch.setenv("SOURCERYKIT_POSTGRES_URL", url)
    monkeypatch.setenv("PROVABLY_ACCESS_TOKEN", "test")
    monkeypatch.setenv("SOURCERYKIT_ORG_ID", str(uuid.uuid4()))
    get_settings.cache_clear()
    _engine._ENGINE = None
    try:
        yield url
    finally:
        _engine._ENGINE = None
        get_settings.cache_clear()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def _query(url: str, sql: str) -> list[tuple[object, ...]]:
    with psycopg.connect(url) as conn:
        return conn.execute(sql).fetchall()


def _migrate() -> None:
    _engine._ENGINE = None  # an async engine belongs to the event loop that made it
    assert _run_migrations()


def test_upgrade_keeps_the_trusted_endpoints_of_tables_made_without_migrations(database: str) -> None:
    """bootstrap_system (and a hosted sandbox) create the tables with no migration version."""

    async def bootstrap() -> None:
        await ensure_schema(_engine.get_engine())
        async with _engine.get_engine().begin() as conn:
            await conn.execute(
                text("INSERT INTO trusted_endpoints (org_id, normalized_url) VALUES (gen_random_uuid(), 'https://a.x')")
            )

    asyncio.run(bootstrap())
    _migrate()
    assert _query(database, "SELECT normalized_url FROM trusted_endpoints") == [("https://a.x",)]
    assert _query(database, "SELECT to_regclass('facts') IS NOT NULL") == [(True,)]


def test_upgrade_replaces_the_trusted_endpoints_table_of_the_sdk_before_1_0(database: str) -> None:
    old_table = "CREATE TABLE trusted_endpoints (id SERIAL PRIMARY KEY, org_id TEXT NOT NULL, normalized_url TEXT)"
    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute(old_table)  # the table of the SDK before 1.0, with an integer id
        conn.execute("INSERT INTO trusted_endpoints (org_id, normalized_url) VALUES ('org', 'https://old.x')")
    _migrate()
    assert _query(
        database,
        "SELECT data_type FROM information_schema.columns WHERE table_name = 'trusted_endpoints' "
        "AND column_name = 'id'",
    ) == [("uuid",)]
    assert _query(database, "SELECT count(*) FROM trusted_endpoints") == [(0,)]
