"""Tests for sourcerykit.intercept._storage — hash_payload and add_intercept_row."""

import hashlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from sourcerykit.intercept._storage import add_intercept_row, hash_payload

# ---------------------------------------------------------------------------
# hash_payload
# ---------------------------------------------------------------------------


def test_hash_is_the_sha256_of_the_stored_body() -> None:
    body = '{"price": 187.30}'
    assert hash_payload(body) == hashlib.sha256(body.encode()).hexdigest()
    assert hash_payload(body) != hash_payload('{"price": 187.3}')  # same JSON value, different body


# ---------------------------------------------------------------------------
# add_intercept_row
# ---------------------------------------------------------------------------


def _make_engine_begin_mock(returned_id: uuid.UUID | None = None) -> MagicMock:
    mock_row = MagicMock()
    mock_row.__getitem__ = MagicMock(return_value=returned_id)

    mock_result = MagicMock()
    mock_result.fetchone.return_value = mock_row if returned_id is not None else None

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock(return_value=mock_result)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_engine = MagicMock()
    mock_engine.begin.return_value = mock_ctx
    return mock_engine


class TestAddInterceptRow:
    async def test_returns_row_id_on_success(self) -> None:
        expected_id = uuid.uuid4()
        engine = _make_engine_begin_mock(returned_id=expected_id)
        with patch("sourcerykit.intercept._storage.get_engine", return_value=engine):
            result = await add_intercept_row(
                url="https://trusted.com",
                method="POST",
                request_payload={"q": 1},
                raw='{"resp": "ok"}',
                agent_id="agent-1",
                action_name="action-a",
            )
        assert result == expected_id
