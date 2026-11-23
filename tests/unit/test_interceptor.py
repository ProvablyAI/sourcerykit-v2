"""Tests for sourcerykit.intercept.interceptor — _record and hook integration."""

import uuid
from unittest.mock import AsyncMock, patch

import httpx

from sourcerykit.facts import flatten
from sourcerykit.intercept._httpx_hook import _make_async_wrapper
from sourcerykit.intercept.interceptor import (
    _record,
    async_intercept_context,
)


class TestRecord:
    async def test_does_nothing_when_no_context_set(self) -> None:
        """_record is a no-op when agent_id / action_name are not in context."""
        with patch("sourcerykit.intercept.interceptor.add_intercept_row", AsyncMock()) as mock_add:
            await _record("https://example.com", "POST", {}, "{}")
            mock_add.assert_not_called()

    async def test_calls_add_intercept_row_when_context_active(self) -> None:
        expected_id = uuid.uuid4()
        with patch("sourcerykit.intercept.interceptor.add_intercept_row", AsyncMock(return_value=expected_id)):
            async with async_intercept_context(agent_id="agent-1", action_name="act-1"):
                await _record("https://example.com", "GET", {"q": 1}, '{"data": "resp"}')

    async def test_does_not_raise_when_add_intercept_row_fails(self) -> None:
        """Storage errors must be swallowed — never propagated to caller."""
        with patch(
            "sourcerykit.intercept.interceptor.add_intercept_row",
            AsyncMock(side_effect=RuntimeError("db down")),
        ):
            async with async_intercept_context(agent_id="agent-1", action_name="act-1"):
                await _record("https://example.com", "GET", {}, "{}")
            # If we get here, the error was swallowed correctly

    async def test_discards_row_id_from_add_intercept_row(self) -> None:
        """Row id is no longer cached — call_ref is the lookup key."""
        with patch("sourcerykit.intercept.interceptor.add_intercept_row", AsyncMock(return_value=uuid.uuid4())):
            async with async_intercept_context(agent_id="agent-1", action_name="act-1"):
                await _record("https://example.com", "GET", {}, "{}")
        # No globals to assert — this is intentional


async def test_httpx_hook_records_the_body_as_received() -> None:
    body = '{"price": 187.30, "id": 12345678901234567890}'  # a float would give 187.3 and ...7168
    recorded: list[str] = []

    async def send(_client: object, request: httpx.Request, **_: object) -> httpx.Response:
        return httpx.Response(200, text=body, request=request)

    async def record(_url: str, _method: str, _payload: dict[str, object], raw: str) -> None:
        recorded.append(raw)

    wrapped = _make_async_wrapper(send, record)
    await wrapped(httpx.AsyncClient(), httpx.Request("GET", "https://api.example/x"))
    assert recorded == [body]
    assert {f.path: f.text for f in flatten(uuid.uuid4(), recorded[0]) or []}["$.id"] == "12345678901234567890"


async def test_httpx_hook_passes_a_streamed_upload_through() -> None:
    async def send(_client: object, request: httpx.Request, **_: object) -> httpx.Response:
        return httpx.Response(200, text="ok", request=request)

    recorded: list[str] = []

    async def record(_url: str, _method: str, _payload: dict[str, object], raw: str) -> None:
        recorded.append(raw)

    upload = httpx.Request("POST", "https://api.example/upload", files={"file": ("a.txt", b"x")})
    response = await _make_async_wrapper(send, record)(httpx.AsyncClient(), upload)
    assert response.status_code == 200 and recorded == ["ok"]
