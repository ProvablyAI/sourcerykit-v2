"""requests provider hook — intercept ``requests.adapters.HTTPAdapter.send``."""

import asyncio
import contextvars
import json
from collections.abc import Callable, Coroutine, Iterator
from typing import Any

from requests.adapters import HTTPAdapter
from requests.models import PreparedRequest, Response

from sourcerykit.intercept._self_egress import is_self_egress
from sourcerykit.logger import get_logger

_log = get_logger(__name__)

_orig_adapter_send: Callable[..., Response] | None = None


def init_requests_hooks(
    record_fn: Callable[[str, str, dict[str, Any], str], Coroutine[Any, Any, None]],
) -> None:
    """Patch ``requests.adapters.HTTPAdapter.send``."""
    global _orig_adapter_send
    if _orig_adapter_send is not None:
        return

    _orig_adapter_send = HTTPAdapter.send
    patched_send = _make_requests_wrapper(_orig_adapter_send, record_fn)
    setattr(HTTPAdapter, "send", patched_send)


def _make_requests_wrapper(
    orig: Callable[..., Response],
    record_fn: Callable[[str, str, dict[str, Any], str], Coroutine[Any, Any, None]],
) -> Callable[..., Response]:
    def wrapped(self: HTTPAdapter, request: PreparedRequest, **kwargs: Any) -> Response:
        response = orig(self, request, **kwargs)

        if is_self_egress():
            return response

        url, method, payload = _requests_request_to_payload(request)
        if not kwargs.get("stream", False):  # requests has read the whole body
            _run_async(record_fn(url, method, payload, response.text))
            return response

        # A streamed body is recorded once the caller has read it to the end, through iter_content,
        # which .content, .json() and iter_lines use too. It may be read after the intercept block
        # ends, so it is recorded in the context of the send.
        context = contextvars.copy_context()
        orig_iter_content = response.iter_content
        recorded = False

        def iter_content(*args: Any, **kw: Any) -> Iterator[Any]:
            nonlocal recorded
            chunks = []
            for chunk in orig_iter_content(*args, **kw):
                chunks.append(chunk)
                yield chunk
            if recorded:  # read again from requests' copy: already recorded
                return
            recorded = True
            if chunks and isinstance(chunks[0], str):  # decode_unicode=True
                text = "".join(chunks)
            else:  # joined first: a character's bytes may span two chunks
                text = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
            context.run(_run_async, record_fn(url, method, payload, text))

        setattr(response, "iter_content", iter_content)
        return response

    return wrapped


def _requests_request_to_payload(req: PreparedRequest) -> tuple[str, str, dict[str, Any]]:
    """Extract parameters from a PreparedRequest object."""
    kwargs: dict[str, Any] = {}

    if body := req.body:
        if isinstance(body, bytes):
            text = body.decode(errors="replace")
        else:
            text = str(body)

        if "application/json" in req.headers.get("Content-Type", ""):
            try:
                kwargs["json"] = json.loads(text)
            except Exception as e:
                _log.debug("requests_request_json_parse_failed", error=str(e))
                kwargs["data"] = text
        else:
            kwargs["data"] = text

    payload: dict[str, Any] = {k: kwargs[k] for k in ("json", "data") if k in kwargs}
    return str(req.url), str(req.method), payload


def _run_async(coroutine: Coroutine[Any, Any, None]) -> None:
    """Safely run an async coroutine from a synchronous context."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coroutine)
        return

    if loop.is_running():
        asyncio.run_coroutine_threadsafe(coroutine, loop)
    else:
        loop.run_until_complete(coroutine)
