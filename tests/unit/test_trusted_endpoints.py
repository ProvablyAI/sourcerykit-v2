"""Tests for sourcerykit.trusted_endpoints"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sourcerykit.errors import SourceryKitStorageError, SourceryKitTrustError
from sourcerykit.trusted_endpoints import (
    endpoint_matches,
    insert_trusted_endpoint,
    is_endpoint_trusted,
    remove_trusted_endpoint,
    sanitize_and_extract_trusted_url,
)

_ORG = uuid.uuid4()


# ---------------------------------------------------------------------------
# sanitize_and_extract_trusted_url
# ---------------------------------------------------------------------------


class TestSanitizeAndExtractTrustedUrl:
    def test_strips_query_string(self) -> None:
        assert sanitize_and_extract_trusted_url("https://api.example.com/v1?foo=1") == "https://api.example.com/v1"

    def test_strips_trailing_slash(self) -> None:
        assert sanitize_and_extract_trusted_url("https://api.example.com/") == "https://api.example.com"

    def test_adds_https_scheme_when_missing(self) -> None:
        result = sanitize_and_extract_trusted_url("api.example.com")
        assert result == "https://api.example.com"

    def test_keeps_http_scheme(self) -> None:
        assert sanitize_and_extract_trusted_url("http://localhost:8080/path") == "http://localhost:8080/path"

    def test_preserves_path(self) -> None:
        assert sanitize_and_extract_trusted_url("https://example.com/api/v2") == "https://example.com/api/v2"

    def test_strips_surrounding_whitespace(self) -> None:
        assert sanitize_and_extract_trusted_url("  https://example.com  ") == "https://example.com"

    def test_raises_trust_error_when_no_netloc(self) -> None:
        with pytest.raises(SourceryKitTrustError):
            sanitize_and_extract_trusted_url("///not-a-url")


# ---------------------------------------------------------------------------
# is_endpoint_trusted
# ---------------------------------------------------------------------------


def _make_connect_mock(scalar_return: bool = True, raise_exc: Exception | None = None) -> MagicMock:
    """Build a mock async engine where .connect() returns a scalar bool."""
    mock_result = MagicMock()
    mock_conn = AsyncMock()

    if raise_exc:
        mock_conn.execute = AsyncMock(side_effect=raise_exc)
    else:
        mock_result.scalar.return_value = scalar_return
        mock_conn.execute = AsyncMock(return_value=mock_result)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_engine = MagicMock()
    mock_engine.connect.return_value = mock_ctx
    return mock_engine


def _make_begin_mock(raise_exc: Exception | None = None) -> MagicMock:
    """Build a mock async engine where .begin() supports execute."""
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()

    mock_ctx = AsyncMock()
    if raise_exc:
        mock_ctx.__aenter__ = AsyncMock(side_effect=raise_exc)
    else:
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_engine = MagicMock()
    mock_engine.begin.return_value = mock_ctx
    return mock_engine


@pytest.mark.parametrize(
    ("url", "endpoint", "trusted"),
    [
        ("https://api.example.com/v1/forecast", "https://api.example.com", True),
        ("https://API.Example.com/x", "https://api.example.com", True),  # hosts ignore case
        ("https://api.example.com:443/x", "https://api.example.com", True),  # the default port
        ("https://api.example.com.evil.net/x", "https://api.example.com", False),  # only starts with the host
        ("https://api.example.com@evil.net/x", "https://api.example.com", False),  # userinfo, then another host
        ("https://api.myXsite.com/x", "https://api.my_site.com", False),  # _ is not a wildcard
        ("http://api.example.com/x", "https://api.example.com", False),  # another scheme
        ("https://api.example.com:8443/x", "https://api.example.com", False),  # another port
        # A listed path covers itself and what is under it
        ("https://api.example.com/v1/forecast", "https://api.example.com/v1/forecast", True),
        ("https://api.example.com/v1/forecast/", "https://api.example.com/v1/forecast", True),
        ("https://api.example.com/v1/forecast?city=x", "https://api.example.com/v1/forecast", True),
        ("https://api.example.com/v1/forecast/daily", "https://api.example.com/v1/forecast", True),
        ("https://api.example.com/v1/forecasts", "https://api.example.com/v1/forecast", False),
        ("https://api.example.com/v10/x", "https://api.example.com/v1", False),
        ("https://api.example.com/v1/../admin", "https://api.example.com/v1", False),
        ("https://api.example.com/", "https://api.example.com/v1", False),
        ("https://api.example.com:notaport/v1", "https://api.example.com/v1", False),
    ],
)
def test_endpoint_matches(url: str, endpoint: str, trusted: bool) -> None:
    assert endpoint_matches(url, endpoint) is trusted


class TestIsEndpointTrusted:
    async def test_a_url_under_a_listed_endpoint_is_trusted(self) -> None:
        listed = AsyncMock(return_value=[{"url": "https://other.example.com"}, {"url": "https://api.example.com/v1"}])
        with patch("sourcerykit.trusted_endpoints.list_all_trusted_endpoints_detailed", listed):
            assert await is_endpoint_trusted("https://api.example.com/v1/path?foo=bar") is True
            assert await is_endpoint_trusted("https://api.example.com/v2/path") is False

    async def test_raises_storage_error_on_db_failure(self) -> None:
        mock_engine = _make_connect_mock(raise_exc=RuntimeError("db down"))
        with (
            patch("sourcerykit.trusted_endpoints.get_engine", return_value=mock_engine),
            patch("sourcerykit.trusted_endpoints.get_settings") as ms,
        ):
            ms.return_value.org_id = _ORG
            with pytest.raises(SourceryKitStorageError):
                await is_endpoint_trusted("https://example.com")


class TestRemoveTrustedEndpoint:
    async def test_only_a_listed_endpoint_can_be_removed(self) -> None:
        """A path under a listed endpoint is trusted, but there is no such entry to remove."""
        listed = AsyncMock(return_value=[{"url": "https://api.example.com"}])
        with (
            patch("sourcerykit.trusted_endpoints.list_all_trusted_endpoints_detailed", listed),
            patch("sourcerykit.trusted_endpoints.get_settings") as ms,
        ):
            ms.return_value.org_id = _ORG
            with pytest.raises(SourceryKitTrustError):
                await remove_trusted_endpoint(url="https://api.example.com/v1")


# ---------------------------------------------------------------------------
# insert_trusted_endpoint
# ---------------------------------------------------------------------------


class TestInsertTrustedEndpoint:
    async def test_inserts_successfully(self) -> None:
        mock_engine = _make_begin_mock()
        with (
            patch("sourcerykit.trusted_endpoints.get_engine", return_value=mock_engine),
            patch("sourcerykit.trusted_endpoints.get_settings") as ms,
        ):
            ms.return_value.org_id = _ORG
            await insert_trusted_endpoint(url="https://new.example.com", display_label="My EP")
        mock_engine.begin.assert_called_once()

    async def test_raises_storage_error_on_db_failure(self) -> None:
        mock_engine = _make_begin_mock(raise_exc=RuntimeError("fail"))
        with (
            patch("sourcerykit.trusted_endpoints.get_engine", return_value=mock_engine),
            patch("sourcerykit.trusted_endpoints.get_settings") as ms,
        ):
            ms.return_value.org_id = _ORG
            with pytest.raises(SourceryKitStorageError):
                await insert_trusted_endpoint(url="https://example.com")

    async def test_rejects_display_label_over_255_chars(self) -> None:
        with patch("sourcerykit.trusted_endpoints.get_settings") as ms:
            ms.return_value.org_id = _ORG
            with pytest.raises(ValueError, match="display_label"):
                await insert_trusted_endpoint(url="https://example.com", display_label="x" * 256)


# ---------------------------------------------------------------------------
# verify_claim_endpoints
# ---------------------------------------------------------------------------
