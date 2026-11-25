"""HTTP intercept: record API responses into Postgres."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any
from uuid import UUID

from sourcerykit._validation import validate_length
from sourcerykit.intercept._aiohttp_hook import init_aiohttp_hooks
from sourcerykit.intercept._httpx_hook import init_httpx_hooks
from sourcerykit.intercept._requests_hook import init_requests_hooks
from sourcerykit.intercept._storage import add_intercept_row
from sourcerykit.logger import get_logger

_log = get_logger(__name__)

_ctx_agent_id: ContextVar[str] = ContextVar("provably_agent_id", default="")
_ctx_action_name: ContextVar[str] = ContextVar("provably_action_name", default="")
# Row ids of the calls recorded inside ``sourcerykit.run()``. A list, not a tuple: tasks the run
# starts get a copy of the context, and must append to the same list.
_ctx_run_calls: ContextVar[list[UUID] | None] = ContextVar("sourcerykit_run_calls", default=None)


@asynccontextmanager
async def async_intercept_context(*, agent_id: str, action_name: str) -> AsyncGenerator[None, None]:
    """Record the HTTP calls made inside the block, tagged with ``agent_id`` and ``action_name``."""
    validate_length("agent_id", agent_id, max_len=255)
    validate_length("action_name", action_name, max_len=255)

    t_agent = _ctx_agent_id.set(agent_id)
    t_action = _ctx_action_name.set(action_name)
    try:
        yield
    finally:
        _ctx_action_name.reset(t_action)
        _ctx_agent_id.reset(t_agent)


async def _record(url: str, method: str, request_payload: dict[str, Any], raw: str) -> None:
    """Store a call made inside ``async_intercept_context``, and add it to the current run."""
    agent_id = _ctx_agent_id.get()
    action_name = _ctx_action_name.get()
    if not agent_id or not action_name:
        return

    try:
        row_id = await add_intercept_row(
            url=url,
            method=method,
            request_payload=request_payload,
            raw=raw,
            agent_id=agent_id,
            action_name=action_name,
        )
    except Exception:
        _log.exception("intercept_record_failed", agent_id=agent_id, action_name=action_name)
        return
    run_calls = _ctx_run_calls.get()
    if run_calls is not None and row_id is not None:
        run_calls.append(row_id)


def init_interceptor() -> None:
    """Install monkey-patches on httpx.AsyncClient, aiohttp and requests."""
    init_httpx_hooks(_record)
    init_aiohttp_hooks(_record)
    init_requests_hooks(_record)
    _log.info("intercept_hooks_installed")
