"""SourceryKit exception hierarchy.

All exceptions raised by this library are subclasses of :class:`SourceryKitError`.
"""

__all__ = [
    "SourceryKitError",
    "SourceryKitConfigError",
    "SourceryKitBootstrapError",
    "SourceryKitStorageError",
    "SourceryKitTrustError",
]


class SourceryKitError(Exception):
    """Base class for all SourceryKit library errors."""


class SourceryKitConfigError(SourceryKitError):
    """Raised when configuration is invalid or missing (e.g. malformed env vars)."""


class SourceryKitBootstrapError(SourceryKitError):
    """Raised when the Provably bootstrap or handshake process fails."""


class SourceryKitStorageError(SourceryKitError):
    """Raised when a database or storage operation fails."""


class SourceryKitTrustError(SourceryKitError):
    """Raised for a trusted endpoint URL with no host, or when the endpoint to remove is not listed."""
