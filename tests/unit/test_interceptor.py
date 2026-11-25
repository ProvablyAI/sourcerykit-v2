"""Tests for sourcerykit.intercept.interceptor — _record and hook integration."""

import asyncio
import json
import threading
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, patch

import aiohttp
import httpx
import pytest
import requests
from requests.adapters import HTTPAdapter

from sourcerykit.facts import flatten
from sourcerykit.intercept._aiohttp_hook import _make_aiohttp_wrapper
from sourcerykit.intercept._httpx_hook import _make_async_wrapper
from sourcerykit.intercept._requests_hook import _make_requests_wrapper
from sourcerykit.intercept.interceptor import (
    _ctx_agent_id,
    _record,
    async_intercept_context,
)
from sourcerykit.verify import run


class TestRecord:
    async def test_does_nothing_when_no_context_set(self) -> None:
        """_record is a no-op when agent_id / action_name are not in context."""
        with patch("sourcerykit.intercept.interceptor.add_intercept_row", AsyncMock()) as mock_add:
            await _record("https://example.com", "POST", {}, "{}")
            mock_add.assert_not_called()

    async def test_stores_the_call_with_the_context_s_tags(self) -> None:
        with patch("sourcerykit.intercept.interceptor.add_intercept_row", AsyncMock(return_value=uuid.uuid4())) as add:
            async with async_intercept_context(agent_id="agent-1", action_name="act-1"):
                await _record("https://example.com", "GET", {"q": 1}, '{"data": "resp"}')
        add.assert_awaited_once_with(
            url="https://example.com",
            method="GET",
            request_payload={"q": 1},
            raw='{"data": "resp"}',
            agent_id="agent-1",
            action_name="act-1",
        )

    async def test_a_failed_store_is_swallowed_and_not_part_of_the_run(self) -> None:
        """Recording must never break the agent's own call."""
        with patch(
            "sourcerykit.intercept.interceptor.add_intercept_row", AsyncMock(side_effect=RuntimeError("db down"))
        ):
            async with async_intercept_context(agent_id="agent-1", action_name="act-1"), run() as current:
                await _record("https://example.com", "GET", {}, "{}")
        assert current.calls == []


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


_CHUNKS = (b'{"current": {"temperature_2m": ', b"5.7}, ", b'"city": "Z\xc3', b'\xbcrich"}')  # \xc3\xbc is ü
_BODY = b"".join(_CHUNKS).decode()


class _API(BaseHTTPRequestHandler):
    """/chunked sends JSON in pieces, as Open-Meteo does; /events a short event stream."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self.send_response(200)
        if self.path == "/events":
            body = b"data: 1\n\n"
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for part in (*_CHUNKS, b""):
            self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def api() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _API)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


async def test_aiohttp_hook_records_a_chunked_body_and_leaves_it_to_the_caller(api: str) -> None:
    recorded: list[str] = []

    async def record(_url: str, _method: str, _payload: dict[str, object], raw: str) -> None:
        recorded.append(raw)

    wrapped = _make_aiohttp_wrapper(aiohttp.ClientSession._request, record)
    with patch.object(aiohttp.ClientSession, "_request", wrapped):
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{api}/chunked") as response:
                received = await response.json()
            async with session.get(f"{api}/events") as response:
                await response.read()
    assert received == json.loads(_BODY)
    assert recorded == [_BODY]  # the event stream is not recorded, rather than recorded as a placeholder


async def test_requests_hook_records_a_streamed_body_once_read_in_the_context_of_the_send(api: str) -> None:
    recorded: list[tuple[str, str]] = []

    async def record(_url: str, _method: str, _payload: dict[str, object], raw: str) -> None:
        recorded.append((_ctx_agent_id.get(), raw))

    wrapped = _make_requests_wrapper(HTTPAdapter.send, record)
    with patch.object(HTTPAdapter, "send", wrapped):
        async with async_intercept_context(agent_id="agent-1", action_name="act-1"):
            response = requests.get(f"{api}/chunked", stream=True, timeout=10)
            assert recorded == []  # nothing read yet, nothing recorded
        received = response.json()  # read after the intercept block ends
        await asyncio.sleep(0.1)  # the record is scheduled on the running loop
        assert list(response.iter_content(4)) and response.json() == received  # read again: not recorded twice

        response.close()
        plain = requests.get(f"{api}/events", timeout=10).text  # not streamed: read in full by requests
        await asyncio.sleep(0.1)
    assert received == json.loads(_BODY)
    assert recorded == [("agent-1", _BODY), ("", plain)]  # the second is outside any intercept block
