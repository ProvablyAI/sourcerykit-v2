"""SQLAlchemy Core DML statements for the ``intercepts`` table."""

import json

from sqlalchemy import Insert, insert

from sourcerykit.db._schema import intercepts


def insert_intercept(
    agent_id: str,
    action_name: str,
    source_url: str,
    request_payload: dict[str, object],
    raw_response: str,
    response_hash: str,
) -> Insert:
    """Return a SQLAlchemy Core INSERT statement for a new intercept row.

    ``raw_response`` is the response body as received, so the numbers in it keep every digit.

    Equivalent raw SQL::

        INSERT INTO intercepts
          (agent_id, action_name, source_url, request_payload, raw_response, response_hash)
        VALUES (...)
        RETURNING id
    """
    return (
        insert(intercepts)
        .values(
            agent_id=agent_id,
            action_name=action_name,
            source_url=source_url,
            request_payload=json.dumps(request_payload),
            raw_response=raw_response,
            response_hash=response_hash,
        )
        .returning(intercepts.c.id)
    )
