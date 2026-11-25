"""SQLAlchemy Core table definitions: what ``bootstrap_system`` creates in your own database.

The tables of earlier versions (``traces``, ``trace_intercepts``, ``intercepts.call_ref``) are
left to the migrations: a database that has them keeps them, unused.
"""

from sqlalchemy import VARCHAR, Column, Index, Integer, MetaData, Numeric, Table, Text, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.ext.asyncio import AsyncEngine

metadata = MetaData()


async def ensure_schema(engine: AsyncEngine) -> None:
    """Idempotent: create all tables if they don't exist."""
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)


intercepts = Table(
    "intercepts",
    metadata,
    Column(
        "id",
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    ),
    Column("agent_id", VARCHAR(255), nullable=False),
    Column("action_name", VARCHAR(255), nullable=False),
    Column("source_url", Text, nullable=False),
    Column("request_payload", Text, nullable=False, server_default=text("'{}'")),
    Column("raw_response", Text, nullable=False),
    Column("response_hash", VARCHAR(64), nullable=False),
    Column(
        "created_at",
        TIMESTAMP(timezone=False),
        nullable=False,
        server_default=text("NOW()"),
    ),
)

Index(
    "ix_intercepts_agent_action",
    intercepts.c.agent_id,
    intercepts.c.action_name,
)

trusted_endpoints = Table(
    "trusted_endpoints",
    metadata,
    Column(
        "id",
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    ),
    Column("org_id", UUID(as_uuid=True), nullable=False),
    Column("entry_type", VARCHAR(50), nullable=False, server_default=text("'endpoint'")),
    Column("normalized_url", Text, nullable=False),
    Column("display_label", VARCHAR(255)),
    Column("policy_version", VARCHAR(20), server_default=text("'v1'")),
    Column("created_at", TIMESTAMP(timezone=False), server_default=text("NOW()")),
    Column("revoked_at", TIMESTAMP(timezone=False)),
    Column("created_by", VARCHAR(255)),
)

Index(
    "uix_trusted_endpoints_org_url_active",
    trusted_endpoints.c.org_id,
    trusted_endpoints.c.normalized_url,
    postgresql_where=trusted_endpoints.c.revoked_at.is_(None),
    unique=True,
)

FACTS_TABLE = "facts"

# The recorded responses of one verification, one row per JSON value (see sourcerykit.facts).
# One table per project, emptied and refilled by each verification under a lock: the claims'
# queries name it, and qedb proves queries on one table.
facts = Table(
    FACTS_TABLE,
    metadata,
    Column("call_ref", UUID(as_uuid=True), nullable=False),
    Column("path", Text, nullable=False),
    Column("pattern", Text, nullable=False),
    Column("idx", Integer),
    Column("obj", Text),
    Column("type", Text, nullable=False),
    Column("num", Numeric),
    Column("text", Text),
)
