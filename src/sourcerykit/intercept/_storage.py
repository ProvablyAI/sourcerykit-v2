import hashlib
from typing import Any
from uuid import UUID

from sourcerykit.db._engine import get_engine
from sourcerykit.db._intercepts import insert_intercept
from sourcerykit.logger import get_logger

_log = get_logger(__name__)


def hash_payload(raw: str) -> str:
    """SHA-256 of the response body as stored, so it can be recomputed from the row."""
    return hashlib.sha256(raw.encode()).hexdigest()


async def add_intercept_row(
    url: str,
    method: str,
    request_payload: dict[str, Any],
    raw: str,
    agent_id: str,
    action_name: str,
) -> UUID | None:
    """Store one recorded call, and return its row id.

    The hooks skip the SDK's own calls, and ``async_intercept_context`` checks the identifiers.
    """
    engine = get_engine()

    stmt = insert_intercept(
        agent_id,
        action_name,
        url,
        request_payload,
        raw,
        hash_payload(raw),
    )

    async with engine.begin() as conn:
        result = await conn.execute(stmt)
        row = result.fetchone()

    _log.info("intercept_stored", agent_id=agent_id, action_name=action_name, url=url, method=method)
    return row[0] if row else None
