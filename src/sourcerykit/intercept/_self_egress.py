"""Self-egress exemption: the SDK's own HTTP calls (the linker, the public verifier) are never recorded."""

import contextvars
from collections.abc import Generator
from contextlib import contextmanager

_self_egress: contextvars.ContextVar[bool] = contextvars.ContextVar("provably_self_egress", default=False)


def is_self_egress() -> bool:
    """Return True if the current task/thread is inside a provably_self_egress() block."""
    return _self_egress.get()


@contextmanager
def provably_self_egress() -> Generator[None, None, None]:
    """Mark a block of code as the SDK's own egress: its HTTP calls are not recorded."""
    token = _self_egress.set(True)
    try:
        yield
    finally:
        _self_egress.reset(token)
