import posixpath
from typing import Any
from urllib.parse import SplitResult, urlparse, urlsplit

from sourcerykit.config import get_settings
from sourcerykit.db._engine import get_engine
from sourcerykit.db._trusted_endpoints import (
    delete_trusted_endpoint,
    select_active_trusted_endpoints,
)
from sourcerykit.db._trusted_endpoints import (
    insert_trusted_endpoint as db_insert_trusted_endpoint,
)
from sourcerykit.errors import SourceryKitStorageError, SourceryKitTrustError
from sourcerykit.logger import get_logger
from sourcerykit.utils.validation import validate_length

_log = get_logger(__name__)


def sanitize_and_extract_trusted_url(raw_url: str) -> str:
    """``raw_url`` as stored: its scheme (``https`` if none), host and path, without query or a trailing ``/``."""
    clean_url = raw_url.strip()
    if not clean_url.startswith(("http://", "https://")):
        clean_url = "https://" + clean_url

    parsed = urlparse(clean_url)

    if not parsed.netloc:
        _log.warning("sanitize_url_invalid", raw_url=raw_url)
        raise SourceryKitTrustError("Invalid URL structure: Could not determine the host domain.")

    normalized_path = parsed.path.rstrip("/")

    return f"{parsed.scheme}://{parsed.netloc}{normalized_path}"


def endpoint_matches(url: str, endpoint: str) -> bool:
    """Whether ``url`` is ``endpoint`` or below it: the same scheme, host and port, and the
    endpoint's path or a path under it.

    ``https://api.example.com/v1`` covers ``/v1``, ``/v1/forecast`` and ``/v1?city=x``, but not
    ``/v10``, ``/v1/../admin``, or a host that only starts with ``api.example.com``.
    """
    call, listed = urlsplit(url.strip()), urlsplit(endpoint)
    try:
        if (call.scheme.lower(), call.hostname, _port(call)) != (listed.scheme.lower(), listed.hostname, _port(listed)):
            return False
    except ValueError:  # a port that is not a number
        return False
    prefix = listed.path.rstrip("/")
    path = posixpath.normpath(call.path) if call.path else ""
    return not prefix or path == prefix or path.startswith(prefix + "/")


def _port(parts: SplitResult) -> int | None:
    return parts.port or {"http": 80, "https": 443}.get(parts.scheme.lower())


async def is_endpoint_trusted(url: str) -> bool:
    """Whether ``url`` is a listed trusted endpoint of the current organization, or below one."""
    return any(endpoint_matches(url, entry["url"]) for entry in await list_all_trusted_endpoints_detailed())


async def insert_trusted_endpoint(*, url: str, display_label: str | None = None) -> None:
    """Insert an active trusted endpoint for the configured org, ignoring conflicts."""

    clean_url = sanitize_and_extract_trusted_url(url)
    validate_length("display_label", display_label, max_len=255, allow_none=True)

    org_id = get_settings().org_id
    engine = get_engine()

    stmt = db_insert_trusted_endpoint(org_id, clean_url, display_label)

    try:
        async with engine.begin() as conn:
            await conn.execute(stmt)
        _log.info("trusted_endpoint_inserted", url=clean_url)
    except Exception as e:
        _log.error("insert_trusted_endpoint_db_error", url=clean_url, error=str(e))
        raise SourceryKitStorageError("Failed to insert trusted endpoint") from e


async def list_all_trusted_endpoints_detailed() -> list[dict[str, Any]]:
    """The organization's active trusted endpoints, newest first: url, label, policy_version, created_by."""
    org_id = get_settings().org_id
    engine = get_engine()
    stmt = select_active_trusted_endpoints(org_id)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(stmt)
            rows = result.fetchall()
    except Exception as e:
        _log.error("list_trusted_endpoints_db_error", error=str(e))
        raise SourceryKitStorageError("Failed to list trusted endpoints") from e

    detailed_list = []
    for row in rows:
        detailed_list.append(
            {
                "url": row.normalized_url,
                "label": row.display_label or row.normalized_url,
                "policy_version": row.policy_version or "v1",
                "created_by": row.created_by or "system",
            }
        )

    return detailed_list


async def remove_trusted_endpoint(*, url: str) -> None:
    """Remove a listed trusted endpoint of the organization; the exact endpoint must be listed."""
    clean_url = sanitize_and_extract_trusted_url(url)

    if clean_url not in [entry["url"] for entry in await list_all_trusted_endpoints_detailed()]:
        raise SourceryKitTrustError(f"Endpoint '{clean_url}' is not trusted")

    stmt = delete_trusted_endpoint(get_settings().org_id, clean_url)
    engine = get_engine()

    try:
        async with engine.begin() as conn:
            await conn.execute(stmt)
        _log.info("remove_endpoint_removed", url=clean_url)
    except Exception as e:
        _log.error("remove_trusted_endpoint_db_error", url=clean_url, error=str(e))
        raise SourceryKitStorageError("Failed to remove trusted endpoint") from e
