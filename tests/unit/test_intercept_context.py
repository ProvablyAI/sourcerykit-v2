"""Tests for sourcerykit.intercept.interceptor — async_intercept_context and related helpers."""

import pytest

from sourcerykit.intercept.interceptor import (
    _ctx_action_name,
    _ctx_agent_id,
    async_intercept_context,
)


@pytest.fixture(autouse=True)
def _reset_globals() -> None:
    """Reset global state in the interceptor module between tests."""
    pass


class TestAsyncInterceptContext:
    async def test_sets_context_vars_within_block(self) -> None:
        async with async_intercept_context(agent_id="agent-1", action_name="action-a"):
            assert _ctx_agent_id.get() == "agent-1"
            assert _ctx_action_name.get() == "action-a"

    async def test_resets_context_vars_after_block(self) -> None:
        async with async_intercept_context(agent_id="agent-1", action_name="action-a"):
            pass
        assert _ctx_agent_id.get() == ""
        assert _ctx_action_name.get() == ""

    async def test_resets_to_outer_values_when_nested(self) -> None:
        async with async_intercept_context(agent_id="outer", action_name="outer-action"):
            async with async_intercept_context(agent_id="inner", action_name="inner-action"):
                assert _ctx_agent_id.get() == "inner"
            assert _ctx_agent_id.get() == "outer"

    async def test_resets_even_if_exception_raised(self) -> None:
        with pytest.raises(ValueError):
            async with async_intercept_context(agent_id="agent-1", action_name="action-a"):
                raise ValueError("boom")
        assert _ctx_agent_id.get() == ""
        assert _ctx_action_name.get() == ""

    async def test_raises_value_error_for_empty_agent_id(self) -> None:
        with pytest.raises(ValueError):
            async with async_intercept_context(agent_id="", action_name="action-a"):
                pass

    async def test_raises_value_error_for_long_action_name(self) -> None:
        with pytest.raises(ValueError):
            async with async_intercept_context(agent_id="a", action_name="x" * 256):
                pass

    async def test_context_isolation_across_concurrent_tasks(self) -> None:
        """ContextVars must be isolated per task — no cross-task leakage."""
        import asyncio

        results: dict[str, str] = {}

        async def task_a() -> None:
            async with async_intercept_context(agent_id="agent-a", action_name="act-a"):
                await asyncio.sleep(0)
                results["a"] = _ctx_agent_id.get()

        async def task_b() -> None:
            async with async_intercept_context(agent_id="agent-b", action_name="act-b"):
                await asyncio.sleep(0)
                results["b"] = _ctx_agent_id.get()

        await asyncio.gather(task_a(), task_b())
        assert results["a"] == "agent-a"
        assert results["b"] == "agent-b"
