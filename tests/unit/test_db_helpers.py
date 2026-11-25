"""Tests for sourcerykit.db helpers — _engine, _intercepts, _trusted_endpoints."""

import uuid
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# DB intercept SQL builders
# ---------------------------------------------------------------------------


class TestInsertIntercept:
    def test_returns_insert_statement(self) -> None:
        from sourcerykit.db._intercepts import insert_intercept

        stmt = insert_intercept(
            agent_id="agent-1",
            action_name="fetch",
            source_url="https://api.example.com",
            request_payload={"method": "GET"},
            raw_response='{"status": 200}',
            response_hash="abc123",
        )
        # Should be a SQLAlchemy Insert object
        from sqlalchemy.sql.dml import Insert

        assert isinstance(stmt, Insert)

    def test_returning_id_clause_present(self) -> None:
        from sourcerykit.db._intercepts import insert_intercept

        stmt = insert_intercept("a", "b", "https://c.com", {}, "{}", "hash")
        compiled = str(stmt.compile())
        assert "RETURNING" in compiled.upper() or "returning" in compiled.lower()


class TestSelectActiveTrustedEndpoints:
    def test_returns_select_object(self) -> None:
        from sqlalchemy.sql.selectable import Select

        from sourcerykit.db._trusted_endpoints import select_active_trusted_endpoints

        org_id = uuid.uuid4()
        stmt = select_active_trusted_endpoints(org_id)
        assert isinstance(stmt, Select)


class TestInsertTrustedEndpointDb:
    def test_returns_insert_object(self) -> None:
        from sqlalchemy.sql.dml import Insert

        from sourcerykit.db._trusted_endpoints import insert_trusted_endpoint

        org_id = uuid.uuid4()
        stmt = insert_trusted_endpoint(org_id, "https://example.com", "My label")
        assert isinstance(stmt, Insert)

    def test_insert_without_label(self) -> None:
        from sourcerykit.db._trusted_endpoints import insert_trusted_endpoint

        org_id = uuid.uuid4()
        stmt = insert_trusted_endpoint(org_id, "https://example.com")
        assert stmt is not None


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://user:p%40ss:w0rd@db.example:5432/app",
        "postgresql+psycopg2://user:p%40ss:w0rd@db.example:5432/app",
        "postgresql+psycopg://user:p%40ss:w0rd@db.example:5432/app",
    ],
)
def test_the_engine_uses_psycopg_3_whatever_driver_the_url_names(url: str) -> None:
    from sourcerykit.db import _engine

    _engine._ENGINE = None
    try:
        with patch("sourcerykit.db._engine.get_settings", return_value=MagicMock(postgres_url=url)):
            engine = _engine.get_engine()
        assert engine.url.drivername == "postgresql+psycopg"
        assert (engine.url.username, engine.url.password, engine.url.host, engine.url.database) == (
            "user",
            "p@ss:w0rd",
            "db.example",
            "app",
        )
    finally:
        _engine._ENGINE = None
