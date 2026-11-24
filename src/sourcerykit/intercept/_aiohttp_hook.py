"""aiohttp provider hook — intercept ``aiohttp.ClientSession._request``."""

from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from sourcerykit.intercept._self_egress import is_self_egress
from sourcerykit.logger import get_logger

_log = get_logger(__name__)

_orig_request: Callable[..., Awaitable[Any]] | None = None
_AIOHTTP_KWARG_KEYS = ("params", "json", "data")


def init_aiohttp_hooks(record_fn: Callable[[str, str, dict[str, Any], str], Awaitable[None]]) -> None:
    """Patch ``aiohttp.ClientSession._request``."""
    global _orig_request
    if _orig_request is not None:
        return

    _orig_request = aiohttp.ClientSession._request

    patched_send = _make_aiohttp_wrapper(_orig_request, record_fn)
    setattr(aiohttp.ClientSession, "_request", patched_send)


def _make_aiohttp_wrapper(
    orig: Callable[..., Awaitable[Any]],
    record_fn: Callable[[str, str, dict[str, Any], str], Awaitable[None]],
) -> Callable[..., Awaitable[Any]]:
    async def wrapped(self: aiohttp.ClientSession, method: str, str_or_url: Any, **kwargs: Any) -> Any:
        response = await orig(self, method, str_or_url, **kwargs)
        if is_self_egress():
            return response

        url_str = str(str_or_url)
        payload: dict[str, Any] = {k: kwargs[k] for k in _AIOHTTP_KWARG_KEYS if k in kwargs and kwargs[k] is not None}

        # An event stream may never end, so reading it here would block the caller: it is not
        # recorded. Any other body, chunked or not, is read in full; aiohttp keeps it for the caller.
        if "text/event-stream" in response.headers.get("Content-Type", "").lower():
            _log.warning("aiohttp_event_stream_not_recorded", url=url_str)
            return response
        try:
            raw = (await response.read()).decode(errors="replace")
        except Exception as e:
            _log.warning("aiohttp_body_read_failed_not_recorded", url=url_str, error=str(e))
            return response

        await record_fn(url_str, method, payload, raw)
        return response

    return wrapped
